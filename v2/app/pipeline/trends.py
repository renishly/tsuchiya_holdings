"""Google Trends exports uploaded by the student (Addendum A51, v2.014). Code only; no model.

The agent may not collect Google Trends itself (robots.txt disallows the explore pages; the official API is a closed
alpha). The student downloads a comparison as CSV (classic or new Explore) and uploads it to the Inbox of Tsuchibot
Main or C&A. This module reads it: one column per search term, values 0-100 relative to the highest point in the
chart ("<1" is stored as 0.5). It keeps the file under inputs/google_trends/, writes monthly averages and each term's
share of search (its value ÷ the sum of all terms) to Alternative Data, and records the latest data date. The data
counts as outdated when its last data point is more than STALE_DAYS old (shown in the error report).
"""
from __future__ import annotations

import csv
import io
import re
from pathlib import Path

from lib import CsvStore, base_row, iso, log, record_id, run_ctx, sha256, write_bytes

STALE_DAYS = 35
_DATE = re.compile(r"^(\d{4})-(\d{2})(?:-(\d{2}))?")
_HEAD = re.compile(r"^(週|月|日|Week|Month|Day|Time|時間|Date|日付)$", re.I)


def looks_like_trends(name: str, body: bytes) -> bool:
    head = body[:600].decode("utf-8-sig", errors="replace")
    n = name.lower()
    return n.endswith(".csv") and (n.startswith(("time_series", "searched_with", "multitimeline", "relatedqueries")) or
                                   bool(re.search(r"カテゴリ|Category|\(日本\)|\(Japan\)|週,|Week,|Time,|月,|Month,", head)))


def store_reference(st, name: str, body: bytes, uploaded_at: str = "") -> dict:
    """Related-queries exports (top / rising searches) are not time series: kept as reference files only."""
    rel = f"inputs/google_trends/{(uploaded_at or iso())[:10]}_{sha256(body)[:8]}_{Path(name).name}"
    write_bytes(rel, body)
    st.setdefault("documents", {}).setdefault("trends_reference", []).append({"file": rel, "uploaded": (uploaded_at or iso())[:10]})
    log(st, "trends", {"file": rel}, "related-queries export kept as reference")
    return {"ok": True, "reference": rel}


def parse(body: bytes) -> dict:
    """{"terms": [...], "geo": "...", "rows": [(date, {term: value})]} from a Trends CSV."""
    text = body.decode("utf-8-sig", errors="replace")
    lines = list(csv.reader(io.StringIO(text)))
    hi = next((i for i, r in enumerate(lines) if r and _HEAD.match(r[0].strip())), None)
    if hi is None:
        return {"error": "no date column header (週/Week/Time…) found"}
    head = lines[hi]
    terms = [re.sub(r":\s*\(.*\)$", "", h).strip() for h in head[1:]]
    geo = (re.search(r"\((.*?)\)", head[1]) or [None, ""])[1] if len(head) > 1 else ""
    rows = []
    for r in lines[hi + 1:]:
        if not r or not _DATE.match(r[0].strip()):
            continue
        vals = {}
        for t, v in zip(terms, r[1:]):
            v = v.strip()
            vals[t] = 0.5 if v.startswith("<") else float(v) if re.fullmatch(r"\d+(\.\d+)?", v) else None
        rows.append((r[0].strip()[:10], vals))
    if not rows or not terms:
        return {"error": "no data rows"}
    return {"terms": terms, "geo": geo, "rows": rows}


def ingest(st, name: str, body: bytes, uploaded_at: str = "") -> dict:
    """Store an uploaded Trends CSV; returns {"ok": True, ...} or {"error": ...}."""
    p = parse(body)
    if "error" in p:
        return p
    h = sha256(body)
    rel = f"inputs/google_trends/{(uploaded_at or iso())[:10]}_{h[:8]}_{Path(name).name}"
    write_bytes(rel, body)
    store = CsvStore("Alternative Data")
    run_id = run_ctx(st)["run_id"]
    months: dict[str, dict[str, list]] = {}
    for d, vals in p["rows"]:
        for t, v in vals.items():
            if v is not None:
                months.setdefault(d[:7], {}).setdefault(t, []).append(v)
    for m, by in sorted(months.items()):
        avg = {t: sum(v) / len(v) for t, v in by.items()}
        tot = sum(avg.values())
        for t, v in avg.items():
            for item, val, unit in ((f"Google検索関心度 ({t})", round(v, 2), "index 0-100"),
                                    (f"検索シェア ({t})", round(v / tot, 4) if tot else None, "share")):
                if val is None:
                    continue
                store.upsert(base_row(
                    record_id=record_id("trends", p["geo"], t, item, m), record_type="marketing", page_type="marketing",
                    observed_item=item, listing_entity=t, value=str(val), unit=unit, scale="1",
                    snapshot_date=iso()[:10], period_label=m, period_start=f"{m}-01", period_end=f"{m}-28",
                    category_reason="brand interest / share of search (relative to the terms in the same chart)",
                    source_type="Google Trends (student export)", source_title=f"Google Trends: {', '.join(p['terms'])} ({p['geo']})",
                    source_url="https://trends.google.com/", location_in_source=rel, extraction_method="CSV (student upload)",
                    correction_note="Index is relative within this chart: compare terms and months, not across charts."), run_id)
    store.save(st)
    last = p["rows"][-1][0]
    st.setdefault("documents", {})["trends"] = {"file": rel, "uploaded": (uploaded_at or iso())[:10], "last_date": last,
                                                "first_date": p["rows"][0][0], "terms": p["terms"], "geo": p["geo"]}
    log(st, "trends", {"file": rel}, f"{len(p['rows'])} points, {len(p['terms'])} terms, last {last}")
    return {"ok": True, "terms": p["terms"], "points": len(p["rows"]), "last_date": last}
