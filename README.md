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
- Telegram delivery uses the durable SQLite notification outbox described below.
- Prometheus/Grafana and a qBittorrent completion hook remain planned.

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


## Durable Telegram notification outbox

New `done`, `failed`, `review`, `blocked`, `missing`, `interrupted`,
`quarantined`, `purged`, and `torrent_retired` journal events enqueue an
immutable message in the existing state database, in the same transaction.
Existing event history is not replayed. Repeated job writes with the same
status and reason do not enqueue another message; a new transition or changed
reason does. Explicit `Journal.event()` calls represent distinct events.

Both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are required for delivery.
Without both credentials, messages remain pending and attempts are not consumed.
Credentials should come from the existing SOPS/Kubernetes Secret workflow;
this change does not create secrets or deploy anything.

Each polling cycle attempts up to 10 due messages, including when qBittorrent
polling fails. Sending occurs outside SQLite transactions. Failed deliveries
retry after 60 seconds, doubling to a maximum of one hour, without a retry
limit. Actual delivery waits for the next polling cycle and any current media
work to finish. `attempts` counts outbox claims, each of which may include the
notifier's internal HTTP retries. Permanent API errors remain pending until
credentials/configuration are corrected.

Claims expire after at least five minutes so interrupted delivery resumes after
a restart. Claim tokens prevent a stale worker acknowledging a newer claim.
Delivery is **at-least-once**, not exactly-once: a crash after Telegram accepts a
message but before SQLite records success can cause a duplicate. Delivered
rows are retained for audit; automatic outbox retention is not implemented.
The journal volume must persist across pod restarts. Keep one media worker;
notification claims do not make media processing safe across multiple replicas.

Observation-only settings remain unchanged: `process_enabled`,
`finalize_enabled`, and `cleanup_enabled` are all false in the checked-in
profile. Observation mode can enqueue review/missing notifications, but does
not enable conversion, retirement, or removal. No deployment is required to
run the offline tests:

```bash
python -m pytest -q
python -m compileall -q app
git diff --check
```
