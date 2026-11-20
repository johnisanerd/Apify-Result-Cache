"""Rules of the daily report (scripts/daily_report.py): what gets raised, and at which level."""

from __future__ import annotations

import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import daily_report as dr  # noqa: E402
from _common import GB  # noqa: E402

TODAY = date(2026, 11, 20)
QUOTA = {"live_blob_bytes": 30 * GB, "egress_30d_bytes": 5 * GB}


def requests(ns, hit=0, miss=0, error=0, failed=0, paying=True):
    return [{"namespace": ns, "outcome": o, "is_paying": paying, "requests": n, "users": 1}
            for o, n in (("hit", hit), ("miss", miss), ("error", error), ("failed", failed)) if n]


def make_report(reqs, db_gb=8.0, index=None, **extra):
    base = {"db_bytes": int(db_gb * GB), "storage_objects_bytes": 5 * GB, "storage_objects_rows": 2_400_000,
            "requests_bytes": int(0.5 * GB), "index_bytes": int(1.3 * GB), "index_rows": 2_400_000,
            "expired_waiting": 0, "oldest_request_ts": "2026-09-30T00:00:00+00:00",
            "requests_24h": reqs, "top_users_24h": [], "index": index or []}
    base.update(extra)
    return base


def day(d, db_gb, disk=12, **namespaces):
    return {"date": d, "db_bytes": int(db_gb * GB), "disk_gb": disk,
            "namespaces": {ns.replace("_", "-"): v for ns, v in namespaces.items()}, "gc": {"complete": True}}


def run(report, history=(), gc=None):
    lines, code, snapshot = dr.build_report(QUOTA, report, list(history), gc, today=TODAY)
    return "\n".join(lines), code, snapshot


def section(text, title):
    if f"\n{title} (" not in text:
        return ""
    return text.split(f"\n{title} (", 1)[1].split("\n\n", 1)[0]


def test_quiet_day_says_so_and_exits_zero():
    history = [day("2026-11-19", 7.95, yt={"requests": 1000, "hits": 300})]
    text, code, _ = run(make_report(requests("yt", hit=300, miss=700)), history)
    assert code == 0
    assert "Nothing needs you today." in text
    assert "ACTION NEEDED" not in text


def test_database_over_the_included_8_gb_is_not_an_error():
    """Spend cap off: past 8 GB the disk grows. That is a number, not an alarm."""
    history = [day("2026-11-19", 8.2, yt={"requests": 1000, "hits": 300})]
    text, code, _ = run(make_report(requests("yt", hit=300, miss=700), db_gb=8.3), history)
    assert code == 0
    assert "on an estimated 12 GB disk" in text


def test_disk_tiers():
    assert dr.disk_gb_for(int(7.0 * GB)) == 8
    assert dr.disk_gb_for(int(7.3 * GB)) == 12       # past 90% of 8 GB
    assert dr.disk_gb_for(int(11.0 * GB)) == 18
    assert dr.disk_gb_for(int(5.0 * GB), floor_gb=12) == 12   # the disk never shrinks
    assert dr.disk_cost(12) == 0.5


def test_disk_step_reported_once_when_it_happens():
    history = [day("2026-11-19", 7.1, disk=8, yt={"requests": 1000, "hits": 300})]
    text, code, snapshot = run(make_report(requests("yt", hit=300, miss=700), db_gb=7.3), history)
    assert code == 1
    assert "stepped up from 8 to 12 GB" in section(text, "WATCH")
    assert snapshot["disk_gb"] == 12


def test_disk_step_due_within_a_week_is_a_watch():
    history = [day("2026-11-13", 9.4, yt={"requests": 1000, "hits": 300})]   # +0.2 GB a day
    text, code, _ = run(make_report(requests("yt", hit=300, miss=700), db_gb=10.6), history)
    assert code == 1
    assert "grows from 12 to 18 GB in about 1 days" in section(text, "WATCH")


def test_namespace_gone_quiet_needs_action():
    history = [day("2026-11-19", 7.9, yt={"requests": 20_000, "hits": 5000}, maps={"requests": 3000, "hits": 900})]
    text, code, _ = run(make_report(requests("yt", hit=5000, miss=15_000)), history)
    assert code == 2
    assert "maps: no requests in the last 24 hours" in section(text, "ACTION NEEDED")


def test_small_namespace_going_quiet_is_ignored():
    history = [day("2026-11-19", 7.9, yt={"requests": 20_000, "hits": 5000}, yelp={"requests": 5, "hits": 1})]
    _, code, _ = run(make_report(requests("yt", hit=5000, miss=15_000)), history)
    assert code == 0


def test_store_errors_are_judged_per_namespace():
    """71 errors are 0.1% of the fleet but 2.8% of the one namespace that has them."""
    reqs = requests("yt", hit=8000, miss=50_000) + requests("maps", hit=1200, miss=1256, error=71)
    history = [day("2026-11-19", 7.9, yt={"requests": 58_000, "hits": 8000}, maps={"requests": 2500, "hits": 1200})]
    text, code, _ = run(make_report(reqs), history)
    assert code == 1
    assert "maps: 71 store errors in 2,527 requests (2.8%)" in section(text, "WATCH")


def test_heavy_store_errors_need_action():
    reqs = requests("maps", hit=100, miss=600, error=300)
    history = [day("2026-11-19", 7.9, maps={"requests": 1000, "hits": 100})]
    text, code, _ = run(make_report(reqs), history)
    assert code == 2
    assert "300 store errors" in section(text, "ACTION NEEDED")


