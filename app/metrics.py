"""Read-only Prometheus snapshots; never open Journal in the HTTP thread."""
import logging
import sqlite3
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

from .journal import JOB_STATUSES

log = logging.getLogger('media-agent.metrics')


class MetricsState:
    def __init__(self):
        self.lock = threading.Lock()
        self.last_poll = 0
        self.poll_errors = 0

    def poll_finished(self, successful):
        with self.lock:
            if successful:
                self.last_poll = time.time()
            else:
                self.poll_errors += 1


def read_metrics(path, cfg, state, timestamp=None):
    timestamp = time.time() if timestamp is None else timestamp
    uri = 'file:' + quote(str(Path(path).resolve()), safe='/') + '?mode=ro'
    db = sqlite3.connect(uri, uri=True, timeout=2)
    try:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        jobs = dict(db.execute('SELECT status, COUNT(*) FROM jobs GROUP BY status'))
        pending, delivered, retried, attempts, oldest = db.execute('''
            SELECT COALESCE(SUM(delivered_at IS NULL),0),
                   COALESCE(SUM(delivered_at IS NOT NULL),0),
                   COALESCE(SUM(attempts > 1),0), COALESCE(SUM(attempts),0),
                   MIN(CASE WHEN delivered_at IS NULL THEN created_at END)
            FROM notification_outbox
        ''').fetchone()
        events = dict(db.execute('SELECT event, COUNT(*) FROM events GROUP BY event'))
    finally:
        db.close()
    lines = []
    def metric(name, help_text, value, kind='gauge'):
        lines.extend([f'# HELP {name} {help_text}', f'# TYPE {name} {kind}', f'{name} {value}'])
    for flag in ('process_enabled', 'finalize_enabled', 'cleanup_enabled'):
        metric('media_agent_' + flag, 'Configured lifecycle switch.', int(bool(cfg.get(flag, False))))
    lines.extend(['# HELP media_agent_jobs Jobs by persisted status.', '# TYPE media_agent_jobs gauge'])
    for status in sorted(JOB_STATUSES):
        lines.append(f'media_agent_jobs{{status="{status}"}} {jobs.get(status, 0)}')
    metric('media_agent_notifications_pending', 'Undelivered outbox messages.', pending)
    metric('media_agent_notifications_delivered', 'Retained delivered outbox messages.', delivered)
    metric('media_agent_notifications_retried', 'Messages claimed more than once; not confirmed duplicates.', retried)
    metric('media_agent_notification_attempts', 'Delivery claims retained in the journal.', attempts)
    age = max(0, timestamp - datetime.fromisoformat(oldest).timestamp()) if oldest else 0
    metric('media_agent_notification_oldest_pending_seconds', 'Age of the oldest undelivered message.', age)
    for event in ('done', 'failed', 'quarantined', 'purged', 'torrent_retired', 'cleanup_failed'):
        metric('media_agent_events_' + event, 'Matching events retained in the journal.', events.get(event, 0))
    with state.lock:
        metric('media_agent_last_successful_poll_timestamp_seconds', 'Last successful qBittorrent poll; zero until first success.', state.last_poll)
        metric('media_agent_poll_errors_total', 'Polling failures since process start.', state.poll_errors, 'counter')
    return '\n'.join(lines) + '\n'


def start_metrics(path, cfg, state):
    if not cfg.get('metrics_enabled', False):
        return None

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path != '/metrics':
                self.send_error(404)
                return
            try:
                body = read_metrics(path, cfg, state).encode()
            except Exception:
                log.warning('Metrics snapshot unavailable')
                self.send_error(503, 'Metrics unavailable')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; version=0.0.4; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer((cfg.get('metrics_host', '127.0.0.1'), cfg.get('metrics_port', 9108)), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True, name='metrics')
    thread.start()
    return server
