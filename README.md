# Apify Result Cache

A shared result cache for [Apify](https://apify.com/?fpr=9n7kx3) Actors: serve a repeat
request from your own store instead of paying to scrape it again.

Scraping the same thing twice costs the same both times. For Actors that fetch through
residential proxy, a repeat request can cost 10x more to scrape than to serve from a
cache. This library gives every Actor in a fleet the same cache: one index, one bucket,
one set of rules, enabled per Actor with an environment variable.

**Two modes.** `keys` only records request keys, so you can measure how often the same
request really comes back without changing an Actor's output. `serve` looks each request
up, returns a hit from storage instead of scraping, and stores every fresh result for next
time.

## How it works

```
run starts ──> RESULT_CACHE_MODE set? ──no──> inert: one log line, zero overhead
                        │yes
                        ▼
       keys:   per request, log(key, "logged")  ──> buffered, flushed in the background
       serve:  lookup_many(keys) once, then per request:
                 hit   ──> get_blob(entry): payload from storage, no scrape, log "hit"
                 miss  ──> scrape as usual, put(key, payload), log "miss"
                 max age 0 ──> scrape, put, log "bypass"
```

- **Index** in Postgres (one small row per cached key) behind `SECURITY DEFINER` RPCs.
- **Payloads** in an S3-compatible bucket, gzipped, sha256-verified on read. The library
  signs S3 requests itself (Signature V4 over httpx, no boto3), so it stays light enough
  for small Actors, and the bucket can move to any S3 endpoint by changing one variable.
- **Request log** (0.1 onward): one row per request, so the hit rate is a SQL query.

## Install

**[INSTALL.md](INSTALL.md) is the step-by-step checklist for adding this to an Actor.**
**[ROLLOUT.md](ROLLOUT.md) lists which Actors have it and in which mode.** The short version:

```toml
dependencies = [
    "apify-result-cache",
]

[tool.uv.sources]
apify-result-cache = { url = "https://github.com/johnisanerd/Apify-Result-Cache/archive/refs/tags/v0.3.0.tar.gz" }
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

| Call | Behaviour |
| --- | --- |
| `await ResultCache.start(namespace, schema_version, hit_event_default=, fresh_event_default=)` | Reads the environment, logs the mode or why it is inert. No network call. Never raises. |
| `cache.key(fields)` | sha256 of the canonical JSON of `fields` plus `ns` and `v`. Works in every mode. |
| `cache.log(key, entity_id, outcome)` | Synchronous, O(1). Call exactly once per request. Outcomes: `logged`, `hit`, `miss`, `bypass`, `expired`, `error`, `failed` (a miss whose fetch ended in a permanent source error; recorded as `miss` unless `RESULT_CACHE_LOG_FAILED=1`). |
| `await cache.lookup_many(keys, max_age_days)` | Serve mode: one RPC per 100 keys, returns `{key: CacheEntry}` fetched within `max_age_days`. `{}` otherwise, on `0`, and on any failure. |
| `await cache.get_blob(entry)` | Serve mode: the stored payload, digest-verified and read only from the key's own object path. `None` on any failure or on a row that names another object (treat as a miss). |
| `await cache.get_blobs(entries)` | Serve mode: `get_blob()` for every hit at once, 16 reads in flight, keyed by `key_hash`. Call it right after `lookup_many()` so no item waits on storage. |
| `cache.put(key, entity_id=..., payload=...)` | Serve mode: encodes now, uploads in the background (three at a time), then indexes. A key already queued or stored this run is skipped. Never raises. |
| `await cache.close()` | Drains the log and pending uploads within a ~6 s budget, logs one summary line, never raises. |

Properties: `mode` (`inert` / `keys` / `serve`), `active`, `serving`, `inert_reason`,
`hit_event`, `fresh_event`, `ttl_days`, `stats`. `CacheEntry.age_days()` gives the age of a hit.

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
| `RESULT_CACHE_MODE` | no | none (inert) | `off`, `keys` (record only), `serve` (look up, serve hits, store fresh results). `serve` without the storage settings records keys only, with a warning. |
| `RESULT_CACHE_TTL_DAYS` | no | `90` | Expiry for stored results (1-365). |
| `RESULT_CACHE_HIT_EVENT` | no | the Actor's default | Pay-per-event name to charge on a hit. |
| `RESULT_CACHE_FRESH_EVENT` | no | the Actor's default | Extra event for a forced fresh fetch; the Actor charges it only once it is priced. |
| `RESULT_CACHE_INDEX_URL` / `RESULT_CACHE_INDEX_KEY` | key yes | none | The cache's own project: its API URL and publishable key. Required whenever a mode is set. The free-tier limiter's `SUPABASE_URL` / `SUPABASE_KEY` are never used. |
| `RESULT_CACHE_S3_ENDPOINT` | no | unset | Serve mode. For Supabase: `https://<ref>.storage.supabase.co/storage/v1/s3`. |
| `RESULT_CACHE_S3_REGION` / `RESULT_CACHE_S3_BUCKET` | no | `us-east-1` / `result-cache` | Serve mode. |
| `RESULT_CACHE_S3_ACCESS_KEY` / `RESULT_CACHE_S3_SECRET_KEY` | **yes** | unset | Serve mode. S3 access keys from the cache project's storage settings. They bypass storage policies, so treat them like any server secret. |
| `RESULT_CACHE_FORCE` | no | unset | `1` ignores the "not on the Apify platform" gate, for a local check against the real index. |
| `RESULT_CACHE_DEBUG` | no | unset | `1` logs which variables are visible. Never prints a secret. |
| `RESULT_CACHE_LOG_FAILED` | no | unset | `1` records the `failed` outcome as such (needs migration 0005 on the index); otherwise it is recorded as `miss`. |

`APIFY_USER_ID`, `APIFY_ACTOR_ID` and `APIFY_USER_IS_PAYING` come from the platform. The
user id is stored only as a sha256.

## Failure behaviour: permissive, on purpose

An outage on our side must never break someone else's run. Every failure logs once and
the run continues with its normal output:

- Missing configuration: inert, with the reason in the log.
- Index unreachable: the batch is dropped (never re-sent, so a timeout that actually
  committed cannot double-count), one warning, and after three consecutive failures the
  cache switches itself off for the rest of the run.
- Lookup fails: every request is a miss. Storage fails: that request is a miss, and after
  three consecutive storage failures serving stops for the run (keys are still recorded).
  A stored object that fails its digest check is never served.
- `close()` waits at most ~6 s in total and never raises.
- Log lines never name the backend and never carry a URL or key; exceptions are reduced
  to their type name or HTTP status.

## Retention

| Data | Limit | Enforced by |
| --- | --- | --- |
| Stored results | `RESULT_CACHE_TTL_DAYS` (default 90, max 365), and per request the Actor's max-age input | `cache_put` / `cache_lookup`; physical deletion by `scripts/cache_gc.py` |
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
and six functions; `migrations/0004_result_cache_serve.sql` adds `cache_outcomes` (the
real hit rate), `cache_expired` and `cache_delete_index` (for the expiry job), all
service-role only, and the private `result-cache` bucket; `migrations/0005_hardening_and_measurement.sql`
(0.2.2) makes `cache_put` accept only the key's own object path, lets `cache_log` record `failed`, and adds
`cache_fragmentation` (service-role). Since 0.1.1 the index lives in its own project, where these are the only
migrations. It is numbered 0003 because 0.1.0 put the tables next to the free-tier ledger's
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

- **Phase 2:** point `RESULT_CACHE_S3_ENDPOINT` at a self-hosted S3 server, keep the old
  bucket as a read fallback for one TTL. The index stays where it is.

## License

MIT
