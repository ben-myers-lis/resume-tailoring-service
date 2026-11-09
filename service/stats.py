"""Application counts per day and per week (weeks start Monday), in the organisation's time zone."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def today(tz: str) -> date:
    return datetime.now(ZoneInfo(tz)).date()


def week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


def day_series(rows: list[dict], end: date, days: int) -> list[dict]:
    counts = {r["applied_on"]: r["n"] for r in rows}
    start = end - timedelta(days=days - 1)
    return [{"date": (start + timedelta(days=i)).isoformat(),
             "count": counts.get((start + timedelta(days=i)).isoformat(), 0)} for i in range(days)]


def week_series(rows: list[dict], end: date, weeks: int) -> list[dict]:
    first = week_start(end) - timedelta(weeks=weeks - 1)
    buckets = {(first + timedelta(weeks=i)).isoformat(): 0 for i in range(weeks)}
    for r in rows:
        ws = week_start(date.fromisoformat(r["applied_on"])).isoformat()
        if ws in buckets:
            buckets[ws] += r["n"]
    return [{"week_start": k, "week_end": (date.fromisoformat(k) + timedelta(days=6)).isoformat(), "count": v}
            for k, v in buckets.items()]


def candidate_stats(db, candidate_id: str, tz: str, days: int = 30, weeks: int = 12) -> dict:
    t = today(tz)
    since = min(t - timedelta(days=days - 1), week_start(t) - timedelta(weeks=weeks - 1))
    rows = db.counts_by_day({candidate_id}, since.isoformat(), t.isoformat())
    per_day = day_series(rows, t, days)
    per_week = week_series(rows, t, weeks)
    total = db.total_applications({candidate_id}).get(candidate_id, 0)
    return {"today": per_day[-1]["count"], "this_week": per_week[-1]["count"], "total": total,
            "per_day": per_day, "per_week": per_week, "as_of": t.isoformat(), "time_zone": tz}


def summary_counts(db, candidate_ids: set[str] | None, tz: str) -> dict:
    """Today / this week / total per candidate, for list pages."""
    t = today(tz)
    ws = week_start(t)
    rows = db.counts_by_day(candidate_ids, ws.isoformat(), t.isoformat(), group_by="candidate_id")
    out: dict[str, dict] = {}
    for r in rows:
        d = out.setdefault(r["candidate_id"], {"today": 0, "this_week": 0, "total": 0})
        d["this_week"] += r["n"]
        if r["applied_on"] == t.isoformat():
            d["today"] += r["n"]
    for cid, n in db.total_applications(candidate_ids).items():
        out.setdefault(cid, {"today": 0, "this_week": 0, "total": 0})["total"] = n
    return out


def dashboard(db, candidate_ids: set[str] | None, tz: str, days: int = 14, weeks: int = 8) -> dict:
    t = today(tz)
    since = min(t - timedelta(days=days - 1), week_start(t) - timedelta(weeks=weeks - 1))
    rows = db.counts_by_day(candidate_ids, since.isoformat(), t.isoformat())
    per_day, per_week = day_series(rows, t, days), week_series(rows, t, weeks)
    ws = week_start(t).isoformat()
    by_member = db.counts_by_day(candidate_ids, ws, t.isoformat(), group_by="applied_by")
    members: dict[str, int] = {}
    for r in by_member:
        members[r["applied_by"]] = members.get(r["applied_by"], 0) + r["n"]
    return {"as_of": t.isoformat(), "time_zone": tz, "week_start": ws,
            "today": per_day[-1]["count"], "this_week": per_week[-1]["count"],
            "per_day": per_day, "per_week": per_week, "this_week_by_member": members}
