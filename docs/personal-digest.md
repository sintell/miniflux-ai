# Personal digest configuration and behavior

Use the existing `config.sample.English.yml` or `config.sample.Chinese.yml` as the
basis for `config.yml`. The English sample contains the configured NAS endpoints;
deployment and migration steps are in [nas-deployment.md](nas-deployment.md).

## Language, editorial settings, and prompts

`language` controls **all newly generated content**, including intermediate article
summaries, other agents, opening text, digest title, section headings, source-link
labels, executive summary, and stories. English is the default. Use a descriptive
language name such as `German`, `Spanish`, or `Japanese`. Original product names,
feed names and source metadata are preserved. Existing saved articles/digests are
not regenerated when the setting changes. Static application/feed-status UI text
is not machine-translated.

`LANGUAGE_POLICY` supplies the configured language at every LLM call. Source data
is placed in a separate user message and is explicitly untrusted. Instructions
require preservation of numbers, versions, qualifications and uncertainty, and
attribution of marketing claims. These are generation instructions, not a claim
that a model can never produce a factual or language error.

Under `ai_news.editorial`:

| Setting | Default | Meaning |
|---|---:|---|
| `at_a_glance_words` | 75 | Approximate executive-summary word target |
| `max_stories` | 8 | Validated maximum number of distinct stories |
| `max_groups` | 3 | Maximum topic groups; 0 disables groups |
| `story_words_min` / `story_words_max` | 40 / 70 | Approximate story length; shorter for thin sources |
| `article_words_min` / `article_words_max` | 80 / 130 | Intermediate summary target |
| `max_records` | 100 | Maximum queue records considered per edition |
| `max_input_chars` | 60000 | Maximum complete JSON input, including context and weather |

Lengths are editorial targets, not padding/truncation rules. Word counts are less
useful for languages without whitespace-separated words; adapt the configurable
prompt if needed. There are no minimum story/group quotas.

`ai_news.prompts.article` and `ai_news.prompts.digest` optionally replace the
defaults in `services/editorial.py`. Placeholders include `${language}` and every
editorial setting, e.g. `${at_a_glance_words}`. For other agents, `agents.<name>.prompt`
remains supported. Legacy `${content}` is accepted but source data is now supplied
separately rather than interpolated into trusted instructions.

**Prompt migration:** the old `ai_news.prompts.greeting`, `summary`, and
`summary_block` are replaced by one `digest` prompt. The summary agent's old
`agents.summary.prompt` is replaced by `ai_news.prompts.article`, fixing the old
three-short-sentences bottleneck. Remove those old keys and copy any intentional
customization into the new prompts. Defaults apply when new keys are absent.

The digest JSON contract is:

```json
{
  "title": "Morning news",
  "labels": {
    "at_a_glance": "At a glance",
    "stories": "The stories",
    "original_article": "Original article",
    "source": "Source"
  },
  "opening": "A short creative, edition-aware opening.",
  "at_a_glance": "One informative paragraph.",
  "stories": [
    {
      "headline": "A specific development",
      "body": "What changed, with supported context and qualifications.",
      "source_ids": ["entry:115", "entry:116"],
      "group": ""
    }
  ]
}
```

All prose/labels use the configured language. Weather belongs exclusively in
`opening`, not the title or story prose. JSON validation requires a nonempty
bounded story list and supplied source IDs; duplicate use of a source across
stories is rejected so coverage is merged. Invalid output preserves the batch
for a future scheduled/manual attempt rather than consuming it.

## Trusted links and input budgets

Each queued record preserves entry/feed IDs, title, URL, summary, date, category,
feed title and useful feed URLs. Model input is JSON with separate records. Models
choose `source_ids`; application code renders links from stored metadata. Model
HTML/Markdown is rendered as escaped text, and link URLs require HTTP(S), a valid
host/port, and no credentials, control characters or backslashes.

