"""Controller-only, replayable record removal, archival and publication."""
import json
import os
from datetime import timedelta
from pathlib import Path

from .paths import checked, final_path, legacy_path, sources, work_dir
from .qbit import READY_STATES
from .quarantine import Quarantine, fingerprint, utcnow
from .safe_move import move_no_replace


def verify(path, identity):
    if list(fingerprint(path)) != identity:
        raise ValueError(f"File differs from validated contents: {path}")


def paths(job, h, root, quarantine):
    src = checked(job["source"], root)
    final = checked(final_path(src), root)
    output = checked(job["output"], root)
    archive = checked(quarantine.destination(h, src), root)
    if quarantine.quarantine_root in src.parents or root / ".media-agent" in src.parents:
        raise ValueError(f"Internal source path: {src}")
    if job["action"] == "copy":
        if output != src:
            raise ValueError("Unexpected unchanged file")
    elif output not in (work_dir(root, h, src) / "output.mp4", legacy_path(src), final):
        raise ValueError("Unexpected validated output")
    return src, output, final, archive


def plan_retirement(torrent, entries, journal, root, archive):
    root = Path(root).resolve()
    h = torrent["hash"]
    quarantine = Quarantine(root, archive, journal)
    selected = sources(torrent, entries, root, quarantine.quarantine_root)
    destinations = set()
    if not selected:
        raise ValueError("No eligible media")
    for src in selected:
        job = journal.get(h, str(src))
        if not job or job["status"] != "validated":
            raise ValueError(f"Incomplete media job: {src}")
        _, output, final, target = paths(job, h, root, quarantine)
        key = str(final).casefold()
        if key in destinations or (output != src and output in selected):
            raise ValueError(f"Torrent filename collision: {final}")
        destinations.add(key)
        if os.path.lexists(target) or (final not in (src, output) and os.path.lexists(final)):
            raise FileExistsError(final)
        verify(src, job["metadata"]["original"])
        verify(output, job["metadata"]["verified"])
    return [str(src) for src in selected]


def finalize_torrent(qbit, torrent, journal, download_root, quarantine_root, *,
                     finalize_enabled=False, allow_torrent_retirement=False):
    if not finalize_enabled:
        raise PermissionError("Finalization is disabled")
    if not allow_torrent_retirement:
        raise PermissionError("Torrent retirement requires an explicit policy opt-in")
    root = Path(download_root).resolve()
    h = torrent["hash"]
    db = journal.db
    row = db.execute("SELECT state,sources,save_path FROM retirements WHERE torrent_hash=?", (h,)).fetchone()
    if row and row[0] == "completed":
        return
    if row:
        state, manifest, save_path = row
        selected = json.loads(manifest)
        torrent = dict(hash=h, save_path=save_path)
    else:
        selected = plan_retirement(torrent, qbit.files(h), journal, root, quarantine_root)
        state = "retirement_pending"
        with db:
            db.execute("INSERT INTO retirements VALUES (?,?,?,?)",
                       (h, torrent["save_path"], json.dumps(selected), state))
    live = qbit.lookup(h)
    if state == "retirement_pending":
        if live is not None:
            if (live["save_path"] != torrent["save_path"] or live["state"] not in READY_STATES
                    or live["progress"] != 1 or live["amount_left"] != 0):
                raise ValueError("Torrent no longer ready for retirement")
            if plan_retirement(live, qbit.files(h), journal, root, quarantine_root) != selected:
                raise ValueError("Torrent file selection changed")
            owned = {Path(name) for name in selected}
            owned.update(final_path(Path(name)) for name in selected)
            owned.update(Path(journal.get(h, name)["output"]) for name in selected)
            qbit.check_shared_paths(h, owned)
            qbit.remove_record_only(h, confirmed=True)
            if qbit.lookup(h) is not None:
                return
        with db:
            db.execute("UPDATE retirements SET state='torrent_retired' WHERE torrent_hash=?", (h,))
            journal._insert_event(h, "", "torrent_retired", "Confirmed absent; payload retained")
    elif live is not None:
        raise ValueError("Retired torrent is present again")

    quarantine = Quarantine(root, quarantine_root, journal)
    for name in selected:
        job = journal.get(h, name)
        src, output, final, target = paths(job, h, root, quarantine)
        meta = job["metadata"]
        if job["action"] == "copy":
            verify(src, meta["verified"])
            continue
        if job["status"] == "validated":
            verify(output, meta["verified"])
            if not target.exists():
                verify(src, meta["original"])
                target.parent.mkdir(parents=True, exist_ok=True)
                move_no_replace(src, target)
            elif src.exists():
                raise ValueError(f"Archive collision: {src}")
            verify(target, meta["original"])
            at = utcnow()
            with db:
                db.execute("""UPDATE jobs SET status='quarantined', quarantined_at=?, purge_after=?,
                              source_size=?, source_sha256=?, fingerprinted_at=?, updated_at=?
                              WHERE torrent_hash=? AND source=?""",
                           (at.isoformat(), (at + timedelta(days=7)).isoformat(), *meta["original"],
                            at.isoformat(), at.isoformat(), h, name))
                journal._insert_event(h, name, "quarantined", str(target))
        if job["status"] not in ("quarantined", "validated"):
            raise ValueError("Unexpected archive state")
        verify(target, meta["original"])
        if output != final and output.exists():
            verify(output, meta["verified"])
            move_no_replace(output, final)
        verify(final, meta["verified"])
        if str(final) != job["output"] or not meta.get("promoted"):
            meta["promoted"] = True
            with db:
                db.execute("UPDATE jobs SET output=?, metadata=? WHERE torrent_hash=? AND source=?",
                           (str(final), json.dumps(meta), h, name))
                journal._insert_event(h, name, "promoted", str(final))
    with db:
        db.execute("UPDATE jobs SET status='done',completed_at=? WHERE torrent_hash=? AND status='validated' AND action='copy'",
                   (utcnow().isoformat(), h))
        db.execute("UPDATE retirements SET state='completed' WHERE torrent_hash=?", (h,))
        journal._insert_event(h, "", "done", "Torrent retired and all selected media published")
