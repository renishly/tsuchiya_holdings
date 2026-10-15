"""Market data and company forecasts (phase P6 and the every-run price refresh).

Sources (Addenda A8, A9):
  - irbank.net/<ticker>/chart : latest daily close (third-party; Agent Warning until verified)
  - irbank.net/<ticker>       : 予想EPS / 予想PER (cross-check rows only)
  - inputs/manual_prices.csv  : student-entered JPX price + quote time + screenshot (takes precedence)
  - company IR pages          : latest 決算短信 / 業績予想の修正 PDF -> 会社予想 (primary)
"""
from __future__ import annotations

import csv
import io
import re
import urllib.parse

from lib import (CONFIG, ROOT, BudgetStop, FetchError, GuardrailError, fetch, iso, log, now,
                 run_ctx, write_bytes)

TICKER_RE = re.compile(r"^[0-9][0-9A-Z]{3}$")

def _check_ticker(t: str):
    if not TICKER_RE.match(t or ""):
        raise GuardrailError(f"bad ticker {t!r}")

def _text(html_s: str) -> str:
    import html as _h
    t = re.sub(r"<script.*?</script>|<style.*?</style>", "", html_s, flags=re.S)
    t = re.sub(r"<[^>]+>", "|", t)
    return re.sub(r"(\s*\|\s*)+", "|", _h.unescape(t))

def _num(s: str):
    s = (s or "").replace(",", "").replace("△", "-").replace("▲", "-").strip()
    if s in ("", "-", "－", "―", "—"):
        return None
    try:
        return float(s)
    except ValueError:
        return None

# ---------------------------------------------------------------- prices
def irbank_close(st, ticker: str) -> dict:
    """Latest daily close from irbank's price table: {'date','close','url'}."""
    _check_ticker(ticker)
    url = f"https://irbank.net/{ticker}/chart"
    body, meta = fetch(st, url, purpose="daily close (third-party)")
    t = _text(body)
    m = re.search(r"株価推移\|日付\|始値\|高値\|安値\|終値\|前日比\|出来高.*?\|(20\d\d)\|(\d{1,2})/(\d{1,2})\|([\d,.]+)\|([\d,.]+)\|([\d,.]+)\|([\d,.]+)\|", t)
    if not m:
        raise FetchError("parse", "price table not found on irbank chart page")
    y, mo, d = m.group(1), int(m.group(2)), int(m.group(3))
    return {"date": f"{y}-{mo:02d}-{d:02d}", "close": _num(m.group(7)), "open": _num(m.group(4)),
            "url": url, "sha256": meta["sha256"]}

def irbank_summary(st, ticker: str) -> dict:
    """irbank 予想PER / 予想EPS / BPS (cross-check only)."""
    _check_ticker(ticker)
    url = f"https://irbank.net/{ticker}"
    body, meta = fetch(st, url, purpose="予想PER/EPS cross-check")
    t = _text(body)
    out = {"url": url}
    m = re.search(r"\|(20\d\d/\d\d/\d\d)\|時価\|", t)
    out["as_of"] = m.group(1).replace("/", "-") if m else ""
    m = re.search(r"PER\|予\|([\-\d,.]+)倍", t)
    out["forward_per"] = _num(m.group(1)) if m else None
    m = re.search(r"EPS（連）\|予\|([\-\d,.]+)\|円/株", t) or re.search(r"EPS（単）\|予\|([\-\d,.]+)\|円/株", t)
    out["forecast_eps"] = _num(m.group(1)) if m else None
    m = re.search(r"BPS（連）\|([\-\d,.]+)\|円/株", t)
    out["bps"] = _num(m.group(1)) if m else None
    return out

