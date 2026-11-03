from pathlib import Path
from tempfile import TemporaryDirectory
from app.journal import Journal
from app.quarantine import Quarantine
import hashlib

with TemporaryDirectory() as tmp:
    root = (Path(tmp) / "downloads").resolve()
    root.mkdir()

    source = (root / "movie.mp4").resolve()
    source.write_bytes(b"test original video")

    output = root / "media-agent-ready" / "movie.mp4"
    output.parent.mkdir()
    output.write_bytes(b"test converted video")

    journal = Journal(Path(tmp) / "journal.sqlite3")
    torrent_hash = "a" * 40

    journal.put(
        torrent_hash,
        str(source),
        "done",
        action="remux",
        output=str(output),
    )

    quarantine = Quarantine(
        root,
        root / "deleted",
        journal,
        retention_days=7,
    )

    target = quarantine.move(torrent_hash, source)
    job = journal.get(torrent_hash, str(source))

    assert job["status"] == "quarantined"
    assert job["source_size"] == target.stat().st_size
    assert job["source_sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()

    print("Quarantine fingerprint verified")
    print("SHA-256:", job["source_sha256"])
    print("Purge after:", job["purge_after"])

    journal.close()
