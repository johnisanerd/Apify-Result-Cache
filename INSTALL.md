# Adding the result cache to an Actor

Six steps. The library is inert until `RESULT_CACHE_MODE` is set, so steps 1-3 can ship
on their own without changing anything a customer sees.

> **Index values.** The index URL and key are not in this public repo. They belong to the
> cache's own Supabase project, `apify-result-cache`: its API URL and its publishable key
> (Project Settings → API). The free-tier limiter's `SUPABASE_URL` / `SUPABASE_KEY` are a
> different project and are never read by the cache.

---

## 0. Pre-flight

- **No env-var collision to check.** The cache reads only `RESULT_CACHE_*` names, so an Actor's
  own database variables cannot clash with it.

- **Decide the namespace and the key.** Namespace: `^[a-z0-9][a-z0-9-]{0,63}$`, one per
  kind of result (e.g. `youtube-transcript`, `crunchbase-company`). Key: the fields that
  identify a request, and nothing that is derived on the way out (output formats,
  metadata joins, pagination of the result). If the fields need cleaning (defaults,
  case, blank-vs-missing), write a normaliser module next to `youtube.py` so Phase 0 and
  Phase 1 hash identically.
- **Decide what is cacheable.** Never error rows, never partial results, never a charged
  add-on that the customer expects fresh.
- **Add `namespaces/<namespace>.json`** in this repo with any example IDs that free "try
  it" traffic repeats, so the hit-rate gate can exclude them.

## 1. Add the dependency

```toml
dependencies = [
    "apify-result-cache",        # <- add
]

[tool.uv.sources]
apify-result-cache = { url = "https://github.com/johnisanerd/Apify-Result-Cache/archive/refs/tags/v0.4.0.tar.gz" }
```

```bash
uv lock
grep -E "COPY (requirements.txt|pyproject.toml)" Dockerfile
```

If the Dockerfile copies `requirements.txt`, regenerate it too, or the build silently
installs nothing new:

```bash
uv export --no-hashes --format requirements-txt > requirements.txt
```

The diff should add the `apify-result-cache @ https://...` line and change no existing pin.

## 2. Wire it in

```python
from apify_result_cache import ResultCache

cache = await ResultCache.start(namespace="<namespace>", schema_version=1)   # after the free-tier guard
try:
    ...
    # per request, after validation and before fetching:
    cache.log(cache.key(fields), entity_id=<id>, outcome="logged")
    ...
finally:
    await guard.close()
    await cache.close()
```

`log()` must be called from the event-loop thread (not from inside `run_in_executor`).
Skip requests that can never be cached (list-only calls, listings).

## 3. Test locally against the real index (optional)

```bash
RESULT_CACHE_MODE=keys RESULT_CACHE_FORCE=1 APIFY_USER_ID=local-test APIFY_ACTOR_ID=local-test \
RESULT_CACHE_INDEX_URL=... RESULT_CACHE_INDEX_KEY=... apify run
```

Expect `Result cache: mode keys ...` and `Result cache: recorded N of N request key(s)`.
Delete the rows afterwards (`delete from result_cache_requests where actor_id = 'local-test';`).

## 4. Turn it on

Console → Actor → Source → the version → Environment variables: add

| Variable | Secret | Value |
| --- | --- | --- |
| `RESULT_CACHE_MODE` | no | `keys` to measure, `serve` to serve |
| `RESULT_CACHE_INDEX_URL` | no | the cache project's API URL |
| `RESULT_CACHE_INDEX_KEY` | **yes** | the cache project's publishable key |
| `RESULT_CACHE_S3_ENDPOINT` | no | serve only: `https://<ref>.storage.supabase.co/storage/v1/s3` |
| `RESULT_CACHE_S3_ACCESS_KEY` | **yes** | serve only: S3 access key id from the project's storage settings |
| `RESULT_CACHE_S3_SECRET_KEY` | **yes** | serve only: its secret |

**Rebuild.** Env vars reach the Actor only through a new build.

### Serving: what the Actor must do

1. Before its videos start, compute every request's key and call `lookup_many()` once.
2. Per request, log exactly one outcome: `hit` if served from storage, `miss` if fetched
   fresh, `bypass` if the caller asked for fresh (max age 0), `error` if the entry was
   listed but `get_blob()` / `get_blobs()` returned nothing for it (fetch fresh), and
   `failed` if the fresh fetch ended in a permanent source error (nothing to cache; the
   library records it as `miss` until `RESULT_CACHE_LOG_FAILED=1` is set on a version
   whose index has migration 0005).
3. On a fresh fetch, snapshot the payload before building the row and `put()` it after
   the row is pushed. Never store errors, partial results or charged add-ons.
4. Rebuild anything derived (formats, joins) from the stored payload so a served row is
   identical to a fresh one; the codec keeps key order for exactly this reason.
5. Add `cached` / `fetched_at` (and `cache_age_days` on hits) to every row in serve mode.

## 5. Verify

| Run | Expect in the log |
| --- | --- |
| Owner run | `Result cache: mode keys ...` then `recorded N of N request key(s)` |
| Forced-free run | recorded count equals processed count |
| Non-owner account | rows with a different user hash |
| Wrong key on a side version | `recording unavailable (HTTP 401); continuing without it`, run SUCCEEDED |

| Log line | Meaning | Fix |
| --- | --- | --- |
| `inert (RESULT_CACHE_MODE not set)` | Not enabled, or the build predates the variable | Set it, rebuild |
| `inert (not configured: ...)` | Index URL/key missing on this version | Add `RESULT_CACHE_INDEX_URL` / `RESULT_CACHE_INDEX_KEY`, rebuild |
| `inert (run identity unavailable)` | Platform variables missing | Should not happen on-platform; check `RESULT_CACHE_DEBUG=1` |
| `recording unavailable (...)` | Index unreachable or refusing | Check the project; runs are unaffected |
| no `Result cache:` line at all | The library is not in the image | Step 1 Dockerfile check |

## 6. Record it

Add a row to [ROLLOUT.md](ROLLOUT.md), and remind the owner to prune the builds that
predate the install.


## Per-namespace retention (0.3.0)

`ResultCache.start(namespace, schema_version, ttl_days=7)` keeps that namespace's
results for 7 days. One Actor can run several namespaces with different
retention. `RESULT_CACHE_TTL_DAYS`, when set, is the ceiling for every
namespace in that build; unset, the range is 1..365. TTL is whole days; for
anything shorter, put a time bucket in the key fields (e.g. 6-hour buckets).

## Removing one entity (0.3.0, needs migration 0006)

`scripts/purge_entity.py --entity <id> --namespaces a,b,c` deletes every live
index row for that entity id in those namespaces (served nowhere from that
moment), deletes the payload objects, and blanks the id in the request log.
Use it for opt-out and removal requests about a person.
