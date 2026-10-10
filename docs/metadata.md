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

## Rule-aware article text for thin feeds

Metadata enrichment retrieves original-article content before generation when
feed text is thin. Hacker News and other link feeds often supply only a title,
article URL, comments URL and counters. The model has no browsing tool; the
companion supplies article text through Miniflux's own extraction pipeline.

```yaml
article_fetch:
  mode: thin # "off", thin, or always; default thin
  min_feed_chars: 500
  min_article_chars: 200
  timeout: 30
  max_bytes: 2097152
  max_text_chars: 20000
  retries: 1
```

`thin` reuses stored content when it already contains sufficient visible text.
The length check excludes link-only anchors and HN-style Article URL, Comments
URL, Points and comment-count paragraphs. `always` requests a preview for every
otherwise eligible new enrichment; `"off"` uses stored content only. This applies
to metadata mode and preserves the intentional legacy-content path.

The preview request is explicitly read-only:

```http
GET /v1/entries/123/fetch-content?update_content=false
```

The inspected server handler loads the entry's feed and user, then uses the same
`ProcessEntryWebPage` pipeline as Miniflux's Fetch original content action:
feed-specific scraper rules, URL rewrites, built-in extraction rules, content
rewrites, configured user agent/cookies/proxy/TLS settings, and sanitizer. It
persists title/content only when `update_content=true`, which this client never
sends. Reading time is returned for the preview, without changing stored entry
fields or `changed_at`. The companion does not copy feed rule implementations,
cookies/passwords/proxy settings into Python or send them to the model.

This gives matching extraction semantics, not a promise of byte-identical content
across different fetch times. HTML is converted to Markdown and bounded for model
input. If Miniflux already fetched full content during normal feed ingestion
(`crawler` enabled), `thin` normally uses the stored content directly. Enabling
feed crawling or changing its rules is a separate Miniflux feed-setting decision.

Publisher fetching, redirects, network access and TLS policies remain controlled
by Miniflux. The companion bounds the preview API timeout, decoded JSON response
size and extracted-text budget, and retries transport/429/5xx failures according
to its bounded API policy. It does not crawl references/comments or add browser,
JavaScript or PDF processing capabilities beyond Miniflux's existing scraper.
An error or insufficient preview text prevents generation and completion/digest
enqueue. This also catches the server returning the original thin content after
an empty extraction. Unread entries remain eligible for a later ordinary poll.

Generation receives preview text as primary `content`, `source.kind:
miniflux_article` with the native entry URL/truncation flag, and compact original
`feed_content` for context. Original title, native tags and selected metadata
keep their roles. The prompt prioritizes article evidence over wrapper counters,
and personal relevance remains tied to configured criteria rather than article
availability. `max_text_chars` and `llm.max_length` bound the excerpt, with explicit
truncation context. Summary language and strict JSON/rating validation still apply.

Publisher fields, native entry URLs/IDs and existing digest source links remain
unchanged. Saved validated results retry metadata persistence without refetching
or regenerating. Existing completed enrichments—including old stub summaries—
are not automatically revisited or reset.

This feature requires companion version 1.1.2 or newer; published 1.1.1
ignores `article_fetch`. Mocked tests cover read-only request parameters, limits
and retries; isolated real-fork tests exercise scraper/rewrite rules and unchanged
stored fields. All generation is mocked; development verification modifies no
production article/queue and consumes no paid LLM requests.

## Discovery interval and publisher quotations

`miniflux.schedule_interval` is measured in **minutes**, not seconds. Set it to
`1` for one-minute discovery; `60` means hourly. The worker checks immediately
on startup and skips scheduled checks while its previous discovery pass is still
running. Restart the worker after changing its configuration.

Ordinary discovery selects unread entries only. Marking an article read before
discovery can prevent enrichment; saved pending results still retry independently
of read status. Completed metadata is not regenerated on subsequent polls.

Starting with 1.1.2, metadata selection accepts publisher content beginning with
`<blockquote>`, even when an agent has `style_block: true`. That generic prefix is
only an old legacy-content heuristic, not proof of metadata completion. Explicit
legacy markers, configured title prefixes, allow/deny rules, internal-feed
exclusions, local completion records and complete metadata assignments still
apply.

## LLM endpoint and failure diagnostics

`llm.api` explicitly selects `chat_completions` (default) or `responses` for the
OpenAI provider. It does not select the metadata storage mode, and there is no
automatic endpoint fallback. Gemini continues using its own generation API.

The NAS Codex gateway v0.2.1's Chat adapter can turn an upstream failed response
into HTTP 200 with empty message content, dropping the backend error/status.
This is a known diagnostic gap, not proof of a particular cause for an empty
response. For a compatible gateway, the native Responses option preserves that
status so the companion can reject it with useful controlled diagnostics:

```yaml
llm:
  provider: openai
  api: responses
  extra_params:
    reasoning_effort: low # Mapped to Responses reasoning.effort
```

Other `extra_params` must match the selected API. Structured generation defaults
to `llm.structured_output: json_object` on either endpoint. If a model rejects
the API format option (`response_format` / gateway `text.format`), explicitly use:

```yaml
llm:
  structured_output: prompt
```

Prompt-only mode retains the JSON contract in the system prompt and preserves
structured source-message boundaries; it omits the API JSON-format parameter.
The same strict local JSON/rating/digest validation still applies: non-JSON output
is rejected, never repaired or marked complete. Remove any `response_format`,
`text.format` or `response_mime_type` overrides from `extra_params`; conflicting
options are rejected before generation. There is no automatic switch to this mode.
This setting and native Responses support require companion version 1.1.1 or
newer; setting these keys on 1.1.0 does not change its requests. The NAS synthetic
probe with `gpt-6-luna` rejected API JSON mode with HTTP 400 naming `text.format`,
then returned valid summary/rating/topics JSON when that option was omitted.
The English Codex example therefore explicitly selects `prompt`. This probe is
evidence for request compatibility, not full-article or production-processing success.

A request rejection does not silently remove options or regenerate using
another endpoint. Failed/incomplete, tool-only, refusal and empty responses are
not successful enrichment. Transient HTTP/transport retries retain the existing
shared request budget and bounded backoff; empty/failed response objects do not
automatically consume extra generation requests.

HTTP error logs include status, endpoint and allowlisted provider codes/parameter
names. Empty-response logs include finish reason and token/choice counts, without
printing text, refusals or tool arguments. Native Responses errors also include
backend status and output/reasoning-item counts. Arbitrary provider messages,
codes, parameters, headers and credentials are withheld. Unknown errors still
require matching gateway logs or an explicitly run synthetic diagnostic probe;
do not assume JSON mode is unsupported solely from a generic HTTP 400.

## Producer field conventions

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
