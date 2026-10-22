"""Legal sources and the legal penalty score (phase P8). Weights per Addendum A5 (50/30/10/10).

The score is an exposure measure, never a finding of wrongdoing.
"""
from __future__ import annotations

import datetime as dt
import json
import re

from lib import (ROOT, BudgetStop, CsvStore, FetchError, GuardrailError, base_row, fetch, iso, log,
                 pii_scrub, record_id, run_ctx, unresolved)

DISCLAIMER = "This is an exposure measure, not a finding of wrongdoing."
WEIGHTS = {"a": 50, "b": 30, "c": 10, "d": 10}  # Addendum A5; changes require approval
ENTITIES = ["株式会社土屋ホールディングス", "株式会社土屋ホーム", "株式会社土屋ホームトピア", "株式会社土屋ホーム不動産",
            "株式会社土屋ホーム不動産販売", "株式会社土屋ホームレジデンス", "株式会社土屋ソーラーファクトリー"]
NEGA = "https://www.mlit.go.jp/nega-inf/cgi-bin/search.cgi"
SEVERITY = {"免許取消": 4, "許可取消": 4, "業務停止": 2, "営業停止": 2, "指示": 1, "行政指導": 0.5, "勧告": 0.5}
TOPICS = {"両手取引", "囲い込み", "手数料規制", "行政処分", "other"}

# Component (d): rules in force or announced that affect brokerage. Points sum to 10.
# Each is counted only if its primary source is fetched this run and contains the keyword.
REG_RULES = [
    {"id": "takken-34-2", "points": 2, "title": "宅地建物取引業法 第34条の2 (媒介契約; 専任媒介の報告・登録義務)",
     "url": "https://laws.e-gov.go.jp/api/2/law_data/327AC1000000176?elm=MainProvision-Article_34_2&response_format=json",
     "keyword": "媒介契約"},
    {"id": "takken-46", "points": 2, "title": "宅地建物取引業法 第46条 (報酬の額の制限)",
     "url": "https://laws.e-gov.go.jp/api/2/law_data/327AC1000000176?elm=MainProvision-Article_46&response_format=json",
     "keyword": "報酬"},
    {"id": "reins-status-2025", "points": 3,
     "title": "宅地建物取引業法施行規則: 指定流通機構への登録事項に「取引の申込みの受付に関する状況」(2025年1月施行, 囲い込み対策)",
     "url": "https://laws.e-gov.go.jp/api/2/law_data/332M50004000012?response_format=json",
     "keyword": "取引の申込みの受付に関する状況"},
    # Primary sources not yet pinned to an allowlisted URL: counted as 0 until the user supplies one.
    {"id": "akiya-fee-2024", "points": 1, "title": "低廉な空家等の媒介報酬の特例 (令和6年国土交通省告示第949号, 2024年7月施行)",
     "url": "", "keyword": "空家"},
    {"id": "kantoku-kijun", "points": 2, "title": "宅地建物取引業者の違反行為に対する監督処分の基準 (囲い込みの扱い)",
     "url": "", "keyword": "監督処分"},
]

def _years(n=5):
    y = int(iso()[:4])
    return y - n, y

