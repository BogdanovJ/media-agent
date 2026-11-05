import json
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import Mock

import pytest

from app.cleanup import cleanup_expired
from app.journal import Journal
from app.metrics import MetricsState, read_metrics, start_metrics
from app.notifier import NotificationDispatcher, TelegramSettings
from app.quarantine import Quarantine


@pytest.fixture
def journal(tmp_path):
    j = Journal(tmp_path / 'state.sqlite3')
    yield j
    j.close()


def test_metrics_do_not_recover_running_jobs(journal):
    journal.put('hash', '/private/path', 'running')
    journal.event('hash', '/private/path', 'review', 'PRIVATE DETAIL')
    text = read_metrics(journal.path, {'process_enabled': True}, MetricsState())
    assert 'media_agent_jobs{status="running"} 1' in text
    assert 'media_agent_notifications_pending 1' in text
    assert 'media_agent_process_enabled 1' in text
    assert 'PRIVATE' not in text and '/private' not in text and 'hash' not in text
    assert journal.status('hash', '/private/path') == 'running'


def test_metrics_retry_count_does_not_claim_confirmed_duplicates(journal):
    journal.event('hash', '/movie', 'done')
    journal.claim_notification(timestamp=0, lease_seconds=1)
    item = journal.claim_notification(timestamp=2)
    journal.finish_notification(item['event_id'], item['claim_token'], delivered=True)
    text = read_metrics(journal.path, {}, MetricsState())
    assert 'media_agent_notifications_retried 1' in text
    assert 'media_agent_notifications_delivered 1' in text
    assert 'media_agent_notifications_pending 0' in text


def test_metrics_missing_database_not_created(tmp_path):
    path = tmp_path / 'absent.sqlite3'
    with pytest.raises(sqlite3.OperationalError):
        read_metrics(path, {}, MetricsState())
    assert not path.exists()


def test_metrics_http_endpoint_and_failure(journal):
    server = start_metrics(journal.path, {'metrics_enabled': True, 'metrics_port': 0}, MetricsState())
    url = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(url + '/metrics', timeout=3) as response:
            assert response.status == 200
            assert 'version=0.0.4' in response.headers['Content-Type']
            assert b'media_agent_jobs' in response.read()
        with pytest.raises(HTTPError) as error:
            urlopen(url + '/unknown', timeout=3)
        assert error.value.code == 404
        journal.db.execute('DROP TABLE notification_outbox')
        journal.db.commit()
        with pytest.raises(HTTPError) as error:
            urlopen(url + '/metrics', timeout=3)
        assert error.value.code == 503
    finally:
        server.shutdown()
        server.server_close()


def test_retried_message_explains_uncertainty_with_stable_id(journal):
    journal.event('hash', '/movie', 'done')
    notifier = Mock(settings=TelegramSettings('fake', 'fake'), send=Mock(return_value=False))
    dispatcher = NotificationDispatcher(journal, notifier, clock=lambda: 0)
    dispatcher.dispatch()
    first = notifier.send.call_args.args[1]
    dispatcher.clock = lambda: 60
    notifier.send.return_value = True
    dispatcher.dispatch()
    retry = notifier.send.call_args.args[1]
    event_id = journal.db.execute('SELECT event_id FROM notification_outbox').fetchone()[0]
    assert f'Event #{event_id}' in first and f'Event #{event_id}' in retry
    assert 'possible duplicate' not in first
    assert 'possible duplicate' in retry and 'not a new media action' in retry


@pytest.fixture
def quarantined(journal, tmp_path):
    root = tmp_path / 'downloads'
    root.mkdir()
    source = root / 'episode.mkv'
    output = root / 'episode.mp4'
    source.write_bytes(b'original')
    output.write_bytes(b'verified-output')
    torrent_hash = 'a' * 40
    journal.put(torrent_hash, str(source), 'done', action='remux', output=str(output))
    quarantine = Quarantine(root, root / 'deleted', journal, retention_days=7)
    target = quarantine.move(torrent_hash, source)
    cfg = dict(download_root=str(root), quarantine_root=str(root / 'deleted'),
               process_enabled=True, finalize_enabled=True, cleanup_enabled=True)
    return cfg, torrent_hash, source, output, target


