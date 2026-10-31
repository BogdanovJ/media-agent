from datetime import datetime, timedelta, timezone

import pytest

from app.journal import Journal
from app.purge import Purge
from app.quarantine import Quarantine


HASH = "a" * 40
NOW = datetime.now(timezone.utc)


@pytest.fixture
def environment(tmp_path):
    downloads = tmp_path / "downloads"
    deleted = downloads / "deleted"
    ready = downloads / "media-agent-ready"

    downloads.mkdir()
    deleted.mkdir()
    ready.mkdir()

    journal = Journal(tmp_path / "journal.sqlite3")

    quarantine = Quarantine(
        download_root=downloads,
        quarantine_root=deleted,
        journal=journal,
        retention_days=7,
    )

    purge = Purge(
        download_root=downloads,
        quarantine_root=deleted,
        journal=journal,
    )

    yield {
        "downloads": downloads,
        "deleted": deleted,
        "ready": ready,
        "journal": journal,
        "quarantine": quarantine,
        "purge": purge,
    }

    journal.close()


def prepare_file(env, name="movie.mp4"):
    source = env["downloads"] / name
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"original video data")

    output = env["ready"] / name
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"converted video data")

    env["journal"].put(
        HASH,
        str(source),
        "done",
        action="remux",
        output=str(output),
    )

    target = env["quarantine"].move(HASH, source)

    return source, target


def set_deadline(env, source, deadline):
    env["journal"].db.execute(
        """
        UPDATE jobs
        SET purge_after = ?
        WHERE torrent_hash = ?
          AND source = ?
        """,
        (deadline.isoformat(), HASH, str(source)),
    )
    env["journal"].db.commit()


def get_job(env, source):
    return env["journal"].get(HASH, str(source))


# --------------------------------------------------
# Retention period
# --------------------------------------------------


def test_newly_quarantined_file_is_retained(environment):
    source, target = prepare_file(environment)

    result = environment["purge"].inspect(
        get_job(environment, source)
    )

    assert result["status"] == "retained"
    assert target.exists()


def test_expired_file_is_eligible(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "eligible"
    assert result["size_bytes"] == target.stat().st_size


def test_future_deadline_is_retained(environment):
    source, _ = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW + timedelta(days=3),
    )

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "retained"


def test_exact_expiry_boundary(environment):
    source, _ = prepare_file(environment)

    set_deadline(environment, source, NOW)

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "eligible"


# --------------------------------------------------
# Journal validation
# --------------------------------------------------


def test_uncompleted_job_blocked(environment):
    source, _ = prepare_file(environment)

    environment["journal"].db.execute(
        """
        UPDATE jobs
        SET status = 'failed'
        WHERE torrent_hash = ?
          AND source = ?
        """,
        (HASH, str(source)),
    )
    environment["journal"].db.commit()

    result = environment["purge"].inspect(
        get_job(environment, source)
    )

    assert result["status"] == "blocked"


def test_missing_deadline_blocked(environment):
    source, _ = prepare_file(environment)

    environment["journal"].db.execute(
        """
        UPDATE jobs
        SET purge_after = NULL
        WHERE torrent_hash = ?
          AND source = ?
        """,
        (HASH, str(source)),
    )
    environment["journal"].db.commit()

    result = environment["purge"].inspect(
        get_job(environment, source)
    )

    assert result["status"] == "blocked"


def test_invalid_torrent_hash_blocked(environment):
    source, _ = prepare_file(environment)

    job = get_job(environment, source)
    job["torrent_hash"] = "../unsafe"

    result = environment["purge"].inspect(job)

    assert result["status"] == "blocked"


def test_missing_completion_event_blocked(environment):
    source, _ = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    environment["journal"].db.execute(
        """
        DELETE FROM events
        WHERE torrent_hash = ?
          AND source = ?
          AND event = 'quarantined'
        """,
        (HASH, str(source)),
    )
    environment["journal"].db.commit()

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"


# --------------------------------------------------
# Filesystem safety
# --------------------------------------------------


def test_missing_quarantine_file_blocked(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    target.unlink()

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"


def test_reappeared_original_blocks_purge(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    source.write_bytes(b"new original")

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"
    assert target.exists()


def test_symlink_target_blocked(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    target.unlink()

    outside = environment["downloads"].parent / "outside.mp4"
    outside.write_bytes(b"unrelated file")

    target.symlink_to(outside)

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"
    assert outside.exists()


# --------------------------------------------------
# Dry-run behaviour
# --------------------------------------------------


def test_scan_does_not_delete_files(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    reports = environment["purge"].scan(
        current_time=NOW
    )

    assert len(reports) == 1
    assert reports[0]["status"] == "eligible"

    # Scanning must never delete anything.
    assert target.exists()

    assert environment["journal"].status(
        HASH, str(source)
    ) == "quarantined"


def test_summary(environment):
    source, _ = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    reports = environment["purge"].scan(
        current_time=NOW
    )

    summary = environment["purge"].summary(reports)

    assert summary["total"] == 1
    assert summary["eligible"] == 1
    assert summary["retained"] == 0
    assert summary["blocked"] == 0
    assert summary["eligible_bytes"] > 0


def test_repeated_scans_are_safe(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    first = environment["purge"].scan(
        current_time=NOW
    )

    second = environment["purge"].scan(
        current_time=NOW
    )

    assert first == second
    assert target.exists()