def search_dispositions(st, store: CsvStore) -> dict:
    """MLIT ネガティブ情報等検索サイト: 宅建業者 and 建設業者 searches for '土屋'; only exact group entities count."""
    from bs4 import BeautifulSoup
    run_id = run_ctx(st)["run_id"]
    y0, y1 = _years()
    stats = {"hits_group": 0, "hits_other": 0, "searches": 0}
    searches = [("takuti", "jigyousya", "宅地建物取引業者"), ("kensetugyousya", "disposal_name1", "建設業者")]
    for cat, field, label in searches:
        form = {"jigyoubunya": cat, "EID": "search", "start_year": str(y0), "start_month": "1",
                "end_year": str(y1), "end_month": "12", field: "土屋"}
        try:
            body, meta = fetch(st, NEGA, data=form, purpose=f"MLIT negative info ({label})")
        except (FetchError, GuardrailError) as e:
            row = base_row(record_id=record_id("nega", cat, iso()[:10]), record_type="legal_article", topic="行政処分",
                           headline=f"MLIT ネガティブ情報 search ({label})", mentions_tsuchiya="")
            row.update(source_url=f"{NEGA}?jigyoubunya={cat}", source_title="国土交通省 ネガティブ情報等検索サイト",
                       source_type="government database", location_in_source=f"search 事業者名=土屋, {y0}-{y1}",
                       extraction_method="HTML")
            store.upsert(unresolved(row, f"Search failed: {e}"), run_id)
            continue
        stats["searches"] += 1
        soup = BeautifulSoup(body, "lxml")
        m = re.search(r"検索結果：\s*(\d+)件", soup.get_text(" "))
        total = int(m.group(1)) if m else None
        group_hits = []
        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if not cells:
                continue
            joined = " ".join(cells)
            ent = next((e for e in ENTITIES if e.replace("株式会社", "") in joined.replace("（株）", "").replace("(株)", "")), None)
            if not ent:
                if "土屋" in joined:
                    stats["hits_other"] += 1
                continue
            date = re.search(r"(20\d\d)年(\d{1,2})月(\d{1,2})日", joined)
            kind = next((k for k in SEVERITY if k in joined), "")
            group_hits.append({"entity": ent, "date": date.group(0) if date else "", "kind": kind})
        for h in group_hits:
            row = base_row(record_id=record_id("nega", cat, h["entity"], h["date"], h["kind"]), record_type="legal_article",
                           publisher="国土交通省 ネガティブ情報等検索サイト", published_date=h["date"],
                           headline=f"{h['kind']} — {h['entity']} ({label})", topic="行政処分", mentions_tsuchiya="yes",
                           summary_en=f"MLIT database lists a {h['kind']} disposition against {h['entity']} dated {h['date']}.",
                           value=h["kind"], unit="", scale="")
            row.update(source_url=f"{NEGA}?jigyoubunya={cat}", source_title="国土交通省 ネガティブ情報等検索サイト",
                       source_type="government database (primary)", location_in_source=f"search 事業者名=土屋, {y0}-{y1}",
                       extraction_method="HTML", period_label=h["date"])
            store.upsert(row, run_id)
            stats["hits_group"] += 1
        row = base_row(record_id=record_id("nega-count", cat, iso()[:10]), record_type="legal_article",
                       publisher="国土交通省 ネガティブ情報等検索サイト", published_date=iso()[:10],
                       headline=f"Search result: {len(group_hits)} disposition(s) against Tsuchiya group entities ({label}, {y0}-{y1})",
                       topic="行政処分", mentions_tsuchiya="yes" if group_hits else "no",
                       summary_en=(f"The MLIT database returned {total} record(s) for '土屋'; {len(group_hits)} match a Tsuchiya group entity. "
                                   "Coverage of prefectural dispositions may be incomplete."),
                       value=str(len(group_hits)), unit="records", scale="1", period_label=f"{y0}-{y1}")
        row.update(source_url=f"{NEGA}?jigyoubunya={cat}", source_title="国土交通省 ネガティブ情報等検索サイト",
                   source_type="government database (primary)", location_in_source=f"POST search 事業者名=土屋, {y0}-{y1}",
                   extraction_method="HTML")
        if total is None:
            row = unresolved(row, "Result count not found in response; page layout may have changed.")
        store.upsert(row, run_id)
    return stats

def check_regulations(st, store: CsvStore) -> dict:
    run_id = run_ctx(st)["run_id"]
    stats = {"verified": 0, "unverified": 0}
    for r in REG_RULES:
        ok, note = False, ""
        if not r["url"]:
            note = "primary-source URL not yet confirmed (builder could not pin an allowlisted page); counted as 0"
        try:
            if not r["url"]:
                raise FetchError("pending", note)
            body, meta = fetch(st, r["url"], purpose=f"regulation source {r['id']}")
            ok = r["keyword"] in body
            note = "" if ok else f"keyword '{r['keyword']}' not found on page"
        except (FetchError, GuardrailError) as e:
            note = str(e)[:150]
        except BudgetStop:
            break
        row = base_row(record_id=record_id("regulation", r["id"]), record_type="legal_article", topic="手数料規制"
                       if "報酬" in r["title"] else ("囲い込み" if "囲い込み" in r["title"] or "REINS" in r["title"] else "other"),
                       publisher="e-Gov / 国土交通省", headline=r["title"], mentions_tsuchiya="no",
                       summary_en=f"Primary source for a brokerage rule counted in legal-score component (d) ({r['points']} pt).",
                       value=str(r["points"]) if ok else "", unit="points", scale="1",
                       period_label=f"checked {iso()[:10]}")
        row.update(source_url=r["url"] or "https://laws.e-gov.go.jp/", source_title=r["title"], source_type="primary legal source",
                   location_in_source=f"keyword '{r['keyword']}'", extraction_method="HTML/API")
        if not ok:
            row = unresolved(row, f"Not verified this run: {note}")
            stats["unverified"] += 1
        else:
            stats["verified"] += 1
        store.upsert(row, run_id)
    return stats

