"""Read-only notification fallback: python -m app.notification_status --db PATH."""
import argparse
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='/var/lib/media-agent/journal.sqlite3')
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--event', type=int, help='Show the saved message for this event ID')
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error('--limit must be between 1 and 1000')
    uri = 'file:' + quote(str(Path(args.db).resolve()), safe='/') + '?mode=ro'
    db = sqlite3.connect(uri, uri=True)
    db.row_factory = sqlite3.Row
    try:
        columns = 'event_id,event,created_at,attempts,available_at,delivered_at,last_error'
        if args.event is not None:
            rows = db.execute(f'SELECT {columns},message FROM notification_outbox WHERE event_id=?', (args.event,))
        else:
            rows = db.execute(f'SELECT {columns} FROM notification_outbox ORDER BY event_id DESC LIMIT ?', (args.limit,))
        for row in rows:
            item = dict(row)
            item['retried_claim'] = item['attempts'] > 1
            item['delivery_note'] = 'At-least-once delivery; repeated event IDs refer to the same media action.'
            print(json.dumps(item))
    finally:
        db.close()


if __name__ == '__main__':
    main()
