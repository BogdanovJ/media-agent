"""
Safe quarantine handling for qBittorrent downloads.

Responsibilities:
- Validate quarantine eligibility.
- Preserve original directory structure.
- Move files without overwriting existing destinations.
- Record quarantine timestamps in SQLite.
- Attempt rollback if journaling fails.
- Identify expired quarantine records.

Automatic deletion is deliberately not implemented here.
"""

import logging
import os
import re
import hashlib

from .safe_move import move_no_replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("media-agent.quarantine")

HASH_PATTERN = re.compile(r"^[a-fA-F0-9]{40}$")


def utcnow():
    return datetime.now(timezone.utc)


def is_within(path: Path, root: Path) -> bool:
    path = Path(path).resolve()
    root = Path(root).resolve()

    return path != root and root in path.parents


def fingerprint(path, chunk_size=4 * 1024 * 1024):
    """
    Calculate SHA-256 and size without loading the entire
    video into memory.

    Returns:
        (size_bytes, sha256_hex)
    """

    path = Path(path)

    if path.is_symlink() or not path.is_file():
        raise ValueError("Fingerprint source must be a regular file")

    digest = hashlib.sha256()
    size = 0

    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
            size += len(chunk)

    return size, digest.hexdigest()


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
        Calculate the destination without modifying files.
        """

        if not HASH_PATTERN.fullmatch(torrent_hash):
            raise ValueError("Invalid torrent hash")

        source = Path(source).resolve()

        if not is_within(source, self.download_root):
            raise ValueError("Source outside download root")

        if is_within(source, self.quarantine_root):
            raise ValueError("Source already inside quarantine")

        relative = source.relative_to(self.download_root)

        target = self.quarantine_root / torrent_hash.lower() / relative

        if not is_within(target, self.quarantine_root):
            raise ValueError("Unsafe quarantine destination")

        return target

    def validate_eligibility(self, torrent_hash, source):
        """
        Check journal eligibility before moving an original.

        Important:
        The output must already have been validated by the
        conversion worker. This method checks the journal
        record and output presence, not video decodability.
        """

        source = Path(source).resolve()

        job = self.journal.get(
            torrent_hash,
            str(source),
        )

        if job is None:
            raise ValueError("No processing journal entry for source")

        if job["status"] != "done":
            raise ValueError("Source processing is not completed: " f"{job['status']}")

        output_name = job.get("output")

        if not output_name:
            raise ValueError("Completed job has no recorded output")

        output = Path(output_name).resolve()

        if not output.is_file():
            raise ValueError("Validated output file is missing")

        if output.stat().st_size == 0:
            raise ValueError("Validated output file is empty")

        if output == source:
            raise ValueError("Output cannot be the original source")

        if is_within(output, self.quarantine_root):
            raise ValueError("Output cannot be inside quarantine")

        return job

    def move(self, torrent_hash, source):
        """
        Move a successfully processed original into quarantine.

        Safeguards:
        - Completed journal record required.
        - Output must exist and be nonempty.
        - Existing destination cannot be overwritten.
        - Source and destination must share a filesystem.
        - Journal intent is recorded before movement.
        - Rollback is attempted if final journaling fails.

        Caller must verify ALL files belonging to the torrent
        before initiating any quarantine operation.
        """

        source = Path(source).resolve()

        target = self.destination(
            torrent_hash,
            source,
        )

        # Check source existence before querying the journal.
        if not source.is_file():
            raise FileNotFoundError(source)

        # Confirm the job completed successfully
        # and its converted output is available.
        self.validate_eligibility(
            torrent_hash,
            source,
        )

        if target.exists() or target.is_symlink():
            raise FileExistsError(target)

        target.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        if os.stat(source).st_dev != os.stat(target.parent).st_dev:
            raise OSError("Source and quarantine must share a filesystem")

        # Capture the original's identity before movement.
        source_size, source_sha256 = fingerprint(source)

        self.journal.event(
            torrent_hash,
            str(source),
            "quarantine_intent",
            str(target),
        )

        # Recheck before moving.
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)

        # Atomically refuse an existing destination.
        # Unsupported filesystem operations fail closed.

        # The filesystem move, fingerprint verification and
        # journal update must share the same recovery handler.
        try:
            move_no_replace(source, target)

            # Confirm the moved file still matches the original.
            target_size, target_sha256 = fingerprint(target)

            if target_size != source_size or target_sha256 != source_sha256:
                raise RuntimeError("Quarantine fingerprint verification failed")

            moved_at = utcnow()
            purge_after = moved_at + timedelta(days=self.retention_days)

            cursor = self.journal.db.execute(
                """
                UPDATE jobs
                SET status = 'quarantined',
                    quarantined_at = ?,
                    purge_after = ?,
                    updated_at = ?,
                    source_size = ?,
                    source_sha256 = ?,
                    fingerprinted_at = ?
                WHERE torrent_hash = ?
                  AND source = ?
                  AND status = 'done'
                  AND source_sha256 IS NULL
                """,
                (
                    moved_at.isoformat(),
                    purge_after.isoformat(),
                    moved_at.isoformat(),
                    source_size,
                    source_sha256,
                    moved_at.isoformat(),
                    torrent_hash,
                    str(source),
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError("Journal eligibility changed during move")

            self.journal._insert_event(
                torrent_hash,
                str(source),
                "quarantined",
                str(target),
            )

            self.journal._insert_event(
                torrent_hash,
                str(source),
                "fingerprinted",
                f"size={source_size} sha256={source_sha256}",
            )

            self.journal.db.commit()

        except Exception:
            self.journal.db.rollback()

            log.exception(
                "Quarantine journal update failed: %s",
                source,
            )

            # Restore original if possible.
            if target.exists() and not source.exists():
                try:
                    move_no_replace(target, source)

                    log.warning(
                        "Restored original after journal failure: %s",
                        source,
                    )

                except OSError:
                    log.critical(
                        "QUARANTINE RECOVERY REQUIRED: " "source=%s target=%s",
                        source,
                        target,
                        exc_info=True,
                    )

            raise

        log.info(
            "Quarantined source=%s target=%s purge_after=%s",
            source,
            target,
            purge_after.isoformat(),
        )

        return target

    def recover(self):
        """
        Reconcile interrupted quarantine moves.

        Looks for quarantine_intent events without a
        corresponding completed quarantine operation.

        Recovery rules:
        - Original exists, target missing: already safe.
        - Original missing, target exists: restore original.
        - Both exist: report conflict, do not modify.
        - Neither exists: report missing files.

        Never deletes files during recovery.
        """

        cursor = self.journal.db.execute("""
            SELECT e.torrent_hash, e.source, e.detail
            FROM events e
            JOIN jobs j
              ON j.torrent_hash = e.torrent_hash
             AND j.source = e.source
            WHERE e.event = 'quarantine_intent'
              AND j.status = 'done'
              AND NOT EXISTS (
                  SELECT 1
                  FROM events later
                  WHERE later.torrent_hash = e.torrent_hash
                    AND later.source = e.source
                    AND later.id > e.id
                    AND later.event IN (
                        'quarantined',
                        'quarantine_recovered'
                    )
              )
            ORDER BY e.id
            """)

        recovered = 0

        for torrent_hash, source_name, target_name in cursor.fetchall():
            source = Path(source_name)
            target = Path(target_name)

            # Validate both recorded paths before touching files.
            try:
                expected = self.destination(
                    torrent_hash,
                    source,
                )

                if target != expected:
                    raise ValueError(
                        "Journal destination does not match " "expected quarantine path"
                    )

            except ValueError:
                log.critical(
                    "Unsafe quarantine recovery record: %s",
                    source,
                    exc_info=True,
                )
                continue

            source_exists = source.exists() or source.is_symlink()
            target_exists = target.exists() or target.is_symlink()

            if source_exists and not target_exists:
                # No move occurred, or rollback already succeeded.
                self.journal.event(
                    torrent_hash,
                    source_name,
                    "quarantine_recovered",
                    "Original already present",
                )
                recovered += 1
                continue

            if not source_exists and target_exists:
                source.parent.mkdir(
                    parents=True,
                    exist_ok=True,
                )

                try:
                    # Fail rather than overwrite a destination
                    # that appeared during recovery.
                    if source.exists() or source.is_symlink():
                        raise FileExistsError(source)

                    move_no_replace(target, source)

                    self.journal.event(
                        torrent_hash,
                        source_name,
                        "quarantine_recovered",
                        "Original restored",
                    )

                    recovered += 1

                    log.warning(
                        "Recovered interrupted move: %s",
                        source,
                    )

                except OSError:
                    log.critical(
                        "Manual quarantine recovery required: %s",
                        source,
                        exc_info=True,
                    )

                continue

            if source_exists and target_exists:
                log.critical(
                    "Quarantine collision: both paths exist. " "source=%s target=%s",
                    source,
                    target,
                )
                continue

            log.critical(
                "Quarantine recovery failed: both paths missing. "
                "source=%s target=%s",
                source,
                target,
            )

        return recovered

    def expired(self):
        """
        List expired quarantine journal records.

        No filesystem deletion takes place.
        """

        cursor = self.journal.db.execute(
            """
            SELECT torrent_hash, source, purge_after
            FROM jobs
            WHERE status = 'quarantined'
              AND purge_after IS NOT NULL
              AND purge_after <= ?
            ORDER BY purge_after
            """,
            (utcnow().isoformat(),),
        )

        return cursor.fetchall()