def ingest_news(st, store: CsvStore, path: str) -> dict:
    """Validate and store news items the agent collected with WebSearch (metadata + own one-sentence summary)."""
    run_id = run_ctx(st)["run_id"]
    items = json.loads((ROOT / path).read_text(encoding="utf-8"))
    stats = {"stored": 0, "rejected": 0}
    for it in items:
        url = (it.get("url") or "").strip()
        head = (it.get("headline") or "").strip()
        summ = (it.get("summary_en") or "").strip()
        topic = it.get("topic") if it.get("topic") in TOPICS else "other"
        ment = "yes" if it.get("mentions_tsuchiya") in (True, "yes") else "no"
        reasons = []
        if not url.startswith("https://"):
            reasons.append("url")
        if not head or len(head) > 200:
            reasons.append("headline length")
        if len(summ) > 300 or summ.count(". ") > 1:
            reasons.append("summary must be one sentence in own words")
        if not re.fullmatch(r"20\d\d-\d\d-\d\d", it.get("published_date") or ""):
            reasons.append("published_date")
        if ment == "yes" and not any(e.replace("株式会社", "") in head + summ for e in ENTITIES + ["土屋ホールディングス", "土屋グループ"]):
            reasons.append("mentions_tsuchiya=yes but no group entity named in headline/summary")
        if reasons:
            stats["rejected"] += 1
            log(st, "news_ingest", {"url": url}, f"rejected: {', '.join(reasons)}")
            continue
        summ, _ = pii_scrub(summ)
        row = base_row(record_id=record_id("news", url), record_type="legal_article", publisher=it.get("publisher", "")[:80],
                       published_date=it["published_date"], headline=head, summary_en=summ, topic=topic,
                       mentions_tsuchiya=ment, value=topic, unit="", scale="", period_label=it["published_date"])
        row.update(source_url=url, source_title=head, source_type="news (web search)",
                   location_in_source="article (headline/URL/date only stored)", extraction_method="web search")
        store.upsert(row, run_id)
        stats["stored"] += 1
    return stats

