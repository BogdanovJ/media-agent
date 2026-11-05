"""Offline durability, delivery and observation-mode integration checks."""

import sqlite3
from unittest.mock import Mock

import pytest

from app.journal import Journal, NOTIFICATION_EVENTS
from app.notifier import NotificationDispatcher, TelegramSettings


@pytest.fixture
def journal(tmp_path):
    instance = Journal(tmp_path / 'journal.sqlite3')
    yield instance
    instance.close()


def rows(journal):
    cursor = journal.db.execute('SELECT * FROM notification_outbox ORDER BY event_id')
    return [dict(zip([c[0] for c in cursor.description], row)) for row in cursor]


def sender(result=True):
    return Mock(settings=TelegramSettings('fake-token', 'fake-chat'), send=Mock(return_value=result))


@pytest.mark.parametrize('event', sorted(NOTIFICATION_EVENTS))
def test_selected_events_queued_atomically(journal, event):
    journal.event('hash', '/movie.mkv', event, 'detail')
    item, = rows(journal)
    assert item['event'] == event
    assert item['attempts'] == 0
    assert 'hash' in item['message'] and '/movie.mkv' in item['message']
    assert 'detail' in item['message']
    assert journal.db.execute('SELECT event FROM events WHERE id=?', (item['event_id'],)).fetchone() == (event,)


@pytest.mark.parametrize('event', ['planned', 'running', 'quarantine_intent', 'purge_intent', 'fingerprinted'])
def test_routine_events_not_queued(journal, event):
    journal.event('hash', '/movie', event)
    assert rows(journal) == []


def test_repeated_status_suppressed_but_new_failure_queued(journal):
    journal.put('hash', '/movie', 'failed', reason='first')
    journal.put('hash', '/movie', 'failed', reason='first')
    assert len(rows(journal)) == 1
    journal.put('hash', '/movie', 'failed', reason='different')
    journal.put('hash', '/movie', 'running')
    journal.put('hash', '/movie', 'failed', reason='different')
    assert len(rows(journal)) == 3


def test_outbox_insert_failure_rolls_back_job_and_event(journal):
    journal.put('hash', '/movie', 'planned')
    journal.db.execute('''CREATE TRIGGER reject_notification BEFORE INSERT ON notification_outbox
                          BEGIN SELECT RAISE(ABORT, 'test failure'); END''')
    with pytest.raises(sqlite3.IntegrityError, match='test failure'):
        journal.put('hash', '/movie', 'done')
    assert not journal.db.in_transaction
    journal.event('other', '', 'planned')  # Later commits cannot leak failed changes.
    assert journal.status('hash', '/movie') == 'planned'
    assert journal.db.execute("SELECT COUNT(*) FROM events WHERE event='done'").fetchone()[0] == 0
    assert rows(journal) == []


