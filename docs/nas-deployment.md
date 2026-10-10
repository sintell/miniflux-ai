# NAS deployment, migration, and rollback

Use the existing `docker-compose.yml` and `config.sample.English.yml`. There is no
separate NAS configuration variant. Compose runs only `miniflux_ai` and joins the
existing external `reverse_proxy` network; Miniflux and the gateway must already
be reachable there as `miniflux:8080` and `codex-api:18080`.

This guide targets the UGREEN NASync DXP4800 Pro (8 GB, Linux/amd64), Docker Compose
through Portainer. Commands are for a NAS shell: only Docker commands use `sudo`.
Use Portainer to update a Portainer-owned stack; do not create a second container
with the same name through a separate Compose project.

## 1. Publish a versioned Docker Hub image

Configure the fork's GitHub repository under **Settings → Secrets and variables →
Actions**:

| Kind | Name | Value |
|---|---|---|
| Repository variable | `DOCKERHUB_IMAGE` | Your Docker Hub `namespace/repository` |
| Secret | `DOCKERHUB_USERNAME` | Docker Hub login with permission to push there |
| Secret | `DOCKERHUB_TOKEN` | Docker Hub access token, not an OAuth credential |

Create the Docker Hub repository and credentials if they do not exist. Nothing
assumes an existing registry login. `.github/workflows/docker-image.yml` runs
offline tests, then builds/pushes Linux/amd64 and Linux/arm64 on a published release
or a manual dispatch with a version such as `1.0.0-rc.1`. A release's tag is used
literally, including a leading `v`. A full commit-SHA tag is also published.
Ordinary pushes run tests without publishing. No `latest` tag is generated.

Do not reuse a release version. Pin Portainer to that version or, for exact
reproducibility, its image digest. Dependency versions and the base-image tag are
currently inherited as unpinned upstream choices; a previously built **image
digest**, not a fresh rebuild of the same source, is the reliable rollback target.

Optional local build on the NAS, from the checked-out repository:

```sh
sudo docker build --platform linux/amd64 -t miniflux-ai:personal-1 .
```

This builds only. To use that local image, set `MINIFLUX_AI_IMAGE` to
`miniflux-ai:personal-1` and disable forced registry pulls in Portainer. Normally,
use the versioned Docker Hub image instead. The build context excludes real
`config.yml`, `.env`, JSON state, SQLite data and backup directories.

## 2. Inspect the existing deployment and back up all state

Before removing/recreating `miniflux_ai`, inspect the actual mounts and numeric
owners. In the original application, `ai_news.json` and `feeds_status.json` are
stored under `/app` with **no persistent mounts in the stock Compose file**. They
may exist only inside the container. Do not assume the NAS deployment added mounts.

```sh
APP_DIR=/volume2/docker/miniflux_ai
ls -ld "$APP_DIR"
sudo docker inspect --format 'image={{.Config.Image}} id={{.Image}} user={{.Config.User}}' miniflux_ai
sudo docker inspect --format '{{range .Mounts}}{{println .Source "->" .Destination "rw=" .RW}}{{end}}' miniflux_ai
sudo docker exec miniflux_ai python -c 'import os; from pathlib import Path; print("uid/gid",os.getuid(),os.getgid()); [(print(str(p),"exists",p.exists(),"uid/gid",(p.stat().st_uid,p.stat().st_gid) if p.exists() else None)) for p in map(Path,["/app/config.yml","/app/entries.json","/app/ai_news.json","/app/feeds_status.json"])]'
ls -ln "$APP_DIR"
```

Save the previous Portainer stack definition, environment settings and the exact
old image ID/digest. Do not print or paste environment contents or API keys into
logs/issues. The stock image defaults to root; an empty inspect `user=` is not
evidence of UID 1000.

Stop the old container before copying mutable state. Do not fetch its old RSS
endpoint during backup: the stock implementation consumes the digest on GET.

