"""The daily report cache_gc.py prints: what needs John, what to watch, what could be saved.

Pure functions over three inputs, so the rules are testable without a database:
  quota    cache_quota() row
  report   cache_daily_report() JSON (migration 0009)
  history  earlier daily snapshots from .cache_history.jsonl (git-ignored)

The spend cap is off (confirmed 2026-11-12), so the 8 GB database quota is a
billing line, not a ceiling: the disk grows by half each time it is 90% full
(8 -> 12 -> 18 -> 27 GB) and never shrinks. The report therefore measures the
database against the disk it is on and says when the next step is due.
"""

from __future__ import annotations

import json
import pathlib
from datetime import date, datetime, timezone

from _common import GB, PRO_QUOTAS, ROOT, human_bytes, namespace_config

HISTORY_PATH = ROOT / ".cache_history.jsonl"
HISTORY_KEEP_DAYS = 120

DISK_TIERS_GB = (8, 12, 18, 27, 40.5, 60.75, 91.125)
DISK_INCLUDED_GB = 8
DISK_USD_PER_GB_MONTH = 0.125
DISK_EXPAND_AT = 0.90

SERVED = ("hit", "hit_negative", "miss", "bypass", "error", "failed")

# Thresholds. Each one is a judgment about what is worth John's attention.
DISK_STEP_WATCH_DAYS = 7          # next disk step this close: watch
BACKLOG_WATCH = 50_000            # expired entries still waiting after a run
NS_ERROR_SHARE_WATCH = 0.02       # one namespace's store errors, last 24 h
NS_ERROR_SHARE_ACTION = 0.10
NS_ERROR_MIN = 20
SILENT_BASELINE_MIN = 200         # requests a day before "went quiet" means anything
SILENT_SHARE = 0.10
HIT_DROP_MIN_REQUESTS = 500
HIT_DROP_BASELINE = 0.10
SURGE_FACTOR = 3.0
SURGE_MIN_REQUESTS = 5_000
FAILED_SHARE_WATCH = 0.20
FAILED_MIN = 500
TTL_MIN_ROWS = 50_000             # only large namespaces justify a TTL change
TTL_USEFUL_SHARE = 0.05
TTL_MIN_FREED_BYTES = 1 * GB      # below this the saving is pennies and not worth a rebuild
LOW_VALUE_MIN_REQUESTS = 2_000
LOW_VALUE_HIT_RATE = 0.05
LOG_SHARE_OF_DB = 0.15
LOG_RETENTION_OPTIONS = (60, 90)
QUOTA_WATCH = 0.80
QUOTA_ACTION = 0.95


# --- history ---------------------------------------------------------------

def load_history(path: pathlib.Path = HISTORY_PATH) -> list[dict]:
    """One snapshot per day (the last written that day), oldest first."""
    if not path.exists():
        return []
    by_day: dict[str, dict] = {}
    for line in path.read_text().splitlines():
        try:
            snap = json.loads(line)
        except ValueError:
            continue
        if isinstance(snap, dict) and snap.get("date"):
            by_day[snap["date"]] = snap
    return [by_day[d] for d in sorted(by_day)]


def save_snapshot(snapshot: dict, path: pathlib.Path = HISTORY_PATH) -> None:
    history = [s for s in load_history(path) if s["date"] != snapshot["date"]]
    history.append(snapshot)
    history = history[-HISTORY_KEEP_DAYS:]
    path.write_text("".join(json.dumps(s, separators=(",", ":")) + "\n" for s in history))


# --- shaping ---------------------------------------------------------------

