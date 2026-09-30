#!/usr/bin/env python3
"""Garbage collection for the result cache. Run monthly, or from the 1 AM crontab.

    export SUPABASE_URL=... SUPABASE_SERVICE_KEY=...
    uv run python scripts/cache_gc.py                        # prune the request log past 180 days, print the quota report
    uv run python scripts/cache_gc.py --dry-run              # say what it would do

What it does today (0.1):
  1. Request-log retention: deletes result_cache_requests rows older than
     --log-retention-days (default 180), in batches, via cache_gc_requests().
     The function refuses anything younger than 7 days.
  2. Prints the quota watch, same as `cache_stats.py --quota`.

What it will do in 0.2 (Phase 1, needs the S3 keys):
  3. Expired payloads: select index rows with expires_at < now(), delete their
     blobs from the bucket (month-prefixed paths make whole-month prefixes
     cheap to list), then delete the index rows. Blob first, row second, so a
     crash leaves an orphan row that points at nothing (a harmless miss), never
     an orphan blob that costs storage forever. The bucket has no lifecycle
     rules; this script is the only thing that frees space.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from _common import load_env_file, quota_report

from apify_result_cache.db import CacheDB, CacheDBError

MIN_RETENTION_DAYS = 14   # never prune inside a measurement window


async def main() -> int:
    load_env_file()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-retention-days", type=int, default=180)
    ap.add_argument("--namespace", default=None, help="limit to one namespace (default: all)")
    ap.add_argument("--batch", type=int, default=50_000)
    ap.add_argument("--max-batches", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan", choices=("pro", "free"), default=os.getenv("RESULT_CACHE_PLAN", "pro"))
    ap.add_argument("--url", default=os.getenv("RESULT_CACHE_INDEX_URL") or os.getenv("SUPABASE_URL"))
    ap.add_argument("--service-key", default=os.getenv("SUPABASE_SERVICE_KEY"))
    args = ap.parse_args()

    if not args.url or not args.service_key:
        print("Set SUPABASE_URL and SUPABASE_SERVICE_KEY (service role).", file=sys.stderr)
        return 2
    if args.log_retention_days < MIN_RETENTION_DAYS:
        print(f"--log-retention-days must be >= {MIN_RETENTION_DAYS}.", file=sys.stderr)
        return 2

    before = datetime.now(timezone.utc) - timedelta(days=args.log_retention_days)
    db = CacheDB(args.url, args.service_key, read_timeout=60.0)
    try:
        print(f"Request log: pruning rows older than {before:%Y-%m-%d %H:%M} UTC "
              f"({args.log_retention_days} days){' in ' + args.namespace if args.namespace else ''}.")
        total = 0
        if args.dry_run:
            print("  dry run: nothing deleted.")
        else:
            for _ in range(args.max_batches):
                n = await db.cache_gc_requests(before.isoformat(), args.namespace, args.batch)
                total += n
                if n < args.batch:
                    break
            print(f"  deleted {total:,} row(s).")

        print("Payload expiry: not in 0.1 (Phase 1 adds blob + index deletion).\n")

        lines, code = quota_report(await db.cache_quota(), args.plan)
        print(f"Quota watch ({args.plan} plan, spend cap on; WARN 80%, ERROR 95%)")
        print("\n".join(lines))
        return code
    except CacheDBError as exc:
        print(f"Index call failed: {exc}", file=sys.stderr)
        return 2
    finally:
        await db.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
