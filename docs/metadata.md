# Native Miniflux metadata enrichment

This Python companion can write article enrichment to the generic metadata API in
the local Miniflux fork. Official/older Miniflux releases do not support it. The
fork has not been deployed to the NAS by this work.

## Configuration

Keep the existing configuration (credentials, language, scheduling, weather,
editorial settings and mounted state directory). Add:

```yaml
language: English # Applies to summary, inferred topic labels, and digest prose
miniflux:
  base_url: http://miniflux:8080
  browser_url: https://rss.home.antki.dev
  api_key: ${MINIFLUX_API_KEY}
  enrichment_mode: metadata
  schedule_interval: 15
  timeout: 30
  retries: 2
metadata:
  relevance_criteria: ${MINIFLUX_AI_RELEVANCE_CRITERIA}
  input_fields: []
agents:
  summary:
    title: 'AI summary:'
    style_block: true
    deny_list:
      - https://ai-news.miniflux
```

`metadata.relevance_criteria` must be your own nonempty personal policy: subjects
and developments you value, exclusions, and any desired rating anchors. There is
no built-in personal profile. Empty criteria fail configuration before any LLM
request. This task does not establish your actual relevance criteria.

`metadata.input_fields` selects **registered, explicit** qualified metadata names
to include in the model's source message, e.g. `[research_app.context]`. Those
fields must be registered for the same Miniflux user. Default is an empty list.
The companion reads its three output fields for processing decisions, but sends
them to the model only if explicitly selected here. It never requests `meta=*`
for enrichment or sends all other applications' values automatically.

`miniflux.enrichment_mode` accepts only:

* `legacy_content`: existing content injection and configured agents, preserved
  for staged rollout. This is the backwards-compatible default when omitted;
  both sample configurations set it explicitly.
* `metadata`: only `agents.summary` selection rules are used. Other agents such
  as translation do not run in this mode. Publisher title/content/tags are never
  updated. `style_block` and summary title remain legacy presentation settings;
  configure actual metadata presentation in Miniflux Settings → Meta.

There is no automatic storage fallback. Metadata API failures never trigger
content injection. `ai_news.prompts.article` still controls the summary brief;
the producer appends its strict JSON/rating/topics contract. The process-wide
language policy and LLM concurrency/RPM/retry budgets still apply.

## Producer conventions

Namespace is `miniflux_ai`, owned by the ordinary authenticated Miniflux user:

| Field | Value |
| --- | --- |
| `miniflux_ai.rating` | String matching `1.0`–`10.0`, exactly one decimal; personal relevance per configured criteria |
| `miniflux_ai.summary` | Nonempty summary in configured language |
| `miniflux_ai.topics` | JSON array of strings serialized **as a string** |

Example atomic namespace merge:

```json
{"rating":"8.5","summary":"The release adds a documented caching mechanism.","topics":"[\"React\",\"TypeScript\"]"}
```

Topic labels are trimmed, case-sensitive, exact duplicates removed, and sorted
by Unicode codepoint. Serialization is compact JSON, UTF-8, without ASCII
escaping (`["React","TypeScript"]`). This is unambiguous even when a label
contains commas or quotes. Native publisher tags are the primary classification
input and, when present, determine topic labels. When native tags are absent or
empty, the model must infer at least one topic. Native tags are never rewritten;
their original spelling/language remains on the entry.

Generated responses must contain exactly `summary`, string `rating`, and a
string array `topics`. Invalid JSON, wrong types, extra/missing fields, invalid
ratings, empty summaries, NUL/invalid UTF-8 and oversized batches are rejected.
Ratings are never clamped, rounded, or silently repaired. The generic Miniflux
backend has no knowledge of this numeric convention.

## Registration, discovery and compatibility

Each polling cycle performs readiness before article processing. Fresh webhook
clients/restarts also run readiness; workers share a lock and a five-minute
positive readiness window on their client. Readiness:

1. Lists the namespace registry using ordinary service credentials.
2. Registers rating, summary and topics idempotently (201 new / 200 existing).
3. Performs a named projection to validate producer and selected input fields.
4. Sends an empty namespace PATCH on an owned entry to verify write support.
   The actual fork treats this as a no-op: no values, content, or timestamps change.
   If there are no entries yet, the write probe is deferred until an entry exists,
   before its generation request.

Definitions are not cached on disk. Re-registration handles restarts, concurrent
instances, credential changes and registry/database replacement. Descriptions
document conventions; repeated registration preserves canonical descriptions and
does not edit them. Unregistered-field errors invalidate readiness so the next
attempt registers again. Failed readiness is briefly cached (30 seconds) to
avoid worker fan-out. Known write outages also stop new generation during that
window; already-running generation can finish into durable local state.

Discovery uses ordinary authenticated `/v1/entries?status=unread`, paginates in
entry-ID order, and projects selected fields without output filters. The web
Unread shortcut's saved filters are never used. Missing output assignments do
not exclude articles. Metadata changes do not advance `changed_at`; no
changed-since synchronization is used. An unread list changing while paginating
can defer an article until the next ordinary poll.

## Persistence, digest queue and retries

The same `digest.sqlite3` remains the only database. Initialization adds an
`enrichments` outbox table; existing records, digest rows and import state are
preserved. Entry locks still coordinate threads/processes sharing the same local
state directory. Keep that directory associated with its original Miniflux
user/database; entry IDs alone are not globally unique across installations.

