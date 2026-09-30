"""Environment parsing for the result cache. Pure: no Actor import, no I/O.

Everything the cache needs at start comes from environment variables, set per
Actor in the Apify Console and baked into the build. This module turns them
into a `Config`, or into the one-line reason for staying inert.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Mapping

from . import messages

MODES = ("off", "keys", "serve")
OUTCOMES = frozenset({"logged", "hit", "miss", "bypass", "expired", "error"})

DEFAULT_TTL_DAYS = 90
MIN_TTL_DAYS = 1
MAX_TTL_DAYS = 365

# The index lives in the same project as the free-tier ledger, so by default it
# uses the two names already baked into every Actor. The RESULT_CACHE_INDEX_*
# override exists so the cache can move to its own project one day without
# touching those Actors. Nothing sets it today.
INDEX_URL_VARS = ("RESULT_CACHE_INDEX_URL", "SUPABASE_URL")
INDEX_KEY_VARS = ("RESULT_CACHE_INDEX_KEY", "SUPABASE_KEY")


@dataclass(frozen=True)
class Config:
    mode: str                     # "keys" | "serve"
    index_url: str
    index_key: str
    actor_id: str
    user_hash: str                # sha256 of the caller's user id; never the id itself
    is_paying: bool
    ttl_days: int
    hit_event: str | None         # None: the Actor's own default applies
    s3_endpoint: str | None       # Phase 1; parsed now, unused in 0.1
    s3_region: str | None
    s3_bucket: str | None
    s3_access_key: str | None
    s3_secret_key: str | None
    force: bool
    debug: bool


@dataclass(frozen=True)
class Resolution:
    config: Config | None
    inert_reason: str | None      # set when config is None
    inert_is_warning: bool        # someone set the mode and expected it to work
    notes: tuple[str, ...]        # non-fatal warnings to log at start


def user_hash(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()


def _first(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def _optional(env: Mapping[str, str], name: str) -> str | None:
    return (env.get(name) or "").strip() or None


def resolve(env: Mapping[str, str], at_home: bool) -> Resolution:
    """First blocking condition wins; the reason strings live in messages.py."""
    notes: list[str] = []

    def inert(reason: str, warning: bool = False) -> Resolution:
        return Resolution(None, reason, warning, tuple(notes))

    raw_mode = (env.get("RESULT_CACHE_MODE") or "").strip().lower()
    if not raw_mode:
        return inert(messages.reason_not_set())
    if raw_mode == "off":
        return inert(messages.reason_off())
    if raw_mode not in MODES:
        return inert(messages.reason_unknown_mode(raw_mode), warning=True)

    force = env.get("RESULT_CACHE_FORCE") == "1"
    if not at_home and not force:
        return inert(messages.reason_off_platform())

    url = _first(env, INDEX_URL_VARS)
    key = _first(env, INDEX_KEY_VARS)
    if not url or not key:
        return inert(messages.reason_not_configured(), warning=True)

    user_id = (env.get("APIFY_USER_ID") or "").strip()
    actor_id = (env.get("APIFY_ACTOR_ID") or "").strip()
    if not user_id or not actor_id:
        return inert(messages.reason_no_identity(), warning=True)

    ttl_days = DEFAULT_TTL_DAYS
    raw_ttl = (env.get("RESULT_CACHE_TTL_DAYS") or "").strip()
    if raw_ttl:
        try:
            ttl_days = max(MIN_TTL_DAYS, min(MAX_TTL_DAYS, int(raw_ttl)))
        except ValueError:
            notes.append(messages.bad_ttl(raw_ttl, DEFAULT_TTL_DAYS))

    config = Config(
        mode=raw_mode,
        index_url=url.rstrip("/"),
        index_key=key,
        actor_id=actor_id[:64],
        user_hash=user_hash(user_id),
        is_paying=env.get("APIFY_USER_IS_PAYING") == "1",
        ttl_days=ttl_days,
        hit_event=_optional(env, "RESULT_CACHE_HIT_EVENT"),
        s3_endpoint=_optional(env, "RESULT_CACHE_S3_ENDPOINT"),
        s3_region=_optional(env, "RESULT_CACHE_S3_REGION"),
        s3_bucket=_optional(env, "RESULT_CACHE_S3_BUCKET"),
        s3_access_key=_optional(env, "RESULT_CACHE_S3_ACCESS_KEY"),
        s3_secret_key=_optional(env, "RESULT_CACHE_S3_SECRET_KEY"),
        force=force,
        debug=env.get("RESULT_CACHE_DEBUG") == "1",
    )
    return Resolution(config, None, False, tuple(notes))


def env_presence(env: Mapping[str, str]) -> dict[str, str]:
    """For RESULT_CACHE_DEBUG=1. Presence and policy values only, never a secret.

    The index URL and key are reported under generic labels on purpose: this
    line lands in a public log and must not name the backend.
    """
    def present(names: tuple[str, ...]) -> str:
        return "set" if _first(env, names) else "MISSING"

    def shown(name: str) -> str:
        return (env.get(name) or "").strip() or "MISSING"

    return {
        "RESULT_CACHE_MODE": shown("RESULT_CACHE_MODE"),
        "RESULT_CACHE_TTL_DAYS": shown("RESULT_CACHE_TTL_DAYS"),
        "RESULT_CACHE_HIT_EVENT": shown("RESULT_CACHE_HIT_EVENT"),
        "RESULT_CACHE_FORCE": shown("RESULT_CACHE_FORCE"),
        "index_url": present(INDEX_URL_VARS),
        "index_key": present(INDEX_KEY_VARS),
        "APIFY_USER_ID": "set" if (env.get("APIFY_USER_ID") or "").strip() else "MISSING",
        "APIFY_ACTOR_ID": "set" if (env.get("APIFY_ACTOR_ID") or "").strip() else "MISSING",
        "APIFY_USER_IS_PAYING": shown("APIFY_USER_IS_PAYING"),
        "s3_endpoint": "set" if _optional(env, "RESULT_CACHE_S3_ENDPOINT") else "MISSING",
        "s3_bucket": shown("RESULT_CACHE_S3_BUCKET"),
        "s3_keys": "set" if (_optional(env, "RESULT_CACHE_S3_ACCESS_KEY")
                            and _optional(env, "RESULT_CACHE_S3_SECRET_KEY")) else "MISSING",
    }