```sh
BACKUP="$APP_DIR/backups/pre-digest-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP"
chmod 700 "$BACKUP"
sudo docker stop miniflux_ai
cp "$APP_DIR/config.yml" "$BACKUP/config.yml"
sudo docker cp miniflux_ai:/app/entries.json "$BACKUP/entries.json"
sudo docker cp miniflux_ai:/app/ai_news.json "$BACKUP/ai_news.json"
sudo docker cp miniflux_ai:/app/feeds_status.json "$BACKUP/feeds_status.json"
```

Run a state-file copy only if inspection confirmed it exists. A missing file can
be absent legitimately; a read/permission/corruption error should be investigated,
not replaced with an empty file. `docker cp` works for a stopped container. Keep
the protected backup directory and original JSON files through staged verification.

## 3. Prepare configuration and permissions

Merge `config.sample.English.yml` into the existing NAS `config.yml`. Preserve
intentional agent filters/customization, replacing obsolete prompts as described
in [personal-digest.md](personal-digest.md). The sample configures:

- `language: ${DIGEST_LANGUAGE:-English}`.
- Internal API: `http://miniflux:8080`.
- Browser links: `https://rss.home.antki.dev`.
- Gateway: `http://codex-api:18080/v1`, model `gpt-6-luna`, timeout 300,
  two shared workers, 15 request starts/minute, `reasoning_effort: low`.
- Separate `${MINIFLUX_API_KEY}` and `${LLM_API_KEY}` values from the environment.
- No temperature parameter (the Codex gateway rejects/drops it).
- 07:30, 18:00 and 22:00 editions; **set `DIGEST_TIMEZONE` before starting**.
- Weather disabled until you supply coordinates, a display location, units and
  the correct timezone. No empty/weather-only publications.

The new loader expands environment placeholders; the old stock image does not.
Keep the backed-up literal-value config for a stock-image rollback. Restart after
changing environment or config. A Compose `.env` supplies interpolation values;
only values explicitly passed in the Compose `environment` section enter the
container. In Portainer, set them in the stack's environment-variable settings.

Create a persistent **directory** for SQLite, lock files and feed status:

```sh
ls -ld "$APP_DIR"
mkdir -p "$APP_DIR/data"
ls -ldn "$APP_DIR/data"
ls -ln "$APP_DIR/config.yml"
```

The application image has no forced user and needs read access to the mounted
config and read/write/create/rename access in the data directory. Preserve the
established container user initially. Do not recursively change ownership of
existing files to `1000:10` merely because that is the NAS default.

The Linux/amd64 image has been locally tested with UID/GID `1000:10`, a writable
data mount, SQLite, timezone data and port 8088. Your NAS bind-mount permissions
still need checking. To evaluate that identity using only temporary test data,
after setting `MINIFLUX_AI_IMAGE` to a locally available candidate image:

```sh
sudo docker run --rm --network none --user 1000:10 \
  --mount "type=bind,src=$APP_DIR/config.yml,dst=/app/config.yml,readonly" \
  --mount "type=bind,src=$APP_DIR/data,dst=/app/data" \
  "$MINIFLUX_AI_IMAGE" python -c 'import socket,tempfile; from pathlib import Path; from services.state import State; Path("/app/config.yml").open().close(); s=socket.socket(); s.bind(("0.0.0.0",8088)); s.close(); t=tempfile.TemporaryDirectory(dir="/app/data"); db=State(Path(t.name)/"check.sqlite3"); db.enqueue({"content":"Permission check","url":"https://example.com"}); assert len(db.queued())==1; t.cleanup(); print("UID/GID 1000:10 preflight passed")'
```

This does not parse credentials, call the gateway, or change existing state.
If it fails, inspect the owner/modes and use the established user or deliberately
prepare only the new directory's permissions through NAS administration. Do not
change established ownership blindly. To adopt `user: "1000:10"`, also set
`FLASK_PORT=8088` if your NAS disallows unprivileged binding to port 80, and update
feed/webhook/proxy URLs to match. Portainer secrets/environment access is a separate
administrative concern from file ownership.

## 4. Import legacy state while the old container is stopped

Choose the same numeric identity that the new container will use. The example
below retains the stock image's root identity (`RUN_AS=0:0`); use `1000:10` only
after the preflight and ownership decision above.

