"""Preflight and coordinated retirement for an entirely processed torrent.

The conversion worker publishes the verified MP4 in its original directory.
This module evaluates whether the *whole torrent* can safely be retired.
No operations occur unless ``finalize_enabled`` is explicitly true.

A torrent can be revised by a publisher after completion; such torrents must
be excluded by policy before calling finalize_torrent.
"""

from dataclasses import dataclass
from pathlib import Path

from .compat import VIDEO_EXTS
from .quarantine import Quarantine, is_within


@dataclass(frozen=True)
class RetirementPlan:
    torrent_hash: str
    originals: tuple[Path, ...]
    unchanged: tuple[Path, ...]
    ignored: tuple[Path, ...]


def plan_retirement(torrent, entries, journal, download_root, quarantine_root):
    """Validate every selected torrent file before any file is moved.

    Non-video files are never moved; they may still belong to the torrent.
    This prevents a partially processed multi-episode torrent being retired.
    """
    torrent_hash = torrent["hash"]
    root = Path(download_root).resolve()
    quarantine_root = Path(quarantine_root).resolve()
    base = Path(torrent["save_path"]).resolve()
    if base != root and not is_within(base, root):
        raise ValueError(f"Torrent save path outside download root: {base}")

    if not entries:
        raise ValueError("Torrent has no file entries")

    originals, unchanged, ignored = [], [], []
    for entry in entries:
        if entry.get("priority") == 0:
            continue
        name = entry["name"]
        candidate = base / name
        if candidate.is_symlink():
            raise ValueError(f"Symlink source: {candidate}")
        source = candidate.resolve()
        if not is_within(source, root) or source == quarantine_root or is_within(source, quarantine_root):
            raise ValueError(f"Unsafe source path: {source}")

        if source.name.startswith("._") or source.name == ".DS_Store":
            ignored.append(source)
            continue
        if source.suffix.lower() not in VIDEO_EXTS:
            ignored.append(source)
            continue

        job = journal.get(torrent_hash, str(source))
        if not job or job["status"] not in ("done", "quarantined"):
            raise ValueError(f"Incomplete media job: {source}")

        output_name = job.get("output")
        if not output_name:
            raise ValueError(f"Missing output record: {source}")
        output = Path(output_name).resolve()
        if not is_within(output, root) or output == quarantine_root or is_within(output, quarantine_root):
            raise ValueError(f"Unsafe output path: {output}")
        if output.parent != source.parent:
            raise ValueError(f"Output must remain alongside source: {source}")
        if not output.is_file() or output.stat().st_size == 0:
            raise ValueError(f"Missing or empty verified output: {output}")

        if output == source:
            if job["status"] != "done":
                raise ValueError(f"Untouched file cannot be quarantined: {source}")
            unchanged.append(source)
        else:
            if job["status"] == "done" and not source.is_file():
                raise ValueError(f"Original missing before quarantine: {source}")
            originals.append(source)

    if not originals and not unchanged:
        raise ValueError("No eligible video files")
    return RetirementPlan(torrent_hash, tuple(originals), tuple(unchanged), tuple(ignored))


def finalize_torrent(qbit, torrent, journal, download_root, quarantine_root,
                     *, finalize_enabled=False, allow_torrent_retirement=False,
                     retention_days=7):
    """Retire a fully processed, explicitly eligible torrent.

    Never invoke for frequently updated torrents. Quarantines verified originals
    before removing the qBittorrent record with deleteFiles=false. A failure
    leaves files intact in their current or quarantined locations for recovery.
    """
    if not finalize_enabled:
        raise PermissionError("Finalization is disabled")
    if not allow_torrent_retirement:
        raise PermissionError("Torrent retirement requires an explicit policy opt-in")

    entries = qbit.files(torrent["hash"])
    plan = plan_retirement(torrent, entries, journal, download_root, quarantine_root)
    quarantine = Quarantine(download_root, quarantine_root, journal, retention_days)
    for source in plan.originals:
        status = journal.status(plan.torrent_hash, str(source))
        if status == "quarantined":
            target = quarantine.destination(plan.torrent_hash, source)
            if not target.is_file():
                raise RuntimeError(f"Quarantine record exists, but file is missing: {target}")
        else:
            quarantine.move(plan.torrent_hash, source)

    # Only reached once every selected video has been accounted for.
    qbit.remove_record_only(plan.torrent_hash, confirmed=True)
    journal.event(plan.torrent_hash, "", "torrent_retired", "Removed qBittorrent record; data retained")
    return plan