def namespace_activity(report: dict) -> dict[str, dict]:
    """Per namespace: 24-hour request counts joined with the index estimates."""
    out: dict[str, dict] = {}

    def slot(ns: str) -> dict:
        return out.setdefault(ns, {"requests": 0, "hits": 0, "errors": 0, "failed": 0,
                                   "paying": 0, "top_user": 0, "rows": 0})

    for r in report.get("requests_24h") or []:
        if r.get("outcome") not in SERVED:
            continue
        s, n = slot(r["namespace"]), int(r.get("requests") or 0)
        s["requests"] += n
        if r["outcome"] in ("hit", "hit_negative"):
            s["hits"] += n
        elif r["outcome"] == "error":
            s["errors"] += n
        elif r["outcome"] == "failed":
            s["failed"] += n
        if r.get("is_paying"):
            s["paying"] += n
    for r in report.get("top_users_24h") or []:
        slot(r["namespace"])["top_user"] = int(r.get("top_user_requests") or 0)
    for r in report.get("index") or []:
        s = slot(r["namespace"])
        s["rows"] = int(r.get("rows") or 0)
        s["index"] = r
    return out


def disk_gb_for(db_bytes: int, floor_gb: float = 0) -> float:
    """The disk the database must be on: the first step it has not yet filled to 90%."""
    for tier in DISK_TIERS_GB:
        if tier >= floor_gb and db_bytes < DISK_EXPAND_AT * tier * GB:
            return tier
    return DISK_TIERS_GB[-1]


def next_tier(disk_gb: float) -> float:
    later = [t for t in DISK_TIERS_GB if t > disk_gb]
    return later[0] if later else disk_gb * 1.5


def disk_cost(disk_gb: float) -> float:
    return max(0.0, disk_gb - DISK_INCLUDED_GB) * DISK_USD_PER_GB_MONTH


def growth_per_day(db_bytes: int, today: date, history: list[dict]) -> tuple[float, int] | None:
    """Bytes a day against the snapshot nearest seven days back, and the days it spans."""
    best = None
    for snap in history:
        try:
            days = (today - date.fromisoformat(snap["date"])).days
        except (KeyError, ValueError):
            continue
        if days < 1 or not snap.get("db_bytes"):
            continue
        if best is None or abs(days - 7) < abs(best[1] - 7):
            best = (snap, days)
    if best is None:
        return None
    return (db_bytes - int(best[0]["db_bytes"])) / best[1], best[1]


def baseline(ns: str, today: date, history: list[dict]) -> dict | None:
    """Average daily requests and the hit rate over up to seven earlier days."""
    days = [s for s in history if s.get("date") and s["date"] < today.isoformat()
            and isinstance(s.get("namespaces"), dict)][-7:]
    seen = [s["namespaces"][ns] for s in days if ns in (s.get("namespaces") or {})]
    if not days:
        return None
    requests = sum(int(x.get("requests") or 0) for x in seen)
    hits = sum(int(x.get("hits") or 0) for x in seen)
    return {"days": len(days), "avg_requests": requests / len(days),
            "hit_rate": hits / requests if requests else 0.0, "known": bool(seen)}


def pct(part: float, whole: float) -> str:
    return f"{part / whole:.0%}" if whole else "n/a"


# --- the report ------------------------------------------------------------