```sh
RUN_AS=0:0
sudo docker run --rm --network none --user "$RUN_AS" \
  --mount "type=bind,src=$BACKUP,dst=/legacy,readonly" \
  --mount "type=bind,src=$APP_DIR/data,dst=/app/data" \
  "$MINIFLUX_AI_IMAGE" python -m services.state \
  --database /app/data/digest.sqlite3 \
  --entries /legacy/entries.json --digest /legacy/ai_news.json
```

Omit the corresponding argument if that legacy file was confirmed absent. If both
are absent, no import is necessary. The importer validates first, commits records
and receipts transactionally, never alters originals, and is safe to rerun on the
same files. Legacy records use content-derived IDs; new records use Miniflux entry
IDs. Old ID-less data cannot be reliably matched to later API IDs, so cross-era
duplicate coverage is merged editorially rather than guessing IDs from URLs.

The old saved digest is retained as safely escaped legacy text, with a stable ID.
Its actual generation time is unavailable; the importer labels the saved file's
modification time as an approximation. A legacy digest already fetched/cleared by
the stock endpoint cannot be recovered from an empty file.

If `feeds_status.json` existed, copy it into the new directory **once**, before
starting the new app. This snapshot format is unchanged:

```sh
cp -n "$BACKUP/feeds_status.json" "$APP_DIR/data/feeds_status.json"
```

Verify the selected runtime user can read it. Future replacements are atomic and
owned by that user. SQLite replaces active use of `entries.json` and `ai_news.json`;
their old individual mounts are removed from the updated stack, but the originals
and backups remain available for rollback.

## 5. Update the existing Portainer stack

Use the repository's `docker-compose.yml`. Set these stack variables:

| Variable | Value |
|---|---|
| `MINIFLUX_AI_IMAGE` | Docker Hub `namespace/repository:version` or `namespace/repository@sha256:...` |
| `MINIFLUX_AI_DIRECTORY` | `/volume2/docker/miniflux_ai` |
| `MINIFLUX_API_KEY` | Miniflux API key |
| `LLM_API_KEY` | Local gateway API key, separate from Miniflux |
| `DIGEST_LANGUAGE` | `English` initially, configurable |
| `DIGEST_TIMEZONE` | Your IANA timezone; UTC is only the unconfigured default |
| `FLASK_PORT` | `80` initially, preserving the existing internal URL |

Add a Docker Hub registry credential to Portainer if the image is private. Mounts
are `/app/config.yml:ro` and `/app/data` (read/write). No database/Redis/weather
service or published host port is added. Existing Miniflux/gateway containers must
also join `reverse_proxy`.

For a shell-managed Compose project only, with variables exported or in a protected
`.env` and the correct Compose project selected:

```sh
sudo docker compose config --quiet
sudo docker compose pull miniflux_ai
sudo docker compose up -d miniflux_ai
```

`config --quiet` validates without dumping expanded secrets. Starting the normal
application enables article polling and can consume LLM quota immediately. Perform
that step only when you are ready for the live rollout. For a quota-free first
startup, temporarily set `agents: {}`, `ai_news.schedule: []`, and leave
`feeds_status.enabled: true`; the RSS server can serve imported state without LLM
generation. Restore the intended configuration and restart only for the live stage.

## 6. Staged verification

1. **Offline:** run unit tests and the network-disabled image preflight first.
2. **Imported state:** compare queued counts and imported editions before starting
   normal polling. Check SQLite integrity without exposing content or credentials:

   ```sh
   sudo docker run --rm --network none \
     --mount "type=bind,src=$APP_DIR/data,dst=/data,readonly" \
     "$MINIFLUX_AI_IMAGE" python -c 'import sqlite3; c=sqlite3.connect("file:/data/digest.sqlite3?mode=ro",uri=True); print("integrity",c.execute("pragma integrity_check").fetchone()[0]); print("queued",c.execute("select count(*) from records where processed_by is null").fetchone()[0]); print("digests",c.execute("select count(*) from digests").fetchone()[0])'
   ```

