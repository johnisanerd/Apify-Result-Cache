"""Thin PostgREST client for the cache RPCs.

Deliberately raw httpx rather than a vendor SDK, for the same reasons as the
free-tier limiter: the Actors in this fleet run under small memory caps and
already ship httpx, so this adds no install weight and no import cost. The
class never retries and never logs; the cache decides what a failure means.
"""

from __future__ import annotations

from typing import Any

import httpx


class CacheDBError(RuntimeError):
    """Any failure talking to the index. Always caught by the cache."""


class CacheDB:
    """Calls the cache RPCs. Works with the anon key (Actors) or a service key (scripts)."""

    def __init__(
        self,
        url: str,
        key: str,
        read_timeout: float = 2.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint = url.rstrip("/") + "/rest/v1/rpc"
        self._client = httpx.AsyncClient(
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(connect=2.0, read=read_timeout, write=2.0, pool=2.0),
            transport=transport,
        )

    async def _rpc(self, function: str, params: dict[str, object]) -> Any:
        try:
            response = await self._client.post(f"{self._endpoint}/{function}", json=params)
        except Exception as exc:  # noqa: BLE001 - not only httpx.HTTPError: InvalidURL is a plain Exception
            # Type name only. The full exception can carry the project URL, and
            # this string lands in a public Actor log.
            raise CacheDBError(type(exc).__name__) from None

        if response.status_code >= 400:
            raise CacheDBError(f"HTTP {response.status_code}")

        body = response.content.strip() if response.content else b""
        if body in (b"", b"null"):
            return None  # `returns void`, or nothing to say
        try:
            return response.json()
        except ValueError:
            raise CacheDBError("bad response") from None

    async def cache_log(self, rows: list[dict[str, Any]]) -> int:
        """Bulk-insert request-log rows. Returns the number inserted."""
        result = await self._rpc("cache_log", {"p_rows": rows})
        try:
            return int(result)
        except (TypeError, ValueError):
            raise CacheDBError("bad response") from None

    async def cache_lookup(self, namespace: str, keys: list[str], max_age_days: int) -> list[dict[str, Any]]:
        """Index rows for `keys` fetched within `max_age_days` and not expired."""
        result = await self._rpc(
            "cache_lookup",
            {"p_namespace": namespace, "p_keys": list(keys), "p_max_age_days": int(max_age_days)},
        )
        if result is None:
            return []
        if not isinstance(result, list):
            raise CacheDBError("bad response")
        return result

    async def cache_put(
        self,
        *,
        namespace: str,
        key_hash: str,
        schema_version: int,
        entity_id: str | None,
        blob_ref: str,
        sha256: str,
        size_bytes: int,
        ttl_days: int,
        actor_id: str,
        is_paying: bool,
    ) -> None:
        """Upsert one index row. The blob itself lives in the bucket."""
        await self._rpc(
            "cache_put",
            {
                "p_namespace": namespace,
                "p_key_hash": key_hash,
                "p_schema_version": int(schema_version),
                "p_entity_id": entity_id,
                "p_blob_ref": blob_ref,
                "p_sha256": sha256,
                "p_size_bytes": int(size_bytes),
                "p_ttl_days": int(ttl_days),
                "p_actor_id": actor_id,
                "p_is_paying": bool(is_paying),
            },
        )

    async def cache_stats(
        self,
        date_from: str,
        date_to: str,
        namespace: str,
        exclude_user_hash: str | None = None,
        exclude_entity_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """The hit-rate gate query. Needs a service key; the anon key is refused."""
        result = await self._rpc(
            "cache_stats",
            {
                "p_from": date_from,
                "p_to": date_to,
                "p_namespace": namespace,
                "p_exclude_user_hash": exclude_user_hash,
                "p_exclude_entity_ids": exclude_entity_ids,
            },
        )
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        raise CacheDBError("bad response")

    async def cache_quota(self) -> dict[str, Any]:
        """Sizes and error counts for the quota watch. Needs a service key."""
        result = await self._rpc("cache_quota", {})
        if isinstance(result, list) and result and isinstance(result[0], dict):
            return result[0]
        raise CacheDBError("bad response")

    async def cache_gc_requests(self, before_iso: str, namespace: str | None = None,
                                limit: int = 50_000) -> int:
        """Delete request-log rows older than `before_iso`. Needs a service key."""
        result = await self._rpc(
            "cache_gc_requests",
            {"p_before": before_iso, "p_namespace": namespace, "p_limit": int(limit)},
        )
        try:
            return int(result or 0)
        except (TypeError, ValueError):
            raise CacheDBError("bad response") from None

    async def cache_outcomes(
        self,
        date_from: str,
        date_to: str,
        namespace: str,
        exclude_user_hash: str | None = None,
        exclude_entity_ids: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Request counts by (is_paying, outcome): the real hit rate once serving. Service key only."""
        result = await self._rpc(
            "cache_outcomes",
            {
                "p_from": date_from,
                "p_to": date_to,
                "p_namespace": namespace,
                "p_exclude_user_hash": exclude_user_hash,
                "p_exclude_entity_ids": exclude_entity_ids,
            },
        )
        if result is None:
            return []
        if not isinstance(result, list):
            raise CacheDBError("bad response")
        return result

    async def cache_expired(self, limit: int = 1000) -> list[dict[str, Any]]:
        """Expired index rows (namespace, key_hash, blob_ref), oldest first. Service key only."""
        result = await self._rpc("cache_expired", {"p_limit": int(limit)})
        if result is None:
            return []
        if not isinstance(result, list):
            raise CacheDBError("bad response")
        return result

    async def cache_delete_index(self, namespace: str, keys: list[str]) -> int:
        """Delete index rows that are already expired. Service key only."""
        result = await self._rpc("cache_delete_index", {"p_namespace": namespace, "p_keys": list(keys)})
        try:
            return int(result or 0)
        except (TypeError, ValueError):
            raise CacheDBError("bad response") from None

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001 - closing must never break a run
            pass