def test_hit_rate_collapse_and_surge_are_watched():
    history = [day("2026-11-19", 7.9, yt={"requests": 10_000, "hits": 4000})]
    report = make_report(requests("yt", hit=3000, miss=37_000, paying=False),
                         top_users_24h=[{"namespace": "yt", "top_user_requests": 30_000}])
    watch = section(run(report, history)[0], "WATCH")
    assert "served-from-cache share fell to 8% from a 40% average" in watch
    assert "traffic surge, 40,000 requests against a 10,000 daily average; 0% from paying accounts" in watch
    assert "busiest caller made 75%" in watch


def test_new_namespace_is_announced_only_with_history():
    reqs = requests("yt", hit=300, miss=700) + requests("bing", hit=10, miss=90)
    assert "new namespace" not in run(make_report(reqs))[0]
    history = [day("2026-11-19", 7.9, yt={"requests": 1000, "hits": 300})]
    assert "bing: new namespace, first seen today" in section(run(make_report(reqs), history)[0], "WATCH")


def test_seed_snapshot_without_namespaces_gives_growth_but_no_baseline():
    history = [{"date": "2026-11-12", "db_bytes": 7 * GB, "disk_gb": 8}]
    text, _, _ = run(make_report(requests("yt", hit=300, miss=700)), history)
    assert "growing 128.0 MB a day (last 8 days)" in text
    assert "new namespace" not in text


def test_cleanup_stopped_early_is_a_watch_then_an_action():
    report = make_report(requests("yt", hit=300, miss=700), expired_waiting=40_000)
    gc = {"objects": 12_952, "rows": 12_952, "failures": 0, "complete": False,
          "error": "index call failed: ReadTimeout", "log_pruned": 0}
    ok = day("2026-11-19", 7.9, yt={"requests": 1000, "hits": 300})
    text, code, snapshot = run(report, [ok], gc)
    assert code == 1
    assert "Cleanup stopped early (index call failed: ReadTimeout) after 12,952 deletions" in section(text, "WATCH")
    assert snapshot["gc"]["complete"] is False
    text, code, _ = run(report, [dict(ok, gc={"complete": False})], gc)
    assert code == 2
    assert "also stopped early on the previous run" in section(text, "ACTION NEEDED")


def test_backlog_after_a_complete_run_is_a_watch():
    report = make_report(requests("yt", hit=300, miss=700), expired_waiting=120_000)
    gc = {"objects": 400_000, "rows": 400_000, "failures": 0, "complete": True, "log_pruned": 0}
    assert "120,000 expired entries are still in the index" in section(run(report, [], gc)[0], "WATCH")


def index_row(ns, ttl, over_7d=0, useful_7d=0, over_30d=0, useful_30d=0):
    return {"namespace": ns, "rows": over_7d + 10_000, "ttl_days": ttl, "over_7d": over_7d,
            "useful_past_7d": useful_7d, "over_30d": over_30d, "useful_past_30d": useful_30d}


def test_ttl_idea_needs_size_and_few_late_hits(monkeypatch):
    monkeypatch.setattr(dr, "namespace_config", lambda ns: {})
    index = [index_row("big", 90, over_7d=1_800_000, useful_7d=30_000, over_30d=1_200_000, useful_30d=10_000),
             index_row("busy", 90, over_7d=1_800_000, useful_7d=400_000, over_30d=1_200_000, useful_30d=300_000),
             index_row("small", 30, over_7d=60_000, useful_7d=0)]
    ideas = section(run(make_report(requests("big", hit=1, miss=1), index=index))[0], "OPPORTUNITIES")
    assert "big (TTL 90 days)" in ideas and "A 7-day TTL would free about" in ideas
    assert "busy" not in ideas     # a quarter of its old entries are still served
    assert "small" not in ideas    # frees well under 1 GB


def test_ttl_idea_dropped_when_refetching_costs_more_than_the_disk(monkeypatch):
    """YouTube on 2026-11-20: 72k late-served entries at $0.00035 beat a year of 4.7 GB of disk."""
    index = [index_row("yt", 90, over_7d=1_820_000, useful_7d=72_000, over_30d=1_280_000, useful_30d=21_000)]
    report = make_report(requests("yt", hit=8000, miss=21_000), index=index)
    monkeypatch.setattr(dr, "namespace_config", lambda ns: {"fresh_cost_usd": 0.00035})
    text = run(report)[0]
    assert "yt (TTL" not in text
    assert "Upstream cost avoided by cache hits, last 24 h: about $2.80" in text
    monkeypatch.setattr(dr, "namespace_config", lambda ns: {"fresh_cost_usd": 0.000001})
    assert "about $0.07 of fresh fetches" in section(run(report)[0], "OPPORTUNITIES")


def test_request_log_idea_only_while_nothing_is_being_pruned():
    report = make_report(requests("yt", hit=300, miss=700), requests_bytes=2 * GB)
    assert "Request log: 2.0 GB (25% of the database) covering 51 days" in section(run(report)[0], "OPPORTUNITIES")
    old = make_report(requests("yt", hit=300, miss=700), requests_bytes=2 * GB,
                      oldest_request_ts="2026-05-01T00:00:00+00:00")
    assert "Request log" not in run(old)[0]


def test_history_keeps_one_snapshot_a_day(tmp_path):
    path = tmp_path / "history.jsonl"
    dr.save_snapshot({"date": "2026-11-19", "db_bytes": 1}, path)
    dr.save_snapshot({"date": "2026-11-20", "db_bytes": 2}, path)
    dr.save_snapshot({"date": "2026-11-20", "db_bytes": 3}, path)
    assert [(s["date"], s["db_bytes"]) for s in dr.load_history(path)] == [("2026-11-19", 1), ("2026-11-20", 3)]
