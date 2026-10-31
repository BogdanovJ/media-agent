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
from datetime import datetime, timezone
from pathlib import Path

from .quarantine import is_within

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
            raise ValueError(
                "Quarantine must be inside download root"
            )

    def inspect(self, job, current_time=None):
        """
        Determine whether a journal record is eligible
        for future purging.

        Does not modify the filesystem or database.
        """

        current_time = current_time or datetime.now(
            timezone.utc
        )

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

        # Do not follow symlinks when establishing file identity.
        source = Path(source_name)
        target_root = (
            self.quarantine_root / torrent_hash.lower()
        )

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

        relative = resolved_source.relative_to(
            self.download_root
        )

        target = target_root / relative
        result["target"] = str(target)

        if not is_within(target, self.quarantine_root):
            result["reason"] = "Unsafe quarantine path"
            return result

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
            result["reason"] = (
                "No matching quarantine completion event"
            )
            return result

        result["size_bytes"] = target.stat().st_size
        result["status"] = "eligible"
        result["reason"] = "Retention expired"

        return result

    def scan(self, current_time=None):
        """
        Inspect all quarantined records.

        Returns a report without deleting any files.
        """

        cursor = self.journal.db.execute(
            """
            SELECT *
            FROM jobs
            WHERE status = 'quarantined'
            ORDER BY purge_after
            """
        )

        columns = [
            item[0] for item in cursor.description
        ]

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

    def summary(self, reports):
        """
        Produce aggregate retention statistics.
        """

        return {
            "total": len(reports),
            "eligible": sum(
                r["status"] == "eligible"
                for r in reports
            ),
            "retained": sum(
                r["status"] == "retained"
                for r in reports
            ),
            "blocked": sum(
                r["status"] == "blocked"
                for r in reports
            ),
            "eligible_bytes": sum(
                r["size_bytes"]
                for r in reports
                if r["status"] == "eligible"
            ),
        }
