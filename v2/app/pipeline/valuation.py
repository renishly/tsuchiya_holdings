"""Baseline valuation, v2.005 (Addenda A37-A38): fixes F1-F3, F5-F7, the own-history band (M2), P/B, Md and the
combined value with weights. Called from metrics.current() after the peer-average rows (M0) are written.

value = target multiple × (company forecast − Md)

  F1  forward peer EV/EBITDA (peers' forecast operating profit) next to the trailing pair (trailing × trailing)
  F2  P/E floor: forecast EPS is used only if it is at least 25% of the clean normalised EPS
  F3  clean normalised EPS = 5-year mean of ordinary profit × (1 − 30.4%) ÷ average shares
  F5  flag when forward EBITDA rests on depreciation alone (forecast operating profit ≤ 0)
  F6  EV fallback: forward EBITDA ≤ 0 → not meaningful (P/B and M2 still give a value)
  F7  forward depreciation: year-to-date depreciation from the latest quarterly 決算短信, scaled by last year's
      seasonal share; else last year's actual (flat). Capex roll-forward is not built yet (needs the PP&E schedule).
  M2  own-history band: Tsuchiya's P/B and EV/EBITDA at past fiscal year-ends (price store × 有報)
  P/B target P/B (peer median) × latest book value per share
  Md  forecast discount on operating profit (yen; positive lowers the forecast); EPS discount derived from it.
      Discount Baseline = Tsuchiya's past guidance misses per month of horizon × months left × n/(n+2).
      Land Price Ave and Supply Costs are logged as % changes since the guidance date with coefficient 0 until
      the panel fit (A37); the Debate Signal is 0 until the Md debate exists.
  Combined value = weighted mean of the valid method prices; equal weights, halved for a fallback basis,
      until the backtest sets them.
Every row states its formula and inputs; missing inputs are reported, never imputed.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import os
import re
import statistics

import market
from lib import CONFIG, ROOT, BudgetStop, FetchError, GuardrailError, fetch, iso, log, now, run_ctx

TAX_RATE = 0.304            # statutory effective tax rate stated in Tsuchiya's 有報 (FY2025 tax note)
EPS_FLOOR = 0.25            # F2: forecast EPS must be ≥ 25% of clean normalised EPS
SHRINK_K = 2                # Md: track record weight n / (n + K) until more years or the panel exist
BOJ_API = "https://www.stat-search.boj.or.jp/api/v1/getDataCode"
BOJ_LUMBER = "PRCG20_2200320001"   # CGPI (2020 base), Producer Price Index, Group: Lumber and wood products
REINFOLIB = "https://www.reinfolib.mlit.go.jp/ex-api/external/XIT001"
LAND_AREA = "01"            # Hokkaido (prefecture code); Tsuchiya's core market (D15: other regions to confirm)
COEF = {"land": 0.0, "supply": 0.0}   # Md coefficients (yen of operating profit per 1% change); fitted later (A37)
DOC = "https://www.tsuchiya.co.jp/ir/news/"
_ZEN = str.maketrans("０１２３４５６７８９，．△", "0123456789,.-")


def _num(s):
    try:
        return float(str(s).translate(_ZEN).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _v(r):
    try:
        return float(r["value"]) * float(r["scale"] or 1)
    except (TypeError, ValueError, KeyError):
        return None


def _pdf_text(body: bytes, pages: int = 20) -> str:
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(body)) as d:
            return "\n".join((p.extract_text() or "") for p in d.pages[:pages])
    except Exception:
        return ""


def _fy_end(label: str) -> dt.date | None:
    m = re.search(r"(20\d\d)\D{0,3}?(\d{1,2})", label or "")
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    return dt.date(y + mo // 12, mo % 12 + 1, 1) - dt.timedelta(days=1)


def _months_between(a: dt.date, b: dt.date) -> float:
    return max(0.0, (b - a).days / 30.44)


# ---------------------------------------------------------------- documents (cached in state: parsed once)
def _doc_cache(st) -> dict:
    return st["documents"].setdefault("valuation_docs", {})


def _parsed_doc(st, d: dict, kind: str) -> dict:
    """Fetch and parse one Tsuchiya document once; later runs read the cached result from state."""
    cache = _doc_cache(st)
    if d["url"] in cache:
        return cache[d["url"]]
    if run_ctx(st).get("mode") == "test":
        return {}
    try:
        body, meta = fetch(st, d["url"], binary=True, purpose=f"決算短信 / notice PDF ({kind})")
    except (FetchError, GuardrailError) as e:
        log(st, "valuation", {"url": d["url"]}, f"fetch failed: {e}")
        return {}
    text = _pdf_text(body)
    res = {"kind": kind, "title": d["title"][:120], "date": d["date"], "url": d["url"]}
    if kind == "revision":
        res.update(_parse_revision(text))
    elif kind == "quarterly":
        res.update(_parse_quarterly(text, d["title"]))
    elif kind == "annual":
        fc = market.parse_forecast_pdf(body)
        res.update({"forecast_fy": fc.get("fiscal_year"), "forecast_op": fc.get("operating_profit"),
                    "unit_scale": fc.get("unit_scale") or 1_000_000})
    cache[d["url"]] = res
    return res


def _parse_revision(text: str) -> dict:
    """業績予想の修正: the fiscal year, the previous forecast (A) and the revised one (B), in 百万円."""
    t = text.translate(_ZEN)
    fy = re.search(r"(20\d\d)年(\d{1,2})月期（", t)
    out = {"fy": f"FY ending {fy.group(1)}-{int(fy.group(2)):02d}" if fy else None}
    for key, label in (("prev", r"前回発表予想"), ("new", r"今回修正予想")):
        m = re.search(label + r"[^\d\n-]*\n?\s*(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)\s+(-?[\d,.]+)", t)
        if m:
            out[key] = {"revenue": _num(m.group(1)), "operating_profit": _num(m.group(2)), "eps": _num(m.group(5))}
    return out


def _parse_quarterly(text: str, title: str) -> dict:
    """Quarterly 決算短信: year-to-date depreciation (current, prior year) in yen, months covered, latest BPS."""
    t = text.translate(_ZEN)
    tt = title.translate(_ZEN)
    months = 3 if re.search(r"第1四半期", tt) else 6 if re.search(r"第2四半期|中間", tt) else 9 if re.search(r"第3四半期", tt) else None
    fy = re.search(r"(20\d\d)年(\d{1,2})月期", tt)
    out = {"months": months, "fy": f"FY ending {fy.group(1)}-{int(fy.group(2)):02d}" if fy else None}
    m = re.search(r"減価償却費\s*([\d,]+)\s*千円\s*([\d,]+)\s*千円", t)       # Q1/Q3 note: prior, current
    if not m:
        m = re.search(r"減価償却費\s+([\d,]+)\s+([\d,]+)", t)                  # Q2 cash-flow statement (千円)
    if m:
        out["da_prior_ytd"], out["da_ytd"] = _num(m.group(1)) * 1000, _num(m.group(2)) * 1000
    b = re.search(r"20\d\d年\d{1,2}月期第?\s*[123]?\s*(?:四半期|中間期)\s+([\d,]+)\s+([\d,]+)\s+([\d.]+)\s+([\d,.]+)", t)
    if b:
        out["bps"] = _num(b.group(4))
    return out


def tsuchiya_docs(st) -> dict:
    """Classified Tsuchiya documents from the IR page (newest first), each parsed once."""
    try:
        docs = market.find_forecast_docs(st, CONFIG["company_code"])
    except (FetchError, GuardrailError, BudgetStop) as e:
        log(st, "valuation", {}, f"IR page unavailable: {e}")
        docs = []
    out = {"revision": [], "quarterly": [], "annual": []}
    for d in docs:
        t = d["title"].translate(_ZEN)
        kind = ("revision" if "業績予想の修正" in t else
                "quarterly" if re.search(r"四半期|中間", t) and "短信" in t else
                "annual" if "決算短信" in t else None)
        if kind:
            p = _parsed_doc(st, d, kind)
            if p:
                out[kind].append(p)
    return out


# ---------------------------------------------------------------- public data for Md (cached in state)
def boj_lumber(st) -> dict:
    """Monthly CGPI lumber and wood products index, fetched at most once a day."""
    c = st["documents"].setdefault("md_series", {}).setdefault("lumber", {})
    today = iso()[:10]
    if c.get("fetched") == today or run_ctx(st).get("mode") == "test":
        return c
    try:
        body, meta = fetch(st, BOJ_API, params={"format": "json", "lang": "en", "db": "PR01", "code": BOJ_LUMBER,
                                                "startDate": "202001"}, purpose="Md: BOJ lumber price index")
        rs = json.loads(body)["RESULTSET"][0]["VALUES"]
        c.update(series=dict(zip((str(d) for d in rs["SURVEY_DATES"]), rs["VALUES"])), fetched=today,
                 source=f"{BOJ_API}?db=PR01&code={BOJ_LUMBER}")
    except (FetchError, GuardrailError, BudgetStop, KeyError, IndexError, ValueError) as e:
        log(st, "md", {"series": "lumber"}, f"unavailable: {str(e)[:120]}")
    return c


def land_prices(st) -> dict:
    """Median land unit price (円/㎡, 宅地(土地)) per quarter in Hokkaido from MLIT XIT001; one new quarter per day."""
    c = st["documents"].setdefault("md_series", {}).setdefault("land", {"series": {}})
    key = os.environ.get("REINFOLIB_API_KEY", "")
    today = iso()[:10]
    if not key or c.get("fetched") == today or run_ctx(st).get("mode") == "test":
        return c
    t = now()
    wanted = []
    for back in range(1, 9):  # the last eight quarters, oldest first; published with a lag of a few months
        q = (t.month - 1) // 3 + 1 - back
        y = t.year + (q - 1) // 4
        q = (q - 1) % 4 + 1
        wanted.append((y, q))
    for y, q in sorted(wanted):
        k = f"{y}Q{q}"
        if k in c["series"] or k in c.get("empty", []):
            continue
        try:
            body, meta = fetch(st, REINFOLIB, params={"year": y, "quarter": q, "area": LAND_AREA},
                               headers={"Ocp-Apim-Subscription-Key": key}, purpose="Md: land prices (Hokkaido)")
            data = json.loads(body).get("data", [])
        except (FetchError, GuardrailError, ValueError) as e:
            log(st, "md", {"quarter": k}, f"land prices unavailable: {str(e)[:120]}")
            break
        except BudgetStop:
            break
        unit = []
        for d in data:
            if "宅地(土地)" != d.get("Type"):
                continue
            u = _num(d.get("UnitPrice"))
            if not u:
                tp, ar = _num(d.get("TradePrice")), _num(d.get("Area"))
                u = tp / ar if tp and ar else None
            if u:
                unit.append(u)
        if unit:
            c["series"][k] = {"median_jpy_m2": round(statistics.median(unit)), "n": len(unit)}
        else:
            c.setdefault("empty", []).append(k)  # not published yet; retried after a month (cursor reset below)
        break  # one quarter per day keeps requests low
    if c.get("empty") and c.get("empty_reset", "")[:7] != today[:7]:
        c["empty"], c["empty_reset"] = [], today
    c["fetched"] = today
    return c


def _change_since(series: dict, guidance: dt.date, monthly: bool) -> tuple[float | None, str]:
    """% change from the last period before the guidance date to the latest period after it."""
    if not series:
        return None, "no data yet"
    keys = sorted(series)
    def as_date(k):
        if monthly:
            return dt.date(int(k[:4]), int(k[4:6]), 1)
        y, q = int(k[:4]), int(k[-1])
        return dt.date(y, 3 * q - 2, 1)
    before = [k for k in keys if as_date(k) < dt.date(guidance.year, guidance.month, 1) - dt.timedelta(days=(0 if monthly else 80))]
    after = [k for k in keys if k not in before]
    if not before or not after:
        return None, f"no period after the guidance date ({guidance.isoformat()}) published yet; latest {keys[-1]}"
    val = lambda k: series[k] if monthly else series[k]["median_jpy_m2"]
    a, b = val(before[-1]), val(after[-1])
    return (b / a - 1) * 100, f"{before[-1]} {a} → {after[-1]} {b}"


# ---------------------------------------------------------------- the valuation
def run(st, w, *, L, last_pe, price, price_id, fc, fids, peers, debt, cash, shares, shares_ids, dy, today,
        tpe, tpe_id, eps_actual, eps_actual_id) -> dict:
    """Writes the v2.005 rows and returns a summary for the run log."""
    res = {}
    put = lambda *a, **k: w.put(*a, url=k.pop("url", DOC), title=k.pop("title", "derived"), location=k.pop("location", ""),
                                key_extra=k.pop("key_extra", today), **k)
    sc = (fc or {}).get("unit_scale") or 1_000_000
    fop = fc["operating_profit"] * sc if fc and fc.get("operating_profit") is not None else None
    feps = fc.get("eps") if fc else None
    fy_end = _fy_end((fc or {}).get("fiscal_year", "")) or (dt.date.fromisoformat(last_pe) + dt.timedelta(days=365) if last_pe else None)
    t_now = dt.date.fromisoformat(today)
    months_left = _months_between(t_now, fy_end) if fy_end else None
    docs = tsuchiya_docs(st)
    net_cash = (_v(cash) - debt) if cash and debt is not None else None

    # ---- F7 forward depreciation
    da_last = L.get((last_pe, "Depreciation")) if last_pe else None
    da_last_v = _v(da_last) if da_last else None
    q = next((d for d in docs["quarterly"] if d.get("fy") and fc and d["fy"] == fc.get("fiscal_year") and d.get("da_ytd")), None)
    if q and da_last_v and q.get("da_prior_ytd"):
        share = q["da_prior_ytd"] / da_last_v
        fda, basis = q["da_ytd"] / share, (f"year-to-date ({q['months']} months) ¥{q['da_ytd'] / 1e6:,.1f}m ÷ last year's share in the "
                                            f"same months {share:.1%} (prior year-to-date ¥{q['da_prior_ytd'] / 1e6:,.1f}m of ¥{da_last_v / 1e6:,.1f}m)")
        fda_src = q["url"]
    elif da_last_v:
        fda, basis, fda_src = da_last_v, "flat: last year's actual (no quarterly figure for this fiscal year yet; capex roll-forward not built)", da_last["source_url"]
    else:
        fda, basis, fda_src = None, "missing", DOC
    fda_id = put("Depreciation (forward, F7)", "derived-current", "", fda, "JPY",
                 "= year-to-date depreciation ÷ last year's seasonal share; else last year's actual", [da_last["record_id"] if da_last else ""],
                 url=fda_src, note=basis)
    febitda = fop + fda if fop is not None and fda is not None else None
    put("EBITDA (forward, F7)", "derived-current", "", febitda, "JPY", "= company-forecast 営業利益 + forward depreciation (F7)",
        [fids.get("operating_profit"), fda_id], note="" if febitda is not None else "Forecast operating profit or depreciation missing.")
    dep_only = fop is not None and fop <= 0
    if dep_only:
        put("Flag: EBITDA rests on depreciation (F5)", "derived-current", "", "yes", "flag", "forecast 営業利益 ≤ 0", [fids.get("operating_profit")],
            note=f"Forecast operating profit ¥{fop / 1e6:,.0f}m: forward EBITDA is depreciation only; the EV/EBITDA method's weight is halved.")

    # ---- F3 clean normalised EPS
    eps_parts, ids = [], []
    for pe in CONFIG["fiscal_years"].values():
        o, s = L.get((pe, "Ordinary profit")), L.get((pe, "Average shares outstanding"))
        if o and s and _v(s):
            eps_parts.append(_v(o) * (1 - TAX_RATE) / _v(s))
            ids += [o["record_id"], s["record_id"]]
    clean = statistics.mean(eps_parts) if eps_parts else None
    clean_id = put("EPS (normalised, ordinary profit, F3)", "derived", "", clean, "JPY/share",
                   f"= mean over {len(eps_parts)} fiscal years of 経常利益 × (1 − {TAX_RATE:.1%}) ÷ average shares", ids,
                   url="https://disclosure2.edinet-fsa.go.jp/", title="有価証券報告書 (5 years)",
                   note="Ordinary profit excludes extraordinary gains and losses (e.g. the FY2024 land sale) and unusual tax charges.")

    # ---- Md (forecast discount, operating profit)
    track = _track_record(st, docs, L)
    n = len(track)
    rate = statistics.mean(m["miss_per_month"] for m in track) if track else None
    base = (-rate * months_left * n / (n + SHRINK_K)) if rate is not None and months_left is not None else None
    cap = max((abs(m["miss"]) for m in track), default=None)
    if base is not None and cap is not None:
        base = max(-cap, min(cap, base))
    put("Md: Discount Baseline", "derived-current", "", base, "JPY",
        "= −(mean past guidance miss per month of horizon) × months left × n/(n+2), capped at the largest past miss",
        [], note=(f"Track record: " + "; ".join(f"{m['fy']}: initial ¥{m['initial'] / 1e6:,.0f}m ({m['initial_src']}), actual ¥{m['actual'] / 1e6:,.0f}m"
                                                for m in track) + f"; months left {months_left:.1f}; n = {n}"
                  if track else "No completed fiscal year with a known initial forecast yet (needs earlier 決算短信 / revision notices)."))
    guidance_date = _guidance_date(docs, fc)
    lum = boj_lumber(st)
    sup, sup_note = _change_since(lum.get("series", {}), guidance_date, monthly=True) if guidance_date else (None, "guidance date unknown")
    put("Md: Supply Costs (% since guidance)", "public data", "", sup, "%", "BOJ CGPI lumber and wood products: latest ÷ last month before the guidance date − 1",
        [], url=lum.get("source", BOJ_API), title="日本銀行 企業物価指数", source_type="government statistics",
        note=f"{sup_note}. Coefficient {COEF['supply']} until fitted on the panel (A37): logged, not yet in Md.")
    land = land_prices(st)
    lnd, lnd_note = _change_since(land.get("series", {}), guidance_date, monthly=False) if guidance_date else (None, "guidance date unknown")
    put("Md: Land Price Ave (% since guidance)", "public data", "", lnd, "%", "MLIT XIT001 Hokkaido 宅地(土地) median ¥/㎡: latest quarter ÷ last quarter before the guidance date − 1",
        [], url=REINFOLIB, title="国土交通省 不動産情報ライブラリ", source_type="government API",
        note=(f"{lnd_note}. Coefficient {COEF['land']} until fitted on the panel (A37): logged, not yet in Md."
              if land.get("series") or os.environ.get("REINFOLIB_API_KEY") else "REINFOLIB_API_KEY not set."))
    md_op = (base or 0.0) + COEF["land"] * (lnd or 0.0) + COEF["supply"] * (sup or 0.0)
    md_op = md_op if base is not None or lnd is not None or sup is not None else None
    md_id = put("Md (operating profit)", "derived-current", "", md_op, "JPY",
                "= Discount Baseline + coef_land × Land Price Ave + coef_supply × Supply Costs + Debate Signal (0: not built)",
                [], note="Positive lowers the company's forecast. Subtracted from forecast 営業利益 before valuation.")
    md_eps = md_op * (1 - TAX_RATE) / shares if md_op is not None and shares else None
    md_eps_id = put("Md (EPS)", "derived-current", "", md_eps, "JPY/share", f"= Md (operating profit) × (1 − {TAX_RATE:.1%}) ÷ shares", [md_id])
    res["md_op"] = md_op

    # ---- methods
    methods = {}   # name -> (price, record_id, weight factor, note)

    # P/E with the F2 floor, on the discounted forecast
    eps_adj = feps - (md_eps or 0.0) if feps is not None else None
    if eps_adj is not None and clean and clean > 0 and eps_adj >= EPS_FLOOR * clean:
        eps_used, eps_basis, factor = eps_adj, "forecast EPS − Md (passes the F2 floor)", 1.0
    elif eps_actual and clean and clean > 0 and eps_actual >= EPS_FLOOR * clean:
        eps_used, eps_basis, factor = eps_actual, "trailing EPS (forecast below the F2 floor)", 0.5
    elif clean and clean > 0:
        eps_used, eps_basis, factor = clean, "clean normalised EPS (F3; forecast and trailing below the F2 floor)", 0.5
    else:
        eps_used, eps_basis, factor = None, "no positive EPS basis", 0.0
    put("EPS basis used (P/E)", "derived-current", "", eps_basis, "text", "F2 chain: forecast − Md, else trailing, else clean normalised",
        [fids.get("eps"), md_eps_id, clean_id],
        note=f"forecast {feps}, Md(EPS) {md_eps if md_eps is None else round(md_eps, 2)}, trailing {eps_actual}, clean normalised "
             f"{None if clean is None else round(clean, 2)}, floor {EPS_FLOOR:.0%} of normalised")
    if tpe and eps_used:
        p = tpe * eps_used
        pid = put("Target Price (P/E, v2.005)", "derived-current", "", p, "JPY/share", "= Target P/E × EPS basis used", [tpe_id, clean_id, md_eps_id],
                  note=eps_basis)
        methods["P/E"] = (p, pid, factor, eps_basis)

    # EV/EBITDA, forward peer multiple (F1) on the discounted forecast
    fwd = {t: r["ev"] / (r["fop"] + r["da"]) for t, r in peers.items()
           if r.get("ev") and r.get("fop") is not None and r.get("da") is not None and r["fop"] + r["da"] > 0 and r["ev"] > 0}
    trail = {t: r["ev_ebitda"] for t, r in peers.items() if r.get("ev_ebitda")}
    mismatch = len(fwd) < 2   # F1 needs at least two peers with a forecast; otherwise the trailing mean, flagged
    tmf = statistics.mean(fwd.values()) if not mismatch else (statistics.mean(trail.values()) if trail else None)
    tmf_id = put("Target EV/EBITDA (forward, F1)", "derived-current", "", tmf, "times",
                 "= mean of peers' EV ÷ (forecast 営業利益 + latest depreciation); trailing mean if fewer than 2 peers have a forecast", [],
                 note=(f"Forward peers: " + ", ".join(f"{t} {v:.2f}" for t, v in fwd.items()) + f"; median {statistics.median(fwd.values()):.2f}")
                 if not mismatch else (f"Only {len(fwd)} peer(s) with a forecast operating profit: trailing mean of "
                                       + ", ".join(f"{t} {v:.2f}" for t, v in trail.items()) + " used (years mismatched; weight halved)"))
    ebitda_adj = (fop - (md_op or 0.0) + fda) if fop is not None and fda is not None else None
    if tmf and ebitda_adj and ebitda_adj > 0 and net_cash is not None and shares:
        p = (tmf * ebitda_adj + net_cash) / shares
        pid = put("Target Price (EV/EBITDA, forward, F1)", "derived-current", "", p, "JPY/share",
                  "= [Target EV/EBITDA (forward) × (forecast 営業利益 − Md + forward depreciation) + net cash] ÷ shares",
                  [tmf_id, md_id, fda_id] + shares_ids + [cash["record_id"]],
                  note="Rests on depreciation and cash (F5)." if dep_only else "")
        f = 0.5 if (dep_only or mismatch) else 1.0
        methods["EV/EBITDA"] = (p, pid, f, ("trailing peer multiple, years mismatched" if mismatch else "forward peer multiple")
                                + (" (depreciation only)" if dep_only else "") + (" (weight halved)" if f < 1 else ""))
    else:
        put("Target Price (EV/EBITDA, forward, F1)", "derived-current", "", "not meaningful" if ebitda_adj is not None and ebitda_adj <= 0 else None,
            "JPY/share", "= [Target EV/EBITDA (forward) × adjusted forward EBITDA + net cash] ÷ shares", [tmf_id],
            note="Adjusted forward EBITDA ≤ 0 (F6): not meaningful; P/B and own history still value the company." if ebitda_adj is not None and ebitda_adj <= 0
            else "Inputs missing.")
    # trailing pair (reference): peers' trailing multiple × Tsuchiya's trailing EBITDA
    evs = [r["ev_ebitda"] for r in peers.values() if r.get("ev_ebitda")]
    eb_t = L.get((last_pe, "Operating profit")) if last_pe else None
    if evs and eb_t and da_last_v and net_cash is not None and shares and _v(eb_t) + da_last_v > 0:
        put("Target Price (EV/EBITDA, trailing pair, F1)", "derived-current", "",
            (statistics.mean(evs) * (_v(eb_t) + da_last_v) + net_cash) / shares, "JPY/share",
            "= [peers' trailing EV/EBITDA (mean) × Tsuchiya's last actual EBITDA + net cash] ÷ shares (years matched; reference)",
            [eb_t["record_id"], da_last["record_id"]])

    # P/B: peer median × latest BPS
    bps_row = L.get((last_pe, "BPS")) if last_pe else None
    bps, bps_src = (_v(bps_row), "有報 " + last_pe) if bps_row else (None, "")
    qb = next((d for d in docs["quarterly"] if d.get("bps")), None)
    if qb and (not last_pe or qb["date"] > last_pe):
        bps, bps_src = qb["bps"], f"quarterly 決算短信 {qb['date']}"
    pbs = {}
    for t, r in peers.items():
        b = _peer_bps(st, r.get("docID"))
        if b and r.get("price"):
            pbs[t] = r["price"] / b
    tpb = statistics.median(pbs.values()) if pbs else None
    tpb_id = put("Target P/B (peer median)", "derived-current", "", tpb, "times", "= median of peers' price ÷ BPS (latest 有報)", [],
                 note=", ".join(f"{t} {v:.2f}" for t, v in pbs.items()) or "Peer BPS not available.")
    if tpb and bps:
        p = tpb * bps
        pid = put("Target Price (P/B, peers)", "derived-current", "", p, "JPY/share", "= Target P/B × latest BPS", [tpb_id, bps_row["record_id"] if bps_row else ""],
                  note=f"BPS ¥{bps:,.2f} ({bps_src})")
        # reference only (weight 0): a peer P/B ignores ROE differences (P/B = P/E × ROE); M1's P/B-on-ROE replaces it (A37)
        methods["P/B (peers)"] = (p, pid, 0.0, f"peer median P/B {tpb:.2f}; reference only until M1 adjusts for ROE")

    # M2: own-history band (P/B and EV/EBITDA at past fiscal year-ends)
    m2 = _own_history(L, bps, net_cash, shares, fop, md_op, fda)
    if m2.get("pb"):
        pb = m2["pb"]
        pid = put("Target Price (own history, P/B, M2)", "derived-current", "", pb["price"], "JPY/share",
                  "= median of Tsuchiya's P/B at past fiscal year-ends × latest BPS", [],
                  note=f"Year-end P/B {', '.join(f'{k[:4]} {v:.2f}' for k, v in pb['by_year'].items())}; median {pb['median']:.2f}; "
                       f"range ¥{pb['low']:,.1f}–¥{pb['high']:,.1f}" + ("; regime changed: latest ROE outside its past range" if m2["regime"] else ""))
        methods["P/B (own history)"] = (pb["price"], pid, 0.5 if m2["regime"] else 1.0, "own-history median" + (" (regime changed: weight halved)" if m2["regime"] else ""))
    if m2.get("ev"):
        ev = m2["ev"]
        pid = put("Target Price (own history, EV/EBITDA, M2)", "derived-current", "", ev["price"], "JPY/share",
                  "= [median own EV/EBITDA at past year-ends × adjusted forward EBITDA + net cash] ÷ shares", [],
                  note=f"Year-end EV/EBITDA {', '.join(f'{k[:4]} {v:.2f}' for k, v in ev['by_year'].items())}; median {ev['median']:.2f}")
        f = 0.5 if (m2["regime"] or dep_only) else 1.0
        methods["EV/EBITDA (own history)"] = (ev["price"], pid, f, "own-history median" + (" (weight halved)" if f < 1 else ""))

    # M1: statistical comparables (A39). Approved parameters carry weight; pending ones give a reference row only.
    import modelfit
    live = modelfit.live_params(st)
    params = live or modelfit.load_params().get("pending")
    feats = modelfit.latest_features("1840") if params else None
    if params and feats and bps:
        pr = modelfit.predict(params["m1"], feats)
        pid = put("Target Price (M1, P/B regression)", "derived-current", "", pr["pb"] * bps, "JPY/share",
                  "= fitted P/B (log P/B on ROE_norm, growth, log revenue, year effect; universe of listed housebuilders) × latest BPS",
                  [], note=f"Fitted P/B {pr['pb']:.3f} (80% interval {pr['low']:.3f}–{pr['high']:.3f}; year effect {pr['year_effect']}); "
                           f"parameters {params['version']} ({'approved' if live else 'pending approval: reference only'}); "
                           f"ROE_norm {feats['roe_norm']:.4f}, growth {feats['growth']:.4f}")
        methods["M1 (P/B regression)"] = (pr["pb"] * bps, pid, 1.0 if live else 0.0,
                                          "statistical comparables" + ("" if live else " (pending approval: weight 0)"))
        put("Valuation range M1 low", "derived-current", "", pr["low"] * bps, "JPY/share", "= fitted P/B low (80%) × BPS", [pid])
        put("Valuation range M1 high", "derived-current", "", pr["high"] * bps, "JPY/share", "= fitted P/B high (80%) × BPS", [pid])
    if params and params.get("m1_ev") and feats and feats.get("margin") is not None and ebitda_adj and ebitda_adj > 0 \
            and net_cash is not None and shares:
        pe_ = modelfit.predict(params["m1_ev"], feats)
        if pe_:
            p = (pe_["multiple"] * ebitda_adj + net_cash) / shares
            pid = put("Target Price (M1, EV/EBITDA regression)", "derived-current", "", p, "JPY/share",
                      "= [fitted EV/EBITDA (margin, growth, log revenue, year effect) × (forecast 営業利益 − Md + forward depreciation) "
                      "+ net cash] ÷ shares", [md_id, fda_id],
                      note=f"Fitted EV/EBITDA {pe_['multiple']:.2f} (80% interval {pe_['low']:.2f}–{pe_['high']:.2f}); parameters "
                           f"{params['version']} ({'approved' if live else 'pending approval: reference only'})"
                           + ("; rests on depreciation (F5)" if dep_only else ""))
            f = (0.5 if dep_only else 1.0) if live else 0.0
            methods["M1 (EV/EBITDA regression)"] = (p, pid, f, "statistical comparables (EV/EBITDA)" +
                                                    ("" if live else " (pending approval: weight 0)") + (" (depreciation only)" if dep_only else ""))

    # ---- combine: approved backtest weights when available (× the fallback factor), else equal
    base_w = (live or {}).get("weights", {}).get("weights", {}) if live else {}
    raw = {k: (base_w.get(k, 1 / max(1, len(methods))) if live else 1.0) * f for k, (_, _, f, _) in methods.items() if f > 0}
    total = sum(raw.values())
    weights = {k: round(v / total, 3) for k, v in raw.items() if v > 0} if total else {}
    comb = sum(weights[k] * methods[k][0] for k in weights) if weights else None
    prices = [methods[k][0] for k in weights]
    put("Method weights", "derived-current", "", json.dumps(weights, ensure_ascii=False) if weights else None, "json",
        (f"approved backtest weights ({live['version']}) × fallback factor" if live else
         "equal across valid methods, halved for a fallback basis (until approved backtest weights exist)"), [],
        note="; ".join(f"{k}: ¥{methods[k][0]:,.1f} ({methods[k][3]})" for k in weights))
    put("Valuation range low", "derived-current", "", min(prices) if prices else None, "JPY/share", "= lowest valid method price", [])
    put("Valuation range high", "derived-current", "", max(prices) if prices else None, "JPY/share", "= highest valid method price", [])
    avg_id = put("Average Valuated Price", "derived-current", "", comb, "JPY/share",
                 "= weighted mean of the valid method prices (v2.005: P/E, EV/EBITDA forward, P/B peers, own-history P/B and EV/EBITDA)",
                 [methods[k][1] for k in weights],
                 note=("Methods: " + "; ".join(f"{k} = {methods[k][0]:.2f} × {weights[k]:.0%}" for k in weights)) if weights
                 else "No valid method price this run; not computed (not imputed).")
    if comb and price:
        put("Expected Return (combined)", "derived-current", "", comb / price - 1 + (dy or 0), "ratio",
            "= Average Valuated Price / price − 1 + dividend yield", [avg_id, price_id])
    res.update(weights=weights, combined=comb, md_eps=md_eps)
    return res


def _guidance_date(docs: dict, fc: dict) -> dt.date | None:
    """Date of the latest guidance for the current forecast year (revision notice or annual 決算短信)."""
    fy = (fc or {}).get("fiscal_year")
    cands = [d["date"] for d in docs["revision"] if d.get("fy") == fy and d.get("date")]
    cands += [d["date"] for d in docs["annual"] if d.get("forecast_fy") == fy and d.get("date")]
    if not cands and fc and fc.get("doc_date"):
        cands = [fc["doc_date"]]
    return dt.date.fromisoformat(max(cands)) if cands else None


def _track_record(st, docs: dict, L: dict) -> list[dict]:
    """Completed fiscal years with a known initial operating-profit forecast and an actual from the 有報."""
    first: dict[str, tuple] = {}   # fy -> (date, initial OP in yen, source)
    for d in docs["annual"]:
        if d.get("forecast_fy") and d.get("forecast_op") is not None:
            v = (d["date"], d["forecast_op"] * d["unit_scale"], "決算短信 " + d["date"])
            if d["forecast_fy"] not in first or v[0] < first[d["forecast_fy"]][0]:
                first[d["forecast_fy"]] = v
    earliest: dict[str, dict] = {}
    for d in docs["revision"]:
        if d.get("fy") and d.get("prev", {}).get("operating_profit") is not None and d.get("date"):
            if d["fy"] not in earliest or d["date"] < earliest[d["fy"]]["date"]:
                earliest[d["fy"]] = d
    for fy, d in earliest.items():
        if fy in first:
            continue
        # The earliest revision's "previous forecast" stands in for the initial one. Its own date is unknown, so it is
        # dated as the usual full-year 決算短信 date: about 1.5 months into the fiscal year (mid-December for Tsuchiya).
        end = _fy_end(fy)
        start = (end - dt.timedelta(days=365) + dt.timedelta(days=45)).isoformat() if end else d["date"]
        first[fy] = (start, d["prev"]["operating_profit"] * 1_000_000, f"previous forecast in the revision of {d['date']}")
    out = []
    for fy, (date, init, src) in first.items():
        end = _fy_end(fy)
        act = L.get((end.isoformat(), "Operating profit")) if end else None
        if not act or not date:
            continue
        horizon = _months_between(dt.date.fromisoformat(date), end) or 1.0
        miss = _v(act) - init
        out.append({"fy": fy, "initial": init, "initial_src": src, "actual": _v(act), "miss": miss, "miss_per_month": miss / horizon})
    return sorted(out, key=lambda m: m["fy"])


def _peer_bps(st, doc_id: str | None) -> float | None:
    """A peer's BPS from its cached 有報 XBRL (no network)."""
    if not doc_id:
        return None
    c = st["documents"].setdefault("peer_bps", {})
    if doc_id in c:
        return c[doc_id]
    z = ROOT / "raw" / "edinet" / doc_id / f"{doc_id}_type5.csv.zip"
    if not z.exists():
        return None
    import edinet
    val = None
    for x in edinet.read_xbrl_csv(z):
        if x["element"] == "jpcrp_cor:NetAssetsPerShareSummaryOfBusinessResults" and x["context"] == "CurrentYearInstant":
            val = _num(x["value"])
            if val:
                break
    c[doc_id] = val
    return val