def manual_prices() -> list[dict]:
    """Rows from inputs/manual_prices.csv (student-entered JPX prices). Columns:
    ticker, price_jpy, quote_time_jst (ISO 8601 +09:00), price_type (intraday|close), source_url, screenshot_ref."""
    p = ROOT / "inputs" / "manual_prices.csv"
    if not p.exists():
        return []
    rows = []
    with open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            try:
                if not TICKER_RE.match(r["ticker"].strip()) or float(r["price_jpy"]) <= 0:
                    continue
                if not re.match(r"^20\d\d-\d\d-\d\dT\d\d:\d\d(:\d\d)?\+09:00$", r["quote_time_jst"].strip()):
                    continue
                rows.append({k: (v or "").strip() for k, v in r.items()})
            except (KeyError, ValueError):
                continue
    return rows

YF_SYMBOL = {"1840": "1840.T", "205A": "205A.T", "1873": "1873.T", "1419": "1419.T", "2976": "2976.S"}  # 2976: 札証 only

def yf_prices(st) -> dict:
    """Latest daily close per ticker from the shared daily price store (Addenda A16, A28).

    The store is updated by factors.update_prices: at most one yfinance batch per JST day, after the TSE close.
    This function itself never calls Yahoo. Returns {ticker: {"close", "date", "symbol", "url"}}.
    """
    cr = run_ctx(st)
    if "yf_prices" in cr:
        return cr["yf_prices"]
    out = {}
    if cr.get("mode") == "test":
        cr["yf_prices"] = out
        return out
    import factors
    upd = factors.update_prices(st)
    for t in YF_SYMBOL:
        c = factors.latest_close(t)
        if c:
            out[t] = c
    log(st, "price_store", {"update": upd}, f"{len(out)} latest closes from the store")
    cr["yf_prices"] = out
    return out

def best_price(st, ticker: str) -> dict:
    """Order: the student's JPX entry for the latest date > yfinance daily close (A16) > irbank daily close."""
    man = sorted((r for r in manual_prices() if r["ticker"] == ticker), key=lambda r: r["quote_time_jst"])
    yv = yf_prices(st).get(ticker)
    if yv and not (man and man[-1]["quote_time_jst"][:10] >= yv["date"]):
        return {"price": yv["close"], "quote_time": f"{yv['date']}T15:30:00+09:00", "price_type": "close",
                "source_type": "market data (Yahoo Finance via yfinance, third-party; Addenda A16, A28)", "source_url": yv["url"],
                "location": f"daily close {yv['date']} ({yv['symbol']})", "third_party": True, "irbank": None}
    ib = None
    try:
        ib = irbank_close(st, ticker)
    except (FetchError, GuardrailError) as e:
        log(st, "price", {"ticker": ticker}, f"irbank close unavailable: {e}")
    if man and (ib is None or man[-1]["quote_time_jst"][:10] >= ib["date"]):
        r = man[-1]
        return {"price": float(r["price_jpy"]), "quote_time": r["quote_time_jst"],
                "price_type": r.get("price_type") or "close", "source_type": "manual (student, JPX)",
                "source_url": r.get("source_url") or "https://www.jpx.co.jp/",
                "location": f"screenshot {r.get('screenshot_ref', '')}", "third_party": False,
                "irbank": ib}
    if ib:
        return {"price": ib["close"], "quote_time": f"{ib['date']}T15:30:00+09:00", "price_type": "close",
                "source_type": "market data (irbank, third-party)", "source_url": ib["url"],
                "location": f"株価推移 table, row {ib['date']}", "third_party": True, "irbank": ib}
    return {}

# ---------------------------------------------------------------- company forecasts (決算短信)
FORECAST_TITLE = re.compile(r"決算短信|業績予想の修正")

def manual_sources(ticker: str) -> list[dict]:
    """inputs/manual_sources.csv: student-supplied 決算短信 URLs (ticker, doc_date, doc_title, doc_url).

    Used when a company's IR list is rendered by JavaScript or hosted on a robots-disallowed service.
    The agent still fetches and parses the PDF itself, only from allowlisted domains.
    """
    p = ROOT / "inputs" / "manual_sources.csv"
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if (r.get("ticker") or "").strip() != ticker or not (r.get("doc_url") or "").startswith("https://"):
                continue
            out.append({"url": r["doc_url"].strip(), "title": (r.get("doc_title") or "決算短信").strip(),
                        "date": (r.get("doc_date") or "").strip(), "page": "inputs/manual_sources.csv"})
    return out

