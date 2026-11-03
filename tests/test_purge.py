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

    result = environment["purge"].inspect(get_job(environment, source))

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

    result = environment["purge"].inspect(get_job(environment, source))

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

    result = environment["purge"].inspect(get_job(environment, source))

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

    reports = environment["purge"].scan(current_time=NOW)

    assert len(reports) == 1
    assert reports[0]["status"] == "eligible"

    # Scanning must never delete anything.
    assert target.exists()

    assert environment["journal"].status(HASH, str(source)) == "quarantined"


def test_summary(environment):
    source, _ = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    reports = environment["purge"].scan(current_time=NOW)

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

    first = environment["purge"].scan(current_time=NOW)

    second = environment["purge"].scan(current_time=NOW)

    assert first == second
    assert target.exists()


# --------------------------------------------------
# SHA-256 fingerprint protection
# --------------------------------------------------


def test_modified_file_same_size_blocked(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    original_size = target.stat().st_size

    # Different contents, identical file size.
    target.write_bytes(b"X" * original_size)

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"
    assert "fingerprint mismatch" in result["reason"].lower()
    assert target.exists()


def test_modified_file_different_size_blocked(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    target.write_bytes(b"modified media with a different size")

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"
    assert "size changed" in result["reason"].lower()


@pytest.mark.parametrize(
    "column",
    ["source_sha256", "source_size"],
)
def test_missing_fingerprint_data_blocked(environment, column):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    # Column name is from a fixed test-controlled list.
    environment["journal"].db.execute(
        f"""
        UPDATE jobs
        SET {column} = NULL
        WHERE torrent_hash = ?
          AND source = ?
        """,
        (HASH, str(source)),
    )
    environment["journal"].db.commit()

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"
    assert target.exists()


def test_invalid_fingerprint_blocked(environment):
    source, _ = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    environment["journal"].db.execute(
        """
        UPDATE jobs
        SET source_sha256 = ?
        WHERE torrent_hash = ?
          AND source = ?
        """,
        ("invalid-digest", HASH, str(source)),
    )
    environment["journal"].db.commit()

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"


def test_fingerprint_read_failure_blocked(environment, monkeypatch):
    from app import purge as purge_module

    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    def fail_fingerprint(path, *args, **kwargs):
        raise OSError("Simulated SMB read failure")

    monkeypatch.setattr(
        purge_module,
        "fingerprint",
        fail_fingerprint,
    )

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"
    assert "verification failed" in result["reason"].lower()
    assert target.exists()


def test_symlinked_quarantine_parent_blocked(environment, tmp_path):
    source, target = prepare_file(
        environment,
        name="Series/Season 3/episode.mp4",
    )

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    # Move the real directory outside the quarantine tree
    # and replace its former location with a symlink.
    season = target.parent
    moved_directory = tmp_path / "moved-season"

    season.rename(moved_directory)
    season.symlink_to(moved_directory, target_is_directory=True)

    result = environment["purge"].inspect(
        get_job(environment, source),
        current_time=NOW,
    )

    assert result["status"] == "blocked"
    assert moved_directory.joinpath("episode.mp4").exists()


def test_matching_fingerprint_eligible(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    job = get_job(environment, source)

    assert job["source_sha256"] is not None
    assert job["source_size"] == target.stat().st_size

    result = environment["purge"].inspect(
        job,
        current_time=NOW,
    )

    assert result["status"] == "eligible"
    assert "sha-256 verified" in result["reason"].lower()
    assert target.exists()


def test_fingerprint_scan_never_modifies_journal(environment):
    source, target = prepare_file(environment)

    set_deadline(
        environment,
        source,
        NOW - timedelta(days=1),
    )

    before = environment["journal"].get(HASH, str(source))

    reports = environment["purge"].scan(current_time=NOW)

    after = environment["journal"].get(HASH, str(source))

    assert reports[0]["status"] == "eligible"
    assert before == after
    assert target.exists()
