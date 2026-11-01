import sqlite3

import pytest

from app.journal import Journal


@pytest.fixture
def journal(tmp_path):
    db_path = tmp_path / "journal.sqlite3"
    instance = Journal(db_path)

    yield instance

    instance.close()


SOURCE = "/mnt/downloads/movie.mp4"
OUTPUT = "/mnt/downloads/media-agent-ready/movie.mp4"
HASH = "test-torrent-hash"


def test_database_created(tmp_path):
    db_path = tmp_path / "state" / "journal.sqlite3"

    journal = Journal(db_path)

    assert db_path.exists()
    assert journal.get(HASH, SOURCE) is None

    journal.close()


def test_new_job(journal):
    journal.put(
        HASH,
        SOURCE,
        "planned",
        action="remux",
        reason="Audio conversion required",
    )

    job = journal.get(HASH, SOURCE)

    assert job["status"] == "planned"
    assert job["action"] == "remux"
    assert job["attempts"] == 0
    assert job["created_at"] is not None


def test_completed_job(journal):
    journal.put(HASH, SOURCE, "running", action="remux")

    journal.put(
        HASH,
        SOURCE,
        "done",
        action="remux",
        output=OUTPUT,
    )

    job = journal.get(HASH, SOURCE)

    assert job["status"] == "done"
    assert job["output"] == OUTPUT
    assert job["completed_at"] is not None


def test_attempt_count(journal):
    journal.put(HASH, SOURCE, "planned", action="transcode")

    journal.put(HASH, SOURCE, "running", action="transcode")
    journal.put(HASH, SOURCE, "failed", reason="FFmpeg error")

    journal.put(HASH, SOURCE, "running", action="transcode")

    assert journal.get(HASH, SOURCE)["attempts"] == 2


def test_failed_job_records_error(journal):
    journal.put(
        HASH,
        SOURCE,
        "failed",
        reason="Output validation failed",
    )

    job = journal.get(HASH, SOURCE)

    assert job["status"] == "failed"
    assert job["last_error"] == "Output validation failed"


def test_metadata_roundtrip(journal):
    metadata = {
        "video_codec": "h264",
        "audio_codec": "aac",
        "width": 1920,
        "height": 1080,
    }

    journal.put(
        HASH,
        SOURCE,
        "planned",
        metadata=metadata,
    )

    assert journal.get(HASH, SOURCE)["metadata"] == metadata


def test_status_lookup(journal):
    assert journal.status(HASH, SOURCE) is None

    journal.put(HASH, SOURCE, "review")

    assert journal.status(HASH, SOURCE) == "review"


def test_jobs_for_torrent(journal):
    journal.put(HASH, "/mnt/downloads/a.mp4", "done")
    journal.put(HASH, "/mnt/downloads/b.mp4", "planned")

    journal.put(
        "another-torrent",
        "/mnt/downloads/c.mp4",
        "done",
    )

    jobs = journal.jobs_for_torrent(HASH)

    assert len(jobs) == 2
    assert {job["status"] for job in jobs} == {
        "done",
        "planned",
    }


def test_invalid_status_rejected(journal):
    with pytest.raises(ValueError, match="Invalid job status"):
        journal.put(HASH, SOURCE, "unknown_status")


def test_journal_survives_restart(tmp_path):
    db_path = tmp_path / "journal.sqlite3"

    first = Journal(db_path)
    first.put(HASH, SOURCE, "done", output=OUTPUT)
    first.close()

    second = Journal(db_path)

    job = second.get(HASH, SOURCE)

    assert job["status"] == "done"
    assert job["output"] == OUTPUT

    second.close()


@pytest.mark.parametrize("status", ["running", "validating"])
def test_interrupted_job_recovery(tmp_path, status):
    db_path = tmp_path / "journal.sqlite3"

    first = Journal(db_path)
    first.put(HASH, SOURCE, status)
    first.close()

    second = Journal(db_path)

    job = second.get(HASH, SOURCE)

    assert job["status"] == "interrupted"
    assert job["last_error"] == f"Interrupted during {status}"

    second.close()


def test_completed_job_not_interrupted(tmp_path):
    db_path = tmp_path / "journal.sqlite3"

    first = Journal(db_path)
    first.put(HASH, SOURCE, "done", output=OUTPUT)
    first.close()

    second = Journal(db_path)

    assert second.status(HASH, SOURCE) == "done"

    second.close()


def test_events_are_recorded(journal):
    journal.put(HASH, SOURCE, "planned")
    journal.put(HASH, SOURCE, "running")
    journal.put(HASH, SOURCE, "done")

    events = journal.db.execute(
        """
        SELECT event
        FROM events
        WHERE torrent_hash = ?
        ORDER BY id
        """,
        (HASH,),
    ).fetchall()

    assert [row[0] for row in events] == [
        "planned",
        "running",
        "done",
    ]


def test_legacy_database_migration(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"

    # Simulate the original v0.1 database.
    connection = sqlite3.connect(db_path)

    connection.execute("""
        CREATE TABLE jobs (
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

    connection.execute(
        """
        INSERT INTO jobs
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            HASH,
            SOURCE,
            "done",
            "remux",
            "Converted successfully",
            OUTPUT,
            "2026-10-31T12:00:00+00:00",
        ),
    )

    connection.commit()
    connection.close()

    # Opening the legacy database should migrate it.
    journal = Journal(db_path)

    job = journal.get(HASH, SOURCE)

    assert job["status"] == "done"
    assert job["output"] == OUTPUT
    assert job["attempts"] == 0
    assert "quarantined_at" in job
    assert "purge_after" in job

    journal.close()


