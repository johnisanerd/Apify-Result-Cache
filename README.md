# Apify Result Cache

A shared result cache for [Apify](https://apify.com/?fpr=9n7kx3) Actors: serve a repeat
request from your own store instead of paying to scrape it again.

Scraping the same thing twice costs the same both times. For Actors that fetch through
residential proxy, a repeat request can cost 10x more to scrape than to serve from a
cache. This library gives every Actor in a fleet the same cache: one index, one bucket,
one set of rules, enabled per Actor with an environment variable.

**It measures before it serves.** Version 0.1 only records request keys, so you can find
out how often the same request really comes back before building anything that changes
an Actor's output. Serving from cache arrives in 0.2.

## How it works

```
run starts ──> RESULT_CACHE_MODE set? ──no──> inert: one log line, zero overhead
                        │yes
                        ▼
       keys (0.1):  per request, log(key)  ──> buffered, flushed in the background
       serve (0.2): lookup_many(keys) once ──> hit: payload from the bucket, no scrape
                                           └─> miss: scrape as today, then put(key, payload)
```

- **Index** in Postgres (one small row per cached key) behind `SECURITY DEFINER` RPCs.
- **Payloads** (0.2) in an S3-compatible bucket, gzipped, sha256-verified on read. The S3
  protocol is used from day one so the bucket can move to any S3 endpoint by changing
  one variable.
- **Request log** (0.1 onward): one row per request, so the hit rate is a SQL query.

## Install

**[INSTALL.md](INSTALL.md) is the step-by-step checklist for adding this to an Actor.**
**[ROLLOUT.md](ROLLOUT.md) lists which Actors have it and in which mode.** The short version:

```toml
dependencies = [
    "apify-result-cache",
]

[tool.uv.sources]
apify-result-cache = { url = "https://github.com/johnisanerd/Apify-Result-Cache/archive/refs/tags/v0.1.1.tar.gz" }
```

Then `uv lock`. The tarball form needs no `git` binary inside the Actor image.

## Use

```python
from apify_result_cache import ResultCache, youtube_key_fields

async def main() -> None:
    async with Actor:
        cache = await ResultCache.start(namespace="youtube-transcript", schema_version=1)
        try:
            for url in urls:
                vid = extract_video_id(url)
                key = cache.key(youtube_key_fields(
                    video_id=vid, languages=langs, transcript_type=kind,
                    translate_to=target, preserve_formatting=keep_fmt,
                ))
                cache.log(key, entity_id=vid, outcome="logged")
                ...   # fetch, push, charge exactly as before
        finally:
            await cache.close()   # sends what is buffered, within ~5 s, never raises
```

| Call | 0.1 behaviour |
| --- | --- |
| `await ResultCache.start(namespace, schema_version)` | Reads the environment, logs the mode or why it is inert. No network call. Never raises. |
| `cache.key(fields)` | sha256 of the canonical JSON of `fields` plus `ns` and `v`. Works in every mode. |
| `cache.log(key, entity_id, outcome)` | Synchronous, O(1). Outcomes: `logged`, `hit`, `miss`, `bypass`, `expired`, `error`. |
| `await cache.lookup_many(keys, max_age_days)` | Always `{}`. |
| `await cache.get_blob(entry)` | Always `None`. |
| `cache.put(key, entity_id=..., payload=...)` | No-op. |
| `await cache.close()` | Drains the buffer within a 5 s budget, logs one summary line, never raises. |

Properties: `mode` (`inert` / `keys` / `serve`), `active`, `inert_reason`, `hit_event`,
`ttl_days`, `stats`.

### Namespaces and keys

The namespace is the only thing an Actor declares. Every table row and blob path is
scoped by it, so one index and one bucket serve the whole fleet. Pick the fields that
identify a request (not the whole input: output formats and metadata joins are derived
at serve time) and pass them to `key()`. A normaliser module per source keeps Phase 0
and Phase 1 keys identical; `youtube_key_fields` is the first one and the template.

## Configuration

Set on the Actor (Console → Settings → Environment variables), then **rebuild**: Apify
bakes env vars into the build image.

| Variable | Secret | Default | Meaning |
| --- | --- | --- | --- |
| `RESULT_CACHE_MODE` | no | none (inert) | `off`, `keys` (record only), `serve` (0.2; runs as `keys` on 0.1). |
| `RESULT_CACHE_TTL_DAYS` | no | `90` | Expiry for cached payloads (1-365). |
| `RESULT_CACHE_HIT_EVENT` | no | the Actor's default | Pay-per-event name to charge on a hit. |
| `RESULT_CACHE_INDEX_URL` / `RESULT_CACHE_INDEX_KEY` | key yes | none | The cache's own project: its API URL and publishable key. Required whenever a mode is set. The free-tier limiter's `SUPABASE_URL` / `SUPABASE_KEY` are never used. |
| `RESULT_CACHE_S3_ENDPOINT` / `_REGION` / `_BUCKET` | no | unset | 0.2. |
| `RESULT_CACHE_S3_ACCESS_KEY` / `_SECRET_KEY` | yes | unset | 0.2. |
| `RESULT_CACHE_FORCE` | no | unset | `1` ignores the "not on the Apify platform" gate, for a local check against the real index. |
| `RESULT_CACHE_DEBUG` | no | unset | `1` logs which variables are visible. Never prints a secret. |

`APIFY_USER_ID`, `APIFY_ACTOR_ID` and `APIFY_USER_IS_PAYING` come from the platform. The
user id is stored only as a sha256.

## Failure behaviour: permissive, on purpose

An outage on our side must never break someone else's run. Every failure logs once and
the run continues with its normal output:

- Missing configuration: inert, with the reason in the log.
- Index unreachable: the batch is dropped (never re-sent, so a timeout that actually
  committed cannot double-count), one warning, and after three consecutive failures the
  cache switches itself off for the rest of the run.
- `close()` waits at most 5 s in total and never raises.
- Log lines never name the backend and never carry a URL or key; exceptions are reduced
  to their type name or HTTP status.

## Retention

| Data | Limit | Enforced by |
| --- | --- | --- |
| Cached payloads (0.2) | `RESULT_CACHE_TTL_DAYS` (default 90, max 365), and per request `maxAgeDays` | `cache_put` / `cache_lookup`; physical deletion by `scripts/cache_gc.py` |
| Request log | 180 days | `scripts/cache_gc.py` |

## Operator scripts

Both need the index project's URL and service key (`RESULT_CACHE_INDEX_URL`, `RESULT_CACHE_SERVICE_KEY`), in this
repo's git-ignored `.env` or exported. The publishable key in Actor images cannot read aggregates.

```bash
uv run python scripts/cache_stats.py --from 2026-10-01 --to 2026-10-14 --exclude-user <APIFY_USER_ID>
uv run python scripts/cache_stats.py --quota
uv run python scripts/cache_gc.py
```

`--quota` compares database size, cached payload bytes and estimated egress against the
plan's included quotas (WARN at 80%, ERROR at 95%) and reports the last day's store-error
share. It is meant for a weekly scheduled check, because the project runs with the spend
cap on and a quota is a ceiling, not a bill.

## Database

`migrations/0003_result_cache.sql` creates `result_cache_requests`, `result_cache_index`
and six functions. Since 0.1.1 the index lives in its own project, where this is the only
migration. It is numbered 0003 because 0.1.0 put the tables next to the free-tier ledger's
two migrations, and that copy is still there until every install is on 0.1.1. RLS is on with zero policies and direct grants are revoked: the anon key
can execute `cache_log`, `cache_lookup` and `cache_put` and nothing else. `cache_stats`,
`cache_quota` and `cache_gc_requests` are service-role only.

### The Security Advisor warnings are expected

Three warnings that anon can execute `SECURITY DEFINER` functions (that is the access
model) and two notices that RLS is enabled with no policies (that is the lock).

## Tests

```bash
uv sync --extra dev && uv run pytest
```

Integration tests against a real project are opt-in and use a throwaway namespace:

```bash
RESULT_CACHE_INDEX_URL=... RESULT_CACHE_INDEX_KEY=... RESULT_CACHE_SERVICE_KEY=... uv run pytest tests/test_integration.py
```

The anon key cannot delete, so purge test rows afterwards from the SQL editor:

```sql
delete from public.result_cache_requests where namespace like 'test-%';
delete from public.result_cache_index    where namespace like 'test-%';
```

## Releasing

Bump the version in `pyproject.toml`, `src/apify_result_cache/_version.py`, and the tarball
pin in README.md and INSTALL.md (`tests/test_version.py` fails if any disagree). Commit,
then an **annotated** tag: `git tag -a vX.Y.Z -m "vX.Y.Z - <what>"` and push it.

## Roadmap

- **0.2 (Phase 1):** real `lookup_many` / `get_blob` / `put` over S3 (boto3 in an executor),
  blob expiry in `cache_gc.py`, `error` outcomes on bucket failures.
- **Phase 2:** point `RESULT_CACHE_S3_ENDPOINT` at a self-hosted S3 server, keep the old
  bucket as a read fallback for one TTL. The index stays where it is.

## License

MIT
