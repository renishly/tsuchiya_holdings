"""Marketing data (Addendum A51, v2.014): alternative data on demand and sales effort. Code only; no model.

From filings already downloaded (no new source):
  orders     受注高 and 受注残高 per segment from each annual report's 受注実績 table (Tsuchiya, タマホーム,
             グランディハウス) or the text (日本ハウス: "受注高につきましては、263億62百万円")
  ad spend   広告宣伝費 (AdvertisingExpensesSGA) and 販売促進費 where tagged; Tsuchiya does not disclose it
  ratio      ad spend ÷ housing orders, per company and year (a cost of winning orders)
From company websites (robots.txt checked 30 Oct 2026; never a form, never a reservation):
  Tsuchiya   model houses (name, address, prefecture) and events (type, title, place) from tsuchiyahome.jp, daily
  peers      タマホーム shops in Hokkaido and Tohoku and its fair/opening notices; 日本ハウス new model-house
             openings and its event list; weekly. グランディハウス is attempted and reported if refused.
From the student (Inbox): Google Trends CSV exports (see trends.py).
Rows go to Alternative Data with record_type "marketing". Every source's status (ok / error / stale) is kept in
state["documents"]["marketing"] for the error report.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import edinet
from lib import ROOT, BudgetStop, CsvStore, FetchError, GuardrailError, base_row, fetch, iso, log, record_id, run_ctx, unresolved

ERRS = (FetchError, GuardrailError, BudgetStop)
COMPANIES = {"1840": "土屋ホールディングス", "1419": "タマホーム", "1873": "日本ハウスホールディングス", "8999": "グランディハウス"}
ORDER_ROWS = {"1840": ["住宅事業", "リフォーム事業"], "1419": ["住宅事業"], "8999": ["戸建住宅", "注文住宅", "小計"]}
HOKKAIDO = r"札幌|函館|旭川|帯広|苫小牧|岩見沢|釧路|北見|江別|千歳|小樽|室蘭|東川|恵庭|石狩|北広島|登別|滝川|名寄|稚内|網走"
PREFS = r"北海道|青森県?|岩手県?|宮城県?|秋田県?|山形県?|福島県?|茨城県?|栃木県?|群馬県?|埼玉県?|千葉県?|東京都?|神奈川県?|長野県?|新潟県?|富山県?|首都圏"
TSU_SITE = "https://www.tsuchiyahome.jp"
TAMA = "https://www.tamahome.jp"
TAMA_REGIONS = ["hokkaido", "tohoku/aomori", "tohoku/iwate", "tohoku/miyagi", "tohoku/akita", "tohoku/yamagata", "tohoku/fukushima"]
NH = "https://www.nihonhouse-hd.co.jp"
GRANDY = "https://www.grandy.co.jp/"
COMPLETION = re.compile(r"完成見学会|お客様邸|上棟|構造現場|引渡")


def _state(st) -> dict:
    return st.setdefault("documents", {}).setdefault("marketing", {"sources": {}, "snapshots": {}})


def _status(st, key, ok: bool, detail: str = "", **extra):
    _state(st)["sources"][key] = {"checked": iso(), "ok": ok, "detail": detail[:200], **extra}


def _row(st, *, item, value, unit, url, title, location, entity, period_end, period_label, reason, method="HTML",
         source_type="company website", key_extra="", note=""):
    today = iso()[:10]
    row = base_row(record_id=record_id("marketing", entity, item, period_end, key_extra), record_type="marketing",
                   revenue_stream="", category_reason=reason, listing_entity=entity, page_type="marketing",
                   observed_item=item, snapshot_date=today, value="" if value is None else str(value), unit=unit,
                   scale="1", period_label=period_label, period_end=period_end, source_type=source_type,
                   source_title=title, source_url=url, location_in_source=location, extraction_method=method,
                   correction_note=note)
    return unresolved(row, note or "not found in the source; not imputed") if value is None else row


def _pref(addr: str) -> str:
    m = re.search(PREFS, addr or "")
    if m:
        p = m.group(0)
        return p if p.endswith(("県", "都", "道", "圏")) else p + ("都" if p == "東京" else "県")
    return "北海道" if re.search(HOKKAIDO, addr or "") else "unknown"


# ---------------------------------------------------------------- filings: orders and ad spend
_AMT = re.compile(r"\d{1,3}(?:,\d{3})+|\d{1,3}(?![\d,.])")
_PCT = re.compile(r"[+＋△▲\-]?\s?\d{1,4}\.\d%?")
_NIL = re.compile(r"[－―\-]")


def _tokens(s: str, n: int) -> list:
    """Alternate amount / percentage tokens from flattened table text: '21,445,458113.66,053,317116.2'."""
    out, i, want_amt = [], 0, True
    s = s.replace("％", "%").replace(" ", "")
    while i < len(s) and len(out) < n:
        if _NIL.match(s, i):
            out.append(None)
            i += 1
            want_amt = not want_amt
            continue
        m = (_AMT if want_amt else _PCT).match(s, i)
        if not m:
            break
        out.append(m.group(0))
        i = m.end()
        want_amt = not want_amt
    return out


def _yen(tok, unit) -> float | None:
    if tok is None:
        return None
    return float(tok.replace(",", "")) * (1000 if unit == "千円" else 1_000_000 if unit == "百万円" else 1)


def _kanji_yen(s: str) -> float | None:
    m = re.match(r"(\d+)億(\d+)百万円|(\d+)億円|(\d+)百万円", s.translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    if not m:
        return None
    if m.group(1):
        return int(m.group(1)) * 1e8 + int(m.group(2)) * 1e6
    return int(m.group(3)) * 1e8 if m.group(3) else int(m.group(4)) * 1e6


def orders_from_text(ticker: str, text: str) -> dict:
    """{segment: {"orders": yen, "backlog": yen, "count": n}} from the MD&A 受注実績 table (or sentence)."""
    i = text.find("受注実績")
    if ticker == "1873" or i < 0:
        m = re.search(r"受注高につきましては、([0-9０-９億百万円]+)", text)
        return {"住宅事業": {"orders": _kanji_yen(m.group(1))}} if m and _kanji_yen(m.group(1)) else {}
    sec = re.sub(r"（[^）]*）|\([^)]*%\)", "", text[i:text.find("販売実績", i) if text.find("販売実績", i) > 0 else i + 3000])
    unit = "百万円" if "百万円" in sec[:400] else "千円"
    out = {}
    for seg in ORDER_ROWS.get(ticker, []):
        j = sec.find(seg, 30)
        if j < 0:
            continue
        rest = sec[j + len(seg):].lstrip(" 　")
        if ticker == "8999":   # 件数, %, 金額, %, 件数, %, 金額, %; amounts always have comma groups
            n, p, a = r"(\d{1,3}(?:,\d{3})*|[－―])", r"(\d{2,3}\.\d|[－―])", r"(\d{1,3}(?:,\d{3})+|[－―])"
            m = re.match(n + p + a + p + n + p + a + p, rest.replace(" ", ""))
            if m:
                num = lambda g: None if g in ("－", "―") else g
                # counts are not kept: a short count runs into its percentage ("50135.1" = 50 / 135.1 or 501 / 35.1)
                out[seg] = {"orders": _yen(num(m.group(3)), unit), "backlog": _yen(num(m.group(7)), unit)}
        else:                  # 受注高, %, 受注残高, %
            t = _tokens(rest, 4)
            if len(t) == 4:
                out[seg] = {"orders": _yen(t[0], unit), "backlog": _yen(t[2], unit)}
    return out


def filings(st, store: CsvStore) -> dict:
    """Orders, ad spend and their ratio for Tsuchiya and the peers, from every cached annual report (6 per company)."""
    run_id = run_ctx(st)["run_id"]
    docs = st["documents"].get("universe", {}).get("docs", {})
    found, gaps = 0, []
    for t, name in COMPANIES.items():
        for lab, d in sorted((docs.get(t) or {}).items()):
            z = ROOT / "raw" / "edinet" / d["docID"] / f"{d['docID']}_type5.csv.zip"
            if not z.exists():
                continue
            facts = edinet.read_xbrl_csv(z)
            url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{d['docID']}"
            pe, fy = d["periodEnd"], f"FY ending {d['periodEnd']}"
            title = f"有価証券報告書 ({name}, {fy})"
            mdna = next((x["value"] for x in facts if x["element"].endswith("ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock")), "")
            orders = orders_from_text(t, mdna)
            if not orders:
                gaps.append(f"{name} {fy}: order table not found")
            for seg, v in orders.items():
                for k, label in (("orders", "受注高"), ("backlog", "受注残高"), ("count", "受注件数")):
                    if v.get(k) is None:
                        continue
                    store.upsert(_row(st, item=f"{label} ({seg})", value=round(v[k]), unit="JPY" if k != "count" else "件",
                                      url=url, title=title, location="経営者による分析: 受注実績", entity=name, period_end=pe,
                                      period_label=fy, reason="order intake: demand won in the year (leads revenue)",
                                      method="XBRL text block", source_type="有価証券報告書"), run_id)
                    found += 1
            ad = {}
            for x in facts:
                el = x["element"].split(":")[-1]
                if el in ("AdvertisingExpensesSGA", "PromotionExpensesSGA") and x["context"] == "CurrentYearDuration":
                    try:
                        ad[el] = float(x["value"])
                    except ValueError:
                        pass
            for el, label in (("AdvertisingExpensesSGA", "広告宣伝費"), ("PromotionExpensesSGA", "販売促進費")):
                if el in ad:
                    store.upsert(_row(st, item=label, value=round(ad[el]), unit="JPY", url=url, title=title,
                                      location=f"XBRL {el}", entity=name, period_end=pe, period_label=fy,
                                      reason="marketing spend", method="XBRL", source_type="有価証券報告書"), run_id)
                    found += 1
            housing = next((v["orders"] for s_, v in orders.items() if s_ in ("住宅事業", "小計") and v.get("orders")), None)
            spend = ad.get("AdvertisingExpensesSGA")
            if housing and spend:
                store.upsert(_row(st, item="広告宣伝費 ÷ 住宅受注高", value=round(spend / housing, 5), unit="ratio", url=url,
                                  title=title, location="= 広告宣伝費 ÷ 受注高 (住宅)", entity=name, period_end=pe,
                                  period_label=fy, reason="ad spend per yen of housing orders", method="derived",
                                  source_type="derived"), run_id)
            elif lab == "latest" and not spend:
                gaps.append(f"{name}: 広告宣伝費 not disclosed in {fy}")
    _status(st, "filings", True, f"{found} values; " + "; ".join(gaps[:6]), gaps=gaps)
    return {"values": found, "gaps": gaps}


# ---------------------------------------------------------------- Tsuchiya's website
def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", re.sub(r"(?s)<(script|style).*?</\1>", " ", html)).split())


def tsuchiya_site(st, store: CsvStore) -> dict:
    run_id, today = run_ctx(st)["run_id"], iso()[:10]
    snaps = _state(st)["snapshots"]
    out = {}
    try:
        page, _ = fetch(st, f"{TSU_SITE}/modelhouse/", purpose="marketing: Tsuchiya model houses")
        mh = {}
        for href, inner in re.findall(r'<a href="(/modelhouse/\d+)"[^>]*>(.*?)</a>', page, re.S):
            name = re.search(r'-tit-small">(.*?)</div>', inner, re.S)
            addr = re.search(r'-body-small">(.*?)</div>', inner, re.S)
            if name:
                mh[href] = {"name": " ".join(re.sub(r"<[^>]+>", "", name.group(1)).split()),
                            "pref": _pref(addr.group(1) if addr else "")}
        if not mh:
            raise FetchError("layout", "no model houses found on the page (layout changed?)")
        prev = set(snaps.get("tsu_modelhouses", {}))
        opened, closed = sorted(set(mh) - prev) if prev else [], sorted(prev - set(mh)) if prev else []
        snaps["tsu_modelhouses"] = mh
        by = {}
        for v in mh.values():
            by[v["pref"]] = by.get(v["pref"], 0) + 1
        for item, val in [("モデルハウス数 (全体)", len(mh))] + [(f"モデルハウス数 ({p})", n) for p, n in sorted(by.items())] + \
                         [("モデルハウス 新規 (前回比)", len(opened)), ("モデルハウス 閉鎖 (前回比)", len(closed))]:
            store.upsert(_row(st, item=item, value=val, unit="棟", url=f"{TSU_SITE}/modelhouse/", title="土屋ホーム モデルハウス・支店",
                              location="model house list", entity="土屋ホーム", period_end=today, period_label=f"observed {today}",
                              reason="place: showrooms where buyers visit",
                              note="; ".join(mh[h]["name"] for h in opened + closed)[:300]), run_id)
        out["modelhouses"] = {"total": len(mh), "opened": len(opened), "closed": len(closed)}
        _status(st, "tsuchiya_modelhouses", True, f"{len(mh)} model houses")
    except ERRS as e:
        _status(st, "tsuchiya_modelhouses", False, str(e))
    try:
        page, _ = fetch(st, f"{TSU_SITE}/event/", purpose="marketing: Tsuchiya events")
        ev = {}
        for href, inner in re.findall(r'<a href="(/event/\d+)" class="-thum-txt-link">(.*?)</a>', page, re.S):
            label = re.search(r'-label[^"]*">(.*?)</span>', inner, re.S)
            title = re.search(r'-thum-txt-link-outline[^"]*">(.*?)</div>', inner, re.S)
            place = re.search(r"住所</dt>\s*<dd[^>]*>(.*?)</dd>", inner, re.S)
            t_ = " ".join(re.sub(r"<[^>]+>", "", title.group(1)).split()) if title else ""
            ev[href] = {"type": " ".join(re.sub(r"<[^>]+>", "", label.group(1)).split()) if label else "その他",
                        "title": t_, "pref": _pref(re.sub(r"<[^>]+>", "", place.group(1)) if place else t_)}
        if not ev:
            raise FetchError("layout", "no events found on the page (layout changed?)")
        prev = set(snaps.get("tsu_events", {}))
        new = [h for h in ev if h not in prev] if prev else []
        snaps["tsu_events"] = ev
        types, prefs = {}, {}
        for v in ev.values():
            types[v["type"]] = types.get(v["type"], 0) + 1
            prefs[v["pref"]] = prefs.get(v["pref"], 0) + 1
        comp = sum(1 for v in ev.values() if COMPLETION.search(v["title"]))
        rows = [("イベント掲載数 (全体)", len(ev)), ("イベント 新規 (前回比)", len(new)),
                ("完成見学会・上棟 (掲載中)", comp)] + [(f"イベント掲載数 ({k})", n) for k, n in sorted(types.items())] + \
               [(f"イベント掲載数 ({p})", n) for p, n in sorted(prefs.items())]
        for item, val in rows:
            store.upsert(_row(st, item=item, value=val, unit="件", url=f"{TSU_SITE}/event/", title="土屋ホーム イベント",
                              location="event list", entity="土屋ホーム", period_end=today, period_label=f"observed {today}",
                              reason="promotion; completion tours and roof-raisings follow actual builds"), run_id)
        out["events"] = {"total": len(ev), "new": len(new), "completion": comp}
        _status(st, "tsuchiya_events", True, f"{len(ev)} events ({comp} completion/roof-raising)")
    except ERRS as e:
        _status(st, "tsuchiya_events", False, str(e))
    return out


# ---------------------------------------------------------------- peers' websites (weekly)
def peers_site(st, store: CsvStore) -> dict:
    run_id, today = run_ctx(st)["run_id"], iso()[:10]
    out = {}
    counts, failed = {}, []
    for reg in TAMA_REGIONS:   # タマホーム shops, one page per prefecture
        try:
            page, _ = fetch(st, f"{TAMA}/modelhouse/{reg}", purpose="marketing: タマホーム shops")
            counts[reg] = len(re.findall(r"所在地\s*(?:</[^>]+>\s*<[^>]+>\s*)*〒", page)) or _text(page).count("所在地 〒")
        except ERRS as e:
            failed.append(f"{reg}: {str(e)[:60]}")
    if counts:
        hk = counts.get("hokkaido", 0)
        th = sum(v for k, v in counts.items() if k.startswith("tohoku"))
        for item, val in (("店舗数 (北海道)", hk), ("店舗数 (東北)", th)):
            store.upsert(_row(st, item=item, value=val, unit="店", url=f"{TAMA}/modelhouse/", title="タマホーム 店舗検索",
                              location="shop list per prefecture", entity="タマホーム", period_end=today,
                              period_label=f"observed {today}", reason="place: competitor showrooms in Tsuchiya's areas"), run_id)
        out["tama_shops"] = {"hokkaido": hk, "tohoku": th}
    _status(st, "tama_shops", bool(counts) and not failed, "; ".join(failed) or f"{sum(counts.values())} shops in 7 prefectures")
    try:
        page, _ = fetch(st, f"{TAMA}/info/event/", purpose="marketing: タマホーム notices")
        items = re.findall(r"(\d{4})\.(\d{2})\.(\d{2})\s*お知らせ\s*(.{0,80}?)(?=\d{4}\.\d{2}\.\d{2}|$)", _text(page))
        cut = (dt.date.fromisoformat(today) - dt.timedelta(days=30)).isoformat()
        recent = [f"{y}-{m}-{d}" for y, m, d, t_ in items if f"{y}-{m}-{d}" >= cut and re.search(r"フェア|見学会|オープン|キャンペーン", t_)]
        store.upsert(_row(st, item="フェア・見学会・オープン告知 (30日)", value=len(recent), unit="件", url=f"{TAMA}/info/event/",
                          title="タマホーム お知らせ", location="notice list", entity="タマホーム", period_end=today,
                          period_label=f"30 days to {today}", reason="promotion intensity (national notices)"), run_id)
        out["tama_notices"] = len(recent)
        _status(st, "tama_events", True, f"{len(recent)} promotional notices in 30 days")
    except ERRS as e:
        _status(st, "tama_events", False, str(e))
    try:
        page, _ = fetch(st, f"{NH}/modelhouse/", purpose="marketing: 日本ハウス model houses")
        # the page lists prefectures and new openings, not every model house: only the openings are counted
        new = re.findall(r"(\S+展示場)\s+\d{1,2}月\d{1,2}日（.）オープン", _text(page))
        for item, val in (("展示場 新規オープン告知", len(new)),):
            store.upsert(_row(st, item=item, value=val, unit="棟", url=f"{NH}/modelhouse/", title="日本ハウスHD お近くの展示場",
                              location="model house page", entity="日本ハウスホールディングス", period_end=today,
                              period_label=f"observed {today}", reason="place: competitor showrooms",
                              note="; ".join(new)[:300]), run_id)
        out["nh_modelhouses"] = {"new": len(new)}
        _status(st, "nh_modelhouses", True, f"{len(new)} opening notice(s); model houses per prefecture are on separate pages, not counted")
    except ERRS as e:
        _status(st, "nh_modelhouses", False, str(e))
    try:
        page, _ = fetch(st, f"{NH}/eventmenu/", purpose="marketing: 日本ハウス events")
        ids = set(re.findall(r"/eventmenu/event/\?id=(\d+)", page))
        store.upsert(_row(st, item="イベント掲載数 (全体)", value=len(ids), unit="件", url=f"{NH}/eventmenu/", title="日本ハウスHD イベント情報",
                          location="event list", entity="日本ハウスホールディングス", period_end=today,
                          period_label=f"observed {today}", reason="promotion intensity"), run_id)
        out["nh_events"] = len(ids)
        _status(st, "nh_events", bool(ids), f"{len(ids)} events" if ids else "no events found (layout changed?)")
    except ERRS as e:
        _status(st, "nh_events", False, str(e))
    try:
        fetch(st, GRANDY, purpose="marketing: グランディハウス site")
        _status(st, "grandy", True, "reachable (no parser yet)")
    except ERRS as e:
        _status(st, "grandy", False, str(e))
    return out


def refresh(st, store: CsvStore, weekly: bool) -> dict:
    """One steady-state step: filings and Tsuchiya's site daily; peers' sites when `weekly`."""
    res = {}
    for key, fn in (("filings", lambda: filings(st, store)), ("tsuchiya", lambda: tsuchiya_site(st, store))) + \
                   ((("peers", lambda: peers_site(st, store)),) if weekly else ()):
        try:
            res[key] = fn()
        except Exception as e:   # one source never stops the run
            _status(st, key, False, f"{type(e).__name__}: {str(e)[:150]}")
            res[key] = {"error": str(e)[:150]}
    if weekly:
        _state(st)["last_peers"] = iso()[:10]
    log(st, "marketing", {"weekly": weekly}, {k: (v if isinstance(v, dict) and "error" in v else "ok") for k, v in res.items()})
    return res
