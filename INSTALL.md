# Adding the result cache to an Actor

Six steps. The library is inert until `RESULT_CACHE_MODE` is set, so steps 1-3 can ship
on their own without changing anything a customer sees.

> **Index values.** The index URL and key are not in this public repo. They are the same
> `SUPABASE_URL` / `SUPABASE_KEY` the free-tier limiter uses; copy them from an Actor that
> already has the cap (Console → Settings → Environment variables).

---

## 0. Pre-flight

- **Env-var collision.** Same check as the limiter: if the Actor reads `SUPABASE_URL` or
  `SUPABASE_KEY` for its *own* database, stop and rename the Actor's variables first.

  ```bash
  grep -rnE "SUPABASE_URL|SUPABASE_KEY|create_client|psycopg|DATABASE_URL" <actor>/src/
  ```

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
apify-result-cache = { url = "https://github.com/johnisanerd/Apify-Result-Cache/archive/refs/tags/v0.1.0.tar.gz" }
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
SUPABASE_URL=... SUPABASE_KEY=... apify run
```

Expect `Result cache: mode keys ...` and `Result cache: recorded N of N request key(s)`.
Delete the rows afterwards (`delete from result_cache_requests where actor_id = 'local-test';`).

## 4. Turn it on

Console → Actor → Source → the version → Environment variables: add
`RESULT_CACHE_MODE=keys` (not secret). **Rebuild.** Env vars reach the Actor only through
a new build.

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
| `inert (not configured)` | Index URL/key missing on this version | Add `SUPABASE_URL` / `SUPABASE_KEY`, rebuild |
| `inert (run identity unavailable)` | Platform variables missing | Should not happen on-platform; check `RESULT_CACHE_DEBUG=1` |
| `recording unavailable (...)` | Index unreachable or refusing | Check the project; runs are unaffected |
| no `Result cache:` line at all | The library is not in the image | Step 1 Dockerfile check |

## 6. Record it

Add a row to [ROLLOUT.md](ROLLOUT.md), and remind the owner to prune the builds that
predate the install.
