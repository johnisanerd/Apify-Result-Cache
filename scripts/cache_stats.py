#!/usr/bin/env python3
"""Hit-rate gate and quota watch for the result cache.

Needs the index project's service key (the publishable key in Actor images is
refused by these RPCs). Put both in this repo's .env (git-ignored) or export them:

    RESULT_CACHE_INDEX_URL=...        # the cache's own project
    RESULT_CACHE_SERVICE_KEY=...      # its service role / secret key, never shipped in an Actor

    # Served rate and outcomes: paying callers, example IDs excluded, John's own runs excluded
    uv run python scripts/cache_stats.py --from 2026-10-01 --to 2026-10-14 --exclude-user <APIFY_USER_ID>
    # Add --phase0-gate for the Phase 0 key-repetition verdict (a namespace still in keys mode)

    # The same, one line per day, to see the trend
    uv run python scripts/cache_stats.py --from 2026-10-01 --to 2026-10-14 --daily

    # Quota watch (warn at 80%, error at 95% of each Pro included quota)
    uv run python scripts/cache_stats.py --quota

    # Key fragmentation: paid requests for a video that was also asked for under
    # another key (a different language list). Decides whether a first-language
    # alias is worth building. Needs migration 0005.
    uv run python scripts/cache_stats.py --fragmentation --from 2026-10-01 --to 2026-10-14

Exit code: 0 ok, 1 warning (quota, or the gate in the ask-John band with --phase0-gate), 2 error (quota, or gate failed).
`--exclude-user` takes the raw APIFY_USER_ID and hashes it locally; the id never
leaves this machine.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, timedelta

from _common import load_env_file, namespace_config, quota_report

from apify_result_cache.config import user_hash
from apify_result_cache.db import CacheDB, CacheDBError

GATE_GO = 0.25
GATE_ASK = 0.15


def verdict(rate: float | None) -> tuple[str, int]:
    if rate is None:
        return "no paying requests in range", 1
    if rate >= GATE_GO:
        return f"PASS (>= {GATE_GO:.0%}): proceed to Phase 1", 0
    if rate >= GATE_ASK:
        return f"ASK JOHN ({GATE_ASK:.0%}-{GATE_GO:.0%})", 1
    return f"STOP (< {GATE_ASK:.0%}): record the number, leave keys mode running", 2


def fmt_rate(rate: float | None) -> str:
    return "n/a" if rate is None else f"{rate:.1%}"


async def run_stats(db: CacheDB, args) -> int:
    ns = namespace_config(args.namespace)
    exclude_ids = [] if args.include_examples else list(ns.get("exclude_entity_ids") or [])
    exclude_user = user_hash(args.exclude_user) if args.exclude_user else None

    print(f"Namespace {args.namespace}, paying callers only, {args.date_from} to {args.date_to} (UTC, inclusive)")
    print(f"Excluding {len(exclude_ids)} example entity id(s)"
          + (", and one user (hashed locally)" if exclude_user else ""))

    if args.daily:
        day = date.fromisoformat(args.date_from)
        end = date.fromisoformat(args.date_to)
        print(f"\n{'day':<12}{'requests':>10}{'distinct':>10}{'any user':>10}{'same user':>11}")
        while day <= end:
            s = await db.cache_stats(day.isoformat(), day.isoformat(), args.namespace,
                                     exclude_user, exclude_ids or None)
            print(f"{day.isoformat():<12}{s['requests']:>10,}{s['distinct_keys']:>10,}"
                  f"{fmt_rate(s['hit_rate_any_user']):>10}{fmt_rate(s['hit_rate_same_user_only']):>11}")
            day += timedelta(days=1)
        print()

    s = await db.cache_stats(args.date_from, args.date_to, args.namespace,
                             exclude_user, exclude_ids or None)
    print(f"requests            {s['requests']:,}")
    print(f"distinct keys       {s['distinct_keys']:,}")
    print(f"distinct users      {s['distinct_users']:,}")
    print(f"hit rate, any user  {fmt_rate(s['hit_rate_any_user'])}")
    print(f"hit rate, same user {fmt_rate(s['hit_rate_same_user_only'])}")
    print(f"first / last        {s['first_ts']} / {s['last_ts']}")
    code = 0
    if args.phase0_gate:
        # The Phase 0 measurement gate (paid key-repetition rate). Historical:
        # serving went live on 2026-09-30 without it; kept for other namespaces.
        text, code = verdict(s["hit_rate_any_user"])
        print(f"\nKey repetition gate: {text}")

    # Once the cache serves, this is the number that matters: what was actually served.
    rows = await db.cache_outcomes(args.date_from, args.date_to, args.namespace,
                                   exclude_user, exclude_ids or None)
    if rows:
        print("\nOutcomes (serve mode logs hit / miss / bypass / error / failed; keys mode logs 'logged'):")
        for paying in (True, False):
            counts = {r["outcome"]: int(r["requests"]) for r in rows if bool(r["is_paying"]) is paying}
            if not counts:
                continue
            served = sum(counts.get(o, 0) for o in ("hit", "miss", "bypass", "error", "failed"))
            detail = ", ".join(f"{o} {n:,}" for o, n in sorted(counts.items()))
            label = "paying" if paying else "free  "
            if served:
                print(f"  {label}: {detail}  ->  served from cache {counts.get('hit', 0) / served:.1%}")
                failed = counts.get("failed", 0)
                if failed:
                    # A `failed` request fetched through proxy and stored nothing, and
                    # a retry does it again. This share is the case for negative caching.
                    print(f"  {label}: negative-cache candidates {failed:,} of {served:,} "
                          f"requests ({failed / served:.1%})")
            else:
                print(f"  {label}: {detail}")
    return code


FRAGMENTATION_BUILD_ALIAS = 0.10


async def run_fragmentation(db: CacheDB, args) -> int:
    """How often one video is requested under more than one key (language lists)."""
    ns = namespace_config(args.namespace)
    exclude_ids = [] if args.include_examples else list(ns.get("exclude_entity_ids") or [])
    exclude_user = user_hash(args.exclude_user) if args.exclude_user else None
    f = await db.cache_fragmentation(args.date_from, args.date_to, args.namespace,
                                     exclude_user, exclude_ids or None)
    requests = int(f.get("requests") or 0)
    entities = int(f.get("entities") or 0)
    frag_entities = int(f.get("fragmented_entities") or 0)
    frag_requests = int(f.get("fragmented_requests") or 0)
    share = frag_requests / requests if requests else 0.0
    print(f"Key fragmentation, namespace {args.namespace}, paying callers only, "
          f"{args.date_from} to {args.date_to} (UTC, inclusive)")
    print(f"requests                     {requests:,}")
    print(f"distinct entities            {entities:,}")
    print(f"entities under > 1 key       {frag_entities:,}")
    print(f"requests on those entities   {frag_requests:,} ({share:.1%} of requests)")
    if not requests:
        print("\nVerdict: no paying requests in range")
        return 1
    if share >= FRAGMENTATION_BUILD_ALIAS:
        print(f"\nVerdict: >= {FRAGMENTATION_BUILD_ALIAS:.0%} of requests: a first-language alias is worth building")
        return 1
    print(f"\nVerdict: < {FRAGMENTATION_BUILD_ALIAS:.0%} of requests: leave the key as it is")
    return 0


async def run_quota(db: CacheDB, plan: str) -> int:
    lines, code = quota_report(await db.cache_quota(), plan)
    print(f"Result cache quota watch ({plan} plan, spend cap on; WARN 80%, ERROR 95%)")
    print("\n".join(lines))
    if code:
        print("\nOptions: lower RESULT_CACHE_TTL_DAYS (rebuild), run cache_gc.py, "
              "or raise the plan / turn the spend cap off (John's call).")
    return code


async def main() -> int:
    load_env_file()
    today = date.today()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--namespace", default="youtube-transcript")
    ap.add_argument("--from", dest="date_from", default=(today - timedelta(days=13)).isoformat())
    ap.add_argument("--to", dest="date_to", default=today.isoformat())
    ap.add_argument("--daily", action="store_true", help="also print one line per day")
    ap.add_argument("--phase0-gate", action="store_true",
                    help="also print the Phase 0 key-repetition gate verdict (PASS / ASK / STOP)")
    ap.add_argument("--exclude-user", help="raw APIFY_USER_ID to exclude (the owner's probe runs)")
    ap.add_argument("--include-examples", action="store_true", help="do not exclude example ids")
    ap.add_argument("--quota", action="store_true", help="run the quota watch instead of the gate")
    ap.add_argument("--fragmentation", action="store_true",
                    help="report requests for one video under several keys instead of the gate")
    ap.add_argument("--plan", choices=("pro", "free"), default=os.getenv("RESULT_CACHE_PLAN", "pro"))
    ap.add_argument("--url", default=os.getenv("RESULT_CACHE_INDEX_URL"))
    ap.add_argument("--service-key", default=os.getenv("RESULT_CACHE_SERVICE_KEY"))
    args = ap.parse_args()

    if not args.url or not args.service_key:
        print("Set RESULT_CACHE_INDEX_URL and RESULT_CACHE_SERVICE_KEY (the index project's service key).",
              file=sys.stderr)
        return 2

    db = CacheDB(args.url, args.service_key, read_timeout=30.0)
    try:
        if args.quota:
            return await run_quota(db, args.plan)
        if args.fragmentation:
            return await run_fragmentation(db, args)
        return await run_stats(db, args)
    except CacheDBError as exc:
        print(f"Index call failed: {exc}", file=sys.stderr)
        return 2
    finally:
        await db.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
