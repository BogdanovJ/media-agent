import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.journal import Journal
from app.quarantine import Quarantine, is_within

TORRENT_HASH = "a" * 40
OTHER_HASH = "b" * 40


@pytest.fixture
def environment(tmp_path):
    downloads = tmp_path / "downloads"
    deleted = downloads / "deleted"
    ready = downloads / "media-agent-ready"

    downloads.mkdir()
    deleted.mkdir()
    ready.mkdir()

    journal = Journal(tmp_path / "state" / "journal.sqlite3")

    quarantine = Quarantine(
        download_root=downloads,
        quarantine_root=deleted,
        journal=journal,
        retention_days=7,
    )

    yield {
        "downloads": downloads,
        "deleted": deleted,
        "ready": ready,
        "journal": journal,
        "quarantine": quarantine,
    }

    journal.close()


def create_source(env, filename="movie.mp4", content=b"test video"):
    source = env["downloads"] / filename
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(content)
    return source


def mark_completed(env, source):
    output = env["ready"] / source.relative_to(env["downloads"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(b"verified output placeholder")

    env["journal"].put(
        TORRENT_HASH,
        str(source),
        "done",
        action="remux",
        reason="Conversion verified",
        output=str(output),
    )

    return output


# --------------------------------------------------
# Configuration and path safety
# --------------------------------------------------


def test_quarantine_must_be_inside_download_root(tmp_path):
    downloads = tmp_path / "downloads"
    downloads.mkdir()

    journal = Journal(tmp_path / "journal.sqlite3")

    try:
        with pytest.raises(ValueError, match="inside download root"):
            Quarantine(
                download_root=downloads,
                quarantine_root=tmp_path / "outside",
                journal=journal,
            )
    finally:
        journal.close()


@pytest.mark.parametrize("days", [0, -1, -7])
def test_invalid_retention_days(environment, days):
    with pytest.raises(ValueError, match="at least one day"):
        Quarantine(
            environment["downloads"],
            environment["deleted"],
            environment["journal"],
            retention_days=days,
        )


@pytest.mark.parametrize(
    "invalid_hash",
    [
        "",
        "abc",
        "../escape",
        "g" * 40,
        "a" * 39,
        "a" * 41,
    ],
)
def test_invalid_torrent_hash_rejected(environment, invalid_hash):
    source = create_source(environment)

    with pytest.raises(ValueError, match="Invalid torrent hash"):
        environment["quarantine"].destination(
            invalid_hash,
            source,
        )


def test_source_outside_downloads_rejected(environment, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")

    with pytest.raises(ValueError, match="outside download root"):
        environment["quarantine"].destination(
            TORRENT_HASH,
            outside,
        )


def test_source_already_in_quarantine_rejected(environment):
    source = environment["deleted"] / "existing.mp4"
    source.write_bytes(b"video")

    with pytest.raises(ValueError, match="already inside quarantine"):
        environment["quarantine"].destination(
            TORRENT_HASH,
            source,
        )


def test_symlink_escaping_download_root_rejected(environment, tmp_path):
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(b"video")

    link = environment["downloads"] / "linked.mp4"
    link.symlink_to(outside)

    with pytest.raises(ValueError, match="outside download root"):
        environment["quarantine"].destination(
            TORRENT_HASH,
            link,
        )


def test_is_within_rejects_root_itself(environment):
    root = environment["downloads"]

    assert not is_within(root, root)


def test_is_within_rejects_similarly_named_directory(environment, tmp_path):
    outside = tmp_path / "downloads-backup" / "movie.mp4"

    assert not is_within(outside, environment["downloads"])


# --------------------------------------------------
# Destination construction
# --------------------------------------------------


def test_destination_preserves_relative_path(environment):
    source = create_source(
        environment,
        "Movies/Example/movie.mp4",
    )

    target = environment["quarantine"].destination(
        TORRENT_HASH,
        source,
    )

    assert target == (
        environment["deleted"] / TORRENT_HASH / "Movies/Example/movie.mp4"
    )


def test_destination_does_not_modify_filesystem(environment):
    source = create_source(environment)

    target = environment["quarantine"].destination(
        TORRENT_HASH,
        source,
    )

    assert source.exists()
    assert not target.exists()


def test_hash_normalized_to_lowercase(environment):
    source = create_source(environment)

    target = environment["quarantine"].destination(
        TORRENT_HASH.upper(),
        source,
    )

    assert target.parts[-2] == TORRENT_HASH


# --------------------------------------------------
# File movement
# --------------------------------------------------


def test_successful_quarantine_move(environment):
    original_data = b"original media content"

    source = create_source(
        environment,
        "Movies/movie.mp4",
        original_data,
    )

    mark_completed(environment, source)

    target = environment["quarantine"].move(
        TORRENT_HASH,
        source,
    )

    assert not source.exists()
    assert target.exists()
    assert target.read_bytes() == original_data

    job = environment["journal"].get(
        TORRENT_HASH,
        str(source),
    )

    assert job["status"] == "quarantined"
    assert job["quarantined_at"] is not None
    assert job["purge_after"] is not None


def test_quarantine_preserves_nested_directories(environment):
    source = create_source(
        environment,
        "Series/Season 3/Episode 01.mp4",
    )

    mark_completed(environment, source)

    target = environment["quarantine"].move(
        TORRENT_HASH,
        source,
    )

    assert target == (
        environment["deleted"] / TORRENT_HASH / "Series/Season 3/Episode 01.mp4"
    )


def test_duplicate_destination_not_overwritten(environment):
    source = create_source(environment, content=b"original")

    mark_completed(environment, source)

    target = environment["quarantine"].destination(
        TORRENT_HASH,
        source,
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"existing file")

    with pytest.raises(FileExistsError):
        environment["quarantine"].move(
            TORRENT_HASH,
            source,
        )

    assert source.read_bytes() == b"original"
    assert target.read_bytes() == b"existing file"


def test_missing_source_rejected(environment):
    missing = environment["downloads"] / "missing.mp4"

    with pytest.raises(FileNotFoundError):
        environment["quarantine"].move(
            TORRENT_HASH,
            missing,
        )


def test_retention_deadline_is_seven_days(environment):
    source = create_source(environment)
    mark_completed(environment, source)

    environment["quarantine"].move(
        TORRENT_HASH,
        source,
    )

    job = environment["journal"].get(
        TORRENT_HASH,
        str(source),
    )

    moved_at = datetime.fromisoformat(job["quarantined_at"])
    purge_after = datetime.fromisoformat(job["purge_after"])

    assert purge_after - moved_at == timedelta(days=7)


def test_quarantine_event_recorded(environment):
    source = create_source(environment)
    mark_completed(environment, source)

    environment["quarantine"].move(
        TORRENT_HASH,
        source,
    )

    events = (
        environment["journal"]
        .db.execute(
            """
        SELECT event
        FROM events
        WHERE torrent_hash = ?
          AND source = ?
        ORDER BY id
        """,
            (TORRENT_HASH, str(source)),
        )
        .fetchall()
    )

    assert "quarantined" in [event[0] for event in events]


# --------------------------------------------------
# Retention queries — no physical deletion
# --------------------------------------------------


def test_newly_quarantined_file_not_expired(environment):
    source = create_source(environment)
    mark_completed(environment, source)

    environment["quarantine"].move(
        TORRENT_HASH,
        source,
    )

    assert environment["quarantine"].expired() == []


def test_expired_file_is_returned(environment):
    source = create_source(environment)
    mark_completed(environment, source)

    target = environment["quarantine"].move(
        TORRENT_HASH,
        source,
    )

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()

    environment["journal"].db.execute(
        """
        UPDATE jobs
        SET purge_after = ?
        WHERE torrent_hash = ?
          AND source = ?
        """,
        (yesterday, TORRENT_HASH, str(source)),
    )
    environment["journal"].db.commit()

    expired = environment["quarantine"].expired()

    assert len(expired) == 1
    assert expired[0][0] == TORRENT_HASH
    assert expired[0][1] == str(source)

    # expired() must never physically delete anything.
    assert target.exists()


# --------------------------------------------------
# Cross-filesystem protection
# --------------------------------------------------


def test_cross_filesystem_move_rejected(environment, monkeypatch):
    source = create_source(environment)
    mark_completed(environment, source)

    # Patch only the os.stat reference used by quarantine.
    # Preserve the original implementation before patching.
    real_stat = os.stat

    def fake_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)

        if os.fspath(path) == os.fspath(source):
            values = list(result)
            values[2] = 100  # st_dev
            return os.stat_result(values)

        return result

    with monkeypatch.context() as patch:
        patch.setattr(os, "stat", fake_stat)

        with pytest.raises(
            OSError,
            match="share a filesystem",
        ):
            environment["quarantine"].move(
                TORRENT_HASH,
                source,
            )

    assert source.exists()


# --------------------------------------------------
# Known safety gaps — expected to fail until fixed
# --------------------------------------------------


def test_cannot_quarantine_uncompleted_job(environment):
    source = create_source(environment)

    environment["journal"].put(
        TORRENT_HASH,
        str(source),
        "failed",
        reason="Conversion failed",
    )

    with pytest.raises(ValueError):
        environment["quarantine"].move(
            TORRENT_HASH,
            source,
        )

    assert source.exists()


def test_cannot_quarantine_when_output_missing(environment):
    source = create_source(environment)
    output = mark_completed(environment, source)

    output.unlink()

    with pytest.raises(ValueError):
        environment["quarantine"].move(
            TORRENT_HASH,
            source,
        )

    assert source.exists()
