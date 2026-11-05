import json
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

JOB_STATUSES = {
    "discovered",
    "planned",
    "running",
    "validating",
    "done",
    "review",
    "blocked",
    "failed",
    "missing",
    "interrupted",
    "quarantined",
    "purged",
    "purging",
}

NOTIFICATION_EVENTS = {
    "done", "failed", "review", "blocked", "missing", "interrupted",
    "quarantined", "purged", "torrent_retired", "cleanup_failed",
}


PROTECTED_STATUSES = {"quarantined", "purging", "purged"}


RETRYABLE_STATUSES = {
    "failed",
    "blocked",
    "interrupted",
}


def now():
    return datetime.now(timezone.utc).isoformat()


class Journal:
    """
    Persistent SQLite journal for media processing.

    Every job is identified by:
        torrent_hash + source_path

    The journal preserves:
        - current processing state
        - selected conversion action
        - input and output paths
        - failure and retry information
        - quarantine and purge timestamps
        - append-only event history
    """

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.db = sqlite3.connect(
            self.path,
            timeout=30,
        )

        self.db.execute("PRAGMA busy_timeout = 30000")
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")

        self._initialize()
        self._recover_interrupted_jobs()

    def _initialize(self):
        # Retain compatibility with the original v0.1 schema.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                torrent_hash TEXT NOT NULL,
                source TEXT NOT NULL,
                status TEXT NOT NULL,
                action TEXT,
                reason TEXT,
                output TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (torrent_hash, source)
            )
            """)

        existing_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(jobs)")
        }

        migrations = {
            "created_at": "TEXT",
            "started_at": "TEXT",
            "completed_at": "TEXT",
            "quarantined_at": "TEXT",
            "purge_after": "TEXT",
            "purged_at": "TEXT",
            "attempts": "INTEGER NOT NULL DEFAULT 0",
            "last_error": "TEXT",
            "metadata": "TEXT",
            "source_size": "INTEGER",
            "source_sha256": "TEXT",
            "fingerprinted_at": "TEXT",
        }

        for column, definition in migrations.items():
            if column not in existing_columns:
                self.db.execute(
                    f"ALTER TABLE jobs " f"ADD COLUMN {column} {definition}"
                )

        self.db.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                torrent_hash TEXT,
                source TEXT,
                event TEXT NOT NULL,
                detail TEXT
            )
            """)

        self.db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_jobs_status
            ON jobs(status)
            """)

        self.db.execute("""
            CREATE INDEX IF NOT EXISTS
            idx_jobs_purge_after
            ON jobs(purge_after)
            """)

        # Existing event history is deliberately not replayed on migration.
        # Keep immutable payloads independent of later event-history pruning.
        self.db.execute("""
            CREATE TABLE IF NOT EXISTS notification_outbox (
                event_id INTEGER PRIMARY KEY,
                event TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0,
                claim_token TEXT,
                delivered_at TEXT,
                last_error TEXT
            )
            """)
        self.db.execute("""
            CREATE INDEX IF NOT EXISTS idx_outbox_pending
            ON notification_outbox(available_at, event_id)
            WHERE delivered_at IS NULL
            """)

        self.db.commit()

    def _recover_interrupted_jobs(self):
        """
        A restart cannot imply a successful conversion.

        Jobs previously marked running or validating
        become interrupted, ready for explicit recovery.
        """

        rows = self.db.execute("""
            SELECT torrent_hash, source, status
            FROM jobs
            WHERE status IN ('running', 'validating')
            """).fetchall()

        with self.db:
            for torrent_hash, source, old_status in rows:
                self.db.execute(
                    """
                    UPDATE jobs
                    SET status = 'interrupted',
                        updated_at = ?,
                        last_error = ?
                    WHERE torrent_hash = ?
                      AND source = ?
                    """,
                    (
                        now(),
                        f"Interrupted during {old_status}",
                        torrent_hash,
                        source,
                    ),
                )

                self._insert_event(
                    torrent_hash,
                    source,
                    "interrupted",
                    f"Recovered from {old_status}",
                )

    def _insert_event(
        self,
        torrent_hash,
        source,
        event,
        detail="",
        *,
        notify=True,
    ):
        cursor = self.db.execute(
            """
            INSERT INTO events (
                at,
                torrent_hash,
                source,
                event,
                detail
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                now(),
                torrent_hash,
                source,
                event,
                detail,
            ),
        )

        event_id = cursor.lastrowid
        if notify and event in NOTIFICATION_EVENTS:
            message = f"Torrent: {torrent_hash or '-'}\nSource: {source or '-'}"
            if detail:
                message += f"\n{detail}"
            self.db.execute(
                """
                INSERT INTO notification_outbox
                    (event_id, event, message, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (event_id, event, message, now()),
            )
        return event_id

    def event(
        self,
        torrent_hash,
        source,
        event,
        detail="",
    ):
        with self.db:
            self._insert_event(
                torrent_hash,
                source,
                event,
                detail,
            )

    def put(
        self,
        torrent_hash,
        source,
        status,
        action="",
        reason="",
        output="",
        metadata=None,
    ):
        if status not in JOB_STATUSES:
            raise ValueError(f"Invalid job status: {status}")

        timestamp = now()

        previous = self.db.execute(
            """
            SELECT status, attempts, reason
            FROM jobs
            WHERE torrent_hash = ?
              AND source = ?
            """,
            (torrent_hash, source),
        ).fetchone()

        if previous and previous[0] in PROTECTED_STATUSES:
            raise ValueError(
                f"Cannot overwrite protected job status {previous[0]!r} with {status!r}; "
                "use the explicit quarantine/purge lifecycle"
            )

        attempts = previous[1] if previous else 0

        if status == "running" and (previous is None or previous[0] != "running"):
            attempts += 1

        metadata_json = (
            json.dumps(metadata, ensure_ascii=False) if metadata is not None else None
        )

        with self.db:
            self.db.execute(
                """
                INSERT INTO jobs (
                    torrent_hash,
                    source,
                    status,
                    action,
                    reason,
                    output,
                    updated_at,
                    created_at,
                    started_at,
                    completed_at,
                    attempts,
                    last_error,
                    metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)

                ON CONFLICT(torrent_hash, source)
                DO UPDATE SET
                    status = excluded.status,
                    action = excluded.action,
                    reason = excluded.reason,
                    output = excluded.output,
                    updated_at = excluded.updated_at,
                    started_at = COALESCE(
                        excluded.started_at,
                        jobs.started_at
                    ),
                    completed_at = COALESCE(
                        excluded.completed_at,
                        jobs.completed_at
                    ),
                    attempts = excluded.attempts,
                    last_error = excluded.last_error,
                    metadata = COALESCE(
                        excluded.metadata,
                        jobs.metadata
                    )
                WHERE jobs.status NOT IN ('quarantined', 'purging', 'purged')
                """,
                (
                    torrent_hash,
                    source,
                    status,
                    action,
                    reason,
                    output,
                    timestamp,
                    timestamp,
                    timestamp if status == "running" else None,
                    timestamp if status == "done" else None,
                    attempts,
                    reason if status == "failed" else None,
                    metadata_json,
                ),
            )

            if self.db.execute("SELECT changes()").fetchone()[0] != 1:
                self.db.rollback()
                raise RuntimeError("Job state changed during update; protected state preserved")

            self._insert_event(
                torrent_hash,
                source,
                status,
                reason,
                notify=previous is None or (previous[0], previous[2]) != (status, reason),
            )


    def status(self, torrent_hash, source):
        row = self.db.execute(
            """
            SELECT status
            FROM jobs
            WHERE torrent_hash = ?
              AND source = ?
            """,
            (torrent_hash, source),
        ).fetchone()

        return row[0] if row else None

    def get(self, torrent_hash, source):
        cursor = self.db.execute(
            """
            SELECT *
            FROM jobs
            WHERE torrent_hash = ?
              AND source = ?
            """,
            (torrent_hash, source),
        )

        row = cursor.fetchone()

        if row is None:
            return None

        columns = [item[0] for item in cursor.description]

        result = dict(zip(columns, row))

        if result.get("metadata"):
            result["metadata"] = json.loads(result["metadata"])

        return result

    def jobs_for_torrent(self, torrent_hash):
        cursor = self.db.execute(
            """
            SELECT *
            FROM jobs
            WHERE torrent_hash = ?
            ORDER BY source
            """,
            (torrent_hash,),
        )

        columns = [item[0] for item in cursor.description]

        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def record_fingerprint(
        self,
        torrent_hash: str,
        source: str,
        size: int,
        sha256: str,
    ):
        """
        Store the verified identity of a quarantined file.

        Only quarantined jobs may receive fingerprints.
        Existing fingerprints cannot be silently replaced.
        """

        import re

        if size < 0:
            raise ValueError("Invalid file size")

        if not re.fullmatch(r"[a-fA-F0-9]{64}", sha256):
            raise ValueError("Invalid SHA-256 digest")

        digest = sha256.lower()

        job = self.get(torrent_hash, source)

        if job is None:
            raise ValueError("Unknown journal entry")

        if job["status"] != "quarantined":
            raise ValueError("File must be quarantined before fingerprinting")

        if job["source_sha256"] is not None:
            if job["source_sha256"] != digest or job["source_size"] != size:
                raise ValueError("Existing fingerprint does not match")

            return

        timestamp = now()

        with self.db:
            cursor = self.db.execute(
                """
                UPDATE jobs
                SET source_size = ?,
                    source_sha256 = ?,
                    fingerprinted_at = ?,
                    updated_at = ?
                WHERE torrent_hash = ?
                  AND source = ?
                  AND status = 'quarantined'
                  AND source_sha256 IS NULL
                """,
                (
                    size,
                    digest,
                    timestamp,
                    timestamp,
                    torrent_hash,
                    source,
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError("Fingerprint journal update rejected")

            self._insert_event(
                torrent_hash,
                source,
                "fingerprinted",
                f"size={size} sha256={digest}",
            )

    def claim_notification(self, *, timestamp=None, lease_seconds=300):
        """Claim one due message, committing before network I/O.

        An expired claim is retried after a crash. Delivery is at-least-once:
        a crash after Telegram accepts a message can cause a duplicate.
        Call only outside an existing journal transaction.
        """
        if lease_seconds <= 0:
            raise ValueError("Notification lease must be positive")
        if self.db.in_transaction:
            raise RuntimeError("Cannot claim inside a journal transaction")
        timestamp = time.time() if timestamp is None else timestamp
        token = uuid.uuid4().hex
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            cursor = self.db.execute(
                """
                SELECT event_id, event, message, attempts
                FROM notification_outbox
                WHERE delivered_at IS NULL AND available_at <= ?
                ORDER BY available_at, event_id LIMIT 1
                """,
                (timestamp,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            self.db.execute(
                """
                UPDATE notification_outbox
                SET claim_token = ?, available_at = ?, attempts = attempts + 1
                WHERE event_id = ?
                """,
                (token, timestamp + lease_seconds, row[0]),
            )
        return dict(event_id=row[0], event=row[1], message=row[2],
                    attempts=row[3] + 1, claim_token=token)

    def finish_notification(self, event_id, claim_token, *, delivered,
                            retry_at=0):
        """Acknowledge only the current claim; retain failures for later retry.

        Store a fixed error label rather than remote responses or credentials.
        """
        if self.db.in_transaction:
            raise RuntimeError("Cannot finish inside a journal transaction")
        with self.db:
            cursor = self.db.execute(
                """
                UPDATE notification_outbox
                SET delivered_at = ?, available_at = ?, claim_token = NULL,
                    last_error = ?
                WHERE event_id = ? AND claim_token = ?
                  AND delivered_at IS NULL
                """,
                (now() if delivered else None, retry_at,
                 None if delivered else "delivery_failed", event_id, claim_token),
            )
        return cursor.rowcount == 1

    def close(self):
        self.db.close()
