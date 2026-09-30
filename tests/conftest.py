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
        return []

    async def cache_put(self, **fields) -> None:
        self.put_calls.append(fields)

    async def aclose(self) -> None:
        self.closed = True


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
