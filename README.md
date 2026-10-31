# media-agent (v0.1 prototype)

qBittorrent-only video compatibility worker for Plex DLNA and older Samsung TVs.

## Current status
**Prototype / observation mode by default. Not production-ready.**

- Polls qBittorrent completed torrents; no qBittorrent hook required yet.
- ffprobe decision journal in SQLite.
- One sequential worker, output in a separate directory.
- Conservative Samsung profile (H.264 High <=4.1, 1080p, AAC stereo).
- Files >4 GiB flagged for review: short-sample playback did not prove a size limit.
- Existing originals and torrent records are **never removed** in v0.1.
- `finalize_enabled` and `cleanup_enabled` are reserved for the next release; changing them does not enable deletion.
- VKGET is not scanned.
- Does not yet implement 7-day quarantine/purge, Telegram, Prometheus, or a qBittorrent completion hook.

## Configuration
Edit `config/samsung-dlna.yaml`. Set QBIT_URL, QBIT_USERNAME,
QBIT_PASSWORD as environment variables (use SOPS-encrypted Kubernetes Secret).
The worker must mount `/mnt/downloads` read/write and `/var/lib/media-agent`
as a persistent volume. Keep replica count at 1.

## Local checks
`pip install -e '.[test]'`
`pytest -q`

## Important implementation notes
- Output is placed in `/mnt/downloads/media-agent-ready/` (exclude this and
  `/mnt/downloads/deleted/` from qBittorrent input).
- Review decisions are not automatically retried after config changes;
  a journal requeue command will be added later.
- A single-file torrent's content_path is used directly. Verify multi-file
  qBittorrent path layouts before enabling conversion.
- Production hardening still needed: transactional finalize, source quarantine,
  7-day purge with recovery, output-duration/decode validation, concurrency
  locking, retries, safe free-space budgeting, and graceful shutdown.
- Do not expose the qBittorrent Web API outside the cluster.
