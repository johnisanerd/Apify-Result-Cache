#!/usr/bin/env python3
"""Garbage collection for the result cache. Run monthly, or from the 1 AM crontab.

    RESULT_CACHE_INDEX_URL=... RESULT_CACHE_SERVICE_KEY=...   # in .env or exported
    uv run python scripts/cache_gc.py                        # prune the request log past 180 days, print the quota report
    uv run python scripts/cache_gc.py --dry-run              # say what it would do

What it does:
  1. Request-log retention: deletes result_cache_requests rows older than
     --log-retention-days (default 180), in batches, via cache_gc_requests().
     The function refuses anything younger than 7 days.
  2. Expired payloads (needs the S3 keys): lists index rows past their
     expiry, deletes each object from the bucket, then deletes those index
     rows. Object first, row second, so a crash leaves a row that points at
     nothing (a harmless miss), never an object that costs storage forever.
     The bucket has no lifecycle rules; this is the only thing that frees
     space. cache_delete_index() only ever removes rows that are still
     expired, so a key re-stored meanwhile keeps its row (at worst its new
     object was deleted, which heals itself on the next miss).
  3. Prints the quota watch, same as `cache_stats.py --quota`.

Storage settings come from the same variables the Actors use:
RESULT_CACHE_S3_ENDPOINT (default: the index project's storage host),
RESULT_CACHE_S3_REGION (default us-east-1), RESULT_CACHE_S3_BUCKET (default
result-cache), RESULT_CACHE_S3_ACCESS_KEY and RESULT_CACHE_S3_SECRET_KEY.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

from _common import load_env_file, quota_report

from apify_result_cache.db import CacheDB, CacheDBError
from apify_result_cache.s3 import S3Client, S3Error

MIN_RETENTION_DAYS = 14   # never prune inside a measurement window


def storage_client(index_url: str) -> S3Client | None:
    access, secret = os.getenv("RESULT_CACHE_S3_ACCESS_KEY"), os.getenv("RESULT_CACHE_S3_SECRET_KEY")
    if not (access and secret):
        return None
    endpoint = os.getenv("RESULT_CACHE_S3_ENDPOINT")
    if not endpoint:
        ref = index_url.split("//", 1)[1].split(".", 1)[0]
        endpoint = f"https://{ref}.storage.supabase.co/storage/v1/s3"
    return S3Client(endpoint, os.getenv("RESULT_CACHE_S3_REGION", "us-east-1"),
                    os.getenv("RESULT_CACHE_S3_BUCKET", "result-cache"), access, secret)


async def _retry_db(coro_factory, what: str, tries: int = 3):
    """One index call, retried on transport errors (ConnectError, ReadTimeout)."""
    last = None
    for attempt in range(tries):
        try:
            return await coro_factory()
        except CacheDBError as exc:
            last = exc
            if attempt + 1 < tries:
                await asyncio.sleep(2 * (attempt + 1))
    raise CacheDBError(f"{what}: {last}")


async def _delete_batch(s3: S3Client, refs: list[str]) -> tuple[list[str], int]:
    """Delete `refs`; return (deleted refs, failures). DeleteObjects first, with
    backoff on 429 and 5xx; single paced deletes if the batch call is refused."""
    delay = 2.0
    for _attempt in range(6):
        try:
            failed = set(await s3.delete_objects(refs))
            return [r for r in refs if r not in failed], len(failed)
        except S3Error as exc:
            text = str(exc)
            if text in ("HTTP 429", "HTTP 503", "HTTP 502", "HTTP 500") or not text.startswith("HTTP"):
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            break   # the batch call is refused outright (e.g. 400/501): fall back
    deleted: list[str] = []
    failures = 0
    for ref in refs:
        for backoff in (0.0, 2.0, 4.0, 8.0, 16.0):
            try:
                if backoff:
                    await asyncio.sleep(backoff)
                await s3.delete_object(ref)
                deleted.append(ref)
                break
            except S3Error as exc:
                if str(exc) != "HTTP 429" and backoff:
                    failures += 1
                    break
        else:
            failures += 1
        await asyncio.sleep(0.05)   # ~20 deletes/s on the fallback path
    return deleted, failures


async def expire_payloads(db: CacheDB, args) -> None:
    s3 = storage_client(args.url)
    if s3 is None:
        print("Payload expiry: skipped (RESULT_CACHE_S3_ACCESS_KEY / _SECRET_KEY not set).\n")
        return
    deleted_objects = deleted_rows = failures = 0
    try:
        for batch in range(args.max_batches):
            rows = await _retry_db(lambda: db.cache_expired(1000), "cache_expired")
            if not rows:
                if batch == 0:
                    print("Payload expiry: nothing has expired yet; no objects or index rows to delete.\n")
                break
            if args.dry_run:
                print(f"Payload expiry: {len(rows)}+ expired result(s) would be deleted (dry run).")
                break
            by_ref = {row["blob_ref"]: row for row in rows}
            done, failed_now = await _delete_batch(s3, list(by_ref))
            failures += failed_now
            deleted_objects += len(done)
            by_namespace: dict[str, list[str]] = {}
            for ref in done:
                row = by_ref[ref]
                by_namespace.setdefault(row["namespace"], []).append(row["key_hash"])
            for namespace, keys in by_namespace.items():
                deleted_rows += await _retry_db(lambda ns=namespace, ks=keys: db.cache_delete_index(ns, ks),
                                                "cache_delete_index")
            if (batch + 1) % 10 == 0:
                print(f"  ... {deleted_objects:,} objects and {deleted_rows:,} index rows so far", flush=True)
            if not done:
                break   # nothing is getting through; stop rather than spin
            if len(rows) < 1000:
                break
    finally:
        await s3.aclose()
    if not args.dry_run:
        print(f"Payload expiry: deleted {deleted_objects:,} object(s) and {deleted_rows:,} index row(s)"
              + (f"; {failures} object delete(s) failed" if failures else "") + ".\n")


async def main() -> int:
    load_env_file()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-retention-days", type=int, default=180)
    ap.add_argument("--namespace", default=None, help="limit to one namespace (default: all)")
    ap.add_argument("--batch", type=int, default=50_000)
    ap.add_argument("--max-batches", type=int, default=200)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--plan", choices=("pro", "free"), default=os.getenv("RESULT_CACHE_PLAN", "pro"))
    ap.add_argument("--url", default=os.getenv("RESULT_CACHE_INDEX_URL"))
    ap.add_argument("--service-key", default=os.getenv("RESULT_CACHE_SERVICE_KEY"))
    args = ap.parse_args()

    if not args.url or not args.service_key:
        print("Set RESULT_CACHE_INDEX_URL and RESULT_CACHE_SERVICE_KEY (the index project's service key).",
              file=sys.stderr)
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

        await expire_payloads(db, args)

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