For each eligible new article:

1. Read original stored title/content, native tags and explicitly selected metadata.
2. Generate and validate the complete result.
3. Save values and source-linked digest record in the outbox (`pending`).
4. PATCH all three strings atomically in `miniflux_ai`.
5. In one SQLite transaction, mark the outbox `complete` and insert the digest
   record using the existing deduplication key.

Generation alone is not completion. A failed batch contributes no digest record.
After a crash between remote success and local completion, the saved batch is
PATCHed again without another model call. PATCH preserves omitted values and
other namespaces. Pending saved batches are retried on polling independently of
unread status, selection-rule changes and `changed_at`. Digest generation keeps
using retained local summaries, source IDs and source URLs; it never needs
summaries embedded in content. Digest publication, RSS storage, weather and
scheduling remain the existing pipeline.

HTTP failure behavior:

* `400`: validation/registration/configuration failure; no immediate HTTP retry,
  no completion or partial-success assumption. Fix configuration/registry; the
  saved batch remains pending for a later readiness/polling attempt.
* `401` / `403`: authentication/access failure; no immediate HTTP retry. Restore
  credentials/access; pending batches are retained.
* Article `404`: deleted/inaccessible article. Saved work is retained as
  `unavailable`, not completed or included in a digest; it is not automatically
  retried indefinitely. There is no automated reassignment/deletion tool.
* Missing metadata endpoints (`404` / `405` / `501` during capability checks):
  actionable fork incompatibility error. Generation is blocked.
* `429`, temporary `5xx`, transport failures: bounded `miniflux.retries` (0–5)
  per operation with existing exponential delays (2, 4, … seconds, capped at 30).
  Saved work remains pending if attempts are exhausted; the next scheduled poll
  can retry. A malformed generation is never saved as a successful result.

Diagnostics report operation, entry ID, qualified names, status, retryability and
controlled error category. Successful merges report field count. Article text,
values, personal criteria, prompts, raw server messages and headers are not logged
by the metadata path.

Completed local records, legacy content-injection markers and articles with all
three existing output assignments are cutover boundaries: no reset, stripping,
backfill or forced regeneration. Partial assignments on an otherwise eligible
article may be completed by the normal producer. Separate instances using
separate state volumes can race generation; atomic namespace merging remains
safe, but LLM deduplication requires the existing shared local state/locks.

## Staged rollout order (separately authorized operations)

1. Make the metadata-enabled **Go Miniflux fork** available first, with database
   backup and version/digest pinning. Use its own image identity, e.g.
   `sintell/miniflux-meta:<version>`, never the companion's identity.
2. Verify the fork's registry, projection and empty-PATCH support with the intended
   Miniflux service user. Keep the companion in `legacy_content` until this passes.
3. Make this companion build available as a versioned `sintell/miniflux-ai` image;
   retain its current state directory and queue/digest data.
4. Supply your actual relevance criteria, selected input fields, language and
   summary selection rules; set `enrichment_mode: metadata` and restart the companion.
5. Verify a newly eligible article: all three string values stored, original
   title/content/tags unchanged, source-linked local digest summary available.
6. Configure Settings → Meta title badges, content headers and optional Unread
   filters. Confirm unprocessed entries remain discoverable through the API.

Publishing a companion image does not deploy either service, modify production,
change subscriptions, stop Newscope, deduplicate entries or perform cutover.
Reverting to legacy mode explicitly re-enables content writes for eligible
articles; switching storage modes is an intentional operational decision.

### Later one-shot script

Separately plan an explicit, backed-up conversion of older injected summaries and
older enrichment. It must identify legacy markers/content forms, preserve original
publisher material/source IDs, decide rating criteria and native/inferred topic
handling, be restartable, and avoid resetting digest completion or reenqueuing
already-published sources. Subscription cutover, duplicate handling and Newscope
retirement remain separate decisions. No historical data conversion is included here.

## Verification

Run mocked contract, processing, queue/digest, weather and existing regression tests:

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Real-fork interoperability is opt-in. Build the actual Go checkout, then give the
test an **empty disposable** loopback database ending in `_test`. This test starts
and stops the fork itself, serves a local RSS fixture, mocks all LLM responses and
weather, and uses a temporary companion SQLite database. It checks registration,
projection, native fields/tags, omitted and other-namespace values, atomic rejection,
authentication/not-found errors, remote-write crash replay and digest source links.
The database remains owned by the caller; discard the container afterward.

Example **local development** commands (adjust absolute paths):

```sh
# Run in the Go fork checkout; /tmp already exists.
go build -o /tmp/miniflux-metadata-test .

docker run --detach --rm --name miniflux-ai-metadata-test-db \
  --publish 127.0.0.1:55441:5432 \
  --env POSTGRES_PASSWORD=companion-test-only \
  --env POSTGRES_DB=companion_metadata_test postgres:17

# Run in the Python companion checkout:
MINIFLUX_METADATA_TEST_BINARY=/tmp/miniflux-metadata-test \
TEST_META_DATABASE_URL='postgres://postgres:companion-test-only@127.0.0.1:55441/companion_metadata_test?sslmode=disable' \
  .venv/bin/python -m unittest discover -s tests -p test_metadata_integration.py -v

docker stop miniflux-ai-metadata-test-db
```

Do not substitute a production connection string. Local interoperability tests
are not evidence of production deployment, reader layout or NAS performance.
