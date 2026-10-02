"""Serve mode: look up, serve from storage, store fresh results.

Same promises as the keys-mode tests, one level up: it serves only when fully
configured, a hit returns exactly what was stored, and no storage or index
failure can break a run.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import time

import pytest

from apify_result_cache import CacheEntry, ResultCache
from apify_result_cache import cache as cache_module
from apify_result_cache import codec

NS = "youtube-transcript"
PAYLOAD = {"video_id": "abc", "language_code": "en",
           "timestamped": [{"text": "hi", "start": 0.0, "duration": 1.5}], "basic_metadata": {"title": "T"}}


def hexkey(i: int) -> str:
    return hashlib.sha256(f"k{i}".encode()).hexdigest()


async def start(**kwargs) -> ResultCache:
    return await ResultCache.start(namespace=NS, schema_version=1, **kwargs)


async def settle_puts(cache: ResultCache) -> None:
    if cache._put_task is not None:
        await asyncio.gather(cache._put_task, return_exceptions=True)


def entry_for(db_put: dict) -> CacheEntry:
    return CacheEntry(key_hash=db_put["key_hash"], blob_ref=db_put["blob_ref"], sha256=db_put["sha256"],
                      size_bytes=db_put["size_bytes"], fetched_at=None, schema_version=1)


def row(key: str, *, version: int = 1, fetched: str = "2026-09-20T12:00:00+00:00") -> dict:
    return {"key_hash": key, "blob_ref": codec.blob_ref(NS, key), "sha256": "a" * 64,
            "size_bytes": 123, "fetched_at": fetched, "schema_version": version}


# ------------------------------------------------ promise 1: serves only when fully configured


async def test_serve_mode_with_storage_is_serving(actor, db, s3, serve_env):
    cache = await start()

    assert cache.mode == "serve" and cache.serving is True
    assert s3.init_args == ("https://storage.invalid/storage/v1/s3", "us-east-1", "result-cache")
    [line] = [m for m in actor.log.infos if "mode serve" in m]
    assert "90 days" in line


async def test_region_and_bucket_can_be_overridden(actor, db, s3, serve_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_S3_REGION", "eu-west-1")
    monkeypatch.setenv("RESULT_CACHE_S3_BUCKET", "other-bucket")

    await start()

    assert s3.init_args[1:] == ("eu-west-1", "other-bucket")


async def test_missing_storage_keys_fall_back_to_keys_mode(actor, db, s3, serve_env, monkeypatch):
    monkeypatch.delenv("RESULT_CACHE_S3_SECRET_KEY")

    cache = await start()
    cache.log(hexkey(1), "v", "logged")

    assert cache.serving is False and cache.active is True
    assert any("needs storage settings" in w for w in actor.log.warnings)
    assert any("mode keys" in m for m in actor.log.infos)
    assert s3.init_args is None


async def test_bad_endpoint_falls_back_to_keys_mode(actor, db, s3, serve_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_S3_ENDPOINT", "storage.invalid")

    cache = await start()

    assert cache.serving is False
    assert any("not an http(s) URL" in w for w in actor.log.warnings)


async def test_keys_mode_never_touches_storage(actor, db, s3, keys_env):
    cache = await start()
    cache.put(hexkey(1), entity_id="v", payload=PAYLOAD)

    assert await cache.lookup_many([hexkey(1)], 90) == {}
    assert s3.init_args is None and db.lookup_calls == []


async def test_event_names_default_from_the_actor_and_env_wins(actor, db, s3, serve_env, monkeypatch):
    cache = await start(hit_event_default="videoprocessed", fresh_event_default="videofresh")
    assert (cache.hit_event, cache.fresh_event) == ("videoprocessed", "videofresh")

    monkeypatch.setenv("RESULT_CACHE_HIT_EVENT", "videocached")
    monkeypatch.setenv("RESULT_CACHE_FRESH_EVENT", "videorealtime")
    cache = await start(hit_event_default="videoprocessed", fresh_event_default="videofresh")
    assert (cache.hit_event, cache.fresh_event) == ("videocached", "videorealtime")


# -------------------------------------------------------- promise 2: lookups and hits are right


async def test_lookup_returns_parsed_entries(actor, db, s3, serve_env):
    db.lookup_rows = [row(hexkey(1)), row(hexkey(2))]
    cache = await start()

    found = await cache.lookup_many([hexkey(1), hexkey(3)], 90)

    assert list(found) == [hexkey(1)]
    entry = found[hexkey(1)]
    assert entry.blob_ref == f"{NS}/{hexkey(1)[:2]}/{hexkey(1)}.json.gz"
    assert entry.fetched_at == datetime.datetime(2026, 9, 20, 12, tzinfo=datetime.timezone.utc)
    assert entry.age_days(datetime.datetime(2026, 9, 30, 13, tzinfo=datetime.timezone.utc)) == 10
    assert db.lookup_calls == [(NS, sorted([hexkey(1), hexkey(3)]), 90)]


async def test_lookup_skips_other_schema_versions(actor, db, s3, serve_env):
    db.lookup_rows = [row(hexkey(1), version=2)]
    cache = await start()

    assert await cache.lookup_many([hexkey(1)], 90) == {}


async def test_zero_days_means_fresh_and_makes_no_call(actor, db, s3, serve_env):
    cache = await start()

    assert await cache.lookup_many([hexkey(1)], 0) == {}
    assert db.lookup_calls == []


async def test_lookups_go_in_chunks_of_100_and_dedupe(actor, db, s3, serve_env):
    cache = await start()
    keys = [hexkey(i) for i in range(250)] + [hexkey(0)]

    await cache.lookup_many(keys, 30)

    assert [len(call[1]) for call in db.lookup_calls] == [100, 100, 50]


async def test_a_failed_lookup_is_a_miss_with_one_warning(actor, db, s3, serve_env):
    db.lookup_fail = True
    cache = await start()

    assert await cache.lookup_many([hexkey(1)], 90) == {}
    assert await cache.lookup_many([hexkey(2)], 90) == {}
    assert len(db.lookup_calls) == 4                      # each lookup tried twice
    assert len([w for w in actor.log.infos if "lookup unavailable" in w]) == 1
    assert actor.log.warnings == []


async def test_one_slow_lookup_is_retried(actor, db, s3, serve_env):
    """Measured 2026-09-30: lookups take ~50-180 ms, with a rare >2.5 s spike."""
    db.lookup_rows = [row(hexkey(1))]
    db.lookup_fail_times = 1
    cache = await start()

    found = await cache.lookup_many([hexkey(1)], 90)

    assert list(found) == [hexkey(1)]
    assert len(db.lookup_calls) == 2
    assert actor.log.warnings == []


async def test_put_then_get_returns_exactly_what_was_stored(actor, db, s3, serve_env):
    cache = await start()

    cache.put(hexkey(1), entity_id="vid-1", payload=PAYLOAD)
    await settle_puts(cache)

    [ref] = s3.puts
    assert ref == codec.blob_ref(NS, hexkey(1))
    [put] = db.put_calls
    assert put["namespace"] == NS and put["key_hash"] == hexkey(1) and put["entity_id"] == "vid-1"
    assert put["ttl_days"] == 90 and put["actor_id"] == "actor-1" and put["is_paying"] is False
    assert put["size_bytes"] == len(s3.objects[ref])
    assert put["sha256"] == hashlib.sha256(s3.objects[ref]).hexdigest()
    assert await cache.get_blob(entry_for(put)) == PAYLOAD
    assert cache.stats["stored"] == 1


async def test_put_encodes_immediately(actor, db, s3, serve_env):
    cache = await start()
    payload = {"video_id": "abc", "timestamped": [{"text": "original"}]}

    cache.put(hexkey(1), entity_id=None, payload=payload)
    payload["timestamped"][0]["text"] = "changed later"
    await settle_puts(cache)

    assert await cache.get_blob(entry_for(db.put_calls[0])) == {"video_id": "abc", "timestamped": [{"text": "original"}]}


async def test_ttl_comes_from_the_environment(actor, db, s3, serve_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_TTL_DAYS", "30")
    cache = await start()

    cache.put(hexkey(1), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)

    assert db.put_calls[0]["ttl_days"] == 30


async def test_per_namespace_ttl_overrides_the_default(actor, db, s3, serve_env):
    cache = await ResultCache.start("test-ns", 1, ttl_days=7)
    cache.put(hexkey(1), entity_id="jane-doe", payload=PAYLOAD)
    await settle_puts(cache)
    assert cache.ttl_days == 7
    assert db.put_calls[0]["ttl_days"] == 7


async def test_env_ttl_caps_per_namespace_ttl(actor, db, s3, serve_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_TTL_DAYS", "30")
    long_lived = await ResultCache.start("test-ns", 1, ttl_days=90)
    short_lived = await ResultCache.start("test-ns2", 1, ttl_days=3)
    assert long_lived.ttl_days == 30
    assert short_lived.ttl_days == 3


async def test_per_namespace_ttl_is_clamped_and_validated(actor, db, s3, serve_env):
    assert (await ResultCache.start("test-ns", 1, ttl_days=0)).ttl_days == 1
    assert (await ResultCache.start("test-ns", 1, ttl_days=10_000)).ttl_days == 365
    assert (await ResultCache.start("test-ns", 1, ttl_days=True)).ttl_days == 90  # bool is not a TTL


# ------------------------------------------------------- promise 3: nothing here can break a run


async def test_a_tampered_object_is_rejected(actor, db, s3, serve_env):
    cache = await start()
    cache.put(hexkey(1), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)
    ref = s3.puts[0]
    blob = bytearray(s3.objects[ref])
    blob[len(blob) // 2] ^= 0xFF          # guaranteed change (the last gzip byte is often already 0)
    s3.objects[ref] = bytes(blob)

    assert await cache.get_blob(entry_for(db.put_calls[0])) is None
    assert len([w for w in actor.log.infos if "being replaced" in w]) == 1
    assert cache.serving is True


async def test_a_missing_object_is_a_miss_not_an_outage(actor, db, s3, serve_env):
    cache = await start()
    entry = CacheEntry(hexkey(1), codec.blob_ref(NS, hexkey(1)), "a" * 64, 10, None, 1)

    for _ in range(5):
        assert await cache.get_blob(entry) is None

    assert cache.stats["storage_failures"] == 0 and cache.serving is True
    assert actor.log.warnings == []


async def test_three_storage_failures_stop_serving_with_one_warning(actor, db, s3, serve_env):
    s3.fail_get = True
    cache = await start()
    entry = CacheEntry(hexkey(1), codec.blob_ref(NS, hexkey(1)), "a" * 64, 10, None, 1)

    results = [await cache.get_blob(entry) for _ in range(3)]
    cache.put(hexkey(2), entity_id=None, payload=PAYLOAD)

    assert results == [None, None, None]
    assert cache.serving is False
    assert cache.active is True                         # keys are still recorded
    assert await cache.lookup_many([hexkey(1)], 90) == {}
    assert len([w for w in actor.log.infos if "storage unavailable" in w]) == 1
    assert actor.log.warnings == []
    assert s3.puts == []


async def test_failed_uploads_are_counted_and_trip_the_breaker(actor, db, s3, serve_env):
    s3.fail_put = True
    cache = await start()

    for i in range(4):
        cache.put(hexkey(i), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)

    assert cache.serving is False
    assert cache.stats["stored"] == 0 and cache.stats["put_dropped"] == 4
    assert db.put_calls == []


async def test_an_index_write_failure_leaves_a_future_miss(actor, db, s3, serve_env):
    db.put_fail = True
    cache = await start()

    cache.put(hexkey(1), entity_id=None, payload=PAYLOAD)
    cache.put(hexkey(2), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)

    assert cache.stats["stored"] == 0 and cache.stats["put_dropped"] == 2
    assert len([w for w in actor.log.infos if "could not be indexed" in w]) == 1
    assert actor.log.warnings == []
    assert cache.serving is True


async def test_oversized_payloads_are_skipped(actor, db, s3, serve_env, monkeypatch):
    monkeypatch.setattr(cache_module, "_BLOB_MAX_BYTES", 10)
    cache = await start()

    cache.put(hexkey(1), entity_id=None, payload=PAYLOAD)

    assert cache.stats["put_dropped"] == 1 and s3.puts == []
    assert any("too large" in w for w in actor.log.warnings)


async def test_the_store_queue_is_bounded_in_bytes(actor, db, s3, serve_env, monkeypatch, fast_close):
    s3.hang = True
    cache = await start()
    size = len(codec.encode(PAYLOAD)[0])
    monkeypatch.setattr(cache_module, "_PUT_QUEUE_BYTES", size * 2)

    for i in range(5):
        cache.put(hexkey(i), entity_id=None, payload=PAYLOAD)
    await asyncio.sleep(0)

    # one in flight (hung), two queued, two dropped
    assert cache.stats["queued_puts"] <= 2
    assert cache.stats["put_dropped"] >= 2
    assert len([w for w in actor.log.warnings if "store queue full" in w]) == 1
    await cache.close()


async def test_close_finishes_pending_uploads(actor, db, s3, serve_env):
    cache = await start()
    for i in range(3):
        cache.put(hexkey(i), entity_id=None, payload=PAYLOAD)

    await cache.close()

    assert len(s3.puts) == 3 and len(db.put_calls) == 3
    assert s3.closed is True and db.closed is True


async def test_close_with_hung_storage_is_bounded(actor, db, s3, serve_env, fast_close):
    s3.hang = True
    cache = await start()
    for i in range(3):
        cache.put(hexkey(i), entity_id=None, payload=PAYLOAD)
    await asyncio.sleep(0)

    started = time.monotonic()
    await cache.close()

    assert time.monotonic() - started < 1.5
    assert cache.stats["stored"] == 0 and cache.stats["put_dropped"] == 3


async def test_put_after_close_does_nothing(actor, db, s3, serve_env):
    cache = await start()
    await cache.close()

    cache.put(hexkey(1), entity_id=None, payload=PAYLOAD)

    assert s3.puts == [] and cache.stats["queued_puts"] == 0


async def test_the_log_breaker_does_not_stop_serving(actor, db, s3, serve_env):
    db.fail = True
    cache = await start()
    for batch in range(3):
        for i in range(batch * 50, batch * 50 + 50):
            cache.log(hexkey(i), None, "miss")
        await asyncio.gather(cache._flush_task, return_exceptions=True)

    assert cache.active is False and cache.serving is True
    cache.put(hexkey(999), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)
    assert cache.stats["stored"] == 1


async def test_serve_summary_reports_hits_and_stores(actor, db, s3, serve_env):
    cache = await start()
    cache.log(hexkey(1), "a", "hit")
    cache.log(hexkey(2), "b", "miss")
    cache.log(hexkey(3), "c", "bypass")
    cache.put(hexkey(2), entity_id="b", payload=PAYLOAD)
    cache.put(hexkey(3), entity_id="c", payload=PAYLOAD)

    await cache.close()

    [line] = [m for m in actor.log.infos if "from cache and stored" in m]
    assert "served 1 of 3 request(s) from cache and stored 2 new result(s)" in line
    assert "recorded 3 of 3 request key(s)" in line


async def test_no_log_line_leaks_storage_details(actor, db, s3, serve_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_DEBUG", "1")
    s3.fail_get = True
    cache = await start()
    entry = CacheEntry(hexkey(1), codec.blob_ref(NS, hexkey(1)), "a" * 64, 10, None, 1)
    await cache.get_blob(entry)
    await cache.close()

    text = "\n".join(actor.log.lines).lower()
    for secret in ("s3-access-value", "s3-secret-value", "storage.invalid", "supabase", "test-key-value"):
        assert secret not in text, secret
    assert "'storage_keys': 'set'" in text


# ------------------------------------ 0.2.2: path-bound rows, prefetch, parallel deduplicated uploads


async def test_a_row_naming_another_keys_object_is_not_served(actor, db, s3, serve_env):
    """An index row may only ever point at its own key's object path; anything else is a bad row."""
    cache = await start()
    cache.put(hexkey(1), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)
    [put] = db.put_calls
    swapped = CacheEntry(key_hash=hexkey(2), blob_ref=put["blob_ref"], sha256=put["sha256"],
                         size_bytes=put["size_bytes"], fetched_at=None, schema_version=1)

    assert await cache.get_blob(swapped) is None
    assert s3.gets == []                                    # refused before any storage call
    assert len([w for w in actor.log.warnings if "integrity check" in w]) == 1
    assert cache.serving is True and cache.stats["storage_failures"] == 0
    assert await cache.get_blob(entry_for(put)) == PAYLOAD  # the key's own row still serves


