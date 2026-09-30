"""Test rig: a fake Actor and a fake index database.

The real `apify.Actor` is a platform singleton, so every test swaps it for a
recorder. That keeps the tests honest about the two things that matter: what
the user sees in the log, and what reached the database.
"""

from __future__ import annotations

import asyncio

import pytest

from apify_result_cache import cache as cache_module
from apify_result_cache.db import CacheDBError
from apify_result_cache.s3 import S3Error


class FakeLog:
    """Records log output. `infos` / `warnings` hold what the user would see."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    @staticmethod
    def _render(message, args) -> str:
        return str(message) % args if args else str(message)

    def info(self, message, *args) -> None:
        self.infos.append(self._render(message, args))

    def warning(self, message, *args) -> None:
        self.warnings.append(self._render(message, args))

    def error(self, message, *args) -> None:
        self.errors.append(self._render(message, args))

    @property
    def lines(self) -> list[str]:
        return self.infos + self.warnings + self.errors


class FakeActor:
    """Stands in for apify.Actor."""

    def __init__(self, at_home: bool = True) -> None:
        self._at_home = at_home
        self.log = FakeLog()

    def is_at_home(self) -> bool:
        return self._at_home


class FakeDB:
    """Records every RPC so tests can assert on batches, not just totals.

    `fail` raises on every call. `hang` never returns. `gate`, when set, makes
    `cache_log` wait for the event first, to hold a flush in flight.
    """

    def __init__(self) -> None:
        self.log_calls: list[list[dict]] = []
        self.lookup_calls: list[tuple] = []
        self.put_calls: list[dict] = []
        self.closed = False
        self.fail = False
        self.hang = False
        self.gate: asyncio.Event | None = None
        self.constructed = 0
        self.url = ""
        self.lookup_rows: list[dict] = []
        self.lookup_fail = False
        self.lookup_fail_times = 0     # fail this many calls, then succeed
        self.put_fail = False

    async def cache_log(self, rows: list[dict]) -> int:
        if self.gate is not None:
            await self.gate.wait()
        if self.hang:
            await asyncio.Event().wait()
        if self.fail:
            raise CacheDBError("ConnectError")
        self.log_calls.append(list(rows))
        return len(rows)

    async def cache_lookup(self, namespace, keys, max_age_days):
        self.lookup_calls.append((namespace, list(keys), max_age_days))
        if self.lookup_fail:
            raise CacheDBError("HTTP 503")
        if self.lookup_fail_times > 0:
            self.lookup_fail_times -= 1
            raise CacheDBError("ReadTimeout")
        return [row for row in self.lookup_rows if row["key_hash"] in keys]

    async def cache_put(self, **fields) -> None:
        if self.put_fail:
            raise CacheDBError("HTTP 500")
        self.put_calls.append(fields)

    async def aclose(self) -> None:
        self.closed = True


class FakeS3:
    """An in-memory bucket with the S3Client surface. Records every call."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts: list[str] = []
        self.gets: list[str] = []
        self.fail_put = False
        self.fail_get = False
        self.hang = False
        self.closed = False
        self.init_args: tuple | None = None

    async def put_object(self, key, body, *, sha256_hex, content_type="application/gzip"):
        if self.hang:
            await asyncio.Event().wait()
        if self.fail_put:
            raise S3Error("HTTP 503")
        self.puts.append(key)
        self.objects[key] = bytes(body)

    async def get_object(self, key, *, max_bytes):
        self.gets.append(key)
        if self.fail_get:
            raise S3Error("ConnectError")
        if key not in self.objects:
            raise S3Error("NotFound")
        return self.objects[key]

    async def delete_object(self, key):
        self.objects.pop(key, None)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()

    def factory(endpoint, region, bucket, access_key, secret_key, **kwargs):
        if not str(endpoint).startswith(("http://", "https://")):
            raise ValueError("storage endpoint must be an http(s) URL")
        fake.init_args = (endpoint, region, bucket)
        return fake

    monkeypatch.setattr(cache_module, "S3Client", factory)
    return fake


@pytest.fixture
def actor(monkeypatch):
    fake = FakeActor()
    monkeypatch.setattr(cache_module, "Actor", fake)
    return fake


@pytest.fixture
def db(monkeypatch):
    fake = FakeDB()

    def factory(url, key, *args, **kwargs):
        fake.constructed += 1
        fake.url = url
        return fake

    monkeypatch.setattr(cache_module, "CacheDB", factory)
    return fake


@pytest.fixture
def keys_env(monkeypatch):
    """A configured Actor in keys mode, run by a free user."""
    monkeypatch.setenv("RESULT_CACHE_MODE", "keys")
    monkeypatch.setenv("RESULT_CACHE_INDEX_URL", "https://example.invalid")
    monkeypatch.setenv("RESULT_CACHE_INDEX_KEY", "test-key-value")
    # The limiter's variables are present on every fleet Actor; the cache must ignore them.
    monkeypatch.setenv("SUPABASE_URL", "https://limiter.invalid")
    monkeypatch.setenv("SUPABASE_KEY", "limiter-key-value")
    monkeypatch.setenv("APIFY_USER_ID", "user-1")
    monkeypatch.setenv("APIFY_ACTOR_ID", "actor-1")
    monkeypatch.setenv("APIFY_USER_IS_PAYING", "0")
    for name in ("RESULT_CACHE_FORCE", "RESULT_CACHE_DEBUG", "RESULT_CACHE_TTL_DAYS",
                 "RESULT_CACHE_HIT_EVENT"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fast_close(monkeypatch):
    """Shrink the close budget so hang tests do not sleep for 5 s."""
    monkeypatch.setattr(cache_module, "_CLOSE_BUDGET_S", 0.2)


@pytest.fixture
def serve_env(keys_env, monkeypatch):
    """keys_env, switched to serve mode with storage configured."""
    monkeypatch.setenv("RESULT_CACHE_MODE", "serve")
    monkeypatch.setenv("RESULT_CACHE_S3_ENDPOINT", "https://storage.invalid/storage/v1/s3")
    monkeypatch.setenv("RESULT_CACHE_S3_ACCESS_KEY", "s3-access-value")
    monkeypatch.setenv("RESULT_CACHE_S3_SECRET_KEY", "s3-secret-value")
    for name in ("RESULT_CACHE_S3_REGION", "RESULT_CACHE_S3_BUCKET", "RESULT_CACHE_FRESH_EVENT"):
        monkeypatch.delenv(name, raising=False)
