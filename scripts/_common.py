"""Shared helpers for the operator scripts. Not part of the installed package."""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

GB = 1024 ** 3

# Pro plan included quotas (2026-09-30). The spend cap stays ON, so these are
# ceilings, not billing thresholds: past one, the project is restricted.
PRO_QUOTAS = {
    "db_bytes": 8 * GB,
    "live_blob_bytes": 100 * GB,
    "egress_30d_bytes": 250 * GB,
}
FREE_QUOTAS = {
    "db_bytes": int(0.5 * GB),
    "live_blob_bytes": 1 * GB,
    "egress_30d_bytes": 5 * GB,
}
WARN_AT = 0.80
ERROR_AT = 0.95
ERROR_SHARE_WARN = 0.01


def load_env_file() -> None:
    """Read KEY=VALUE lines from ./.env if present, without overriding the shell."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        os.environ.setdefault(name.strip(), value.strip().strip('"').strip("'"))


def namespace_config(namespace: str) -> dict:
    path = ROOT / "namespaces" / f"{namespace}.json"
    if not path.exists():
        return {"namespace": namespace, "exclude_entity_ids": []}
    return json.loads(path.read_text())


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


def quota_report(q: dict, plan: str) -> tuple[list[str], int]:
    """Lines for the quota watch, and an exit code: 0 ok, 1 warn, 2 error."""
    quotas = PRO_QUOTAS if plan == "pro" else FREE_QUOTAS
    lines: list[str] = []
    worst = 0
    labels = {
        "db_bytes": "Database size",
        "live_blob_bytes": "Cached payload bytes (live index rows)",
        "egress_30d_bytes": "Estimated hit egress, last 30 days",
    }
    for field, label in labels.items():
        used = int(q.get(field) or 0)
        cap = quotas[field]
        share = used / cap if cap else 0.0
        level = "OK"
        if share >= ERROR_AT:
            level, worst = "ERROR", max(worst, 2)
        elif share >= WARN_AT:
            level, worst = "WARN", max(worst, 1)
        lines.append(f"  {level:5} {label}: {human_bytes(used)} of {human_bytes(cap)} ({share:.1%})")

    lines.append(f"        request log: {human_bytes(int(q.get('requests_bytes') or 0))}, "
                 f"{int(q.get('request_rows') or 0):,} rows; index: "
                 f"{human_bytes(int(q.get('index_bytes') or 0))}, "
                 f"{int(q.get('index_rows_live') or 0):,} live rows")

    rows_24h = int(q.get("rows_24h") or 0)
    errors_24h = int(q.get("error_rows_24h") or 0)
    share = errors_24h / rows_24h if rows_24h else 0.0
    level = "OK"
    if share > ERROR_SHARE_WARN:
        level, worst = "WARN", max(worst, 1)
    lines.append(f"  {level:5} Store errors, last 24 h: {errors_24h:,} of {rows_24h:,} requests ({share:.2%})")
    return lines, worst