def expire(journal, source):
    journal.db.execute('UPDATE jobs SET purge_after=? WHERE source=?',
                       ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), str(source)))
    journal.db.commit()


def test_cleanup_preserves_active_pack_without_retirement_event(journal, quarantined):
    cfg, torrent_hash, source, output, target = quarantined
    expire(journal, source)
    assert cleanup_expired(cfg, journal) == 0
    assert target.exists() and output.exists()


def test_cleanup_retains_originals_seven_days(journal, quarantined):
    cfg, torrent_hash, source, output, target = quarantined
    journal.event(torrent_hash, '', 'torrent_retired')
    assert cleanup_expired(cfg, journal) == 0
    assert target.exists()


def test_cleanup_deletes_only_expired_original_and_preserves_output(journal, quarantined):
    cfg, torrent_hash, source, output, target = quarantined
    journal.event(torrent_hash, '', 'torrent_retired')
    expire(journal, source)
    assert cleanup_expired(cfg, journal) == 1
    assert not target.exists()
    assert output.read_bytes() == b'verified-output'
    assert journal.status(torrent_hash, str(source)) == 'purged'
    assert cleanup_expired(cfg, journal) == 0


@pytest.mark.parametrize('failure', ['missing_output', 'changed_original', 'disabled', 'interrupted'])
def test_cleanup_fails_closed(journal, quarantined, failure):
    cfg, torrent_hash, source, output, target = quarantined
    journal.event(torrent_hash, '', 'torrent_retired')
    expire(journal, source)
    if failure == 'missing_output':
        output.unlink()
    elif failure == 'changed_original':
        target.write_bytes(b'tampered')
    elif failure == 'disabled':
        cfg['cleanup_enabled'] = False
    else:
        journal.db.execute("UPDATE jobs SET status='purging'")
        journal.db.commit()
    assert cleanup_expired(cfg, journal) == 0
    assert cleanup_expired(cfg, journal) == 0
    assert target.exists()
    failures = journal.db.execute("SELECT COUNT(*) FROM notification_outbox WHERE event='cleanup_failed'").fetchone()[0]
    assert failures == (1 if failure in ('missing_output', 'changed_original') else 0)


def test_fallback_audit_read_only(journal, monkeypatch, capsys):
    from app.notification_status import main
    journal.put('hash', '/movie', 'running')
    journal.event('hash', '/movie', 'review', 'details')
    monkeypatch.setattr('sys.argv', ['notification_status', '--db', str(journal.path)])
    main()
    row = json.loads(capsys.readouterr().out)
    assert row['event'] == 'review'
    assert row['retried_claim'] is False
    assert 'message' not in row
    assert journal.status('hash', '/movie') == 'running'


def test_updated_pack_is_not_retired_without_hash_approval(journal, tmp_path, monkeypatch):
    from app import main as app_main
    source = tmp_path / 'episode.mkv'
    source.write_bytes(b'original-still-needed-by-torrent-monitor')
    output = tmp_path / 'episode.mp4'
    output.write_bytes(b'playable')
    torrent_hash = 'a' * 40
    journal.put(torrent_hash, str(source), 'done', action='remux', output=str(output))
    qbit = Mock()
    qbit.files.return_value = [dict(name=source.name, priority=1)]
    retire = Mock(side_effect=AssertionError('An unapproved pack must not be retired'))
    monkeypatch.setattr(app_main, 'finalize_torrent', retire)
    cfg = dict(download_root=str(tmp_path), quarantine_root=str(tmp_path / 'deleted'),
               process_enabled=True, finalize_enabled=True, cleanup_enabled=True,
               retirement_allow_hashes=[])
    app_main.process_torrent(qbit, dict(hash=torrent_hash, save_path=str(tmp_path)), cfg, journal)
    retire.assert_not_called()
    assert source.exists() and output.exists()
