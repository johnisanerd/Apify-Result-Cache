# Apify Result Cache — Design

> One shared library that lets any Actor in the fleet serve a repeat request from our
> own store instead of re-scraping it, and that measures how often requests repeat
> before it serves anything.

Built for `johnvc/YoutubeTranscripts` first, where a transcript fetched through
residential proxy costs roughly 10x what serving it from a cache would. The economics,
the phase gates and the storage decision are in the owner's plan documents
(`RESULT_CACHE_PLAN.md`, `RESULT_CACHE_HANDOFF_YOUTUBE.md`); this file records the
technical decisions.

## Phases

| Phase | Library | Mode | What happens |
| --- | --- | --- | --- |
| 0 | 0.1 | `keys` | Record one row per request. No output change. 14 days, then the gate: paid hit rate >= 25% go, 15-25% ask, < 15% stop. |
| 1 | 0.2 | `serve` | Look up before fetching; serve hits from the bucket; store misses. New per-run input `maxAgeDays`. Hits charge `RESULT_CACHE_HIT_EVENT`. |
| 2 | 0.2+ | `serve` | Move the bucket to a self-hosted S3 endpoint; old bucket as read fallback for one TTL. Index stays in Postgres. |

## Decisions

| # | Decision | Why |
| --- | --- | --- |
| 1 | Same shape as `apify-free-tier`: public repo, pinned tarball, raw httpx PostgREST client, RPC-only surface, RLS on with zero policies, env-var configuration, inert when unset. | Proven across 47 Actors; one install checklist for both. |
| 2 | Own project (`apify-result-cache`, Pro org, us-east-1) read only through `RESULT_CACHE_INDEX_URL` / `RESULT_CACHE_INDEX_KEY`. No fallback to the limiter's `SUPABASE_URL` / `SUPABASE_KEY`. | John's call on 2026-09-30 (0.1.1): keeps the fleet-critical limiter isolated from the cache, for about $10/month of compute. 0.1.0 shared the limiter's project, which still holds a copy of the tables, so a fallback would silently log to the wrong database. |
| 3 | Modes `off` / `keys` / `serve`; absent means inert with one log line. | Safe to install fleet-wide; silence would be indistinguishable from "not installed". |
| 4 | `key(fields)` is generic; per-source normalisers (`youtube.py`) own the field cleaning. | Normalisation drift between Phase 0 and Phase 1 would make the measurement meaningless. A golden-hash test pins the YouTube wire contract. |
| 5 | Store the upstream artifact, not the dataset row. | One cached payload serves every output-format combination; row fields are computed on the way out. |
| 6 | Payloads never in Postgres; S3 protocol from day one. | A payload cache fills the plan's disk in days and pushes compute up the ladder; an index of ~150 B/row does not. S3 makes Phase 2 an endpoint change. |
| 7 | `log()` is sync and O(1); one background flush in flight; batches of 50, RPC cap 500, queue cap 5,000. | The per-request hot path must never wait on the network. |
| 8 | A failed log batch is dropped, never re-queued. | A read timeout can fire after the server committed; re-sending would count keys twice and bias the hit rate upward. Dropping under-counts at random, which is harmless. |
| 9 | Warn on the first failed flush; switch off after three in a row; report drops in the close summary. | Visible on a small run, bounded on a dead index. |
| 10 | `cache_log` refuses inserts past 4 GB of request log. | With the spend cap on, the disk stays at the included 8 GB and a full disk makes the project read-only. The stop leaves room for the Phase 1 index. |
| 11 | `cache_stats`, `cache_quota`, `cache_gc_requests` are service-role only; exclusions are parameters, not SQL literals. | The anon key ships in Actor images; the shared SQL stays namespace-agnostic. |
| 12 | `ts`, `fetched_at`, `expires_at` are server clock. | A container with a wrong clock cannot write into the wrong window. |
| 13 | `cache_lookup` takes `p_max_age_days int`, not an interval. | Unambiguous JSON; maps 1:1 to the Actor's max-age input. |
| 14 | gzip level 6 with `mtime=0`; sha256 of the gzipped bytes; 16 MB decode cap. Payload JSON keeps the caller's key order (keys still hash sorted). | Verifiable on read and bomb-proof, and a served row reads exactly like a fresh one, down to CSV column order. |
| 15 | Spend cap stays on. A quota watch warns at 80% and errors at 95% of each included quota. | Over quota, uploads fail (not cached) and downloads fail (miss); no run fails. The watch makes the ceiling visible before it bites. |
| 16 | Request log retention 180 days; payload TTL default 90, max 365. | Bounded storage for both tables; GC is a script because the bucket has no lifecycle rules. |
| 17 | S3 signing implemented in the library (Signature V4 over httpx, pinned to AWS's published examples), not boto3. | boto3 adds ~90 MB and a noticeable import to every run of every Actor; three single-object calls need ~80 lines. |
| 18 | One stable object path per key (`<ns>/<key[:2]>/<key>.json.gz`). | A key that expires and is re-fetched overwrites its own object, so the expiry job only deletes what the index says expired and no orphans accumulate. |
| 19 | One batched lookup before the videos start; blob GET only on a hit; uploads in a background queue capped at 32 MB, one in flight. | Keeps the per-video path free of index round trips and bounds memory on runs with many misses. |
| 20 | Each request logs exactly one outcome (`hit` / `miss` / `bypass` / `error`). | `cache_stats` counts rows as requests; one row per request keeps both the key-repetition rate and the real hit rate honest. |
| 21 | Storage breaker: three consecutive storage failures stop serving for the run; a missing object or a failed digest check does not count. | An outage costs at most three slow requests; a single bad object is just a miss. |
| 22 | The run's one lookup gets a 4 s read timeout and one retry (0.2.1). | Measured 2026-09-30: lookups take 50-180 ms, with a rare spike past 2.5 s. One slow answer would otherwise turn every hit in the run into a fresh fetch; worst case the run starts ~8 s later and fetches fresh. |

## State machine

```
start() ── config missing / off / invalid ──> inert   (log, lookup, put, close: no-ops; key() works)
   │
   └── keys (or serve on 0.1) ──> active ── 3 consecutive flush failures ──> deactivated
                                     │                                         (buffer dropped, client closed)
                                     └── close() ──> drain (<= 5 s) ──> summary line ──> closed
```

## Buffer and flush

- `log()` appends to a bounded deque. At 50 buffered rows it schedules a flush unless one
  is already running.
- A flush sends up to 500 rows per RPC and loops while at least 50 remain.
- `close()` waits for the in-flight flush within the 5 s budget, then sends the remainder
  in batches until the budget runs out. Whatever is left is counted as dropped.
- `log()` after `close()` does nothing.

## Key and payload contract (YouTube)

- Key: sha256 of `{"languages":[...],"ns":"youtube-transcript","preserve_formatting":bool,"transcript_type":"any|manual|generated","translate_to":null|"xx","v":1,"video_id":"..."}`
  with sorted keys and no whitespace. `languages` defaults to `["en"]`, order and case
  preserved. Not in the key: `output_formats`, `include_metadata`, `include_extended_metadata`.
- Payload (0.2): the dict `fetch_youtube_transcript()` returns, minus the derived `srt` /
  `vtt` / `text`, plus `basic_metadata` and `cache_written_at`.
- Not cached: list-only calls, channel listings, extended (charged) metadata, error rows,
  failed translations.

## PostgREST notes

- Never overload these function names; PostgREST cannot pick between overloads.
- `returns table` columns are OUT variables in PL/pgSQL; every function uses
  `#variable_conflict use_column` and alias-qualified columns.
- `returns void` answers with an empty body; the client does not parse it.
- A new function can 404 for a few seconds after a migration while the schema cache
  reloads.
