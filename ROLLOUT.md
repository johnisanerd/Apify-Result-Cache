# Rollout status

Which Actors have the result cache installed, in which mode, and what each install taught us.

| Actor | Actor ID | Namespace | Mode | Library | Since | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | `zPumutvB61fpEsglh` | `youtube-transcript` | serve verified on side version 0.8; live not switched (PRs #7 and #8 open) | v0.2.1 | 2026-09-30 | Pilot. Hits charge `videoprocessed`; `max_age_days` input (default 90); owner and paid pool account verified (store, then serve). Go-live order and current state: the Actor's dev notes, "Result cache". |
| `youtube-shorts-api` | `I4jjsTkELEeJKK8o3` | `youtube-shorts`, `youtube-shorts-channel` | keys (seen 2026-09-30) | v0.1.0 | 2026-09-30 | Installed from another session. On 0.1.0 it writes to the free-tier ledger's project; moving to 0.1.1 needs the two index variables. |

## Project

- Index: its own project, `apify-result-cache` (Pro org, us-east-1), since 0.1.1. 0.1.0
  wrote to the free-tier ledger's project, whose copy of the tables stays until every
  install is on 0.1.1.
- Plan: Pro with the spend cap **on**. Quota watch: `scripts/cache_stats.py --quota`, weekly (needs
  `RESULT_CACHE_SERVICE_KEY` in the local `.env`).
- Measured 2026-09-30: `cache_lookup` 50-180 ms (1 or 100 keys); stored transcripts 0.9-20 KB gzipped.

## Timers

| Actor | Phase | Started | Gate due | Result |
| --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | 0 (`keys`) | — | start + 14 days | — |
