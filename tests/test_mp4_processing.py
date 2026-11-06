from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

from app import main, publisher, worker
from app.cleanup import cleanup_expired
from app.journal import Journal
from app.paths import final_path, legacy_path, work_dir


@pytest.fixture
def setup(tmp_path, monkeypatch):
    src = tmp_path / "Эпизод.Mp4"
    src.write_bytes(b"original")
    db = Journal(tmp_path / "journal")
    h = "a" * 40
    cfg = dict(download_root=str(tmp_path), quarantine_root=str(tmp_path / "deleted"),
               process_enabled=True, finalize_enabled=True, retirement_allow_hashes=[],
               review_over_bytes=10000, min_free_bytes=0, ffmpeg_threads=1)
    q = Mock()
    q.files.return_value = [dict(name=src.name), dict(name="._metadata.mp4")]
    t = dict(hash=h, save_path=str(tmp_path), state="stalledUP", progress=1, amount_left=0)
    q.lookup.side_effect = [t, None]
    monkeypatch.setattr(main, "probe", lambda path: {})
    monkeypatch.setattr(worker, "probe", lambda path: {})
    monkeypatch.setattr(worker, "validate", Mock())
    monkeypatch.setattr(main, "decision", lambda *args: ("remux", "audio"))
    convert = Mock(side_effect=lambda source, dst, action, threads: dst.write_bytes(b"converted"))
    monkeypatch.setattr(worker, "convert", convert)
    yield src, db, cfg, q, t, convert
    db.close()


@pytest.mark.parametrize("suffix", [".mp4", ".MP4", ".Mp4", ".mkv"])
def test_final_names(suffix):
    src = Path("Movie" + suffix)
    assert final_path(src) == (src if suffix.lower() == ".mp4" else Path("Movie.mp4"))


@pytest.mark.parametrize("legacy", [False, True])
def test_staging_retirement_and_cleanup(setup, legacy):
    src, db, cfg, q, t, convert = setup
    h = t["hash"]
    if legacy:
        legacy_path(src).write_bytes(b"converted")
        db.put(h, str(src), "done", "remux", output=str(legacy_path(src)))
    else:
        db.put(h, str(src), "done", "remux", output=str(src))
    main.process_torrent(q, t, cfg, db)
    main.process_torrent(q, t, cfg, db)
    output = Path(db.get(h, str(src))["output"])
    assert db.status(h, str(src)) == "validated"
    assert output == (legacy_path(src) if legacy else work_dir(src.parent, h, src) / "output.mp4")
    assert src.read_bytes() == b"original"
    assert convert.call_count == (0 if legacy else 1)
    q.remove_record_only.assert_not_called()
    cfg["retirement_allow_hashes"] = [h]
    main.process_torrent(q, t, cfg, db)
    assert src.read_bytes() == b"converted"
    assert not output.exists()
    job = db.get(h, str(src))
    assert datetime.fromisoformat(job["purge_after"]) - datetime.fromisoformat(job["quarantined_at"]) == timedelta(days=7)
    cfg["cleanup_enabled"] = True
    assert cleanup_expired(cfg, db) == 0
    with db.db:
        db.db.execute("UPDATE jobs SET purge_after=?", ((datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),))
    assert cleanup_expired(cfg, db) == 1
    assert src.read_bytes() == b"converted"
    q.remove_record_only.assert_called_once_with(h, confirmed=True)


@pytest.mark.parametrize("crash_move", [1, 2])
def test_recovery_after_filesystem_operation(setup, monkeypatch, crash_move):
    src, db, cfg, q, t, convert = setup
    cfg["retirement_allow_hashes"] = [t["hash"]]
    move = publisher.move_no_replace
    calls = []
    def crash(source, target):
        move(source, target)
        calls.append(target)
        if len(calls) == crash_move:
            raise EOFError("simulated process exit before commit")
    monkeypatch.setattr(publisher, "move_no_replace", crash)
    with pytest.raises(EOFError):
        main.process_torrent(q, t, cfg, db)
    q.lookup.side_effect = [None]
    restarted = Journal(db.path)
    main.recover(q, cfg, restarted)
    main.recover(q, cfg, restarted)
    assert restarted.db.execute("SELECT state FROM retirements").fetchone() == ("completed",)
    assert src.read_bytes() == b"converted"
    assert q.remove_record_only.call_count == convert.call_count == 1
    restarted.close()


def test_ambiguous_removal_does_not_archive(setup):
    src, db, cfg, q, t, convert = setup
    cfg["retirement_allow_hashes"] = [t["hash"]]
    q.lookup.side_effect = [t, ConnectionError(), None]
    with pytest.raises(ConnectionError):
        main.process_torrent(q, t, cfg, db)
    assert src.read_bytes() == b"original"
    main.recover(q, cfg, db)
    assert src.read_bytes() == b"converted"
    assert q.remove_record_only.call_count == 1


def test_unattributed_collision_and_invalid_adoption(setup):
    src, db, cfg, q, t, convert = setup
    out = legacy_path(src)
    out.write_bytes(b"other")
    main.process_torrent(q, t, cfg, db)
    assert db.status(t["hash"], str(src)) == "review"
    db.put(t["hash"], str(src), "done", "remux", output=str(out))
    worker.validate.side_effect = ValueError("invalid")
    main.process_torrent(q, t, cfg, db)
    assert db.status(t["hash"], str(src)) == "failed"
    assert src.read_bytes() == b"original" and out.read_bytes() == b"other"
    convert.assert_not_called()


def test_compatible_file_is_validated(setup, monkeypatch):
    src, db, cfg, q, t, convert = setup
    monkeypatch.setattr(main, "decision", lambda *args: ("copy", "compatible"))
    cfg["retirement_allow_hashes"] = [t["hash"]]
    main.process_torrent(q, t, cfg, db)
    worker.validate.assert_called_once()
    convert.assert_not_called()
    assert src.read_bytes() == b"original"
    assert db.status(t["hash"], str(src)) == "done"
