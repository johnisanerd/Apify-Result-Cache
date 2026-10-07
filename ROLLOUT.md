# Rollout status

Which Actors have the result cache installed, in which mode, and what each install taught us.

| Actor | Actor ID | Namespace | Mode | Library | Since | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | `zPumutvB61fpEsglh` | `youtube-transcript` | **serve, live** on build 0.5.100 since 2026-09-30 21:32 UTC (PRs #7 and #8 merged; MCP gate run KP67mQOUwp3l75DOb PASS) | v0.2.2 | 2026-09-30 | Pilot. Hits charge `videoprocessed`; `max_age_days` input (default 90); owner and paid pool account verified (store, then serve). Go-live order and current state: the Actor's dev notes, "Result cache". |
| `johnvc/Google-AI-Overview-API` | `XqEZodkkqvqAtiSkV` | `google-ai-overview` | **serve, live** on build 0.0.149 since 2026-10-07 19:29 UTC (actor v1.3.0; MCP gate run pv3CBFWB4oq6KdeVb PASS) | v0.4.0 | 2026-10-07 | TTL 7 days; served when younger than the per-run `max_cache_age_hours` input (default 48, 0 = live); hits charge `overview-retrieval`; a failed live check attaches the last good copy as `stale: true`, never charged. Phase 0 skipped by John. Verified store then serve on paid and free accounts (0 vendor calls on the repeat). |
| `youtube-shorts-api` | `I4jjsTkELEeJKK8o3` | `youtube-shorts`, `youtube-shorts-channel` | keys (seen 2026-09-30) | v0.1.0 | 2026-09-30 | Installed from another session. On 0.1.0 it writes to the free-tier ledger's project; moving to 0.1.1 needs the two index variables. |
| `johnvc/google-images-api` | `bvAQMqCbp6wE53JzK` | `google-images-page` | built 2026-10-07 on branch `cost-controls`; side-version testing before serve | v0.3.1 | 2026-10-07 | One entry = one 100-image results page. `ttl_days=1`, input `maxAgeDays` (0 or 1, default 1). Hits charge `image_scraped`, same price as fresh. Paired with a per-call spend gate; motivation and numbers in the Actor's dev notes, "Cost controls (2026-10)". |
| `johnvc/google-maps-places-api` | `WQbrHYgrJV5fP6b09` | `google-maps-page` | built 2026-10-07 on branch `cost-controls`; side-version testing before serve | v0.3.1 | 2026-10-07 | One entry = one results page (up to 20 places); the page viewport is stored in the payload and checked before a later page is served. `ttl_days=1`. Hits charge `place`, same price as fresh. |

## Migrations

- 0006 (`cache_purge_entity`, service role only) applied 2026-10-02 to `apify-result-cache`; verified with a throwaway `test-purge3` namespace (only the named entity removed; anon cannot execute). Used by `scripts/purge_entity.py`.

## Project

- Index: its own project, `apify-result-cache` (Pro org, us-east-1), since 0.1.1. 0.1.0
  wrote to the free-tier ledger's project, whose copy of the tables stays until every
  install is on 0.1.1.
- Plan: Pro with the spend cap **on**. Quota watch: `scripts/cache_stats.py --quota`, weekly (needs
  `RESULT_CACHE_SERVICE_KEY` in the local `.env`).
- Measured 2026-09-30: `cache_lookup` 50-180 ms (1 or 100 keys). YoutubeTranscripts, 25 videos on the side
  version: platform cost $0.00112 per fresh video vs $0.000026 per cached video (~43x; the difference is
  residential proxy). Warmed with 377 known video IDs: 294 cached, 14 MB gzipped (mean 48 KB, max 598 KB).

## Timers

| Actor | Phase | Started | Gate due | Result |
| --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | 1 (`serve`, Phase 0 gate skipped by John) | 2026-09-30 21:32 UTC | served rate read weekly from `cache_stats.py` | — |
| `johnvc/Google-AI-Overview-API` | 1 (`serve`, Phase 0 gate skipped by John) | 2026-10-07 19:29 UTC | served rate read weekly from `cache_stats.py` | — |
| `johnvc/google-images-api` | serve, 24 h TTL | go-live date (D0) | D14: served share of paying traffic, residual 24 h repeats, credits per 1k delivered images | — |
| `johnvc/google-maps-places-api` | serve, 24 h TTL | go-live date (D0) | D14: served share, revenue per $1 of upstream spend | — |
