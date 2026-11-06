import json
import logging
import os
from pathlib import Path
import shutil
import time
import yaml
from .compat import decision
from .media import probe
from .quarantine import is_within as within
from . import worker
from .jobs import submit
from .paths import final_path, legacy_path, sources, work_dir, checked
from .journal import Journal
from .notifier import NotificationDispatcher, TelegramNotifier, TelegramSettings
from .metrics import MetricsState, start_metrics
from .cleanup import cleanup_expired
from .qbit import Qbit
from .publisher import finalize_torrent


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s"
)
log = logging.getLogger("media-agent")


def approved(cfg, h):
    allowed = cfg.get("retirement_allow_hashes", [])
    if not isinstance(allowed, list):
        raise ValueError("retirement_allow_hashes must be a list")
    return (cfg.get("process_enabled") and cfg.get("finalize_enabled")
            and (cfg.get("retire_all_completed") is True or h.lower() in {value.lower() for value in allowed}))


def process_file(h, src, cfg, journal, root):
    job = journal.get(h, str(src)) or {}
    status = job.get("status")
    if status == "review":
        # Re-evaluate legacy MP4s once under the chapter-aware validation rule.
        if job["reason"] != "Output does not meet the Samsung MP4 profile" or not (job.get("metadata") or {}).get("work"):
            return
        status = "interrupted"
    if status in ("validated", "quarantined", "purging", "purged"):
        return
    if not src.is_file():
        journal.put(h, str(src), "missing", reason="Source not found")
        return
    candidate = None
    if status == "done" and job["output"] != str(src):
        candidate = legacy_path(src)
        if job["output"] != str(candidate) or not candidate.is_file():
            journal.put(h, str(src), "review", reason="Legacy output missing or unexpected")
            return
        action = job["action"]
        reason = "Revalidating recorded legacy output"
    elif status in ("queued", "failed", "interrupted") and (job.get("metadata") or {}).get("work"):
        action = job["action"]
        candidate = Path(job["metadata"]["candidate"]) if job["metadata"]["candidate"] else None
        reason = "Resuming staged work"
    else:
        action, reason = decision(probe(src), src.stat().st_size, cfg["review_over_bytes"])
        if action == "copy":
            candidate = src
    if action == "review" or not cfg["process_enabled"]:
        if status != "done":
            journal.put(h, str(src), "review" if action == "review" else "planned", action, reason)
        return
    if candidate is not None:
        if candidate not in (legacy_path(src), src) or (candidate == src) != (action == "copy"):
            raise ValueError("Invalid adoption candidate")
        checked(candidate, root)
    elif any(os.path.lexists(path) for path in {final_path(src), legacy_path(src)} - {src}):
        journal.put(h, str(src), "review", action, "Unattributed output already exists")
        return
    directory = work_dir(root, h, src)
    directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    receipt = checked(directory / "validated.json", root)
    failure = checked(directory / "review-v2.txt", root)
    if failure.exists():
        if job.get("status") == "review":
            return
        journal.put(h, str(src), "review", action, failure.read_text()[:1000])
        return
    if not receipt.exists() and candidate is None and shutil.disk_usage(directory).free < cfg["min_free_bytes"]:
        journal.put(h, str(src), "blocked", action, "Insufficient free space")
        return
    descriptor = dict(work=True, candidate=str(candidate or ""))
    if status != "queued":
        journal.put(h, str(src), "queued", action, reason, metadata=descriptor)
    try:
        if not receipt.exists():
            if cfg.get("worker_jobs", False):
                submit(root, directory, src, action, candidate)
                return
            worker.run(src, directory, action, candidate, cfg["ffmpeg_threads"])
        result = json.loads(receipt.read_text())
        if (result["source"], result["action"], result["candidate"]) != (str(src), action, descriptor["candidate"]):
            raise ValueError("Staged result belongs to different work")
        journal.put(h, str(src), "validated", action, "Media validated; awaiting torrent retirement",
                    str(candidate or directory / "output.mp4"), metadata=result)
    except Exception as exc:
        journal.put(h, str(src), "failed", action, str(exc), metadata=descriptor)
        log.exception("Processing failed: %s", src)


def process_torrent(q, t, cfg, journal):
    h = t["hash"]
    if journal.db.execute("SELECT 1 FROM retirements WHERE torrent_hash=?", (h,)).fetchone():
        return  # Recovery is independent of the completed-torrent listing.
    root = Path(cfg["download_root"]).resolve()
    selected = sources(t, q.files(h), root, Path(cfg["quarantine_root"]).resolve())
    for src in selected:
        process_file(h, src, cfg, journal, root)
    if approved(cfg, h) and selected and all(journal.status(h, str(s)) == "validated" for s in selected):
        finalize_torrent(q, t, journal, root, cfg["quarantine_root"],
                         finalize_enabled=True, allow_torrent_retirement=True)


def recover(q, cfg, journal):
    pending = journal.db.execute("SELECT torrent_hash,save_path FROM retirements WHERE state!='completed'")
    for h, path in pending.fetchall():
        if approved(cfg, h):
            try:
                finalize_torrent(q, dict(hash=h, save_path=path), journal, cfg["download_root"],
                                 cfg["quarantine_root"], finalize_enabled=True, allow_torrent_retirement=True)
            except Exception:
                log.exception("Retirement recovery blocked: %s", h)


def main():
    with open(
        os.getenv("CONFIG_FILE", "/opt/media-agent/config/samsung-dlna.yaml"),
        encoding="utf-8",
    ) as f:
        cfg = yaml.safe_load(f)
    Path(cfg["state_db"]).parent.mkdir(parents=True, exist_ok=True)
    journal = Journal(cfg["state_db"])
    telegram_settings = TelegramSettings.from_environment()
    dispatcher = NotificationDispatcher(
        journal, TelegramNotifier(telegram_settings) if telegram_settings else None,
    )
    log.info(
        "media-agent v0.1 started process_enabled=%s finalize_enabled=%s",
        cfg["process_enabled"],
        cfg["finalize_enabled"],
    )
    metrics_state = MetricsState()
    metrics_server = start_metrics(cfg["state_db"], cfg, metrics_state)
    try:
        while True:
            try:
                q = Qbit(
                    os.environ["QBIT_URL"],
                    os.environ["QBIT_USERNAME"],
                    os.environ["QBIT_PASSWORD"],
                )
                recover(q, cfg, journal)
                for torrent in q.completed():
                    try:
                        process_torrent(q, torrent, cfg, journal)
                    except Exception:
                        log.exception("Torrent processing error: %s", torrent.get("hash"))
                metrics_state.poll_finished(True)
            except Exception:
                metrics_state.poll_finished(False)
                log.exception("Polling error")
            try:
                cleanup_expired(cfg, journal)
            except Exception:
                log.exception("Cleanup scan failed")
            # Retry notifications even when qBittorrent polling fails.
            dispatcher.dispatch()
            time.sleep(cfg["poll_seconds"])
    finally:
        if metrics_server is not None:
            metrics_server.shutdown()
            metrics_server.server_close()
        journal.close()


if __name__ == "__main__":
    main()