def score(st, store: CsvStore) -> dict:
    """Recompute the 0-100 legal exposure score from rows already in Alternative Data.csv."""
    run_id = run_ctx(st)["run_id"]
    today = dt.date.fromisoformat(iso()[:10])
    five_y = today.replace(year=today.year - 5)
    rows = list(store.rows.values())

    def recent(r):
        m = re.search(r"(20\d\d)[-年](\d{1,2})[-月](\d{1,2})", r.get("published_date") or "")
        return bool(m) and dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))) >= five_y

    comps = {}
    # (a) dispositions
    disp = [r for r in rows if r["record_type"] == "legal_article" and r["topic"] == "行政処分"
            and r["mentions_tsuchiya"] == "yes" and r["value"] in SEVERITY and recent(r)]
    searched = [r for r in rows if r["record_type"] == "legal_article" and r["headline"].startswith("Search result:")
                and r["agent_status"] != "unresolved"]
    pts = sum(SEVERITY[r["value"]] for r in disp)
    comps["a"] = None if not searched else WEIGHTS["a"] * min(1.0, pts / 4)
    a_in = [r["record_id"] for r in disp + searched]
    a_f = f"{WEIGHTS['a']} × min(1, Σ severity / 4); severity 免許/許可取消=4, 業務/営業停止=2, 指示=1, 行政指導/勧告=0.5; last 5 years; Σ={pts}"
    # (b) news/court items naming a group entity in a brokerage legal context
    news = [r for r in rows if r["record_type"] == "legal_article" and r["source_type"].startswith("news")
            and r["mentions_tsuchiya"] == "yes" and r["topic"] in ("両手取引", "囲い込み", "手数料規制", "行政処分") and recent(r)]
    news_any = [r for r in rows if r["record_type"] == "legal_article" and r["source_type"].startswith("news")]
    comps["b"] = None if not news_any and not st["daily"].get("news_searched") else WEIGHTS["b"] * min(1.0, len(news) / 3)
    b_f = f"{WEIGHTS['b']} × min(1, n / 3); n = news/court items naming a group entity, topic in 両手取引/囲い込み/手数料規制/行政処分, last 5 years; n={len(news)}"
    # (c) share of observed group brokerage listings under 専任/専属専任
    brok = [r for r in rows if r["record_type"] == "listing" and r["status"] == "active"
            and r["revenue_stream"] == "不動産事業: 仲介" and not r["rent_jpy"]]
    excl = [r for r in brok if re.search(r"専任|専属", r["取引態様"])]
    comps["c"] = None if not brok else WEIGHTS["c"] * len(excl) / len(brok)
    c_f = f"{WEIGHTS['c']} × (専任+専属専任 listings / all active group brokerage sale listings) = {WEIGHTS['c']} × {len(excl)}/{len(brok) or 0}"
    # (d) regulation severity
    regs = [r for r in rows if r["record_type"] == "legal_article" and r["unit"] == "points"]
    ver = sum(float(r["value"]) for r in regs if r["value"])
    comps["d"] = None if not ver else WEIGHTS["d"] * ver / 10  # nothing verified -> unavailable, not zero
    d_f = f"{WEIGHTS['d']} × (verified rule points / 10); verified points = {ver:g}"
    inputs = {"a": a_in, "b": [r["record_id"] for r in news], "c": [r["record_id"] for r in brok], "d": [r["record_id"] for r in regs]}
    formulas = {"a": a_f, "b": b_f, "c": c_f, "d": d_f}
    names = {"a": "Administrative dispositions (MLIT records)", "b": "News/court items naming a group entity",
             "c": "Exclusive-mandate share of observed brokerage listings", "d": "Severity of brokerage regulation in force"}
    total, avail = 0.0, 0
    for k in "abcd":
        v = comps[k]
        row = base_row(record_id=record_id("legal_score", k), record_type="legal_score", topic="other",
                       headline=f"Legal score component ({k}): {names[k]}", value="" if v is None else f"{v:.2f}",
                       unit="points", scale="1", period_label=f"as of {iso()[:10]}", mentions_tsuchiya="",
                       correction_note=f"Formula: {formulas[k]}. Inputs: {', '.join(inputs[k][:30]) or 'none'}. {DISCLAIMER}")
        row.update(source_url=_first_url(store, inputs[k]) or "https://www.mlit.go.jp/nega-inf/",
                   source_title="Inputs listed in correction_note", source_type="derived",
                   location_in_source="Alternative Data.csv rows listed", extraction_method="derived")
        if v is None:
            row = unresolved(row, f"No inputs available yet for this component. Formula: {formulas[k]}. {DISCLAIMER}")
        else:
            total += v
            avail += WEIGHTS[k]
        store.upsert(row, run_id)
    row = base_row(record_id=record_id("legal_score", "total"), record_type="legal_score", topic="other",
                   headline="Legal penalty score (total, 0-100)", value=f"{total:.2f}" if avail else "",
                   unit="points (0-100)", scale="1", period_label=f"as of {iso()[:10]}",
                   correction_note=(f"Total = a + b + c + d (weights 50/30/10/10, Addendum A5). Components available: {avail} of 100 points"
                                    + ("" if avail == 100 else "; missing components are excluded, not imputed") + f". {DISCLAIMER}"))
    row.update(source_url="https://www.mlit.go.jp/nega-inf/", source_title="Component rows (record_type=legal_score)",
               source_type="derived", location_in_source="legal_score component rows", extraction_method="derived")
    if not avail:
        row = unresolved(row, f"No components available. {DISCLAIMER}")
    store.upsert(row, run_id)
    return {"total": round(total, 2), "available_weight": avail, "components": comps}

def _first_url(store, ids):
    for i in ids:
        r = store.get(i)
        if r and r["source_url"]:
            return r["source_url"]
    return ""