def find_forecast_docs(st, ticker: str) -> list[dict]:
    """Return candidate forecast PDFs from the company's IR page (plus student-supplied URLs), newest first."""
    from bs4 import BeautifulSoup
    base = CONFIG["peer_ir"].get(ticker)
    extra = manual_sources(ticker)
    if not base:
        return sorted(extra, key=lambda d: d["date"], reverse=True)
    try:
        body, meta = fetch(st, base, purpose="IR page: find latest 決算短信")
    except (FetchError, GuardrailError) as e:
        log(st, "forecast", {"ticker": ticker}, f"IR page unavailable: {e}")
        return sorted(extra, key=lambda d: d["date"], reverse=True)
    soup = BeautifulSoup(body, "lxml")
    out = []
    for a in soup.find_all("a", href=True):
        href = urllib.parse.urljoin(base, a["href"])
        ctx = a.find_parent(["li", "tr", "dl", "article", "div"]) or a
        text = re.sub(r"\s+", " ", ctx.get_text(" ", strip=True))[:200]
        if not href.lower().split("?")[0].endswith(".pdf") or not FORECAST_TITLE.search(text + a.get_text()):
            continue
        if "説明" in text and "短信" not in text:
            continue
        from lib import injection_hits
        if injection_hits(text):
            continue  # link text carrying instructions is not followed
        m = re.search(r"(20\d\d)[./年](\d{1,2})[./月](\d{1,2})", text)
        date = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else ""
        out.append({"url": href, "title": text, "date": date, "page": base})
    out += [e for e in extra if e["url"] not in {o["url"] for o in out}]
    out.sort(key=lambda d: d["date"], reverse=True)
    return out

def parse_forecast_pdf(pdf_bytes: bytes) -> dict:
    """Extract the full-year consolidated forecast from a 決算短信 or a 業績予想の修正 notice.

    Returns {'revenue','operating_profit','ordinary_profit','net_income','eps','dps', 'unit_scale','page','row'}
    with None where not found. Never fills gaps.
    """
    import pdfplumber
    res = {k: None for k in ("revenue", "operating_profit", "ordinary_profit", "net_income", "eps", "dps")}
    res.update({"unit_scale": None, "page": None, "row": None, "fiscal_year": None})
    tok = re.compile(r"△?▲?[\d,]+(?:\.\d+)?|－|―|—")
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as d:
        for pno, page in enumerate(d.pages[:3], start=1):
            t = page.extract_text() or ""
            # revision notice: 今回修正予想（Ｂ） rev op ord ni eps
            m = re.search(r"今回修正予想[（(]\s*[ＢB]\s*[)）]\s*([^\n]+)", t)
            if m:
                vals = tok.findall(m.group(1))
                if len(vals) >= 5:
                    res.update(revenue=_num(vals[0]), operating_profit=_num(vals[1]), ordinary_profit=_num(vals[2]),
                               net_income=_num(vals[3]), eps=_num(vals[4]), page=pno, row="今回修正予想（Ｂ）")
            # 決算短信 page 1: section "連結業績予想" then a 通期 row
            i = t.find("業績予想")
            mj = re.compile(r"\n通\s*期").search(t, i if i >= 0 else 0)
            if res["eps"] is None and i >= 0 and mj:
                line = t[mj.end():].split("\n", 1)[0]
                vals = tok.findall(line)
                if len(vals) >= 9:
                    res.update(revenue=_num(vals[0]), operating_profit=_num(vals[2]), ordinary_profit=_num(vals[4]),
                               net_income=_num(vals[6]), eps=_num(vals[8]), page=pno, row="通期")
                elif len(vals) >= 5:
                    res.update(revenue=_num(vals[0]), operating_profit=_num(vals[1]), ordinary_profit=_num(vals[2]),
                               net_income=_num(vals[3]), eps=_num(vals[4]), page=pno, row="通期")
            if res["fiscal_year"] is None:
                m = re.search(r"(20\d\d)年\s*(\d{1,2})月期(?:通期)?の?(?:連結)?業績予想", t) or \
                    re.search(r"(20\d\d)年\s*(\d{1,2})月期通期連結業績予想", t)
                if m:
                    res["fiscal_year"] = f"FY ending {m.group(1)}-{int(m.group(2)):02d}"
            m = re.search(r"\n(20\d\d)年\s*\d{1,2}月期[（(]予想[)）]\s*([^\n]+)", t)
            if m and res["dps"] is None:
                vals = tok.findall(m.group(2))
                # columns: 1Q末 2Q末 3Q末 期末 合計 [配当性向]; quarter-only rows list fewer columns
                if len(vals) >= 5:
                    res["dps"] = _num(vals[4])
                elif vals:
                    res["dps"] = _num(vals[-1])
            if res["eps"] is not None:
                if "百万円" in t:
                    res["unit_scale"] = 1_000_000
                elif "千円" in t:
                    res["unit_scale"] = 1_000
                break
    return res

