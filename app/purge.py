"""
Journal-backed quarantine retention scanner.

Responsibilities:
- Find expired quarantine entries.
- Verify journal records and filesystem paths.
- Reject unsafe, missing or unexpected files.
- Produce structured dry-run reports.

Physical file deletion is intentionally not implemented yet.
"""

import logging
import re
import os
import stat
from unittest import result

from .quarantine import fingerprint, is_within
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("media-agent.purge")

HASH_PATTERN = re.compile(r"^[a-fA-F0-9]{40}$")


class Purge:
    def __init__(
        self,
        download_root,
        quarantine_root,
        journal,
    ):
        self.download_root = Path(download_root).resolve()
        self.quarantine_root = Path(quarantine_root).resolve()
        self.journal = journal

        if not is_within(
            self.quarantine_root,
            self.download_root,
        ):
            raise ValueError("Quarantine must be inside download root")

    def inspect(self, job, current_time=None):
        """
        Determine whether a journal record is eligible
        for future purging.

        Does not modify the filesystem or database.
        """

        current_time = current_time or datetime.now(timezone.utc)

        torrent_hash = job["torrent_hash"]
        source_name = job["source"]

        result = {
            "torrent_hash": torrent_hash,
            "source": source_name,
            "target": None,
            "status": "blocked",
            "reason": None,
            "size_bytes": 0,
        }

        if job["status"] != "quarantined":
            result["reason"] = "Job is not quarantined"
            return result

        if not HASH_PATTERN.fullmatch(torrent_hash):
            result["reason"] = "Invalid torrent hash"
            return result

        purge_after = job.get("purge_after")

        if not purge_after:
            result["reason"] = "Missing retention deadline"
            return result

        try:
            deadline = datetime.fromisoformat(purge_after)

            if deadline.tzinfo is None:
                raise ValueError("Timezone is missing")

        except (ValueError, TypeError):
            result["reason"] = "Invalid retention deadline"
            return result

        if current_time < deadline:
            result["status"] = "retained"
            result["reason"] = "Retention period not expired"
            return result

        # Permanent deletion requires a recorded file identity.
        expected_size = job.get("source_size")
        expected_sha256 = job.get("source_sha256")

        if not isinstance(expected_size, int) or expected_size <= 0:
            result["reason"] = "Missing or invalid recorded file size"
            return result

        if not isinstance(expected_sha256, str) or not re.fullmatch(
            r"[a-fA-F0-9]{64}", expected_sha256
        ):
            result["reason"] = "Missing or invalid SHA-256 fingerprint"
            return result

        # Do not follow symlinks when establishing file identity.
        source = Path(source_name)
        target_root = self.quarantine_root / torrent_hash.lower()

        if not source.is_absolute():
            result["reason"] = "Source path is not absolute"
            return result

        # Reject symlinks in the original source path.
        if source.is_symlink():
            result["reason"] = "Source path is a symlink"
            return result

        resolved_source = source.resolve()

        if not is_within(
            resolved_source,
            self.download_root,
        ):
            result["reason"] = "Source outside download root"
            return result

        if is_within(
            resolved_source,
            self.quarantine_root,
        ):
            result["reason"] = "Source already in quarantine"
            return result

        relative = resolved_source.relative_to(self.download_root)

        target = target_root / relative
        result["target"] = str(target)

        if not is_within(target, self.quarantine_root):
            result["reason"] = "Unsafe quarantine path"
            return result
        # Reject symlinks anywhere beneath the download root.
        # Checking only the final file would miss redirected folders.
        current = target

        while current != self.download_root:
            if current.is_symlink():
                result["reason"] = "Symlink in quarantine path"
                return result

            if current == current.parent:
                result["reason"] = "Invalid quarantine path"
                return result

            current = current.parent
        if target.is_symlink():
            result["reason"] = "Quarantine target is a symlink"
            return result

        if not target.is_file():
            result["reason"] = "Quarantined file is missing"
            return result

        # A duplicate source means the journal cannot
        # establish which file should be retained.
        if resolved_source.exists():
            result["reason"] = "Original source path exists again"
            return result

        # Require a matching completed movement event.
        event = self.journal.db.execute(
            """
            SELECT id
            FROM events
            WHERE torrent_hash = ?
              AND source = ?
              AND event = 'quarantined'
              AND detail = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (
                torrent_hash,
                source_name,
                str(target),
            ),
        ).fetchone()

        if event is None:
            result["reason"] = "No matching quarantine completion event"
            return result

        # First check the size to avoid unnecessary hashing
        # when the file is clearly different.
        try:
            actual_size = target.stat().st_size

            if actual_size != expected_size:
                result["reason"] = "Quarantined file size changed"
                return result

            # Read and hash the actual quarantined file.
            hashed_size, actual_sha256 = fingerprint(target)

        except (OSError, ValueError) as exc:
            log.warning(
                "Unable to verify quarantine fingerprint: %s",
                target,
                exc_info=True,
            )

            result["reason"] = f"Fingerprint verification failed: {type(exc).__name__}"
            return result

        if hashed_size != expected_size or actual_sha256 != expected_sha256.lower():
            result["reason"] = "Quarantined file fingerprint mismatch"
            return result

        result["size_bytes"] = hashed_size
        result["status"] = "eligible"
        result["reason"] = "Retention expired; SHA-256 verified"

        return result

    def scan(self, current_time=None):
        """
        Inspect all quarantined records.

        Returns a report without deleting any files.
        """

        cursor = self.journal.db.execute("""
            SELECT *
            FROM jobs
            WHERE status = 'quarantined'
            ORDER BY purge_after
            """)

        columns = [item[0] for item in cursor.description]

        results = []

        for row in cursor.fetchall():
            job = dict(zip(columns, row))

            report = self.inspect(
                job,
                current_time=current_time,
            )

            results.append(report)

            log.info(
                "Purge scan: status=%s source=%s reason=%s",
                report["status"],
                report["source"],
                report["reason"],
            )

        return results

    def delete_expired(self, torrent_hash, source, enabled=False):
        """
        Permanently delete one expired, verified quarantine file.

        Disabled by default.

        Safety:
        - Re-read the current SQLite journal entry.
        - Re-run retention and fingerprint verification.
        - Open the quarantine directory without following symlinks.
        - Open the file without following symlinks.
        - Verify the file through its open descriptor.
        - Record deletion intent before unlinking.
        - Never automatically retry interrupted purges.

        This operation assumes the quarantine directory is
        exclusively managed by media-agent.
        """

        if not enabled:
            raise PermissionError("Permanent deletion is disabled")

        job = self.journal.get(torrent_hash, source)

        if job is None:
            raise ValueError("No journal record for requested file")

        # Inspect against the current time, not a supplied
        # or previously cached eligibility report.
        report = self.inspect(
            job,
            current_time=datetime.now(timezone.utc),
        )

        if report["status"] != "eligible":
            raise ValueError(f"Purge rejected: {report['reason']}")

        target = Path(report["target"])

        # Recheck the expected path.
        expected = (
            self.quarantine_root
            / torrent_hash.lower()
            / Path(source).resolve().relative_to(self.download_root)
        )

        if target != expected:
            raise ValueError("Quarantine destination mismatch")

        expected_size = job["source_size"]
        expected_sha256 = job["source_sha256"].lower()

        # Walk from the trusted quarantine root using
        # directory descriptors and O_NOFOLLOW.
        # This prevents following symlinked child directories.
        root_fd = os.open(
            self.quarantine_root,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
        )

        directory_fd = root_fd
        file_fd = None

        try:
            relative = target.relative_to(self.quarantine_root)

            for component in relative.parts[:-1]:
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )

                if directory_fd != root_fd:
                    os.close(directory_fd)

                directory_fd = next_fd

            filename = relative.name

            file_fd = os.open(
                filename,
                os.O_RDONLY | os.O_NOFOLLOW,
                dir_fd=directory_fd,
            )

            before = os.fstat(file_fd)

            if not stat.S_ISREG(before.st_mode):
                raise ValueError("Purge target is not a regular file")

            if before.st_size != expected_size:
                raise ValueError("Purge target size changed")

            # Hash through the open descriptor, rather
            # than reopening an untrusted pathname.
            import hashlib

            digest = hashlib.sha256()
            actual_size = 0

            while True:
                chunk = os.read(
                    file_fd,
                    4 * 1024 * 1024,
                )

                if not chunk:
                    break

                digest.update(chunk)
                actual_size += len(chunk)

            after = os.fstat(file_fd)

            if actual_size != expected_size or digest.hexdigest() != expected_sha256:
                raise ValueError("Final SHA-256 verification failed")

            if (
                before.st_ino != after.st_ino
                or before.st_dev != after.st_dev
                or before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns
                or before.st_ctime_ns != after.st_ctime_ns
            ):
                raise ValueError("File changed during verification")

            # Confirm that the directory entry still
            # refers to the verified file.
            current = os.stat(
                filename,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )

            if (
                current.st_ino != after.st_ino
                or current.st_dev != after.st_dev
                or not stat.S_ISREG(current.st_mode)
            ):
                raise ValueError("File identity changed before deletion")

            # Persist intent before performing any
            # irreversible filesystem operation.
            timestamp = datetime.now(timezone.utc).isoformat()

            with self.journal.db:
                cursor = self.journal.db.execute(
                    """
                    UPDATE jobs
                    SET status = 'purging',
                        updated_at = ?
                    WHERE torrent_hash = ?
                      AND source = ?
                      AND status = 'quarantined'
                    """,
                    (
                        timestamp,
                        torrent_hash,
                        source,
                    ),
                )

                if cursor.rowcount != 1:
                    raise RuntimeError("Journal state changed before purge")

                self.journal._insert_event(
                    torrent_hash,
                    source,
                    "purge_intent",
                    str(target),
                )

            # An unexpected failure from here leaves the
            # job in 'purging', requiring reconciliation.
            os.unlink(
                filename,
                dir_fd=directory_fd,
            )

            os.fsync(directory_fd)

            finished_at = datetime.now(timezone.utc).isoformat()

            with self.journal.db:
                self.journal.db.execute(
                    """
                    UPDATE jobs
                    SET status = 'purged',
                        purged_at = ?,
                        updated_at = ?
                    WHERE torrent_hash = ?
                      AND source = ?
                      AND status = 'purging'
                    """,
                    (
                        finished_at,
                        finished_at,
                        torrent_hash,
                        source,
                    ),
                )

                self.journal._insert_event(
                    torrent_hash,
                    source,
                    "purged",
                    str(target),
                )

            log.info(
                "Permanently purged file: %s",
                target,
            )

            return {
                "status": "purged",
                "source": source,
                "target": str(target),
                "bytes_deleted": actual_size,
            }

        finally:
            if file_fd is not None:
                os.close(file_fd)

            if directory_fd != root_fd:
                os.close(directory_fd)

            os.close(root_fd)

    def summary(self, reports):
        """
        Produce aggregate retention statistics.
        """

        return {
            "total": len(reports),
            "eligible": sum(r["status"] == "eligible" for r in reports),
            "retained": sum(r["status"] == "retained" for r in reports),
            "blocked": sum(r["status"] == "blocked" for r in reports),
            "eligible_bytes": sum(
                r["size_bytes"] for r in reports if r["status"] == "eligible"
            ),
        }
