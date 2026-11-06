# Media Agent

The controller discovers completed, stable qBittorrent downloads. It probes selected videos,
ignores AppleDouble files, and queues one assignment per source. New conversions stay under
`.media-agent/<hash>/<source-key>/` on the downloads filesystem. Workers decode the entire
MP4 and verify its container, video, stereo audio and duration. Compatible originals and
journal-linked legacy outputs receive the same validation without reconversion.

The controller persists the selected file list and validated fingerprints before requesting
record-only retirement. An authenticated exact-hash lookup must confirm absence. The controller
then archives each converted original under the configured quarantine directory, promotes the
verified output using the original basename (preserving MP4 extension case), and marks the
torrent complete. Each move refuses to overwrite and can be reconciled after a restart.
Seven days after archival, cleanup verifies both the archived and published file identities.
Conflicts, incomplete selections, shared files and API failures block finalization.

Set `retire_all_completed: true` to retire all eligible torrents. Otherwise use
`retirement_allow_hashes` for individual approvals. Re-added or revised packs must pass the
same checks. A completed hash is never replayed automatically.

With `worker_jobs: true`, the singleton controller owns the existing journal PVC and submits
named Kubernetes Jobs. The namespace quota admits at most two Jobs. Each worker has a two-CPU
limit, read-only downloads, write access to only its assignment directory, and no journal,
qBittorrent credentials or service account token. Finished Jobs expire after 60 seconds.
The worker image follows the controller’s pinned image. Without the Job setting, the same
validation and staging logic runs locally.

## Files and credentials

Application repository: `/Users/jay/Projects/media-agent`.
GitOps repository: `/Users/jay/Projects/homeLab`.

Within `homeLab`:

- `apps/media-agent/configmap.yaml`: pod settings, seven-day retention, retirement hash approvals, metrics on port 9108. Pod media paths start with `/downloads`; the host volume is `/mnt/downloads`.
- `apps/media-agent/deployment.yaml`: proposed writable media mount, one replica with `Recreate`, credentials references, and digest-pinned ARM64 image.
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

Telegram sends one summary per published torrent plus actionable exceptions. It shows the event ID, delivery attempt, last five hash characters, filenames and processing milestones. Intermediate retirement, archival and purge events stay in the full SQLite journal. Retried deliveries additionally say **possible duplicate** and explain that the same event ID is not a new media action. A crash or lost response can leave delivery uncertain; exactly-once delivery is not possible with this send/acknowledge sequence. Even internal HTTP retries within one attempt can duplicate a message, so compare event IDs rather than attempt numbers alone.

The SQLite journal on the persistent `media-agent-journal` volume is the fallback record. No old event history is replayed when the outbox schema is introduced. Pending messages survive restarts and retry with a delay from 60 seconds up to one hour. Delivered rows remain for audit. Retries are handled after each polling cycle, so long conversions can delay delivery.

After deployment, inspect recent delivery records without modifying the database:

```bash
kubectl -n media-agent exec deploy/media-agent -- python -m app.notification_status
# Add --event EVENT_ID to see the saved message for one event.
```

Grafana shows pending, delivered, and retried messages, pending age, job states, lifecycle switches, and polling status. Retried counts indicate possible duplicates, not proven duplicates. Alerts cover delayed notifications, unavailable metrics, and interrupted purges. **An external fallback alert channel is not configured**: Prometheus/Alertmanager rules alone do not send email or another message. The journal audit and Grafana are the available fallback views.

## Release procedure

First review both repositories, the retirement list, and credentials. Check the configured retirement policy. Back up the journal with SQLite's online backup facility or while the pod is stopped; copying only a live WAL database file is not a reliable backup.

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
  -t ghcr.io/bogdanovj/media-agent:lifecycle-$(git rev-parse --short HEAD) --push .
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