`miniflux.base_url` is the internal API endpoint. `miniflux.browser_url` is the
browser-facing base, including any Miniflux installation subpath. New records use
`/feed/{feed_id}/entry/{entry_id}`; ID-only records use `/history/entry/{entry_id}`.
Both upstream handlers retrieve the target without a read-status filter. Sources:
[feed handler](https://github.com/miniflux/v2/blob/main/internal/ui/entry_feed.go),
[history handler](https://github.com/miniflux/v2/blob/main/internal/ui/entry_read.go).
Verify both on the installed Miniflux version, with read and unread entries.
Deleted/purged entries can still become unavailable; original-article links remain.
Legacy records without entry IDs get original links only. Missing/unsafe URLs
produce no corresponding link.

`llm.max_length` now applies only to article text after HTML-to-Markdown conversion.
Structured digest requests bypass that truncation and conversion. Digest input is
bounded by whole records. Overflow stays queued for the next edition. If even the
first record does not fit, generation stops with the queue intact; raise
`max_input_chars` or inspect that record rather than silently losing its metadata.

## Weather and editions

Weather is optional and disabled by default. Configure these under `ai_news.weather`:

- `enabled`: enable only after setting coordinates and timezone.
- `location`: human-readable display name. It is a label, **not a geocoding query**.
- `latitude`, `longitude`: actual forecast coordinates.
- `units`: `metric` (°C, km/h) or `imperial` (°F, mph).
- `timeout`: network timeout in seconds, default 5, maximum 30.
- `max_age_minutes`: maximum elapsed generation time using that fetched forecast,
  default 30. If exceeded, discard the weather-bearing opening and use the localized
  news-only title as the opening; publish the news without another LLM request.
- `rain_probability_threshold`: report precipitation windows meeting this percent,
  default 30. Lower probabilities are still available as window maxima.
- `notable_wind_kmh`: notable-wind threshold, default 30; converted for imperial units.

`ai_news.timezone` is an IANA timezone used for forecast windows, dates, edition
selection and both digest/feed-status schedules. It defaults to UTC, not the
container's previous `TZ`. The existing 07:30, 18:00 and 22:00 schedule is retained.
Change the timezone before deploying. The application includes timezone data for
the Alpine image.

Edition start times are independently configurable in `ai_news.editions`:
morning 05:00, evening 12:00, night 21:00. The last boundary wraps through midnight.
Morning receives today's daily high/low and remaining hourly forecast; evening
receives remaining evening and tomorrow; night receives overnight and the next
morning (06:00–12:00, overnight through 08:00). In early post-midnight night editions,
the next morning is on the same local date. Time windows carry explicit dates,
offsets and units, including DST transitions.

The LLM may creatively style the opening using this context—for example, a cold
and rainy **forecast** can inspire a playful news introduction. It must not portray
forecasts as observations or invent weather when none was supplied. Data is fetched
directly from [Open-Meteo](https://open-meteo.com/), with a bounded response size and
coverage/unit checks. Fetch timestamps are retained separately from forecast valid
times; the API does not provide a model-issuance timestamp here. There is no weather
cache: unavailable/malformed/out-of-window data is omitted. Empty news queues never
produce weather-only publications or invoke weather/LLM calls.

## Environment variables and port

YAML **values** support `${VARIABLE}` and `${VARIABLE:-default}` for uppercase
environment names. Missing variables without defaults raise an error naming only
the variable. Defaults apply to missing or empty variables. Nested mappings/lists
are supported; mapping keys are not expanded. Use `$${VARIABLE}` for a literal
`${VARIABLE}`. Lowercase prompt placeholders remain untouched.

Expansion occurs after YAML parsing. A secret containing quotes, newlines, `#`,
colons or another `${...}` is a literal value, not new YAML or recursive expansion.
Known numeric/boolean settings accept environment strings; booleans use
`true`/`false`. For example:

```yaml
language: ${DIGEST_LANGUAGE:-English}
miniflux:
  api_key: ${MINIFLUX_API_KEY}
llm:
  api_key: ${LLM_API_KEY}
  RPM: ${LLM_RPM:-15}
```

`FLASK_PORT` directly controls the Flask listener, defaults to 80, and must be
1–65535. Both existing samples use it in AI-news/feed-status URLs. Changing the port
also requires updating existing Miniflux subscriptions and webhook/reverse-proxy
targets. Default port 80 is canonicalized to the existing port-less feed URL to
avoid duplicate subscriptions. Restart the container after changing settings or
environment; the LLM runtime is shared and initialized once per process.

## Request limits and state

`llm.max_workers` is a shared in-process concurrent-request ceiling across polling,
webhooks, digest generation and retries. `llm.RPM` limits all request starts in a
rolling minute. This corrects the old per-entry rate limiter and per-webhook-only
worker limits. `llm.retries` defaults to two (three attempts total) with bounded
backoff for transient errors; SDK automatic retries are disabled. The 300-second
timeout is **per attempt**, not the entire edition deadline. The gateway's four
shared slots are still shared with other clients; 503 does not queue a request.

Run one application process/container for this request budget. SQLite and sidecar
file locks coordinate queue writes and overlapping digest generation across local
processes, but request budgets are not distributed across replicas. Polling runs in
a coalesced background worker so a backlog does not monopolize the scheduler.

`state.directory` defaults to `data`; samples use `/app/data`. Keep it on local NAS
storage, not SMB/NFS, and mount the whole directory:

- `digest.sqlite3`: queue, processed source records, immutable published digests,
  import receipts and pending refresh status. SQLite handles transaction atomicity
  and rollback journals; no additional database server is required.
- `*.lock`: stable generation/refresh/entry-processing locks. Do not delete these
  while a process is running.
- `feeds_status.json`: the existing status snapshot, now written by atomic replace.

A digest commits together with consumption of its **considered** input batch.
Editorially omitted weak/repetitive inputs are marked processed, but their records
remain in SQLite for inspection. Overflow and concurrent arrivals remain queued.
An empty/invalid result is not published and leaves the batch queued.

After commit, refresh is a separate best-effort step retried every five minutes and
at startup. Feed matching uses its configured URL, not its mutable title. Refresh
acceptance is not proof Miniflux ingested the digest. RSS GETs are read-only and
serve all retained editions with stable IDs/timestamps; no automatic pruning is
performed. Miniflux's GUID deduplication prevents repeated GETs creating new copies.
Keep normal Miniflux feed polling enabled. Monitor database/feed growth over time.

## Diagnosing failed editions

Since version `1.0.1`, diagnostics identify the failing stage (`input`, `llm`, `validation`,
`rendering` or `storage`), candidate/selected record counts and input/response
character counts. Controlled validation errors include the field path and reason,
such as unknown source IDs, missing localized labels or a story/group limit.
Raw model text, source records, returned ID values and arbitrary exception messages
are not logged; HTTP/SDK exceptions can contain credentials. The queue is preserved
and validation failures do not trigger additional LLM calls automatically.

The original `1.0.0` image logs only the exception type, e.g. `(ValueError)`, and
does not retain rejected LLM responses. That historical line cannot identify the
failed validation rule, and changing log level cannot recover it. A build with
the new diagnostics reports the reason on the next normal generation attempt.

## Running offline tests

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
```

Tests mock LLM/Miniflux/weather calls, use temporary SQLite databases and include
failure/rollback, migration, language/environment interpolation, links, escaping,
freshness, midnight/DST and repeat RSS reads. The old live feed-status test is now
explicitly opt-in with `MINIFLUX_LIVE_TESTS=1`. No live LLM tests run in CI.
