# Rollout status

Which Actors have the result cache installed, in which mode, and what each install taught us.

| Actor | Actor ID | Namespace | Mode | Library | Since | Notes |
| --- | --- | --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | `zPumutvB61fpEsglh` | `youtube-transcript` | pending | v0.1.0 | — | Phase 0 pilot. Gate: 14 days of `keys`, then `scripts/cache_stats.py`. |

## Project

- Index: the shared project that also carries the free-tier ledger (migration `0003`).
- Plan: Free at install time; Pro with the spend cap **on** before Phase 0 goes live on a
  default build. Quota watch: `scripts/cache_stats.py --quota`, weekly.

## Timers

| Actor | Phase | Started | Gate due | Result |
| --- | --- | --- | --- | --- |
| `johnvc/YoutubeTranscripts` | 0 (`keys`) | — | start + 14 days | — |