def _own_history(L: dict, bps_now, net_cash, shares, fop, md_op, fda) -> dict:
    import pricestore
    closes = pricestore.load_store().get("1840.T", [])
    def close_on(day: str):
        c = [r for r in closes if r[0] <= day]
        return c[-1][1] if c else None
    pb, ev, roe = {}, {}, {}
    for pe in CONFIG["fiscal_years"].values():
        px = close_on(pe)
        b = L.get((pe, "BPS"))
        if px and b and _v(b):
            pb[pe] = px / _v(b)
        sh, tr = L.get((pe, "Shares issued")), L.get((pe, "Treasury shares"))
        op, da, cash = L.get((pe, "Operating profit")), L.get((pe, "Depreciation")), L.get((pe, "Cash and equivalents, end of period"))
        debt = sum(_v(L[(pe, it)]) for it in ("Short-term borrowings", "Current portion of long-term borrowings", "Long-term borrowings",
                                              "Bonds payable", "Current portion of bonds", "Lease obligations (current)",
                                              "Lease obligations (non-current)") if (pe, it) in L)
        if px and sh and tr and op and da and cash and _v(op) + _v(da) > 0:
            evv = px * (_v(sh) - _v(tr)) + debt - _v(cash)
            if evv > 0:
                ev[pe] = evv / (_v(op) + _v(da))
        r = L.get((pe, "ROE"))
        if r and _v(r) is not None:
            roe[pe] = _v(r)
    out = {"regime": False}
    if len(roe) >= 3:
        hist = list(roe.values())[:-1]
        net = L.get((max(CONFIG["fiscal_years"].values()), "Profit attributable to owners of parent"))
        latest_neg = net is not None and _v(net) is not None and _v(net) < 0
        out["regime"] = latest_neg or not (min(hist) <= list(roe.values())[-1] <= max(hist))
    if len(pb) >= 3 and bps_now:
        med = statistics.median(pb.values())
        out["pb"] = {"by_year": pb, "median": med, "price": med * bps_now, "low": min(pb.values()) * bps_now, "high": max(pb.values()) * bps_now}
    adj = (fop - (md_op or 0.0) + fda) if fop is not None and fda is not None else None
    if len(ev) >= 3 and adj and adj > 0 and net_cash is not None and shares:
        med = statistics.median(ev.values())
        out["ev"] = {"by_year": ev, "median": med, "price": (med * adj + net_cash) / shares}
    return out
