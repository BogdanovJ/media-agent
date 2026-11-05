from pathlib import Path
from unittest.mock import Mock

import pytest

from app import main
from app.journal import Journal


@pytest.fixture
def setup(tmp_path, monkeypatch):
    source = tmp_path / 'Эпизод.mp4'
    source.write_bytes(b'original')
    journal = Journal(tmp_path / 'journal.sqlite3')
    cfg = dict(download_root=str(tmp_path), quarantine_root=str(tmp_path / 'deleted'),
               process_enabled=True, finalize_enabled=False, review_over_bytes=10000,
               min_free_bytes=0, ffmpeg_threads=1)
    qbit = Mock()
    qbit.files.return_value = [dict(name=source.name, priority=1)]
    torrent = dict(hash='a' * 40, save_path=str(tmp_path))
    monkeypatch.setattr(main, 'probe', lambda path: {})
    monkeypatch.setattr(main, 'decision', lambda *args: ('remux', 'Audio needs conversion'))
    convert = Mock(side_effect=lambda src, dst, action, threads: dst.write_bytes(b'converted'))
    monkeypatch.setattr(main, 'convert', convert)
    yield source, journal, cfg, qbit, torrent, convert
    journal.close()


@pytest.mark.parametrize('suffix', ['.mp4', '.MP4', '.Mp4'])
@pytest.mark.parametrize('action', ['remux', 'transcode'])
def test_mp4_output_is_separate(suffix, action):
    source = Path('movie' + suffix)
    assert main.destination_for(source, action) == Path('movie.media-agent.mp4')


def test_other_container_and_compatible_output_names():
    assert main.destination_for('movie.mkv', 'remux') == Path('movie.mp4')
    assert main.destination_for('movie.mp4', 'copy') == Path('movie.mp4')


def test_mp4_requires_conversion_before_done(setup):
    source, j, cfg, q, torrent, convert = setup
    main.process_torrent(q, torrent, cfg, j)
    output = source.with_name(source.stem + '.media-agent.mp4')
    convert.assert_called_once_with(source, output, 'remux', 1)
    job = j.get(torrent['hash'], str(source))
    assert job['status'] == 'done' and job['output'] == str(output)
    assert source.read_bytes() == b'original'
    assert output.read_bytes() == b'converted'
    message = j.db.execute("SELECT message FROM notification_outbox WHERE event='done'").fetchone()[0]
    assert 'completed and validated' in message and str(output) in message


@pytest.mark.parametrize('action', ['remux', 'transcode'])
def test_historical_false_done_is_repaired_once(setup, action):
    source, j, cfg, q, torrent, convert = setup
    j.put(torrent['hash'], str(source), 'done', action=action, output=str(source))
    main.process_torrent(q, torrent, cfg, j)
    main.process_torrent(q, torrent, cfg, j)
    assert convert.call_count == 1
    job = j.get(torrent['hash'], str(source))
    assert job['status'] == 'done' and job['output'] != str(source)
    assert j.db.execute("SELECT COUNT(*) FROM events WHERE event='interrupted'").fetchone()[0] == 1
    assert source.read_bytes() == b'original'


@pytest.mark.parametrize('action, same_output', [('copy', True), ('remux', False), ('transcode', False)])
def test_valid_completions_not_retried(setup, action, same_output):
    source, j, cfg, q, torrent, convert = setup
    output = source if same_output else source.with_name('valid.mp4')
    j.put(torrent['hash'], str(source), 'done', action=action, output=str(output))
    main.process_torrent(q, torrent, cfg, j)
    convert.assert_not_called()


def test_failed_conversion_is_not_reported_done(setup):
    source, j, cfg, q, torrent, convert = setup
    convert.side_effect = RuntimeError('test failure')
    main.process_torrent(q, torrent, cfg, j)
    assert j.status(torrent['hash'], str(source)) == 'failed'
    assert j.db.execute("SELECT COUNT(*) FROM notification_outbox WHERE event='done'").fetchone()[0] == 0


def test_existing_output_is_not_overwritten(setup):
    source, j, cfg, q, torrent, convert = setup
    output = main.destination_for(source, 'remux')
    output.write_bytes(b'keep-existing')
    main.process_torrent(q, torrent, cfg, j)
    assert j.status(torrent['hash'], str(source)) == 'review'
    assert output.read_bytes() == b'keep-existing'
    convert.assert_not_called()


def test_repair_obeys_disabled_processing(setup):
    source, j, cfg, q, torrent, convert = setup
    j.put(torrent['hash'], str(source), 'done', action='remux', output=str(source))
    cfg['process_enabled'] = False
    main.process_torrent(q, torrent, cfg, j)
    assert j.status(torrent['hash'], str(source)) == 'planned'
    convert.assert_not_called()


def test_compatible_file_message_is_explicit(setup, monkeypatch):
    source, j, cfg, q, torrent, convert = setup
    monkeypatch.setattr(main, 'decision', lambda *args: ('copy', 'compatible'))
    main.process_torrent(q, torrent, cfg, j)
    convert.assert_not_called()
    message = j.db.execute("SELECT message FROM notification_outbox WHERE event='done'").fetchone()[0]
    assert 'no conversion needed' in message