# --------------------------------------------------
# Quarantined file fingerprint tests
# --------------------------------------------------

VALID_SHA256 = "a" * 64
OTHER_SHA256 = "b" * 64


def test_record_fingerprint(journal):
    journal.put(HASH, SOURCE, "quarantined")

    journal.record_fingerprint(
        HASH,
        SOURCE,
        size=123456789,
        sha256=VALID_SHA256,
    )

    job = journal.get(HASH, SOURCE)

    assert job["source_size"] == 123456789
    assert job["source_sha256"] == VALID_SHA256
    assert job["fingerprinted_at"] is not None


def test_fingerprint_survives_restart(tmp_path):
    db_path = tmp_path / "journal.sqlite3"

    first = Journal(db_path)

    first.put(HASH, SOURCE, "quarantined")
    first.record_fingerprint(
        HASH,
        SOURCE,
        size=1024,
        sha256=VALID_SHA256,
    )

    first.close()

    second = Journal(db_path)
    job = second.get(HASH, SOURCE)

    assert job["source_size"] == 1024
    assert job["source_sha256"] == VALID_SHA256

    second.close()


def test_duplicate_fingerprint_is_idempotent(journal):
    journal.put(HASH, SOURCE, "quarantined")

    journal.record_fingerprint(HASH, SOURCE, 1024, VALID_SHA256)

    first_timestamp = journal.get(HASH, SOURCE)["fingerprinted_at"]

    journal.record_fingerprint(HASH, SOURCE, 1024, VALID_SHA256)

    job = journal.get(HASH, SOURCE)

    assert job["source_sha256"] == VALID_SHA256
    assert job["fingerprinted_at"] == first_timestamp


def test_fingerprint_cannot_be_replaced(journal):
    journal.put(HASH, SOURCE, "quarantined")

    journal.record_fingerprint(HASH, SOURCE, 1024, VALID_SHA256)

    with pytest.raises(
        ValueError,
        match="Existing fingerprint does not match",
    ):
        journal.record_fingerprint(HASH, SOURCE, 1024, OTHER_SHA256)

    assert journal.get(HASH, SOURCE)["source_sha256"] == VALID_SHA256


def test_fingerprint_size_cannot_change(journal):
    journal.put(HASH, SOURCE, "quarantined")

    journal.record_fingerprint(HASH, SOURCE, 1024, VALID_SHA256)

    with pytest.raises(
        ValueError,
        match="Existing fingerprint does not match",
    ):
        journal.record_fingerprint(HASH, SOURCE, 2048, VALID_SHA256)


@pytest.mark.parametrize(
    "invalid_digest",
    [
        "",
        "abc",
        "z" * 64,
        "a" * 63,
        "a" * 65,
    ],
)
def test_invalid_fingerprint_rejected(journal, invalid_digest):
    journal.put(HASH, SOURCE, "quarantined")

    with pytest.raises(
        ValueError,
        match="Invalid SHA-256 digest",
    ):
        journal.record_fingerprint(HASH, SOURCE, 1024, invalid_digest)


def test_negative_fingerprint_size_rejected(journal):
    journal.put(HASH, SOURCE, "quarantined")

    with pytest.raises(ValueError, match="Invalid file size"):
        journal.record_fingerprint(HASH, SOURCE, -1, VALID_SHA256)


def test_unknown_job_cannot_be_fingerprinted(journal):
    with pytest.raises(
        ValueError,
        match="Unknown journal entry",
    ):
        journal.record_fingerprint(HASH, SOURCE, 1024, VALID_SHA256)


@pytest.mark.parametrize(
    "status",
    ["planned", "running", "failed", "done", "review"],
)
def test_only_quarantined_jobs_accept_fingerprints(journal, status):
    journal.put(HASH, SOURCE, status)

    with pytest.raises(
        ValueError,
        match="File must be quarantined",
    ):
        journal.record_fingerprint(HASH, SOURCE, 1024, VALID_SHA256)


def test_fingerprint_event_recorded(journal):
    journal.put(HASH, SOURCE, "quarantined")

    journal.record_fingerprint(HASH, SOURCE, 1024, VALID_SHA256)

    events = journal.db.execute(
        """
        SELECT event, detail
        FROM events
        WHERE torrent_hash = ?
          AND source = ?
          AND event = 'fingerprinted'
        """,
        (HASH, SOURCE),
    ).fetchall()

    assert len(events) == 1
    assert events[0][0] == "fingerprinted"
    assert VALID_SHA256 in events[0][1]


def test_fingerprint_normalizes_uppercase(journal):
    journal.put(HASH, SOURCE, "quarantined")

    journal.record_fingerprint(
        HASH,
        SOURCE,
        size=1024,
        sha256="ABCDEF" * 10 + "ABCD",
    )

    job = journal.get(HASH, SOURCE)

    assert job["source_sha256"] == ("abcdef" * 10 + "abcd")
