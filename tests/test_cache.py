"""Behaviour tests for ResultCache.

Grouped by the promise each one protects: it is inert exactly when it should
be, it records the right rows, and nothing about it can break a run.
"""

from __future__ import annotations

import asyncio
import hashlib
import time

import httpx
import pytest

from apify_result_cache import CacheEntry, ResultCache
from apify_result_cache import cache as cache_module
from apify_result_cache.db import CacheDB, CacheDBError

NS = "youtube-transcript"


async def start(**kwargs) -> ResultCache:
    return await ResultCache.start(namespace=NS, schema_version=1, **kwargs)


def hexkey(i: int) -> str:
    return hashlib.sha256(str(i).encode()).hexdigest()


async def settle(cache: ResultCache) -> None:
    """Let the background flush task run to completion."""
    task = cache._flush_task
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)


# ------------------------------------------ promise 1: inert exactly when it should be


async def test_mode_absent_is_inert_with_one_info_line(actor, db, keys_env, monkeypatch):
    monkeypatch.delenv("RESULT_CACHE_MODE")

    cache = await start()
    cache.log(hexkey(1), "v1", "logged")
    await cache.close()

    assert cache.mode == "inert"
    assert cache.active is False
    assert "RESULT_CACHE_MODE not set" in cache.inert_reason
    assert len(actor.log.infos) == 1
    assert "inert" in actor.log.infos[0] and "RESULT_CACHE_MODE" in actor.log.infos[0]
    assert actor.log.warnings == []
    assert db.constructed == 0


