#!/usr/bin/env python3
"""Hit-rate gate and quota watch for the result cache.

Needs the index project's service key (the publishable key in Actor images is
refused by these RPCs). Put both in this repo's .env (git-ignored) or export them:

    RESULT_CACHE_INDEX_URL=...        # the cache's own project
    RESULT_CACHE_SERVICE_KEY=...      # its service role / secret key, never shipped in an Actor

    # The Phase 0 gate: paying callers, example IDs excluded, John's own runs excluded
    uv run python scripts/cache_stats.py --from 2026-10-01 --to 2026-10-14 --exclude-user <APIFY_USER_ID>

    # The same, one line per day, to see the trend
    uv run python scripts/cache_stats.py --from 2026-10-01 --to 2026-10-14 --daily

    # Quota watch (warn at 80%, error at 95% of each Pro included quota)
    uv run python scripts/cache_stats.py --quota

Exit code: 0 ok, 1 warning (or gate in the ask-John band), 2 error (or gate failed).
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
    text, code = verdict(s["hit_rate_any_user"])
    print(f"\nGate: {text}")
    return code


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
    ap.add_argument("--exclude-user", help="raw APIFY_USER_ID to exclude (the owner's probe runs)")
    ap.add_argument("--include-examples", action="store_true", help="do not exclude example ids")
    ap.add_argument("--quota", action="store_true", help="run the quota watch instead of the gate")
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
        return await run_stats(db, args)
    except CacheDBError as exc:
        print(f"Index call failed: {exc}", file=sys.stderr)
        return 2
    finally:
        await db.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
