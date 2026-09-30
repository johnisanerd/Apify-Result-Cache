"""The result cache: serve a repeat request from our own store, not the source.

Shape of the thing
------------------
One object per run, created by `ResultCache.start()`. Four calls in the Actor:
`key()` to name a request, `lookup_many()` before fetching, `put()` + `log()`
after, `close()` in the `finally`. In this version (0.1, Phase 0) only the
request log is real: `lookup_many()` always misses and `put()` is a no-op, so
an Actor's output is byte-for-byte what it was before the install. The log is
what measures the hit rate that decides whether Phase 1 is worth building.

Three properties matter more than the feature, and they are the same three the
free-tier limiter earned the hard way:

1. Absent configuration means inert. No `RESULT_CACHE_MODE`, no behaviour, one
   log line. The library can be installed fleet-wide and switched on per Actor.
2. The hot path never blocks. `log()` is synchronous and O(1); rows accumulate
   and flush in the background with at most one request in flight.
3. It fails permissive. Every failure - no config, index down, bad row - logs
   once and continues. Our infrastructure having a bad day must never break
   someone else's run.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from apify import Actor

from . import codec, config, messages
from ._version import __version__
from .db import CacheDB

# A flush is triggered once this many rows are buffered...
_BATCH_SIZE = 50
# ...and one RPC carries at most this many (matches the cap in cache_log()).
_RPC_MAX_ROWS = 500
# Bounded memory: ~300 B per row, so this is ~1.5 MB worst case.
_MAX_QUEUE = 5_000
# After this many consecutive failed flushes the cache stops trying.
_MAX_CONSECUTIVE_FAILURES = 3
# close() gives the index this long, in total, to take what is buffered.
_CLOSE_BUDGET_S = 5.0
_MAX_DRAIN_CALLS = 10

_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTITY_ID_MAX = 128


@dataclass(frozen=True)
class CacheEntry:
    """One index row, as returned by `lookup_many()` (Phase 1)."""

    key_hash: str
    blob_ref: str
    sha256: str
    size_bytes: int
    fetched_at: datetime
    schema_version: int


class ResultCache:
    """Created by `ResultCache.start()`. Do not instantiate directly."""

    def __init__(self, namespace: str, schema_version: int, hit_event_default: str | None) -> None:
        self._namespace = namespace
        self._schema_version = schema_version
        self._hit_event_default = hit_event_default
        self._config: config.Config | None = None
        self._mode = "inert"
        self._inert_reason: str | None = None
        self._active = False
        self._closed = False
        self._db: CacheDB | None = None
        self._buffer: deque[dict[str, Any]] = deque()
        self._flush_task: asyncio.Task[None] | None = None
        self._failures = 0
        self._recorded = 0
        self._flushed = 0
        self._dropped = 0
        self._warned: set[str] = set()

    # ------------------------------------------------------------------ start

    @classmethod
    async def start(
        cls,
        namespace: str,
        schema_version: int = 1,
        *,
        hit_event_default: str | None = None,
    ) -> ResultCache:
        """Read the environment, log the mode, return. No network call. Never raises."""
        cache = cls(str(namespace), schema_version, hit_event_default)
        try:
            cache._configure()
        except Exception as exc:  # noqa: BLE001 - permissive by design
            cache._active = False
            cache._mode = "inert"
            cache._inert_reason = type(exc).__name__
            Actor.log.warning(messages.unavailable(type(exc).__name__))
        return cache

    def _configure(self) -> None:
        env = os.environ
        if env.get("RESULT_CACHE_DEBUG") == "1":
            self._log_environment(env)

        if not _NAMESPACE_RE.match(self._namespace):
            self._go_inert(messages.bad_namespace(self._namespace), warning=True)
            return
        if not isinstance(self._schema_version, int) or isinstance(self._schema_version, bool) \
                or self._schema_version < 1:
            self._go_inert(messages.bad_schema_version(self._schema_version), warning=True)
            return

        resolution = config.resolve(env, at_home=bool(Actor.is_at_home()))
        for note in resolution.notes:
            Actor.log.warning(note)
        if resolution.config is None:
            self._go_inert(resolution.inert_reason or "unknown", warning=resolution.inert_is_warning)
            return

        cfg = resolution.config
        self._config = cfg
        self._mode = cfg.mode
        if cfg.mode == "serve":
            # 0.1 records only. Someone who sets `serve` early must not lose
            # the measurement, so run as keys and say so.
            Actor.log.warning(messages.serve_not_available(__version__))

        self._db = CacheDB(cfg.index_url, cfg.index_key)
        self._active = True
        Actor.log.info(messages.mode_keys())

    def _go_inert(self, reason: str, warning: bool) -> None:
        self._mode = "inert"
        self._inert_reason = reason
        self._active = False
        line = messages.inert(reason)
        if warning:
            Actor.log.warning(line)
        else:
            Actor.log.info(line)

    def _log_environment(self, env: Mapping[str, str]) -> None:
        """RESULT_CACHE_DEBUG=1. Presence and policy values, never a secret."""
        seen = config.env_presence(env)
        Actor.log.info(messages.debug_env(seen))
        resolution = config.resolve(env, at_home=bool(Actor.is_at_home()))
        cfg = resolution.config
        Actor.log.info(messages.debug_config(
            at_home=bool(Actor.is_at_home()),
            version=__version__,
            mode=cfg.mode if cfg else None,
            ttl_days=cfg.ttl_days if cfg else None,
            hit_event=(cfg.hit_event or self._hit_event_default) if cfg else None,
            force=env.get("RESULT_CACHE_FORCE") == "1",
        ))

    # ------------------------------------------------------------- properties

    @property
    def namespace(self) -> str:
        return self._namespace

    @property
    def schema_version(self) -> int:
        return self._schema_version

    @property
    def mode(self) -> str:
        """`inert`, `keys` or `serve` - what was asked for, after validation."""
        return self._mode

    @property
    def active(self) -> bool:
        """True while this run is actually recording. Flips off after the breaker trips."""
        return self._active

    @property
    def inert_reason(self) -> str | None:
        return self._inert_reason

    @property
    def hit_event(self) -> str | None:
        """The pay-per-event name to charge on a hit. Env var, else the Actor's default."""
        if self._config is not None and self._config.hit_event:
            return self._config.hit_event
        return self._hit_event_default

    @property
    def ttl_days(self) -> int:
        return self._config.ttl_days if self._config is not None else config.DEFAULT_TTL_DAYS

    @property
    def stats(self) -> dict[str, int]:
        return {
            "recorded": self._recorded,
            "flushed": self._flushed,
            "dropped": self._dropped,
            "failures": self._failures,
            "buffered": len(self._buffer),
        }

    # -------------------------------------------------------------------- key

    def key(self, fields: Mapping[str, Any]) -> str:
        """sha256 of the canonical JSON of `fields` plus this cache's `ns` and `v`.

        Works in every mode, including inert, so the Actor never branches on it.
        """
        doc = dict(fields)
        doc["ns"] = self._namespace
        doc["v"] = self._schema_version
        try:
            raw = codec.canonical_json(doc)
        except codec.CodecError:
            bad = sorted(name for name, value in doc.items() if not _is_json(value))
            self._warn_once("unstable_key", messages.unstable_key_field(bad or ["?"]))
            raw = json.dumps(doc, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, default=repr).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    # -------------------------------------------------------------------- log

    def log(self, key: str, entity_id: str | None, outcome: str) -> None:
        """Record one request. Synchronous, O(1), never raises, never blocks.

        Call from the event-loop thread. Rows are buffered and sent in the
        background; anything left at `close()` is sent then.
        """
        if not self._active or self._closed or self._config is None:
            return
        if outcome not in config.OUTCOMES:
            self._warn_once(f"outcome:{outcome!r}", messages.bad_outcome(outcome))
            return
        if not isinstance(key, str) or not _HEX64_RE.match(key):
            # One malformed row would get the whole batch rejected server-side.
            self._warn_once("bad_key", messages.bad_key(key))
            return

        if len(self._buffer) >= _MAX_QUEUE:
            self._buffer.popleft()
            self._dropped += 1
            self._warn_once("queue_full", messages.queue_full(_MAX_QUEUE))

        cfg = self._config
        self._buffer.append({
            "namespace": self._namespace,
            "key_hash": key,
            "entity_id": None if entity_id is None else str(entity_id)[:_ENTITY_ID_MAX],
            "actor_id": cfg.actor_id,
            "user_hash": cfg.user_hash,
            "is_paying": cfg.is_paying,
            "outcome": outcome,
        })
        self._recorded += 1
        if len(self._buffer) >= _BATCH_SIZE:
            self._schedule_flush()

    # ---------------------------------------------------------- phase 1 shape

    async def lookup_many(self, keys: Sequence[str], max_age_days: int) -> dict[str, CacheEntry]:
        """Index entries for `keys` fetched within `max_age_days`. Always {} in 0.1."""
        return {}

    async def get_blob(self, entry: CacheEntry) -> dict[str, Any] | None:
        """The payload behind an index entry, digest-verified. Always None in 0.1."""
        return None

    def put(self, key: str, *, entity_id: str | None, payload: Mapping[str, Any]) -> None:
        """Store a fresh payload under `key`. Fire-and-forget. No-op in 0.1."""
        return None

    # ------------------------------------------------------------------ flush

    def _schedule_flush(self) -> None:
        """At most one flush in flight. Later rows ride along in the next batch."""
        if self._flush_task is not None and not self._flush_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # wrong thread or no loop: close() will drain instead
        self._flush_task = loop.create_task(self._flush())

    async def _flush(self) -> None:
        while self._active and len(self._buffer) >= _BATCH_SIZE:
            await self._send_batch()

    async def _send_batch(self) -> None:
        if self._db is None or not self._buffer:
            return
        n = min(len(self._buffer), _RPC_MAX_ROWS)
        batch = [self._buffer.popleft() for _ in range(n)]
        sent = False
        try:
            await self._db.cache_log(batch)
            sent = True
            self._flushed += n
            self._failures = 0
        except Exception as exc:  # noqa: BLE001 - CacheDBError or anything else; never propagates
            self._failures += 1
            if self._failures == 1:
                self._warn_once("unavailable", messages.unavailable(str(exc)))
            if self._failures >= _MAX_CONSECUTIVE_FAILURES:
                await self._deactivate()
        finally:
            # A failed or cancelled batch is dropped, never re-queued: a read
            # timeout can fire after the server committed, and re-sending would
            # count the same keys twice, which biases the hit rate upward.
            if not sent:
                self._dropped += n

    async def _drain(self) -> None:
        """Give the in-flight flush the budget, then push what is left. Bounded."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _CLOSE_BUDGET_S

        task = self._flush_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=_CLOSE_BUDGET_S)
            except Exception:  # noqa: BLE001 - includes TimeoutError; the task is cancelled by then
                pass
            if not task.done():
                return  # still cancelling; leftovers are counted by _deactivate()

        calls = 0
        while self._active and self._buffer and calls < _MAX_DRAIN_CALLS:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait_for(self._send_batch(), timeout=remaining)
            except Exception:  # noqa: BLE001
                break
            calls += 1

    async def _deactivate(self) -> None:
        self._active = False
        self._dropped += len(self._buffer)
        self._buffer.clear()
        if self._db is not None:
            await self._db.aclose()
            self._db = None

    async def close(self) -> None:
        """Send what is buffered, within ~5 s, and say how it went. Never raises."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._drain()
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._deactivate()
        except Exception:  # noqa: BLE001
            pass
        if self._mode != "inert" and self._recorded:
            Actor.log.info(messages.close_summary(self._recorded, self._flushed, self._dropped))

    # ---------------------------------------------------------------- helpers

    def _warn_once(self, tag: str, line: str) -> None:
        if tag in self._warned:
            return
        self._warned.add(tag)
        Actor.log.warning(line)


def _is_json(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False
