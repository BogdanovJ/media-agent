# Media Agent: rollout quick read

**Release guide: processing enabled; updated season-pack retirement remains explicitly approved.**

## What will happen

- Poll fully completed, stable qBittorrent torrents; remux/transcode when required.
- Keep verified MP4 outputs beside the original files until **you remove them manually**.
- Already-compatible files stay in place: they are the playable copy, not disposable originals.
- Processing, finalization, and cleanup switches are enabled in the proposed configuration.
- **Your TorrentMonitor updates season packs. Keep those hashes out of `retirement_allow_hashes`.** Moving their originals or removing their qBittorrent entries can interfere with future updates.
- Retirement remains limited to explicitly approved, finished torrent hashes. Both proposed allowlists are empty, so no torrents will be retired by this rollout until you approve them.
- Once an approved torrent is fully processed, originals move to quarantine; its qBittorrent record is removed with `deleteFiles=false`. After seven days, cleanup deletes only fingerprint-verified quarantined originals with a recorded successful retirement and an existing playable output.
- Incomplete packs are not processed episode by episode. Replacing a pack may produce a new torrent hash; existing output collisions go to manual review rather than overwriting your media. Automatic lifecycle support for evolving packs still needs a TorrentMonitor policy/integration.

## Files and credentials

Application repository: `/Users/jay/Projects/media-agent`.
GitOps repository: `/Users/jay/Projects/homeLab`.

Within `homeLab`:

- `apps/media-agent/configmap.yaml`: pod settings, seven-day retention, retirement hash approvals, metrics on port 9108. Pod media paths start with `/downloads`; the host volume is `/mnt/downloads`.
- `apps/media-agent/deployment.yaml`: proposed writable media mount, one replica with `Recreate`, credentials references, and planned ARM64 image tag `outbox-metrics-20261105-1`.
- `apps/media-agent/secret.enc.yaml`: SOPS-encrypted Secret `media-agent-qbit`, keys `username` and `password`. Deployment supplies `QBIT_URL` separately.
- `apps/media-agent/ghcr-pull.enc.yaml`: encrypted Docker pull Secret `ghcr-pull`. This does **not** sign your Mac into Docker for pushing images.
- `apps/media-agent/example.telegram.enc.yaml`: encrypted placeholder template; excluded from deployment.
- `apps/media-agent/telegram.enc.yaml`: encrypted credentials configured and included in deployment. Create Secret `media-agent-telegram` with `stringData.bot_token` and `stringData.chat_id`; deployment maps these to `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- `.sops.yaml`: existing age recipient and encryption rules for `*.enc.yaml`. Flux uses the `flux-system/sops-age` Secret to decrypt. A matching private age key is needed locally to edit/decrypt; it must not be committed. The local key is `/Users/jay/.config/sops/age/keys.txt`; set `SOPS_AGE_KEY_FILE` to that path.
- `apps/media-agent/metrics.yaml`: internal Service, ServiceMonitor, and alert rules.
- `apps/grafana/dashboards/media-agent.json`: dashboard, provisioned by Grafana's existing sidecar.

Edit existing encrypted credentials with SOPS, not a plain-text editor:

```bash
cd /Users/jay/Projects/homeLab
sops apps/media-agent/secret.enc.yaml
```

For Telegram, copy the encrypted example and edit the copy through SOPS:

```bash
cd /Users/jay/Projects/homeLab
cp -n apps/media-agent/example.telegram.enc.yaml apps/media-agent/telegram.enc.yaml
sops apps/media-agent/telegram.enc.yaml
```

In the SOPS editor, replace `stringData.bot_token` and `stringData.chat_id`
with your real bot token and destination chat ID. Keep the Secret name
`media-agent-telegram` and namespace `media-agent`. Remove the example-only
comments from your working copy, then save and exit; SOPS encrypts the values
on disk. The copy command does not overwrite an existing credential file.
Do not enter real credentials into the example itself or paste them into chat.

After saving the real values, add `- telegram.enc.yaml` to
`apps/media-agent/kustomization.yaml` resources. **Never add
`example.telegram.enc.yaml` as a resource.** Keep the example as encrypted
placeholders. Telegram references are optional: without both values,
notifications stay queued. Adding/changing environment-based secrets requires
a pod restart to pick them up.

Once the real encrypted credentials are ready, validate and commit the source
and GitOps changes, build/push the ARM64 image, and only then push GitOps for
Flux to deploy. Do not deploy placeholder credentials.

## Telegram uncertainty and fallback

Every delivered message includes a stable **event ID** and attempt number. Retried deliveries additionally say **possible duplicate** and explain that the same event ID is not a new media action. A crash or lost response can leave delivery uncertain; exactly-once delivery is not possible with this send/acknowledge sequence. Even internal HTTP retries within one attempt can duplicate a message, so compare event IDs rather than attempt numbers alone.

The SQLite journal on the persistent `media-agent-journal` volume is the fallback record. No old event history is replayed when the outbox schema is introduced. Pending messages survive restarts and retry with a delay from 60 seconds up to one hour. Delivered rows remain for audit. Retries are handled after each polling cycle, so long conversions can delay delivery.

After deployment, inspect recent delivery records without modifying the database:

```bash
kubectl -n media-agent exec deploy/media-agent -- python -m app.notification_status
# Add --event EVENT_ID to see the saved message for one event.
```

Grafana shows pending, delivered, and retried messages, pending age, job states, lifecycle switches, and polling status. Retried counts indicate possible duplicates, not proven duplicates. Alerts cover delayed notifications, unavailable metrics, and interrupted purges. **An external fallback alert channel is not configured**: Prometheus/Alertmanager rules alone do not send email or another message. The journal audit and Grafana are the available fallback views.

## Release procedure

First review both repositories, the retirement list, and credentials. Keep active season packs unapproved. Back up the journal with SQLite's online backup facility or while the pod is stopped; copying only a live WAL database file is not a reliable backup.

Application validation:

```bash
cd /Users/jay/Projects/media-agent
source .venv/bin/activate
python -m pytest -q
python -m compileall -q app
git diff --check
git diff
```

After review, commit the application changes, then build/push the **ARM64** image before pushing the enabled GitOps configuration. Confirm Docker is signed into GHCR with push permission. The planned tag must match the deployment and must not be reused for different code:

```bash
cd /Users/jay/Projects/media-agent
docker buildx build --platform linux/arm64 \
  -t ghcr.io/bogdanovj/media-agent:outbox-metrics-20261105-1 --push .
```

Before the GitOps commit/push:

```bash
cd /Users/jay/Projects/homeLab
kubectl kustomize apps/media-agent >/dev/null
kubectl kustomize apps/grafana >/dev/null
git diff --check
git diff
```

Pushing `homeLab` lets Flux reconcile the enabled settings. Do not push it before the image exists. After rollout, check the pod, `/metrics`, the Prometheus target, Grafana dashboard **Media Agent — lifecycle and delivery**, and notification audit. Confirm the running image digest matches the release before considering rollout complete.

Emergency stop: scale the deployment to zero for immediate intervention, then set all three lifecycle flags false and restore the read-only media mount in GitOps. Flux can undo manual scaling, so persist the stop in GitOps. Changing flags or rolling back does not restore files already purged.

## Monitoring format references

[Prometheus text exposition](https://prometheus.io/docs/instrumenting/exposition_formats/) and [Grafana dashboard JSON model](https://grafana.com/docs/grafana/latest/visualizations/dashboards/build-dashboards/view-dashboard-json-model/).
