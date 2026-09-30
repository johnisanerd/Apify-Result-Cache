"""The result cache: serve a repeat request from our own store, not the source.

Shape of the thing
------------------
One object per run, created by `ResultCache.start()`. The Actor calls:

    key()          name a request (pure; works in every mode)
    lookup_many()  once, before its videos start: which keys are cached (serve)
    get_blob()     on a hit: the stored payload, digest-verified (serve)
    put()          after a fresh fetch: store the payload (serve, fire-and-forget)
    log()          exactly one outcome per request: logged, hit, miss, bypass, error
    close()        in the `finally`: send what is buffered, bounded, never raises

Modes, from RESULT_CACHE_MODE:

    unset / off    inert: one log line, nothing else
    keys           record request keys only; the Actor's output is unchanged
    serve          look up, serve hits from storage, store fresh results

Three properties matter more than the feature, and they are the same three the
free-tier limiter earned the hard way:

1. Absent configuration means inert. The library can be installed fleet-wide
   and switched on per Actor.
2. The hot path never blocks. `log()` and `put()` are synchronous and cheap;
   rows and payloads go out in the background with one request in flight each.
   The only awaited network calls are the single lookup before the videos
   start and the object GET on a hit.
3. It fails permissive. Index down, storage down, a corrupt object: each one is
   a miss, logged once. Our infrastructure having a bad day must never break
   someone else's run.
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
import re
from collections import Counter, deque
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from apify import Actor

from . import codec, config, messages
from ._version import __version__
from .db import CacheDB
from .s3 import S3Client, S3Error

# A log flush is triggered once this many rows are buffered...
_BATCH_SIZE = 50
# ...and one RPC carries at most this many (matches the cap in cache_log()).
_RPC_MAX_ROWS = 500
# Bounded memory for the request log: ~300 B per row, so ~1.5 MB worst case.
_MAX_QUEUE = 5_000
# After this many consecutive failures a subsystem (log flushes, storage) stops.
_MAX_CONSECUTIVE_FAILURES = 3
# close() gives the index and the storage this long, in total.
_CLOSE_BUDGET_S = 6.0
_MAX_DRAIN_CALLS = 10
# cache_lookup() accepts at most this many keys per call; each call gets one retry.
_LOOKUP_CHUNK = 100
_LOOKUP_ATTEMPTS = 2
# cache_put() refuses larger objects; the largest real transcript is ~0.4 MB gzipped.
_BLOB_MAX_BYTES = 2_000_000
# Payloads waiting to be stored, in encoded bytes. Past this, new ones are dropped.
_PUT_QUEUE_BYTES = 32 * 1024 * 1024

_NAMESPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_ENTITY_ID_MAX = 128


@dataclass(frozen=True)
class CacheEntry:
    """One index row, as returned by `lookup_many()`."""

    key_hash: str
    blob_ref: str
    sha256: str
    size_bytes: int
    fetched_at: datetime.datetime | None
    schema_version: int

    def age_days(self, now: datetime.datetime | None = None) -> int:
        """Whole days since the payload was fetched from the source."""
        if self.fetched_at is None:
            return 0
        now = now or datetime.datetime.now(datetime.timezone.utc)
        return max(0, (now - self.fetched_at).days)


class ResultCache:
    """Created by `ResultCache.start()`. Do not instantiate directly."""

    def __init__(self, namespace: str, schema_version: int,
                 hit_event_default: str | None, fresh_event_default: str | None) -> None:
        self._namespace = namespace
        self._schema_version = schema_version
        self._hit_event_default = hit_event_default
        self._fresh_event_default = fresh_event_default
        self._config: config.Config | None = None
        self._mode = "inert"
        self._inert_reason: str | None = None
        self._active = False
        self._closed = False
        self._warned: set[str] = set()
        # request log
        self._db: CacheDB | None = None
        self._buffer: deque[dict[str, Any]] = deque()
        self._flush_task: asyncio.Task[None] | None = None
        self._failures = 0
        self._recorded = 0
        self._flushed = 0
        self._dropped = 0
        self._outcomes: Counter[str] = Counter()
        # serving
        self._s3: S3Client | None = None
        self._serving = False
        self._serve_started = False
        self._storage_failures = 0
        self._put_queue: deque[tuple[str, str | None, bytes, str, int]] = deque()
        self._put_bytes = 0
        self._put_task: asyncio.Task[None] | None = None
        self._stored = 0
        self._put_dropped = 0

    # ------------------------------------------------------------------ start

    @classmethod
    async def start(
        cls,
        namespace: str,
        schema_version: int = 1,
        *,
        hit_event_default: str | None = None,
        fresh_event_default: str | None = None,
    ) -> ResultCache:
        """Read the environment, log the mode, return. No network call. Never raises."""
        cache = cls(str(namespace), schema_version, hit_event_default, fresh_event_default)
        try:
            cache._configure()
        except Exception as exc:  # noqa: BLE001 - permissive by design
            cache._active = False
            cache._serving = False
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
        self._db = CacheDB(cfg.index_url, cfg.index_key)
        self._active = True

        if cfg.mode == "serve":
            if not cfg.storage_configured:
                Actor.log.warning(messages.storage_not_configured())
            else:
                try:
                    self._s3 = S3Client(cfg.s3_endpoint or "", cfg.s3_region, cfg.s3_bucket,
                                        cfg.s3_access_key or "", cfg.s3_secret_key or "")
                except ValueError:
                    Actor.log.warning(messages.storage_endpoint_invalid())
                else:
                    self._serving = True
                    self._serve_started = True
                    Actor.log.info(messages.mode_serve(cfg.ttl_days))
                    return
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
        Actor.log.info(messages.debug_env(config.env_presence(env)))
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
        """True while this run is recording request keys."""
        return self._active

    @property
    def serving(self) -> bool:
        """True while hits can be served and fresh results stored.

        False in keys mode, when storage is not configured, after the storage
        breaker trips, and after `close()`.
        """
        return self._serving and not self._closed

    @property
    def inert_reason(self) -> str | None:
        return self._inert_reason

    @property
    def hit_event(self) -> str | None:
        """The pay-per-event name to charge for a hit. Env var, else the Actor's default."""
        if self._config is not None and self._config.hit_event:
            return self._config.hit_event
        return self._hit_event_default

    @property
    def fresh_event(self) -> str | None:
        """The extra event for a forced fresh fetch. Env var, else the Actor's default."""
        if self._config is not None and self._config.fresh_event:
            return self._config.fresh_event
        return self._fresh_event_default

    @property
    def ttl_days(self) -> int:
        return self._config.ttl_days if self._config is not None else config.DEFAULT_TTL_DAYS

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "recorded": self._recorded,
            "flushed": self._flushed,
            "dropped": self._dropped,
            "failures": self._failures,
            "buffered": len(self._buffer),
            "outcomes": dict(self._outcomes),
            "stored": self._stored,
            "put_dropped": self._put_dropped,
            "queued_puts": len(self._put_queue),
            "storage_failures": self._storage_failures,
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

        Call exactly once per request, from the event-loop thread. Rows are
        buffered and sent in the background; anything left at `close()` is
        sent then.
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
        self._outcomes[outcome] += 1
        if len(self._buffer) >= _BATCH_SIZE:
            self._schedule_flush()

    # ---------------------------------------------------------------- serving

    async def lookup_many(self, keys: Sequence[str], max_age_days: int) -> dict[str, CacheEntry]:
        """Index entries for `keys` fetched within `max_age_days` and not expired.

        One RPC per 100 keys, retried once: this is the one place a run can
        afford to wait, and a single slow answer would otherwise turn every hit
        in the run into a fresh fetch. Returns {} when not serving, when
        max_age_days is 0 or less, and on any failure. Entries written under
        another schema_version are left out: they are misses.
        """
        if not self.serving or self._db is None:
            return {}
        try:
            days = int(max_age_days)
        except (TypeError, ValueError):
            return {}
        if days <= 0:
            return {}
        wanted = sorted({k for k in keys if isinstance(k, str) and _HEX64_RE.match(k)})
        found: dict[str, CacheEntry] = {}
        for start in range(0, len(wanted), _LOOKUP_CHUNK):
            chunk = wanted[start:start + _LOOKUP_CHUNK]
            rows = None
            last_exc: BaseException | None = None
            for _attempt in range(_LOOKUP_ATTEMPTS):
                try:
                    rows = await self._db.cache_lookup(self._namespace, chunk, days)
                    break
                except Exception as exc:  # noqa: BLE001 - a failed lookup is a miss
                    last_exc = exc
            if rows is None:
                self._warn_once("lookup", messages.lookup_unavailable(str(last_exc)))
                return found
            for row in rows:
                entry = _entry_from_row(row)
                if entry is not None and entry.schema_version == self._schema_version:
                    found[entry.key_hash] = entry
        return found

    async def get_blob(self, entry: CacheEntry | None) -> dict[str, Any] | None:
        """The payload behind an index entry, digest-verified. None on any failure."""
        if not self.serving or self._s3 is None or not isinstance(entry, CacheEntry):
            return None
        try:
            blob = await self._s3.get_object(entry.blob_ref, max_bytes=_BLOB_MAX_BYTES)
        except S3Error as exc:
            if str(exc) != "NotFound":      # a missing object is not an outage
                self._storage_failed(exc)
            return None
        except Exception as exc:  # noqa: BLE001
            self._storage_failed(exc)
            return None
        self._storage_failures = 0
        try:
            return codec.decode(blob, entry.sha256)
        except codec.CodecError as exc:
            self._warn_once("corrupt", messages.blob_rejected(str(exc)))
            return None

    def put(self, key: str, *, entity_id: str | None, payload: Mapping[str, Any]) -> None:
        """Store a fresh payload under `key`. Synchronous and fire-and-forget; never raises.

        The payload is encoded now (so later changes to the caller's dicts
        cannot leak in) and uploaded in the background.
        """
        if not self.serving:
            return
        if not isinstance(key, str) or not _HEX64_RE.match(key):
            self._warn_once("bad_key", messages.bad_key(key))
            return
        try:
            blob, sha, size = codec.encode(dict(payload))
        except (codec.CodecError, TypeError, ValueError) as exc:
            self._put_dropped += 1
            self._warn_once("encode", messages.store_skipped(type(exc).__name__))
            return
        if size > _BLOB_MAX_BYTES:
            self._put_dropped += 1
            self._warn_once("too_large", messages.store_skipped("too large"))
            return
        if self._put_bytes + size > _PUT_QUEUE_BYTES:
            self._put_dropped += 1
            self._warn_once("put_queue", messages.store_skipped("store queue full"))
            return
        self._put_queue.append((key, None if entity_id is None else str(entity_id)[:_ENTITY_ID_MAX],
                                blob, sha, size))
        self._put_bytes += size
        self._schedule_puts()

    def _schedule_puts(self) -> None:
        if self._put_task is not None and not self._put_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # wrong thread or no loop: close() will drain instead
        self._put_task = loop.create_task(self._put_worker())

    async def _put_worker(self) -> None:
        while self._put_queue and self._serving and self._s3 is not None and self._db is not None:
            item = self._put_queue.popleft()
            self._put_bytes -= item[4]
            stored = False
            try:
                stored = await self._store_one(item)
            finally:
                # Covers cancellation at close too: an unfinished upload is dropped.
                if not stored:
                    self._put_dropped += 1

    async def _store_one(self, item: tuple[str, str | None, bytes, str, int]) -> bool:
        key, entity_id, blob, sha, size = item
        s3, db, cfg = self._s3, self._db, self._config
        if s3 is None or db is None or cfg is None:
            return False
        ref = codec.blob_ref(self._namespace, key)
        try:
            await s3.put_object(ref, blob, sha256_hex=sha)
        except Exception as exc:  # noqa: BLE001
            self._storage_failed(exc)
            return False
        self._storage_failures = 0
        try:
            await db.cache_put(
                namespace=self._namespace, key_hash=key, schema_version=self._schema_version,
                entity_id=entity_id, blob_ref=ref, sha256=sha, size_bytes=size,
                ttl_days=cfg.ttl_days, actor_id=cfg.actor_id, is_paying=cfg.is_paying,
            )
        except Exception as exc:  # noqa: BLE001
            self._warn_once("index_put", messages.index_write_failed(str(exc)))
            return False
        self._stored += 1
        return True

    def _storage_failed(self, exc: BaseException) -> None:
        self._storage_failures += 1
        self._warn_once("storage", messages.storage_unavailable(str(exc) or type(exc).__name__))
        if self._storage_failures >= _MAX_CONSECUTIVE_FAILURES:
            # Stop trying for the rest of the run: every video fetches fresh.
            self._serving = False
            self._put_dropped += len(self._put_queue)
            self._put_queue.clear()
            self._put_bytes = 0

    # ------------------------------------------------------------- log flush

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
                await self._deactivate_log()
        finally:
            # A failed or cancelled batch is dropped, never re-queued: a read
            # timeout can fire after the server committed, and re-sending would
            # count the same keys twice, which biases the hit rate upward.
            if not sent:
                self._dropped += n

    async def _deactivate_log(self) -> None:
        """The log breaker: stop recording. Serving is independent and carries on."""
        self._active = False
        self._dropped += len(self._buffer)
        self._buffer.clear()
        if not self._serving and self._db is not None:
            await self._db.aclose()
            self._db = None

    # ------------------------------------------------------------------ close

    async def _drain_log(self, deadline: float) -> None:
        loop = asyncio.get_running_loop()
        task = self._flush_task
        if task is not None and not task.done():
            remaining = deadline - loop.time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(task, timeout=remaining)
                except Exception:  # noqa: BLE001 - includes TimeoutError; the task is cancelled by then
                    pass
            if not task.done():
                return
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

    async def _drain_puts(self, deadline: float) -> None:
        task = self._put_task
        running = task is not None and not task.done()
        if not running and not self._put_queue:
            return
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return
        try:
            if running:
                await asyncio.wait_for(task, timeout=remaining)  # the worker empties the queue
            elif self._serving:
                await asyncio.wait_for(self._put_worker(), timeout=remaining)
        except Exception:  # noqa: BLE001 - includes TimeoutError; leftovers are dropped below
            pass

    async def _deactivate(self) -> None:
        self._active = False
        self._serving = False
        self._dropped += len(self._buffer)
        self._buffer.clear()
        self._put_dropped += len(self._put_queue)
        self._put_queue.clear()
        self._put_bytes = 0
        if self._db is not None:
            await self._db.aclose()
            self._db = None
        if self._s3 is not None:
            await self._s3.aclose()
            self._s3 = None

    async def close(self) -> None:
        """Send what is buffered, within ~6 s, and say how it went. Never raises."""
        if self._closed:
            return
        self._closed = True
        try:
            deadline = asyncio.get_running_loop().time() + _CLOSE_BUDGET_S
            await self._drain_log(deadline)          # small and essential: the stats
            await self._drain_puts(deadline)         # best effort: an unsent result is a future miss
        except Exception:  # noqa: BLE001
            pass
        try:
            await self._deactivate()
        except Exception:  # noqa: BLE001
            pass
        if self._mode == "inert" or not (self._recorded or self._stored):
            return
        if self._serve_started:
            requests = sum(self._outcomes[o] for o in ("hit", "miss", "bypass", "error"))
            Actor.log.info(messages.serve_summary(
                self._outcomes["hit"], requests, self._stored,
                self._recorded, self._flushed, self._dropped,
            ))
        else:
            Actor.log.info(messages.close_summary(self._recorded, self._flushed, self._dropped))

    # ---------------------------------------------------------------- helpers

    def _warn_once(self, tag: str, line: str) -> None:
        if tag in self._warned:
            return
        self._warned.add(tag)
        Actor.log.warning(line)


def _entry_from_row(row: Any) -> CacheEntry | None:
    """Parse one cache_lookup() row. Anything malformed is skipped, i.e. a miss."""
    try:
        key_hash = str(row["key_hash"])
        if not _HEX64_RE.match(key_hash):
            return None
        fetched_raw = row.get("fetched_at")
        fetched_at = None
        if fetched_raw:
            fetched_at = datetime.datetime.fromisoformat(str(fetched_raw).replace("Z", "+00:00"))
            if fetched_at.tzinfo is None:
                fetched_at = fetched_at.replace(tzinfo=datetime.timezone.utc)
        return CacheEntry(
            key_hash=key_hash,
            blob_ref=str(row["blob_ref"]),
            sha256=str(row["sha256"]),
            size_bytes=int(row["size_bytes"]),
            fetched_at=fetched_at,
            schema_version=int(row["schema_version"]),
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def _is_json(value: Any) -> bool:
    try:
        json.dumps(value)
        return True
    except (TypeError, ValueError):
        return False