async def test_get_blobs_prefetches_hits_and_skips_the_rest(actor, db, s3, serve_env):
    cache = await start()
    for i in (1, 2, 3):
        cache.put(hexkey(i), entity_id=None, payload=dict(PAYLOAD, video_id=f"v{i}"))
    await settle_puts(cache)
    puts = {p["key_hash"]: p for p in db.put_calls}
    good = [entry_for(puts[hexkey(1)]), entry_for(puts[hexkey(2)])]
    bad_digest = CacheEntry(hexkey(3), puts[hexkey(3)]["blob_ref"], "b" * 64, 10, None, 1)
    missing = CacheEntry(hexkey(4), codec.blob_ref(NS, hexkey(4)), "c" * 64, 10, None, 1)
    swapped = CacheEntry(hexkey(5), puts[hexkey(1)]["blob_ref"], puts[hexkey(1)]["sha256"], 10, None, 1)

    found = await cache.get_blobs(good + [bad_digest, missing, swapped, None])

    assert sorted(found) == sorted([hexkey(1), hexkey(2)])
    assert found[hexkey(1)]["video_id"] == "v1" and found[hexkey(2)]["video_id"] == "v2"
    assert cache.serving is True
    assert await cache.get_blobs([]) == {}


async def test_get_blobs_reads_concurrently(actor, db, s3, serve_env):
    cache = await start()
    for i in range(6):
        cache.put(hexkey(i), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)
    entries = [entry_for(p) for p in db.put_calls]
    in_flight = peak = 0
    real_get = s3.get_object

    async def slow_get(key, *, max_bytes):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return await real_get(key, max_bytes=max_bytes)

    s3.get_object = slow_get

    found = await cache.get_blobs(entries, concurrency=3)

    assert len(found) == 6 and peak == 3


