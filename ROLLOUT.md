# Rollout status

Which Actors have the result cache installed, in which mode, and what each install taught us.

| Actor | Actor ID | Namespace | Mode | Library | Since | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | `zPumutvB61fpEsglh` | `youtube-transcript` | pending | v0.1.1 | — | Phase 0 pilot. Gate: 14 days of `keys`, then `scripts/cache_stats.py`. |
| `youtube-shorts-api` | `I4jjsTkELEeJKK8o3` | `youtube-shorts`, `youtube-shorts-channel` | keys (seen 2026-09-30) | v0.1.0 | 2026-09-30 | Installed from another session. On 0.1.0 it writes to the free-tier ledger's project; moving to 0.1.1 needs the two index variables. |

## Project

- Index: its own project, `apify-result-cache` (Pro org, us-east-1), since 0.1.1. 0.1.0
  wrote to the free-tier ledger's project, whose copy of the tables stays until every
  install is on 0.1.1.
- Plan: Pro with the spend cap **on**. Quota watch: `scripts/cache_stats.py --quota`, weekly.

## Timers

| Actor | Phase | Started | Gate due | Result |
| --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | 0 (`keys`) | — | start + 14 days | — |
