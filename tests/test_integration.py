"""Opt-in tests against the real index project.

    RESULT_CACHE_INDEX_URL=... RESULT_CACHE_INDEX_KEY=<publishable key> uv run pytest tests/test_integration.py
    (add RESULT_CACHE_SERVICE_KEY=... to exercise cache_stats and cache_quota)

Every test uses a throwaway namespace so it never touches real rows. The anon
key cannot delete, so purge afterwards from the SQL editor:

    delete from public.result_cache_requests where namespace like 'test-%';
    delete from public.result_cache_index    where namespace like 'test-%';
"""

from __future__ import annotations

import hashlib
import os
import uuid

import httpx
import pytest

from apify_result_cache.db import CacheDB, CacheDBError

URL = os.getenv("RESULT_CACHE_INDEX_URL")
KEY = os.getenv("RESULT_CACHE_INDEX_KEY")
SERVICE_KEY = os.getenv("RESULT_CACHE_SERVICE_KEY")

pytestmark = pytest.mark.skipif(not (URL and KEY),
                                reason="RESULT_CACHE_INDEX_URL / RESULT_CACHE_INDEX_KEY not set")


def h(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def row(ns: str, i: int, **over) -> dict:
    base = {"namespace": ns, "key_hash": h(f"key-{i}"), "entity_id": f"vid-{i}",
            "actor_id": "test-actor", "user_hash": h("user-a"), "is_paying": True, "outcome": "logged"}
    base.update(over)
    return base


@pytest.fixture
def ns() -> str:
    return f"test-{uuid.uuid4()}"


@pytest.fixture
async def db():
    client = CacheDB(URL, KEY)
    yield client
    await client.aclose()


async def test_log_inserts_and_counts(db, ns):
    assert await db.cache_log([row(ns, 1), row(ns, 2), row(ns, 3)]) == 3
    assert await db.cache_log([]) == 0


async def test_log_rejects_oversized_batches(db, ns):
    with pytest.raises(CacheDBError, match="HTTP 4"):
        await db.cache_log([row(ns, i) for i in range(501)])


@pytest.mark.parametrize("bad", [
    {"key_hash": "nothex"},
    {"user_hash": "ABC"},
    {"outcome": "nonsense"},
    {"actor_id": ""},
    {"entity_id": "x" * 129},
    {"is_paying": "yes"},
])
async def test_log_rejects_bad_shapes(db, ns, bad):
    with pytest.raises(CacheDBError, match="HTTP 4"):
        await db.cache_log([row(ns, 1), row(ns, 2, **bad)])


async def test_lookup_unknown_keys_is_empty(db, ns):
    assert await db.cache_lookup(ns, [h("nope")], 90) == []


async def test_put_then_lookup_roundtrip(db, ns):
    key = h("k1")
    await db.cache_put(namespace=ns, key_hash=key, schema_version=1, entity_id="vid",
                       blob_ref=f"{ns}/2026/10/{key}.json.gz", sha256=h("blob"),
                       size_bytes=1234, ttl_days=30, actor_id="test-actor", is_paying=True)

    found = await db.cache_lookup(ns, [key, h("other")], 1)
    assert len(found) == 1
    assert found[0]["key_hash"] == key
    assert found[0]["blob_ref"].endswith(".json.gz")
    assert found[0]["sha256"] == h("blob")
    assert found[0]["size_bytes"] == 1234
    assert found[0]["schema_version"] == 1

    assert await db.cache_lookup(ns, [key], 0) == []  # 0 = always fresh


@pytest.mark.parametrize("over, match", [
    ({"size_bytes": 2_000_001}, "HTTP 4"),
    ({"ttl_days": 366}, "HTTP 4"),
    ({"blob_ref": "other-namespace/x.json.gz"}, "HTTP 4"),
    ({"sha256": "zz"}, "HTTP 4"),
])
async def test_put_rejects_out_of_range(db, ns, over, match):
    fields = dict(namespace=ns, key_hash=h("k"), schema_version=1, entity_id=None,
                  blob_ref=f"{ns}/x.json.gz", sha256=h("b"), size_bytes=10, ttl_days=10,
                  actor_id="a", is_paying=False)
    fields.update(over)
    with pytest.raises(CacheDBError, match=match):
        await db.cache_put(**fields)


async def test_lookup_rejects_more_than_100_keys(db, ns):
    with pytest.raises(CacheDBError, match="HTTP 4"):
        await db.cache_lookup(ns, [h(str(i)) for i in range(101)], 90)


async def test_direct_table_access_is_denied():
    headers = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}
    async with httpx.AsyncClient(headers=headers, timeout=10) as client:
        for table in ("result_cache_requests", "result_cache_index"):
            got = await client.get(f"{URL}/rest/v1/{table}?select=*&limit=1")
            assert got.status_code >= 400 or got.json() == [], table
            posted = await client.post(f"{URL}/rest/v1/{table}", json={"namespace": "x"})
            assert posted.status_code >= 400, table


async def test_stats_is_refused_to_the_anon_key(db):
    with pytest.raises(CacheDBError, match="HTTP 4"):
        await db.cache_stats("2026-01-01", "2026-01-02", "youtube-transcript")


@pytest.mark.skipif(not SERVICE_KEY, reason="RESULT_CACHE_SERVICE_KEY not set")
async def test_stats_with_the_service_key(ns):
    anon = CacheDB(URL, KEY)
    service = CacheDB(URL, SERVICE_KEY)
    try:
        await anon.cache_log([row(ns, 1), row(ns, 1), row(ns, 2),
                              row(ns, 3, user_hash=h("user-b")), row(ns, 1, user_hash=h("user-b")),
                              row(ns, 9, entity_id="EXCLUDED")])
        from datetime import date
        today = date.today().isoformat()
        stats = await service.cache_stats(today, today, ns, exclude_entity_ids=["EXCLUDED"])
        assert stats["requests"] == 5
        assert stats["distinct_keys"] == 3
        assert abs(stats["hit_rate_any_user"] - (1 - 3 / 5)) < 1e-9
        assert abs(stats["hit_rate_same_user_only"] - (1 - 4 / 5)) < 1e-9
        assert stats["distinct_users"] == 2

        quota = await service.cache_quota()
        assert quota["db_bytes"] > 0 and "error_rows_24h" in quota
    finally:
        await anon.aclose()
        await service.aclose()