async def test_duplicate_puts_upload_once(actor, db, s3, serve_env):
    cache = await start()
    cache.put(hexkey(1), entity_id="v", payload=PAYLOAD)
    cache.put(hexkey(1), entity_id="v", payload=PAYLOAD)      # queued already
    await settle_puts(cache)
    cache.put(hexkey(1), entity_id="v", payload=PAYLOAD)      # stored already
    await settle_puts(cache)

    assert s3.puts == [codec.blob_ref(NS, hexkey(1))]
    assert cache.stats["stored"] == 1 and cache.stats["put_deduped"] == 2
    assert cache.stats["put_dropped"] == 0 and actor.log.warnings == []


async def test_uploads_run_three_at_a_time(actor, db, s3, serve_env):
    cache = await start()
    in_flight = peak = 0
    real_put = s3.put_object

    async def slow_put(key, body, *, sha256_hex, content_type="application/gzip"):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        await real_put(key, body, sha256_hex=sha256_hex, content_type=content_type)

    s3.put_object = slow_put
    for i in range(8):
        cache.put(hexkey(i), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)

    assert cache.stats["stored"] == 8 and len(db.put_calls) == 8
    assert peak == cache_module._PUT_IN_FLIGHT


async def test_failed_is_a_request_outcome_once_opted_in(actor, db, s3, serve_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_LOG_FAILED", "1")
    cache = await start()
    cache.log(hexkey(1), "a", "hit")
    cache.log(hexkey(2), "b", "failed")

    await cache.close()

    assert [r["outcome"] for r in db.log_calls[0]] == ["hit", "failed"]
    [line] = [m for m in actor.log.infos if "from cache and stored" in m]
    assert "served 1 of 2 request(s)" in line
    assert actor.log.warnings == []


async def test_failed_is_recorded_as_a_miss_until_opted_in(actor, db, s3, serve_env):
    """An index without migration 0005 rejects a whole batch holding a `failed` row."""
    cache = await start()
    cache.log(hexkey(1), "a", "failed")
    cache.log(hexkey(2), "b", "miss")

    await cache.close()

    assert [r["outcome"] for r in db.log_calls[0]] == ["miss", "miss"]
    assert cache.stats["outcomes"] == {"miss": 2}
    assert actor.log.warnings == []


async def test_an_index_write_is_retried_once(actor, db, s3, serve_env):
    db.put_fail_times = 1
    cache = await start()

    cache.put(hexkey(1), entity_id=None, payload=PAYLOAD)
    await settle_puts(cache)

    assert cache.stats["stored"] == 1 and cache.stats["put_dropped"] == 0
    assert not any("could not be indexed" in line for line in actor.log.lines)


async def test_the_mode_line_names_what_is_cached(actor, db, s3, serve_env):
    await cache_module.ResultCache.start("li-serp", 1, ttl_days=1, label="search results")
    await cache_module.ResultCache.start("li-profile", 1, ttl_days=7, label="profiles")

    assert any("on for search results" in i and "kept for 1 day." in i for i in actor.log.infos)
    assert any("on for profiles" in i and "kept for 7 days." in i for i in actor.log.infos)
