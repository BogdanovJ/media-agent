import sqlite3
from datetime import datetime, timezone

def now():
    return datetime.now(timezone.utc).isoformat()

class Journal:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("""CREATE TABLE IF NOT EXISTS jobs (
            torrent_hash TEXT NOT NULL,
            source TEXT NOT NULL,
            status TEXT NOT NULL,
            action TEXT,
            reason TEXT,
            output TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (torrent_hash, source)
        )""")
        self.db.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            at TEXT NOT NULL,
            torrent_hash TEXT,
            source TEXT,
            event TEXT NOT NULL,
            detail TEXT
        )""")
        self.db.commit()

    def put(self, torrent_hash, source, status, action="", reason="", output=""):
        self.db.execute("""INSERT INTO jobs VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(torrent_hash,source) DO UPDATE SET
            status=excluded.status, action=excluded.action,
            reason=excluded.reason, output=excluded.output,
            updated_at=excluded.updated_at""",
            (torrent_hash, source, status, action, reason, output, now()))
        self.event(torrent_hash, source, status, reason)

    def event(self, torrent_hash, source, event, detail=""):
        self.db.execute("INSERT INTO events(at,torrent_hash,source,event,detail) VALUES (?,?,?,?,?)",
                        (now(), torrent_hash, source, event, detail))
        self.db.commit()

    def status(self, torrent_hash, source):
        row = self.db.execute("SELECT status FROM jobs WHERE torrent_hash=? AND source=?",
                              (torrent_hash, source)).fetchone()
        return row[0] if row else None
