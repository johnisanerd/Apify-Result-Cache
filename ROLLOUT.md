# Rollout status

Which Actors have the result cache installed, in which mode, and what each install taught us.

| Actor | Actor ID | Namespace | Mode | Library | Since | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | `zPumutvB61fpEsglh` | `youtube-transcript` | **serve, live** on build 0.5.100 since 2026-09-30 21:32 UTC (PRs #7 and #8 merged; MCP gate run KP67mQOUwp3l75DOb PASS) | v0.2.2 | 2026-09-30 | Pilot. Hits charge `videoprocessed`; `max_age_days` input (default 90); owner and paid pool account verified (store, then serve). Go-live order and current state: the Actor's dev notes, "Result cache". |
| `youtube-shorts-api` | `I4jjsTkELEeJKK8o3` | `youtube-shorts`, `youtube-shorts-channel` | keys (seen 2026-09-30) | v0.1.0 | 2026-09-30 | Installed from another session. On 0.1.0 it writes to the free-tier ledger's project; moving to 0.1.1 needs the two index variables. |

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