def parse_actuals_pdf(pdf_bytes: bytes, fy_label: str) -> dict:
    """Headline actuals from a full-year 決算短信 for the cross-source check, e.g. fy_label='2025年10月期'."""
    import pdfplumber
    tok = re.compile(r"△?▲?[\d,]+(?:\.\d+)?|－|―|—")
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as d:
        t = d.pages[0].extract_text() or ""
    out = {"revenue": None, "operating_profit": None, "ordinary_profit": None, "net_income": None, "unit_scale": None}
    i = t.find("経営成績")
    m = re.search(rf"\n{re.escape(fy_label)}\s+([^\n]+)", t[i:] if i >= 0 else t)
    if m:
        vals = tok.findall(m.group(1))
        if len(vals) >= 8:
            out.update(revenue=_num(vals[0]), operating_profit=_num(vals[2]), ordinary_profit=_num(vals[4]),
                       net_income=_num(vals[6]))
    out["unit_scale"] = 1_000_000 if "百万円" in t else (1_000 if "千円" in t else None)
    return out

def latest_forecast(st, ticker: str) -> dict:
    """Fetch and parse the newest forecast document. Returns parse result + source info."""
    docs = find_forecast_docs(st, ticker)
    best = {}
    for d in docs[:3]:
        try:
            body, meta = fetch(st, d["url"], binary=True, purpose="決算短信/業績予想 PDF")
        except (FetchError, GuardrailError) as e:
            log(st, "forecast", {"url": d["url"]}, f"fetch failed: {e}")
            continue
        host = urllib.parse.urlsplit(d["url"]).hostname
        write_bytes(f"raw/web/{host}/{meta['sha256']}.pdf", body)
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(body)) as _d:
                from lib import flag_injection
                flag_injection(st, d["url"], "\n".join((p.extract_text() or "") for p in _d.pages[:3]))
        except Exception:
            pass
        res = parse_forecast_pdf(body)
        fy = re.match(r"FY ending (\d{4})-(\d{2})", res.get("fiscal_year") or "")
        if fy and (int(fy.group(1)), int(fy.group(2))) < (int(iso()[:4]), int(iso()[5:7])):
            log(st, "forecast", {"url": d["url"]}, f"stale: forecast is for {res['fiscal_year']}, which has ended")
            continue
        if res["eps"] is not None or res["operating_profit"] is not None:
            if not best:
                res.update(source_url=d["url"] + (f"#page={res['page']}" if res["page"] else ""),
                           source_title=d["title"], doc_date=d["date"], sha256=meta["sha256"])
                best = res
                if best.get("dps") is not None:
                    return best
            elif best.get("dps") is None and res.get("dps") is not None and res.get("fiscal_year") == best.get("fiscal_year"):
                # the newest notice (e.g. 業績予想の修正) may omit dividends; take DPS from the latest 短信 for the same FY
                best["dps"] = res["dps"]
                best["dps_source_url"] = d["url"] + (f"#page={res['page']}" if res["page"] else "")
                return best
            continue
        log(st, "forecast", {"url": d["url"]}, "no forecast table recognised")
    return best
