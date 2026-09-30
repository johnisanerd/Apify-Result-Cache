"""Every user-visible string the cache emits, in one place.

These land in the public run log of every Actor that installs the library, so
the wording is a product decision. Two rules, both hard:

- No backend vendor name and no URL, ever. "Cached" and "recorded" are fine;
  the name of the database is not.
- No secret value, ever. The debug dump reports presence, never contents.
"""

from __future__ import annotations

_PREFIX = "Result cache:"


# ----------------------------------------------------------------- inert why

def reason_not_set() -> str:
    return "RESULT_CACHE_MODE not set"


def reason_off() -> str:
    return "RESULT_CACHE_MODE=off"


def reason_unknown_mode(value: str) -> str:
    return f"unknown RESULT_CACHE_MODE {value!r}"


def reason_off_platform() -> str:
    return "not running on the Apify platform"


def reason_not_configured() -> str:
    return "not configured: set RESULT_CACHE_INDEX_URL and RESULT_CACHE_INDEX_KEY"


def reason_no_identity() -> str:
    return "run identity unavailable"


def bad_namespace(namespace: str) -> str:
    return f"invalid namespace {namespace!r}"


def bad_schema_version(value: object) -> str:
    return f"invalid schema_version {value!r}"


def inert(reason: str) -> str:
    """Logged once on every run where the cache does nothing, and why.

    Silence would be indistinguishable from "not installed", which is the
    single most expensive thing to diagnose across a fleet.
    """
    return f"{_PREFIX} inert ({reason})."


# ----------------------------------------------------------------- mode lines

def mode_keys() -> str:
    return (
        f"{_PREFIX} mode keys. Recording request keys only; nothing is served "
        "from cache and the output is unchanged."
    )


def mode_serve() -> str:
    return f"{_PREFIX} mode serve. Repeat requests may be served from cache."


def serve_not_available(version: str) -> str:
    return (
        f"{_PREFIX} mode serve is not available in version {version}; "
        "running as keys (recording only)."
    )


# ----------------------------------------------------------------- permissive

def unavailable(reason: str) -> str:
    """The permissive path. Our outage must not break someone else's run."""
    return (
        f"{_PREFIX} recording unavailable ({reason}); continuing without it. "
        "This run's results are unaffected."
    )


def queue_full(cap: int) -> str:
    return f"{_PREFIX} more than {cap} request keys buffered; the oldest are being dropped."


def bad_outcome(outcome: object) -> str:
    return f"{_PREFIX} ignoring unknown outcome {outcome!r}."


def bad_key(key: object) -> str:
    return f"{_PREFIX} ignoring a malformed key (expected 64 hex characters)."


def unstable_key_field(names: list[str]) -> str:
    return (
        f"{_PREFIX} key field(s) {', '.join(names)} are not JSON-serialisable; "
        "using their repr, which may not be stable across runs."
    )


def bad_ttl(raw: str, default: int) -> str:
    return (
        f"{_PREFIX} RESULT_CACHE_TTL_DAYS={raw!r} is not a whole number of days; "
        f"using {default}."
    )


def close_summary(recorded: int, flushed: int, dropped: int) -> str:
    line = f"{_PREFIX} recorded {flushed} of {recorded} request key(s) this run."
    if dropped:
        line += f" ({dropped} not recorded.)"
    return line


# ---------------------------------------------------------------------- debug

def debug_env(seen: dict[str, str]) -> str:
    return f"[result-cache debug] env: {seen}"


def debug_config(at_home: bool, version: str, mode: str | None, ttl_days: int | None,
                 hit_event: str | None, force: bool) -> str:
    return (
        f"[result-cache debug] is_at_home={at_home} version={version} mode={mode} "
        f"ttl_days={ttl_days} hit_event={hit_event} force={force}"
    )
