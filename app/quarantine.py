"""
Safe quarantine handling for qBittorrent downloads.

This module does not automatically quarantine or delete anything.
Operations must be explicitly invoked by the application after
the complete torrent has passed validation.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("media-agent.quarantine")

HASH_PATTERN = re.compile(r"^[a-fA-F0-9]{40}$")


def utcnow():
    return datetime.now(timezone.utc)


def is_within(path: Path, root: Path) -> bool:
    """
    Return True only when path is strictly inside root.
    """

    path = Path(path).resolve()
    root = Path(root).resolve()

    return path != root and root in path.parents


class Quarantine:
    def __init__(
        self,
        download_root,
        quarantine_root,
        journal,
        retention_days=7,
    ):
        self.download_root = Path(download_root).resolve()
        self.quarantine_root = Path(quarantine_root).resolve()
        self.journal = journal
        self.retention_days = retention_days

        if retention_days < 1:
            raise ValueError("Retention must be at least one day")

        if not is_within(
            self.quarantine_root,
            self.download_root,
        ):
            raise ValueError("Quarantine must be inside download root")

    def destination(self, torrent_hash, source):
        """
        Calculate the quarantine destination without
        modifying the filesystem.
        """

        if not HASH_PATTERN.fullmatch(torrent_hash):
            raise ValueError("Invalid torrent hash")

        source = Path(source).resolve()

        if not is_within(source, self.download_root):
            raise ValueError("Source outside download root")

        if is_within(source, self.quarantine_root):
            raise ValueError("Source already inside quarantine")

        relative = source.relative_to(self.download_root)

        return self.quarantine_root / torrent_hash.lower() / relative

    def move(self, torrent_hash, source):
        """
        Move an original into quarantine.

        The caller must have verified that the corresponding
        output exists and is valid.

        No existing destination is overwritten.
        """

        source = Path(source).resolve()
        target = self.destination(torrent_hash, source)

        if not source.is_file():
            raise FileNotFoundError(source)

        if target.exists():
            raise FileExistsError(target)

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        # os.rename does not silently copy files across devices.
        # We require a single filesystem for atomic movement.
        if os.stat(source).st_dev != os.stat(target.parent).st_dev:
            raise OSError("Source and quarantine must share a filesystem")

        # Reserve the name without overwriting an existing file.
        # Hard-link followed by unlink works only on a filesystem
        # that supports hard links.
        os.link(source, target)

        try:
            source.unlink()
        except Exception:
            target.unlink()
            raise

        moved_at = utcnow()
        purge_after = moved_at + timedelta(days=self.retention_days)

        self.journal.db.execute(
            """
            UPDATE jobs
            SET status = 'quarantined',
                quarantined_at = ?,
                purge_after = ?,
                updated_at = ?
            WHERE torrent_hash = ?
              AND source = ?
              AND status = 'done'
            """,
            (
                moved_at.isoformat(),
                purge_after.isoformat(),
                moved_at.isoformat(),
                torrent_hash,
                str(source),
            ),
        )

        self.journal.event(
            torrent_hash,
            str(source),
            "quarantined",
            str(target),
        )

        log.info(
            "Quarantined %s -> %s; purge after %s",
            source,
            target,
            purge_after.isoformat(),
        )

        return target

    def expired(self):
        """
        Return journal entries eligible for future purging.
        Does not delete files.
        """

        cursor = self.journal.db.execute(
            """
            SELECT torrent_hash, source, purge_after
            FROM jobs
            WHERE status = 'quarantined'
              AND purge_after IS NOT NULL
              AND purge_after <= ?
            """,
            (utcnow().isoformat(),),
        )

        return cursor.fetchall()