def test_event_failure_rolls_back(journal):
    journal.db.execute('''CREATE TRIGGER reject_notification BEFORE INSERT ON notification_outbox
                          BEGIN SELECT RAISE(ABORT, 'test failure'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        journal.event('hash', '/movie', 'review')
    assert not journal.db.in_transaction
    assert journal.db.execute('SELECT COUNT(*) FROM events').fetchone()[0] == 0


def test_caller_rollback_includes_outbox(journal):
    with pytest.raises(RuntimeError):
        with journal.db:
            journal._insert_event('hash', '/movie', 'purged')
            raise RuntimeError('rollback')
    assert rows(journal) == []


def test_migration_preserves_history_without_replay(tmp_path):
    path = tmp_path / 'legacy.sqlite3'
    first = Journal(path)
    first.put('hash', '/movie', 'done')
    first.db.execute('DROP TABLE notification_outbox')
    first.db.commit()
    first.close()
    second = Journal(path)
    assert rows(second) == []
    assert second.status('hash', '/movie') == 'done'
    second.event('hash', '/other', 'review')
    assert len(rows(second)) == 1
    second.close()


def test_pending_and_delivered_survive_restart(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    first = Journal(path)
    first.event('hash', '/a', 'done')
    first.event('hash', '/b', 'failed')
    item = first.claim_notification(timestamp=100)
    assert first.finish_notification(item['event_id'], item['claim_token'], delivered=True)
    first.close()
    second = Journal(path)
    item = second.claim_notification(timestamp=101)
    assert item['event'] == 'failed'
    assert second.claim_notification(timestamp=101) is None
    second.close()


def test_crashed_claim_expires_and_old_ack_is_rejected(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    first = Journal(path)
    first.event('hash', '/movie', 'done')
    old = first.claim_notification(timestamp=100, lease_seconds=10)
    first.close()
    second = Journal(path)
    assert second.claim_notification(timestamp=109) is None
    new = second.claim_notification(timestamp=110)
    assert new['attempts'] == 2
    assert new['claim_token'] != old['claim_token']
    assert not second.finish_notification(old['event_id'], old['claim_token'], delivered=True)
    assert second.finish_notification(new['event_id'], new['claim_token'], delivered=True)
    assert second.claim_notification(timestamp=10000) is None
    second.close()


def test_two_connections_cannot_claim_same_message(journal):
    journal.event('hash', '/movie', 'done')
    other = Journal(journal.path)
    assert journal.claim_notification(timestamp=100)
    assert other.claim_notification(timestamp=100) is None
    other.close()


def test_claim_refuses_to_commit_callers_transaction(journal):
    journal._insert_event('hash', '/movie', 'done')
    with pytest.raises(RuntimeError, match='transaction'):
        journal.claim_notification()
    journal.db.rollback()
    assert rows(journal) == []


def test_restart_recovery_queues_once(tmp_path):
    path = tmp_path / 'journal.sqlite3'
    first = Journal(path)
    first.put('hash', '/movie', 'running')
    first.close()
    for _ in range(2):
        current = Journal(path)
        assert current.status('hash', '/movie') == 'interrupted'
        assert [r['event'] for r in rows(current)] == ['interrupted']
        current.close()


def test_success_outside_transaction_never_resent(journal):
    journal.event('hash', '/movie', 'done')
    notifier = sender()
    def send(event, message):
        assert not journal.db.in_transaction
        assert event == 'done'
        return True
    notifier.send.side_effect = send
    dispatcher = NotificationDispatcher(journal, notifier, clock=lambda: 100)
    assert dispatcher.dispatch() == 1
    assert dispatcher.dispatch() == 0
    assert notifier.send.call_count == 1
    assert rows(journal)[0]['delivered_at'] is not None


def test_disabled_notifier_does_not_claim(journal):
    journal.event('hash', '/movie', 'review')
    assert NotificationDispatcher(journal, None).dispatch() == 0
    assert rows(journal)[0]['attempts'] == 0


def test_failure_backoff_and_eventual_delivery(journal):
    journal.put('hash', '/movie', 'failed', reason='conversion failed')
    notifier = sender(False)
    current_time = [100]
    dispatcher = NotificationDispatcher(journal, notifier, clock=lambda: current_time[0])
    assert dispatcher.dispatch() == 0
    assert rows(journal)[0]['available_at'] == 160
    assert dispatcher.dispatch() == 0
    assert notifier.send.call_count == 1
    current_time[0] = 160
    assert dispatcher.dispatch() == 0
    assert rows(journal)[0]['available_at'] == 280
    notifier.send.return_value = True
    current_time[0] = 280
    assert dispatcher.dispatch() == 1
    assert rows(journal)[0]['last_error'] is None
    assert journal.status('hash', '/movie') == 'failed'


def test_transport_exception_is_redacted_and_retried(journal, caplog):
    journal.event('hash', '/movie', 'done')
    notifier = sender()
    notifier.send.side_effect = RuntimeError('SECRET-TOKEN')
    assert NotificationDispatcher(journal, notifier, clock=lambda: 100).dispatch() == 0
    assert 'SECRET-TOKEN' not in caplog.text
    assert rows(journal)[0]['last_error'] == 'delivery_failed'
    assert rows(journal)[0]['claim_token'] is None


def test_ack_failure_leaves_claim_for_recovery(journal, monkeypatch, caplog):
    journal.event('hash', '/movie', 'done')
    notifier = sender()
    monkeypatch.setattr(journal, 'finish_notification', Mock(side_effect=sqlite3.OperationalError('SECRET')))
    assert NotificationDispatcher(journal, notifier, clock=lambda: 100).dispatch() == 0
    assert journal.claim_notification(timestamp=399) is None
    assert journal.claim_notification(timestamp=400)['attempts'] == 2
    assert 'SECRET' not in caplog.text


def test_batch_limit_leaves_remaining_work(journal):
    for i in range(3):
        journal.event('hash', str(i), 'done')
    notifier = sender()
    assert NotificationDispatcher(journal, notifier).dispatch(limit=2) == 2
    assert sum(row['delivered_at'] is None for row in rows(journal)) == 1


@pytest.mark.parametrize('poll_fails', [False, True])
def test_main_dispatches_in_observation_mode_even_if_polling_fails(tmp_path, monkeypatch, poll_fails):
    from app import main as app_main
    path = tmp_path / 'state.sqlite3'
    journal = Journal(path)
    journal.event('hash', '/movie', 'review')
    journal.close()
    config = tmp_path / 'config.yaml'
    config.write_text(f'state_db: {path}\nprocess_enabled: false\nfinalize_enabled: false\ncleanup_enabled: false\npoll_seconds: 60\n')
    monkeypatch.setenv('CONFIG_FILE', str(config))
    for key in ['QBIT_URL', 'QBIT_USERNAME', 'QBIT_PASSWORD', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']:
        monkeypatch.setenv(key, 'fake')
    notifier = sender()
    monkeypatch.setattr(app_main, 'TelegramNotifier', lambda settings: notifier)
    qbit = Mock()
    qbit.completed.return_value = []
    if poll_fails:
        qbit.completed.side_effect = RuntimeError('offline')
    monkeypatch.setattr(app_main, 'Qbit', Mock(return_value=qbit))
    process = Mock()
    monkeypatch.setattr(app_main, 'process_torrent', process)
    monkeypatch.setattr(app_main.time, 'sleep', Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        app_main.main()
    assert notifier.send.call_count == 1
    process.assert_not_called()
    journal = Journal(path)
    assert rows(journal)[0]['delivered_at'] is not None
    journal.close()


def test_retry_schedule_survives_restart_and_backoff_is_capped(tmp_path):
    path = tmp_path / 'state.sqlite3'
    current_time = 100
    for attempt in range(1, 9):
        journal = Journal(path)
        if attempt == 1:
            journal.event('hash', '/movie', 'done')
        notifier = sender(False)
        dispatcher = NotificationDispatcher(journal, notifier, clock=lambda: current_time)
        assert dispatcher.dispatch() == 0
        item, = rows(journal)
        assert item['attempts'] == attempt
        assert item['available_at'] - current_time == min(60 * 2 ** (attempt - 1), 3600)
        assert dispatcher.dispatch() == 0
        assert notifier.send.call_count == 1
        current_time = item['available_at']
        journal.close()


@pytest.mark.parametrize('action, expected_status', [('remux', 'planned'), ('review', 'review')])
def test_observation_processing_never_converts_or_retires(journal, tmp_path, monkeypatch, action, expected_status):
    from app import main as app_main
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    source = downloads / 'movie.mkv'
    source.write_bytes(b'test')
    cfg = dict(download_root=str(downloads), quarantine_root=str(downloads / 'deleted'),
               process_enabled=False, finalize_enabled=False, cleanup_enabled=False,
               review_over_bytes=1000)
    qbit = Mock()
    qbit.files.return_value = [dict(name='movie.mkv', priority=1)]
    monkeypatch.setattr(app_main, 'probe', lambda path: {})
    monkeypatch.setattr(app_main, 'decision', lambda *args: (action, 'test reason'))
    convert = Mock(side_effect=AssertionError('conversion must remain disabled'))
    retire = Mock(side_effect=AssertionError('retirement must remain disabled'))
    monkeypatch.setattr(app_main, 'convert', convert)
    monkeypatch.setattr(app_main, 'finalize_torrent', retire)
    app_main.process_torrent(qbit, dict(hash='hash', save_path=str(downloads)), cfg, journal)
    assert journal.status('hash', str(source)) == expected_status
    assert len(rows(journal)) == (1 if action == 'review' else 0)
    assert source.read_bytes() == b'test'
    convert.assert_not_called()
    retire.assert_not_called()
