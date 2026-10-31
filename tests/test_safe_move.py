import errno
import os
from pathlib import Path

import pytest

from app.safe_move import (
    UnsupportedMoveError,
    move_no_replace,
)


def test_successful_move(tmp_path):
    source = tmp_path / "original.mp4"
    destination = tmp_path / "deleted.mp4"

    source.write_bytes(b"original video")

    result = move_no_replace(source, destination)

    assert result == destination
    assert not source.exists()
    assert destination.read_bytes() == b"original video"


def test_existing_destination_not_overwritten(tmp_path):
    source = tmp_path / "original.mp4"
    destination = tmp_path / "deleted.mp4"

    source.write_bytes(b"original video")
    destination.write_bytes(b"existing video")

    with pytest.raises(FileExistsError):
        move_no_replace(source, destination)

    assert source.read_bytes() == b"original video"
    assert destination.read_bytes() == b"existing video"


def test_missing_source_rejected(tmp_path):
    source = tmp_path / "missing.mp4"
    destination = tmp_path / "deleted.mp4"

    with pytest.raises(ValueError, match="regular file"):
        move_no_replace(source, destination)


def test_directory_source_rejected(tmp_path):
    source = tmp_path / "directory"
    destination = tmp_path / "deleted"

    source.mkdir()

    with pytest.raises(ValueError, match="regular file"):
        move_no_replace(source, destination)


def test_symlink_source_rejected(tmp_path):
    actual = tmp_path / "actual.mp4"
    source = tmp_path / "linked.mp4"
    destination = tmp_path / "deleted.mp4"

    actual.write_bytes(b"video")
    source.symlink_to(actual)

    with pytest.raises(ValueError, match="regular file"):
        move_no_replace(source, destination)

    assert actual.exists()
    assert source.is_symlink()


def test_destination_symlink_not_overwritten(tmp_path):
    source = tmp_path / "original.mp4"
    actual = tmp_path / "actual.mp4"
    destination = tmp_path / "deleted.mp4"

    source.write_bytes(b"original")
    actual.write_bytes(b"existing")
    destination.symlink_to(actual)

    with pytest.raises(FileExistsError):
        move_no_replace(source, destination)

    assert source.exists()
    assert destination.is_symlink()
    assert actual.read_bytes() == b"existing"


def test_nested_destination(tmp_path):
    source = tmp_path / "original.mp4"
    destination = tmp_path / "deleted" / "movie.mp4"

    source.write_bytes(b"video")
    destination.parent.mkdir(parents=True)

    move_no_replace(source, destination)

    assert destination.read_bytes() == b"video"
    assert not source.exists()


def test_no_overwrite_when_called_twice(tmp_path):
    source = tmp_path / "original.mp4"
    destination = tmp_path / "deleted.mp4"

    source.write_bytes(b"first")

    move_no_replace(source, destination)

    source.write_bytes(b"second")

    with pytest.raises(FileExistsError):
        move_no_replace(source, destination)

    assert source.read_bytes() == b"second"
    assert destination.read_bytes() == b"first"


def test_cross_filesystem_rejected(tmp_path, monkeypatch):
    source = tmp_path / "original.mp4"
    destination = tmp_path / "deleted" / "movie.mp4"

    source.write_bytes(b"video")
    destination.parent.mkdir()

    real_stat = Path.stat

    def fake_stat(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)

        if self == source:
            values = list(result)
            values[2] = result.st_dev + 1
            return os.stat_result(values)

        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "stat", fake_stat)

        with pytest.raises(
            UnsupportedMoveError,
            match="share a filesystem",
        ):
            move_no_replace(source, destination)

    assert source.exists()
    assert not destination.exists()


def test_file_content_preserved(tmp_path):
    source = tmp_path / "original.mp4"
    destination = tmp_path / "deleted.mp4"

    payload = os.urandom(1024 * 1024)
    source.write_bytes(payload)

    move_no_replace(source, destination)

    assert destination.read_bytes() == payload


def test_unsupported_filesystem_error_is_oserror():
    assert issubclass(UnsupportedMoveError, OSError)