async def test_mode_off_is_inert(actor, db, keys_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_MODE", "off")

    cache = await start()

    assert cache.mode == "inert"
    assert actor.log.warnings == []
    assert db.constructed == 0


async def test_unknown_mode_is_inert_with_a_warning(actor, db, keys_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_MODE", "bogus")

    cache = await start()

    assert cache.mode == "inert"
    assert any("bogus" in w for w in actor.log.warnings)
    assert db.constructed == 0


async def test_off_platform_is_inert(actor, db, keys_env):
    actor._at_home = False

    cache = await start()

    assert cache.mode == "inert"
    assert "platform" in cache.inert_reason
    assert actor.log.warnings == []


async def test_force_overrides_the_platform_gate(actor, db, keys_env, monkeypatch):
    actor._at_home = False
    monkeypatch.setenv("RESULT_CACHE_FORCE", "1")

    cache = await start()

    assert cache.mode == "keys"
    assert cache.active is True


async def test_missing_index_config_warns(actor, db, keys_env, monkeypatch):
    monkeypatch.delenv("RESULT_CACHE_INDEX_KEY")

    cache = await start()

    assert cache.mode == "inert"
    assert any("not configured" in w and "RESULT_CACHE_INDEX_KEY" in w for w in actor.log.warnings)
    assert db.constructed == 0


async def test_limiter_variables_are_never_used_as_the_index(actor, db, keys_env, monkeypatch):
    """SUPABASE_URL/KEY point at the limiter's project, which also holds 0.1.0's
    copy of the cache tables. Falling back to them would log to the wrong place."""
    monkeypatch.delenv("RESULT_CACHE_INDEX_URL")
    monkeypatch.delenv("RESULT_CACHE_INDEX_KEY")

    cache = await start()

    assert cache.mode == "inert"
    assert db.constructed == 0


async def test_uses_the_index_address(actor, db, keys_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_INDEX_URL", "https://index.invalid/")

    cache = await start()

    assert cache.active is True
    assert db.url == "https://index.invalid"


async def test_missing_identity_warns(actor, db, keys_env, monkeypatch):
    monkeypatch.delenv("APIFY_USER_ID")

    cache = await start()

    assert cache.mode == "inert"
    assert any("identity" in w for w in actor.log.warnings)


async def test_serve_without_storage_records_keys_with_one_warning(actor, db, keys_env, monkeypatch):
    """Setting serve before the storage keys exist must not switch measurement off."""
    monkeypatch.setenv("RESULT_CACHE_MODE", "serve")

    cache = await start()
    cache.log(hexkey(1), "v1", "logged")
    await cache.close()

    assert cache.mode == "serve"
    assert cache.serving is False
    assert len([w for w in actor.log.warnings if "needs storage settings" in w]) == 1
    assert [r["key_hash"] for r in db.log_calls[0]] == [hexkey(1)]


async def test_bad_namespace_is_inert_without_raising(actor, db, keys_env):
    cache = await ResultCache.start(namespace="Bad NS!", schema_version=1)
    cache.log(hexkey(1), "v1", "logged")
    await cache.close()

    assert cache.mode == "inert"
    assert any("namespace" in w for w in actor.log.warnings)
    assert db.constructed == 0


async def test_bad_schema_version_is_inert_without_raising(actor, db, keys_env):
    cache = await ResultCache.start(namespace=NS, schema_version=0)

    assert cache.mode == "inert"
    assert any("schema_version" in w for w in actor.log.warnings)


async def test_start_makes_no_database_calls(actor, db, keys_env):
    cache = await start()

    assert cache.active is True
    assert db.log_calls == []
    assert db.lookup_calls == []
    assert any("mode keys" in m for m in actor.log.infos)


async def test_key_works_on_the_inert_object(actor, db, keys_env, monkeypatch):
    monkeypatch.delenv("RESULT_CACHE_MODE")
    cache = await start()

    key = cache.key({"video_id": "abc"})

    assert len(key) == 64 and int(key, 16) >= 0


async def test_bad_ttl_is_noted_and_defaulted(actor, db, keys_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_TTL_DAYS", "ninety")

    cache = await start()

    assert cache.active is True
    assert cache.ttl_days == 90
    assert any("RESULT_CACHE_TTL_DAYS" in w for w in actor.log.warnings)


async def test_ttl_is_clamped(actor, db, keys_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_TTL_DAYS", "9999")

    cache = await start()

    assert cache.ttl_days == 365


async def test_hit_event_env_beats_the_actor_default(actor, db, keys_env, monkeypatch):
    cache = await start(hit_event_default="videoprocessed")
    assert cache.hit_event == "videoprocessed"

    monkeypatch.setenv("RESULT_CACHE_HIT_EVENT", "videocached")
    cache = await start(hit_event_default="videoprocessed")
    assert cache.hit_event == "videocached"


# --------------------------------------------- promise 2: it records the right rows


async def test_row_shape_and_values(actor, db, keys_env, monkeypatch):
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "1")

    cache = await start()
    cache.log(hexkey(7), "vid-7", "logged")
    await cache.close()

    [[row]] = db.log_calls
    assert row == {
        "namespace": NS,
        "key_hash": hexkey(7),
        "entity_id": "vid-7",
        "actor_id": "actor-1",
        "user_hash": hashlib.sha256(b"user-1").hexdigest(),
        "is_paying": True,
        "outcome": "logged",
    }


async def test_paying_users_are_recorded_not_skipped(actor, db, keys_env, monkeypatch):
    """Paying callers are the population being measured."""
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "1")

    cache = await start()
    assert cache.active is True


async def test_49_rows_do_not_flush_and_the_50th_does(actor, db, keys_env):
    cache = await start()

    for i in range(49):
        cache.log(hexkey(i), None, "logged")
    await asyncio.sleep(0)
    assert db.log_calls == []

    cache.log(hexkey(49), None, "logged")
    await settle(cache)
    assert [len(b) for b in db.log_calls] == [50]
    assert [r["key_hash"] for r in db.log_calls[0]] == [hexkey(i) for i in range(50)]


async def test_large_bursts_go_in_batches_of_500(actor, db, keys_env):
    cache = await start()

    for i in range(1200):
        cache.log(hexkey(i), None, "logged")
    await settle(cache)

    assert [len(b) for b in db.log_calls] == [500, 500, 200]
    assert cache.stats["flushed"] == 1200


async def test_only_one_flush_in_flight(actor, db, keys_env):
    db.gate = asyncio.Event()
    cache = await start()

    for i in range(50):
        cache.log(hexkey(i), None, "logged")
    await asyncio.sleep(0)  # the task starts and blocks on the gate
    first_task = cache._flush_task
    for i in range(50, 100):
        cache.log(hexkey(i), None, "logged")

    assert cache._flush_task is first_task
    assert db.log_calls == []

    db.gate.set()
    await settle(cache)
    assert [len(b) for b in db.log_calls] == [50, 50]


async def test_close_flushes_a_partial_buffer(actor, db, keys_env):
    cache = await start()
    for i in range(7):
        cache.log(hexkey(i), None, "logged")

    await cache.close()

    assert [len(b) for b in db.log_calls] == [7]
    assert db.closed is True


async def test_close_is_idempotent(actor, db, keys_env):
    cache = await start()
    cache.log(hexkey(1), None, "logged")

    await cache.close()
    await cache.close()

    assert [len(b) for b in db.log_calls] == [1]
    assert len([m for m in actor.log.infos if "recorded" in m]) == 1


async def test_log_after_close_records_nothing(actor, db, keys_env):
    cache = await start()
    await cache.close()

    cache.log(hexkey(1), None, "logged")

    assert cache.stats["recorded"] == 0
    assert db.log_calls == []


async def test_duplicate_keys_in_one_run_are_logged_twice(actor, db, keys_env):
    """Two requests for the same thing are two requests; that is the measurement."""
    cache = await start()
    cache.log(hexkey(1), "v", "logged")
    cache.log(hexkey(1), "v", "logged")
    await cache.close()

    assert [len(b) for b in db.log_calls] == [2]


async def test_bad_outcome_is_dropped_with_one_warning(actor, db, keys_env):
    cache = await start()
    cache.log(hexkey(1), None, "nonsense")
    cache.log(hexkey(2), None, "nonsense")
    await cache.close()

    assert db.log_calls == []
    assert len([w for w in actor.log.warnings if "outcome" in w]) == 1


async def test_malformed_key_is_dropped_with_one_warning(actor, db, keys_env):
    cache = await start()
    cache.log("not-a-hash", None, "logged")
    cache.log("ABCDEF" * 10 + "ABCD", None, "logged")  # upper-case hex is not canonical
    await cache.close()

    assert db.log_calls == []
    assert len([w for w in actor.log.warnings if "malformed" in w]) == 1


async def test_entity_id_is_truncated(actor, db, keys_env):
    cache = await start()
    cache.log(hexkey(1), "x" * 300, "logged")
    await cache.close()

    assert len(db.log_calls[0][0]["entity_id"]) == 128


async def test_queue_cap_drops_oldest_with_one_warning(actor, db, keys_env, fast_close, monkeypatch):
    monkeypatch.setattr(cache_module, "_MAX_QUEUE", 100)
    db.hang = True
    cache = await start()

    for i in range(150):
        cache.log(hexkey(i), None, "logged")

    assert cache.stats["buffered"] == 100
    assert cache.stats["dropped"] == 50
    assert cache.stats["recorded"] == 150
    assert len([w for w in actor.log.warnings if "buffered" in w]) == 1

    await cache.close()  # the hung flush is cancelled; everything counts as dropped
    assert cache.stats["dropped"] == 150


async def test_close_summary_line(actor, db, keys_env):
    cache = await start()
    for i in range(3):
        cache.log(hexkey(i), None, "logged")
    await cache.close()

    [line] = [m for m in actor.log.infos if "recorded" in m]
    assert "recorded 3 of 3 request key(s)" in line
    assert "not recorded" not in line


async def test_no_summary_when_nothing_was_recorded(actor, db, keys_env):
    cache = await start()
    await cache.close()

    assert not any("recorded" in m for m in actor.log.infos)


# ------------------------------------------ promise 3: it cannot break a run


async def test_flush_failure_is_dropped_with_one_warning(actor, db, keys_env):
    db.fail = True
    cache = await start()

    for i in range(50):
        cache.log(hexkey(i), None, "logged")
    await settle(cache)

    stats = cache.stats
    assert (stats["recorded"], stats["flushed"], stats["dropped"], stats["failures"], stats["buffered"]) \
        == (50, 0, 50, 1, 0)
    assert cache.active is True
    [warning] = [w for w in actor.log.warnings if "continuing without it" in w]
    assert "ConnectError" in warning


async def test_three_consecutive_failures_trip_the_breaker(actor, db, keys_env):
    db.fail = True
    cache = await start()

    # Three separate flushes. (150 rows logged without yielding would go as a
    # single 150-row batch, which is one failure, not three.)
    for batch in range(3):
        for i in range(batch * 50, batch * 50 + 50):
            cache.log(hexkey(i), None, "logged")
        await settle(cache)

    assert cache.active is False
    assert db.closed is True
    assert cache.stats["failures"] == 3
    assert cache.stats["dropped"] == 150
    assert len([w for w in actor.log.warnings if "continuing without it" in w]) == 1

    cache.log(hexkey(999), None, "logged")
    assert cache.stats["recorded"] == 150  # inert from here on

    await cache.close()
    [line] = [m for m in actor.log.infos if "recorded" in m]
    assert "recorded 0 of 150" in line and "(150 not recorded.)" in line


async def test_a_success_resets_the_failure_counter(actor, db, keys_env):
    db.fail = True
    cache = await start()
    for i in range(50):
        cache.log(hexkey(i), None, "logged")
    await settle(cache)
    assert cache.stats["failures"] == 1

    db.fail = False
    for i in range(50, 100):
        cache.log(hexkey(i), None, "logged")
    await settle(cache)

    assert cache.stats["failures"] == 0
    assert [len(b) for b in db.log_calls] == [50]
    assert cache.active is True


async def test_hung_database_close_returns_within_budget(actor, db, keys_env, fast_close):
    db.hang = True
    cache = await start()
    for i in range(50):
        cache.log(hexkey(i), None, "logged")
    await asyncio.sleep(0)

    started = time.monotonic()
    await cache.close()

    assert time.monotonic() - started < 1.5
    assert cache.stats["flushed"] == 0
    assert cache.stats["dropped"] == 50
    [line] = [m for m in actor.log.infos if "recorded" in m]
    assert "(50 not recorded.)" in line


async def test_hung_database_close_with_partial_buffer_is_bounded(actor, db, keys_env, fast_close):
    db.hang = True
    cache = await start()
    for i in range(7):
        cache.log(hexkey(i), None, "logged")

    started = time.monotonic()
    await cache.close()

    assert time.monotonic() - started < 1.5
    assert cache.stats["dropped"] == 7


async def test_close_completes_a_flush_that_is_in_flight(actor, db, keys_env):
    db.gate = asyncio.Event()
    cache = await start()
    for i in range(50):
        cache.log(hexkey(i), None, "logged")
    await asyncio.sleep(0)

    closing = asyncio.create_task(cache.close())
    await asyncio.sleep(0)
    db.gate.set()
    await closing

    assert [len(b) for b in db.log_calls] == [50]
    assert cache.stats["flushed"] == 50


async def test_start_never_raises_even_if_configure_blows_up(actor, db, keys_env, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("secret detail https://example.invalid")

    monkeypatch.setattr(cache_module.config, "resolve", boom)

    cache = await start()

    assert cache.mode == "inert"
    assert any("RuntimeError" in w for w in actor.log.warnings)
    assert not any("example.invalid" in w for w in actor.log.warnings)


async def test_no_log_line_leaks_the_backend_or_the_key(actor, db, keys_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_DEBUG", "1")
    db.fail = True
    cache = await start()
    for i in range(50):
        cache.log(hexkey(i), None, "logged")
    await settle(cache)
    await cache.close()

    text = "\n".join(actor.log.lines).lower()
    assert "supabase" not in text
    assert "example.invalid" not in text
    assert "limiter.invalid" not in text
    assert "test-key-value" not in text
    assert "limiter-key-value" not in text
    assert "index_key': 'set'" in text or "'index_key': 'set'" in text


async def test_debug_dump_reports_presence_only(actor, db, keys_env, monkeypatch):
    monkeypatch.setenv("RESULT_CACHE_DEBUG", "1")
    monkeypatch.delenv("RESULT_CACHE_INDEX_KEY")

    await start()

    [env_line] = [m for m in actor.log.infos if "debug] env" in m]
    assert "'index_key': 'MISSING'" in env_line
    assert "'index_url': 'set'" in env_line
    assert "'RESULT_CACHE_MODE': 'keys'" in env_line


async def test_key_with_unserialisable_field_warns_once_and_still_hashes(actor, db, keys_env):
    cache = await start()

    k1 = cache.key({"video_id": "abc", "weird": object()})
    k2 = cache.key({"video_id": "abc", "weird": object()})

    assert len(k1) == 64 and len(k2) == 64
    assert len([w for w in actor.log.warnings if "not JSON-serialisable" in w]) == 1


# --------------------------------------------------------- the PostgREST client


def _transport(handler):
    return httpx.MockTransport(handler)


async def test_rpc_hides_exception_details():
    def handler(request):
        raise httpx.ConnectError("boom https://secret.invalid/rest", request=request)

    db = CacheDB("https://secret.invalid", "k", transport=_transport(handler))
    with pytest.raises(CacheDBError) as exc:
        await db.cache_log([])
    assert str(exc.value) == "ConnectError"
    await db.aclose()


async def test_rpc_reports_http_status_only():
    def handler(request):
        return httpx.Response(401, json={"message": "JWT expired at https://secret.invalid"})

    db = CacheDB("https://secret.invalid", "k", transport=_transport(handler))
    with pytest.raises(CacheDBError) as exc:
        await db.cache_log([])
    assert str(exc.value) == "HTTP 401"
    await db.aclose()


async def test_rpc_sends_the_expected_shape_and_parses_the_count():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read()
        return httpx.Response(200, content=b"3")

    db = CacheDB("https://secret.invalid/", "anon-key", transport=_transport(handler))
    n = await db.cache_log([{"a": 1}, {"a": 2}, {"a": 3}])
    await db.aclose()

    assert n == 3
    assert seen["url"] == "https://secret.invalid/rest/v1/rpc/cache_log"
    assert seen["headers"]["apikey"] == "anon-key"
    assert seen["headers"]["authorization"] == "Bearer anon-key"
    assert seen["body"] == b'{"p_rows":[{"a":1},{"a":2},{"a":3}]}'


async def test_lookup_gets_a_longer_read_timeout():
    seen = {}

    def handler(request):
        seen["timeout"] = request.extensions.get("timeout")
        return httpx.Response(200, json=[])

    db = CacheDB("https://secret.invalid", "k", transport=_transport(handler))
    await db.cache_lookup("ns", ["a" * 64], 90)
    await db.aclose()

    assert seen["timeout"]["read"] == 4.0


async def test_rpc_void_and_bad_bodies():
    def void(request):
        return httpx.Response(204)

    def garbage(request):
        return httpx.Response(200, content=b"<html>")

    db = CacheDB("https://secret.invalid", "k", transport=_transport(void))
    await db.cache_put(namespace="n", key_hash="a" * 64, schema_version=1, entity_id=None,
                       blob_ref="n/x", sha256="b" * 64, size_bytes=1, ttl_days=1,
                       actor_id="a", is_paying=False)
    await db.aclose()

    db = CacheDB("https://secret.invalid", "k", transport=_transport(garbage))
    with pytest.raises(CacheDBError) as exc:
        await db.cache_log([])
    assert str(exc.value) == "bad response"
    await db.aclose()


# ------------------------------------------------------- phase 1 surface, present


async def test_phase_1_surface_is_inert_in_keys_mode(actor, db, keys_env):
    cache = await start()

    assert await cache.lookup_many([hexkey(1)], max_age_days=90) == {}
    assert await cache.get_blob(CacheEntry(hexkey(1), "ns/x", "a" * 64, 1, None, 1)) is None
    assert cache.put(hexkey(1), entity_id="v", payload={"a": 1}) is None
    assert db.lookup_calls == []
    assert db.put_calls == []