def build_report(quota: dict, report: dict, history: list[dict], gc: dict | None,
                 today: date | None = None, log_retention_days: int = 180) -> tuple[list[str], int, dict]:
    """Return (lines, exit code, snapshot). Exit code: 0 quiet, 1 watch, 2 action needed."""
    today = today or datetime.now(timezone.utc).date()
    action: list[str] = []
    watch: list[str] = []
    ideas: list[str] = []
    numbers: list[str] = []

    activity = namespace_activity(report)
    db_bytes = int(report.get("db_bytes") or quota.get("db_bytes") or 0)

    # Database and disk.
    known_disk = max((float(s.get("disk_gb") or 0) for s in history), default=0)
    disk = disk_gb_for(db_bytes, known_disk)
    step_at = DISK_EXPAND_AT * disk * GB
    growth = growth_per_day(db_bytes, today, history)
    line = (f"Database {human_bytes(db_bytes)} on an estimated {disk:g} GB disk "
            f"({db_bytes / (disk * GB):.0%} full), ${disk_cost(disk):.2f} a month over the plan")
    following = next_tier(disk)
    if growth and growth[0] > 0:
        days_left = (step_at - db_bytes) / growth[0]
        line += (f"; growing {human_bytes(growth[0])} a day (last {growth[1]} days), next disk step "
                 f"({following:g} GB, ${disk_cost(following):.2f} a month) in about {days_left:.0f} days")
        if days_left <= DISK_STEP_WATCH_DAYS:
            watch.append(f"The database disk grows from {disk:g} to {following:g} GB in about "
                         f"{days_left:.0f} days (${disk_cost(disk):.2f} to ${disk_cost(following):.2f} "
                         f"a month). The step is permanent; see Opportunities for ways to hold it off.")
    elif growth:
        line += f"; shrinking {human_bytes(-growth[0])} a day (last {growth[1]} days)"
    else:
        line += f"; next disk step at {human_bytes(step_at)} (no history yet for a growth rate)"
    numbers.append(line)
    if known_disk and disk > known_disk:
        watch.append(f"The database disk stepped up from {known_disk:g} to {disk:g} GB since the last report "
                     f"(now ${disk_cost(disk):.2f} a month over the plan). Supabase never shrinks it back.")

    objects_bytes = int(report.get("storage_objects_bytes") or 0)
    requests_bytes = int(report.get("requests_bytes") or 0)
    index_bytes = int(report.get("index_bytes") or 0)
    numbers.append(f"Space: stored-object records {human_bytes(objects_bytes)}, request log "
                   f"{human_bytes(requests_bytes)}, index {human_bytes(index_bytes)}")

    # The two other billed quotas.
    for field, label in (("live_blob_bytes", "Cached payloads"), ("egress_30d_bytes", "Hit egress, 30 days")):
        used, cap = int(quota.get(field) or 0), PRO_QUOTAS[field]
        share = used / cap
        numbers.append(f"{label}: {human_bytes(used)} of {human_bytes(cap)} included ({share:.0%})")
        if share >= QUOTA_ACTION:
            action.append(f"{label} are at {share:.0%} of the included {human_bytes(cap)}; past it Supabase bills the overage.")
        elif share >= QUOTA_WATCH:
            watch.append(f"{label} are at {share:.0%} of the included {human_bytes(cap)}.")

    # Cleanup.
    backlog = int(report.get("expired_waiting") or 0)
    if gc is not None:
        text = (f"Cleanup: {gc.get('objects', 0):,} expired entries deleted, "
                f"{gc.get('log_pruned', 0):,} request-log rows pruned")
        if gc.get("failures"):
            text += f", {gc['failures']:,} object deletes failed (retried next run)"
        numbers.append(text + f"; about {backlog:,} expired entries still waiting")
        if not gc.get("complete", True):
            previous = history[-1] if history else {}
            repeat = (previous.get("gc") or {}).get("complete") is False
            message = (f"Cleanup stopped early ({gc.get('error') or 'index call failed'}) after "
                       f"{gc.get('objects', 0):,} deletions; about {backlog:,} expired entries are still waiting.")
            if repeat:
                action.append(message + " It also stopped early on the previous run, so the backlog is not clearing.")
            else:
                watch.append(message + " The next run picks them up.")
    if backlog >= BACKLOG_WATCH and (gc is None or gc.get("complete", True)):
        watch.append(f"About {backlog:,} expired entries are still in the index after cleanup; "
                     f"the daily run is not keeping up (raise --max-batches).")

    # Traffic, per namespace.
    total = sum(a["requests"] for a in activity.values())
    hits = sum(a["hits"] for a in activity.values())
    errors = sum(a["errors"] for a in activity.values())
    numbers.append(f"Last 24 h: {total:,} requests, {pct(hits, total)} served from the cache, "
                   f"{errors:,} store errors ({errors / total:.2%})" if total else "Last 24 h: no requests")
    if not total and history:
        action.append("No cache requests at all in the last 24 hours. Every Actor has stopped logging; "
                      "check the index project and the Actors' RESULT_CACHE_* settings.")

    for ns, a in sorted(activity.items(), key=lambda kv: -kv[1]["requests"]):
        n, base = a["requests"], baseline(ns, today, history)
        if a["errors"] >= NS_ERROR_MIN and n and a["errors"] / n >= NS_ERROR_SHARE_WATCH:
            text = (f"{ns}: {a['errors']:,} store errors in {n:,} requests ({a['errors'] / n:.1%}). "
                    f"Those requests fell back to a fresh fetch, so customers were served but nothing was saved.")
            (action if a["errors"] / n >= NS_ERROR_SHARE_ACTION else watch).append(text)
        if a["failed"] >= FAILED_MIN and a["failed"] / n >= FAILED_SHARE_WATCH:
            watch.append(f"{ns}: {a['failed']:,} of {n:,} requests failed upstream ({a['failed'] / n:.0%}).")
        if base and base["known"]:
            if n >= SURGE_MIN_REQUESTS and base["avg_requests"] and n >= SURGE_FACTOR * base["avg_requests"]:
                watch.append(f"{ns}: traffic surge, {n:,} requests against a {base['avg_requests']:,.0f} daily average; "
                             f"{pct(a['paying'], n)} from paying accounts, the busiest caller made {pct(a['top_user'], n)}.")
            if (n >= HIT_DROP_MIN_REQUESTS and base["hit_rate"] >= HIT_DROP_BASELINE
                    and a["hits"] / n < base["hit_rate"] / 2):
                watch.append(f"{ns}: served-from-cache share fell to {a['hits'] / n:.0%} from a "
                             f"{base['hit_rate']:.0%} average over the last {base['days']} days.")
        elif base and not base["known"] and n:
            watch.append(f"{ns}: new namespace, first seen today ({n:,} requests, {pct(a['hits'], n)} served from the cache).")

    for ns in sorted({n for s in history[-7:] for n in (s.get("namespaces") or {})} - {
            n for n, a in activity.items() if a["requests"]}):
        base = baseline(ns, today, history)
        if base and base["avg_requests"] >= SILENT_BASELINE_MIN:
            action.append(f"{ns}: no requests in the last 24 hours against a {base['avg_requests']:,.0f} daily average. "
                          f"The Actor may be down or have lost its cache settings in a rebuild.")
    for ns, a in activity.items():
        base = baseline(ns, today, history)
        if (base and base["known"] and a["requests"] and base["avg_requests"] >= SILENT_BASELINE_MIN
                and a["requests"] < SILENT_SHARE * base["avg_requests"]):
            action.append(f"{ns}: only {a['requests']:,} requests in the last 24 hours against a "
                          f"{base['avg_requests']:,.0f} daily average.")

    # What the cache saved, where a namespace has a fresh-fetch cost on file.
    fresh_costs = {ns: cost for ns in activity
                   if (cost := namespace_config(ns).get("fresh_cost_usd")) is not None}
    if fresh_costs:
        saved = sum(activity[ns]["hits"] * cost for ns, cost in fresh_costs.items())
        numbers.append(f"Upstream cost avoided by cache hits, last 24 h: about ${saved:,.2f} "
                       f"({', '.join(sorted(fresh_costs))} only; no fetch cost on file for the rest)")

    # Opportunities.
    object_rows = int(report.get("storage_objects_rows") or 0)
    index_rows = int(report.get("index_rows") or 0)
    per_entry = ((objects_bytes / object_rows if object_rows else 0)
                 + (index_bytes / index_rows if index_rows else 0))
    for ns, a in sorted(activity.items(), key=lambda kv: -kv[1]["rows"]):
        idx = a.get("index") or {}
        ttl = float(idx.get("ttl_days") or 0)
        fresh_cost = fresh_costs.get(ns)
        for days in (7, 30):
            kept, useful = int(idx.get(f"over_{days}d") or 0), int(idx.get(f"useful_past_{days}d") or 0)
            freed = kept * per_entry
            if not (ttl > days + 1 and kept >= TTL_MIN_ROWS and freed >= TTL_MIN_FREED_BYTES
                    and useful / kept < TTL_USEFUL_SHARE):
                continue
            disk_usd = freed / GB * DISK_USD_PER_GB_MONTH
            if fresh_cost is not None:
                refetch_usd = useful * fresh_cost
                if refetch_usd >= 12 * disk_usd:
                    continue    # the re-served entries are worth more than a year of the disk they sit on
                cost_text = f"about ${refetch_usd:,.2f} of fresh fetches for the {useful:,} that were"
            else:
                cost_text = f"a fresh fetch for the roughly {useful:,} that were (no fetch cost on file)"
            ideas.append(
                f"{ns} (TTL {ttl:g} days): about {kept:,} entries are older than {days} days and "
                f"{useful / kept:.1%} of them were served again after day {days}. A {days}-day TTL would free about "
                f"{human_bytes(freed)} of the database (${disk_usd:.2f} a month of disk, and a lighter index); "
                f"the cost is {cost_text}.")
            break
        n = a["requests"]
        base = baseline(ns, today, history)
        rate = base["hit_rate"] if base and base["known"] and base["avg_requests"] else (a["hits"] / n if n else 0)
        if n >= LOW_VALUE_MIN_REQUESTS and rate < LOW_VALUE_HIT_RATE:
            ideas.append(f"{ns}: only {rate:.1%} of requests are served from the cache ({n:,} requests in 24 h). "
                         f"The cache stores far more than it serves here; a different key or no cache may fit better.")

    oldest = report.get("oldest_request_ts")
    if oldest and db_bytes and requests_bytes / db_bytes >= LOG_SHARE_OF_DB:
        span = max(1, (today - date.fromisoformat(str(oldest)[:10])).days)
        per_day = requests_bytes / span
        if span < log_retention_days:
            options = ", ".join(f"{d} days would cap it near {human_bytes(per_day * d)}" for d in LOG_RETENTION_OPTIONS)
            ideas.append(
                f"Request log: {human_bytes(requests_bytes)} ({requests_bytes / db_bytes:.0%} of the database) "
                f"covering {span} days. With {log_retention_days}-day retention nothing is pruned for another "
                f"{log_retention_days - span} days and it heads for about {human_bytes(per_day * log_retention_days)}. "
                f"A shorter --log-retention-days: {options}. Disk is $0.125 per GB a month, so this is about "
                f"query speed on a small instance more than money.")

    # Namespace table, busiest first.
    rows = [(ns, a) for ns, a in sorted(activity.items(), key=lambda kv: -kv[1]["requests"]) if a["requests"]]
    table = [f"  {'namespace':<32}{'requests':>10}{'served':>8}{'errors':>8}{'entries':>11}"]
    for ns, a in rows[:10]:
        table.append(f"  {ns:<32}{a['requests']:>10,}{pct(a['hits'], a['requests']):>8}"
                     f"{a['errors']:>8,}{a['rows']:>11,}")
    if len(rows) > 10:
        rest = rows[10:]
        table.append(f"  {'(' + str(len(rest)) + ' smaller namespaces)':<32}"
                     f"{sum(a['requests'] for _, a in rest):>10,}")

    lines = [f"Result cache daily report, {today.isoformat()} (spend cap off)"]
    for title, items in (("ACTION NEEDED", action), ("WATCH", watch), ("OPPORTUNITIES", ideas)):
        if items:
            lines.append(f"\n{title} ({len(items)})")
            lines.extend(f"  - {item}" for item in items)
    if not action and not watch:
        lines.append("\nNothing needs you today.")
    lines.append("\nNUMBERS")
    lines.extend(f"  {n}" for n in numbers)
    lines.extend(table)

    snapshot = {
        "date": today.isoformat(),
        "db_bytes": db_bytes,
        "disk_gb": disk,
        "storage_objects_bytes": objects_bytes,
        "requests_bytes": requests_bytes,
        "index_bytes": index_bytes,
        "backlog": backlog,
        "gc": None if gc is None else {k: gc.get(k) for k in ("objects", "log_pruned", "complete")},
        "namespaces": {ns: {"requests": a["requests"], "hits": a["hits"], "errors": a["errors"],
                            "failed": a["failed"], "rows": a["rows"]} for ns, a in activity.items()},
    }
    return lines, (2 if action else 1 if watch else 0), snapshot
