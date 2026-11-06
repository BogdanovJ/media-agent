"""Run the existing verified purge only after successful torrent retirement."""
import logging
from pathlib import Path

from .purge import Purge
from .quarantine import is_within

log = logging.getLogger('media-agent.cleanup')


def cleanup_expired(cfg, journal):
    if not cfg.get('cleanup_enabled', False):
        return 0
    if not cfg.get('process_enabled', False) or not cfg.get('finalize_enabled', False):
        raise ValueError('Cleanup requires processing and finalization enabled')
    purge = Purge(cfg['download_root'], cfg['quarantine_root'], journal)
    cursor = journal.db.execute("SELECT torrent_hash, source FROM jobs WHERE status='quarantined'")
    deleted = 0
    for torrent_hash, source in cursor.fetchall():
        # A quarantine move alone does not prove qBittorrent retirement succeeded.
        retired = journal.db.execute(
            "SELECT 1 FROM events WHERE torrent_hash=? AND event='torrent_retired' LIMIT 1",
            (torrent_hash,),
        ).fetchone()
        lifecycle = journal.db.execute("SELECT state FROM retirements WHERE torrent_hash=?", (torrent_hash,)).fetchone()
        if lifecycle and lifecycle[0] != "completed":
            continue
        if not retired:
            continue
        try:
            job = journal.get(torrent_hash, source)
            output = Path(job.get('output') or '')
            root = Path(cfg['download_root']).resolve()
            quarantine = Path(cfg['quarantine_root']).resolve()
            if (not output.is_absolute() or output.is_symlink()
                    or not output.is_file() or output.stat().st_size == 0
                    or (output.resolve() == Path(source).resolve() and not lifecycle)
                    or output.resolve().parent != Path(source).resolve().parent
                    or not is_within(output.resolve(), root)
                    or output.resolve() == quarantine or is_within(output.resolve(), quarantine)):
                raise ValueError('Verified output is unavailable or unsafe')
            report = purge.inspect(job)
            if report['status'] == 'retained':
                continue
            if report['status'] != 'eligible':
                raise ValueError('Quarantine verification rejected deletion')
            purge.delete_expired(torrent_hash, source, enabled=True)
            deleted += 1
        except Exception as exc:
            log.warning('Cleanup blocked hash=%s error_type=%s', torrent_hash, type(exc).__name__)
            # One unresolved failure event per file, avoiding a notification flood.
            existing = journal.db.execute(
                "SELECT 1 FROM events WHERE torrent_hash=? AND source=? AND event='cleanup_failed' LIMIT 1",
                (torrent_hash, source),
            ).fetchone()
            if not existing:
                journal.event(torrent_hash, source, 'cleanup_failed',
                              'Cleanup blocked; inspect the journal and retained output before retrying.')
    return deleted