3. **RSS persistence:** fetch the new endpoint twice, with no intervening edition;
   verify identical GUIDs/content and that the digest count remains unchanged.
   With the running container, this command makes only local HTTP requests:

   ```sh
   sudo docker exec miniflux_ai python -c 'import os,urllib.request; u="http://127.0.0.1:"+os.environ.get("FLASK_PORT","80")+"/rss/ai-news"; a=urllib.request.urlopen(u,timeout=5).read(); b=urllib.request.urlopen(u,timeout=5).read(); assert a==b; print("Repeat RSS fetch stable",len(a),"bytes")'
   ```

4. **Miniflux integration:** confirm the subscription URL remains
   `http://miniflux_ai/rss/ai-news` for port 80. For a custom port, edit the existing
   subscription rather than retaining both old and new URLs. Verify read and unread
   entry links at `https://rss.home.antki.dev`, original-article links, and merged
   story references. Miniflux private-network fetching must be enabled as required
   by its installed version.
5. **Live generation (quota-consuming):** after explicit rollout approval, enable
   the desired agents/schedules and inspect the next edition for language, detail,
   qualifications, references and local-time title. Confirm a refresh failure leaves
   a stored digest and pending retry, not an empty queue with no digest.
6. **Weather:** once configured, check forecast units/windows/fetch provenance and
   a creative but factual opening. The LLM has no browsing tools. Weather failures
   should produce news-only openings. There should be no editions for empty queues.

Logs are available via `sudo docker logs --since 30m miniflux_ai`. RSS item identity
and edition time come from generation, never the time Miniflux fetches the feed.

## Backups and rollback

Keep the original image digest, old Portainer definition/environment, old config
and three JSON backups. For subsequent backups, stop the application and copy the
**entire data directory** (including any SQLite journal), then restart. Alternatively
use SQLite's online backup API for the database; copying a live database file alone
is not a consistent backup. Keep config/environment backups separately protected.

Before rolling back, stop the new container and preserve its entire data directory
and current config under a new timestamped backup. Do not delete the SQLite state.

- **Rollback to another SQLite-capable fork image:** restore its matching config
  and compatible data backup, pin the exact previous image, then recreate through
  the same Portainer stack. Keep the current-state backup for reconciliation.
- **Rollback immediately to stock, before new processing:** restore the original
  stack, literal-key config and JSON mounts/files from the pre-upgrade snapshot.
- **Rollback to stock after processing:** stock cannot read SQLite. Replaying the
  original queue can duplicate already published coverage and omit new arrivals.
  Export only the current unprocessed SQLite queue to a fresh JSON file before
  reconciling/replacing the stock queue. Retain all SQLite digests separately;
  stock can expose only one digest and consumes it on GET. The fork's processing
  marker also differs from stock, so initially disable stock agents to avoid
  re-summarizing fork-annotated articles until filters/markers are reconciled.

Example export, with the new app stopped and a **new, empty** rollback directory
chosen as `$ROLLBACK`:

```sh
ROLLBACK="$APP_DIR/backups/rollback-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$ROLLBACK"
chmod 700 "$ROLLBACK"
sudo docker run --rm -i --network none \
  --mount "type=bind,src=$APP_DIR/data,dst=/data,readonly" \
  --mount "type=bind,src=$ROLLBACK,dst=/rollback" \
  "$MINIFLUX_AI_IMAGE" python - <<'PY'
import json
import sqlite3

db = sqlite3.connect('file:/data/digest.sqlite3?mode=ro', uri=True)
records = []
for (payload,) in db.execute('SELECT payload FROM records WHERE processed_by IS NULL ORDER BY rowid'):
    record = json.loads(payload)
    record['content'] = record.pop('summary')
    records.append(record)
with open('/rollback/entries.json', 'x', encoding='utf-8') as output:
    json.dump(records, output, ensure_ascii=False, indent=2)
print('Exported queued records:', len(records))
PY
```

Inspect counts and reconcile the rollout interval before resuming stock processing.
A rollback restores software and state compatibility; it cannot undo already
delivered Miniflux articles or recreate state the stock implementation previously
cleared.
