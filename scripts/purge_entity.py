"""Remove every cached result about one entity (an opt-out or removal request).

    uv run python scripts/purge_entity.py --entity jane-doe --namespaces li-profile,li-post,li-serp
    uv run python scripts/purge_entity.py --entity jane-doe --namespaces li-profile --dry-run

Deletes the live index rows (so nothing is served again, effective at once),
then each payload object from the bucket, and blanks the entity in the request
log. Needs RESULT_CACHE_INDEX_URL plus RESULT_CACHE_SERVICE_KEY, and the S3
keys for the object deletes, in the shell or ./.env.

For LinkedIn the entity id is the profile slug (li-profile), the post's
activity id (li-post), or the search query text (li-serp).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from _common import load_env_file

from apify_result_cache.db import CacheDB, CacheDBError
from apify_result_cache.s3 import S3Error

from cache_gc import storage_client


async def main() -> int:
    load_env_file()
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--entity", required=True)
    parser.add_argument("--namespaces", required=True, help="comma-separated")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    namespaces = [n.strip() for n in args.namespaces.split(",") if n.strip()]

    url = os.getenv("RESULT_CACHE_INDEX_URL")
    key = os.getenv("RESULT_CACHE_SERVICE_KEY")
    if not (url and key):
        print("RESULT_CACHE_INDEX_URL and RESULT_CACHE_SERVICE_KEY are required.", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"Dry run: would purge entity {args.entity!r} from {', '.join(namespaces)}.")
        return 0

    db = CacheDB(url, key, read_timeout=30.0)
    try:
        rows = await db.cache_purge_entity(namespaces, args.entity)
    except CacheDBError as exc:
        print(f"Purge failed: {exc}", file=sys.stderr)
        return 1
    finally:
        pass
    print(f"Index: removed {len(rows)} live row(s); nothing about this entity can be served now.")

    s3 = storage_client(url)
    deleted = failed = 0
    if rows and s3 is None:
        print("S3 keys not set: payload objects left in the bucket (unreachable without an index row; "
              "cache_gc will not see them). Re-run with the keys to delete them.", file=sys.stderr)
    for row in rows:
        if s3 is None:
            break
        try:
            await s3.delete_object(row["blob_ref"])
            deleted += 1
        except (S3Error, Exception) as exc:  # noqa: BLE001
            failed += 1
            print(f"  object delete failed for {row['blob_ref']}: {type(exc).__name__}", file=sys.stderr)
    print(f"Bucket: deleted {deleted} object(s)" + (f", {failed} failed" if failed else "") + ".")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
