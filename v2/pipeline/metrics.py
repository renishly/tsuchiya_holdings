"""Baseline Metrics (phase P6 and the every-run refresh).

Valuation methods per Addendum A3 (syllabus 2.4): P/E and EV/EBITDA. Prices per Addendum A8.
Every derived row states its formula and the record_ids of its inputs. Missing inputs -> unresolved.
"""
from __future__ import annotations

import statistics

import edinet
import market
from lib import (CONFIG, CsvStore, FetchError, GuardrailError, BudgetStop, base_row, iso, log, record_id,
                 run_ctx, unresolved)

TS = CONFIG["company_code"]
NAME = CONFIG["company_name"]

def _v(r):
    try:
        return float(r["value"]) * float(r["scale"] or 1)
    except (TypeError, ValueError, KeyError):
        return None

def _latest_rows(sec: CsvStore) -> dict:
    """(period_end, line_item_en) -> row from the newest filing that reports that period (consolidated)."""
    best = {}
    for r in sec.rows.values():
        if r["value"] == "" or r["agent_status"] == "unresolved":
            continue
        if r["scope"] != "consolidated" and not (r["scope"] == "non-consolidated" and r["statement"] in ("Shares", "Dividend")):
            continue
        k = (r["period_end"], r["line_item_en"])
        # prefer student-corrected/verified, then the current-year column of the newest filing
        rank = (r["agent_status"] in ("student-corrected", "student-verified"), "Prior-year" not in r["correction_note"],
                r["as_reported_in"])
        if k not in best or rank > best[k][0]:
            best[k] = (rank, r)
    return {k: v[1] for k, v in best.items()}

class Writer:
    def __init__(self, st, store: CsvStore, ticker: str, cname: str):
        self.st, self.store, self.ticker, self.cname = st, store, ticker, cname
        self.run_id = run_ctx(st)["run_id"]

    def put(self, metric, basis, period_end, value, unit, formula, inputs, *, url, title, location,
            source_type="derived", quote_time="", price_type="", note="", period_label=None, key_extra=""):
        rid = record_id("baseline", self.ticker, metric, basis, period_end, key_extra)
        row = base_row(record_id=rid, ticker=self.ticker, company_name=self.cname, metric=metric, basis=basis,
                       formula=formula, input_record_ids=";".join(i for i in inputs if i), quote_time_jst=quote_time,
                       price_type=price_type, unit=unit, scale="1", period_label=period_label or (f"FY ending {period_end}" if period_end else "current"),
                       period_end=period_end or "", source_type=source_type, source_title=title, source_url=url,
                       location_in_source=location, as_reported_in="", extraction_method="derived" if source_type == "derived" else "HTML/PDF",
                       correction_note=note)
        if value is None or value == "":
            row = unresolved(row, note or "Inputs missing; not computed.")
        elif isinstance(value, str):
            row["value"] = value
        else:
            row["value"] = f"{value:.6g}" if abs(value) < 1e6 else f"{value:.0f}"
        self.store.upsert(row, self.run_id)
        return rid if row["value"] != "" else None

# ---------------------------------------------------------------- historical (5 fiscal years, Tsuchiya)
def historical(st, sec: CsvStore, base: CsvStore) -> dict:
    w = Writer(st, base, TS, NAME)
    L = _latest_rows(sec)
    n = 0
    for term, pe in CONFIG["fiscal_years"].items():
        g = lambda item: L.get((pe, item))
        def src(r):
            return dict(url=r["source_url"], title=r["source_title"], location=r["location_in_source"], source_type=r["source_type"])
        for metric, item, unit in (("Revenue", "Revenue", "JPY"), ("Operating Profit", "Operating profit", "JPY"),
                                   ("Net Profit", "Profit attributable to owners of parent", "JPY"),
                                   ("EPS (actual)", "EPS (basic)", "JPY/share"), ("Operating Cash Flow", "Operating cash flow", "JPY")):
            r = g(item)
            if r:
                w.put(metric, "actual", pe, _v(r), unit, f"= 有報 {r['line_item_ja']}", [r["record_id"]], **src(r))
            else:
                w.put(metric, "actual", pe, None, unit, f"= 有報 {item}", [], url="https://disclosure2.edinet-fsa.go.jp/",
                      title="有価証券報告書", location="", note=f"{item} for FY ending {pe} not extracted yet.")
            n += 1
        ocf, ppe, intang = g("Operating cash flow"), g("Purchase of PP&E"), g("Purchase of intangible assets")
        if ocf and ppe:
            fcf = _v(ocf) + _v(ppe) + (_v(intang) if intang else 0.0)
            note = "" if intang else "無形固定資産の取得による支出 not reported in the CF statement; formula uses PP&E only (verify)."
            w.put("Free Cash Flow", "derived", pe, fcf, "JPY",
                  "FCF = 営業CF + 有形固定資産の取得による支出 + 無形固定資産の取得による支出 (outflows are negative in the CF statement)",
                  [ocf["record_id"], ppe["record_id"], intang["record_id"] if intang else ""],
                  url=ocf["source_url"], title=ocf["source_title"], location="CF statement", note=note)
        else:
            w.put("Free Cash Flow", "derived", pe, None, "JPY", "FCF = 営業CF + 有形・無形固定資産の取得による支出", [],
                  url="https://disclosure2.edinet-fsa.go.jp/", title="有価証券報告書", location="", note="Inputs missing.")
        op, da = g("Operating profit"), g("Depreciation")
        if op and da:
            w.put("EBITDA", "derived", pe, _v(op) + _v(da), "JPY", "EBITDA = 営業利益 + 減価償却費 (CF statement)",
                  [op["record_id"], da["record_id"]], url=op["source_url"], title=op["source_title"], location="PL + CF statement")
        else:
            w.put("EBITDA", "derived", pe, None, "JPY", "EBITDA = 営業利益 + 減価償却費", [], url="https://disclosure2.edinet-fsa.go.jp/",
                  title="有価証券報告書", location="", note="営業利益 or 減価償却費 missing.")
        debt, ids, missing = _debt(g)
        cash = g("Cash and equivalents, end of period")
        if debt is not None:
            w.put("Interest-bearing debt", "derived", pe, debt, "JPY",
                  "= 短期借入金 + 1年内返済予定の長期借入金 + 長期借入金 + 社債 + 1年内償還予定の社債 + リース債務(流動+固定)", ids,
                  url=cash["source_url"] if cash else "https://disclosure2.edinet-fsa.go.jp/", title="有価証券報告書",
                  location="BS", note=(f"Not reported in the balance sheet (treated as not present; verify): {', '.join(missing)}" if missing else ""))
        if debt is not None and cash:
            w.put("Net debt", "derived", pe, debt - _v(cash), "JPY", "Net debt = interest-bearing debt − 現金及び現金同等物",
                  ids + [cash["record_id"]], url=cash["source_url"], title="有価証券報告書", location="BS + CF")
        sh, tr = g("Shares issued"), g("Treasury shares")
        if sh and tr:
            w.put("Shares Outstanding (ex-treasury)", "derived", pe, _v(sh) - _v(tr), "shares", "= 発行済株式総数 − 自己株式数",
                  [sh["record_id"], tr["record_id"]], url=sh["source_url"], title="有価証券報告書", location="株式等の状況")
        else:
            w.put("Shares Outstanding (ex-treasury)", "derived", pe, None, "shares", "= 発行済株式総数 − 自己株式数", [],
                  url="https://disclosure2.edinet-fsa.go.jp/", title="有価証券報告書", location="", note="Share counts missing.")
        n += 5
    return {"historical_rows": n}

def _debt(g):
    items = ["Short-term borrowings", "Current portion of long-term borrowings", "Long-term borrowings", "Bonds payable",
             "Current portion of bonds", "Lease obligations (current)", "Lease obligations (non-current)"]
    total, ids, missing = 0.0, [], []
    present = 0
    for it in items:
        r = g(it)
        if r:
            total += _v(r)
            ids.append(r["record_id"])
            present += 1
        else:
            missing.append(it)
    return (total if present else None), ids, missing

# ---------------------------------------------------------------- current market metrics
def current(st, sec: CsvStore, base: CsvStore, peers_too: bool = True) -> dict:
    out = {"warnings": []}
    today = iso()[:10]
    L = _latest_rows(sec)
    last_pe = max((pe for pe in CONFIG["fiscal_years"].values() if (pe, "Revenue") in L), default=None)
    w = Writer(st, base, TS, NAME)
    px = market.best_price(st, TS)
    price_id = None
    if px:
        price_id = w.put("Stock Price", "market", "", px["price"], "JPY/share", "quoted price", [], url=px["source_url"],
                         title=px["source_type"], location=px["location"], source_type=px["source_type"],
                         quote_time=px["quote_time"], price_type=px["price_type"], period_label=f"quote {px['quote_time'][:10]}",
                         key_extra=px["quote_time"][:10],
                         note=(f"Third-party daily close ({'Yahoo Finance via yfinance, Addendum A16' if 'yfinance' in px['source_type'] else 'irbank'})."
                               if px["third_party"] else "Student-entered JPX quote."))
        prev = st["last_price"].get(TS)
        if prev and prev["price"] and abs(px["price"] / prev["price"] - 1) > 0.10:
            out["warnings"].append(f"Stock price moved {px['price'] / prev['price'] - 1:+.1%} since previous run")
        st["last_price"][TS] = {"price": px["price"], "quote_time": px["quote_time"]}
        if px["third_party"]:
            out["warnings"].append(f"Price from a third-party source ({px['source_type']})")
    else:
        w.put("Stock Price", "market", "", None, "JPY/share", "quoted price", [], url="https://irbank.net/1840/chart",
              title="price", location="", key_extra=today, note="No price available this run (irbank failed, no manual entry).")
    fc = {}
    try:
        fc = market.latest_forecast(st, TS)
    except (FetchError, GuardrailError) as e:
        log(st, "forecast", {"ticker": TS}, str(e))
    fids = {}
    if fc:
        sc = fc.get("unit_scale") or 1_000_000
        for metric, key, unit, mult in (("Revenue (company forecast)", "revenue", "JPY", sc),
                                        ("Operating Profit (company forecast)", "operating_profit", "JPY", sc),
                                        ("Net Profit (company forecast)", "net_income", "JPY", sc),
                                        ("EPS (company forecast)", "eps", "JPY/share", 1),
                                        ("DPS (company forecast)", "dps", "JPY/share", 1)):
            v = fc.get(key)
            fids[key] = w.put(metric, "company forecast", "", None if v is None else v * mult, unit,
                              f"= 会社予想 {fc.get('row', '')} in latest 決算短信/業績予想の修正", [],
                              url=fc.get("dps_source_url", fc["source_url"]) if key == "dps" else fc["source_url"], title=fc["source_title"], location=f"p.{fc.get('page')} row {fc.get('row')}",
                              source_type="決算短信 / 適時開示 (company)", period_label=fc.get("fiscal_year") or "current FY forecast",
                              key_extra=fc.get("fiscal_year", ""),
                              note=("" if v is not None else f"{key} not found in forecast table; not imputed."))
    # cross-check rows (irbank)
    ib = {}
    try:
        ib = market.irbank_summary(st, TS)
    except (FetchError, GuardrailError) as e:
        log(st, "irbank", {"ticker": TS}, str(e))
    if ib:
        w.put("Forward P/E (irbank cross-check)", "market", "", ib.get("forward_per"), "times", "irbank 予想PER (third-party)",
              [], url=ib["url"], title="irbank", location=f"PER 予 (as of {ib.get('as_of')})", source_type="market data (irbank, third-party)",
              key_extra=today, period_label=f"as of {ib.get('as_of')}")
        w.put("EPS (company forecast, irbank cross-check)", "market", "", ib.get("forecast_eps"), "JPY/share",
              "irbank EPS（連）予 (third-party)", [], url=ib["url"], title="irbank", location="EPS（連）予",
              source_type="market data (irbank, third-party)", key_extra=today, period_label=f"as of {ib.get('as_of')}")
        if fc.get("eps") is not None and ib.get("forecast_eps") is not None and abs(ib["forecast_eps"] - fc["eps"]) > 0.01:
            out["warnings"].append(f"Forecast EPS differs: 決算短信 {fc['eps']} vs irbank {ib['forecast_eps']} (irbank may be stale)")
    g = lambda item: L.get((last_pe, item)) if last_pe else None
    sh, tr = g("Shares issued"), g("Treasury shares")
    shares = (_v(sh) - _v(tr)) if sh and tr else None
    shares_ids = [sh["record_id"], tr["record_id"]] if sh and tr else []
    price = px.get("price") if px else None
    mcap = price * shares if price and shares else None
    mcap_id = w.put("Market Cap", "derived-current", "", mcap, "JPY", "Market cap = price × shares outstanding (ex-treasury, latest FY end)",
                    [price_id] + shares_ids, url=px.get("source_url", "https://irbank.net/1840/chart") if px else "https://irbank.net/1840/chart",
                    title="derived", location="", key_extra=today,
                    note="" if mcap else "Price or share count missing.")
    debt, debt_ids, _ = _debt(g)
    cash = g("Cash and equivalents, end of period")
    ev = mcap + debt - _v(cash) if mcap is not None and debt is not None and cash else None
    ev_id = w.put("EV", "derived-current", "", ev, "JPY", "EV = market cap + interest-bearing debt − 現金及び現金同等物 (latest FY end)",
                  [mcap_id] + debt_ids + ([cash["record_id"]] if cash else []), url=cash["source_url"] if cash else "https://disclosure2.edinet-fsa.go.jp/",
                  title="derived", location="", key_extra=today, note="" if ev is not None else "Inputs missing.")
    eps_a = g("EPS (basic)")
    pe_t = price / _v(eps_a) if price and eps_a and _v(eps_a) > 0 else None
    w.put("P/E (trailing)", "derived-current", "", pe_t if pe_t else ("not meaningful" if eps_a and price else None), "times",
          "Trailing P/E = price / actual EPS (latest FY)", [price_id, eps_a["record_id"] if eps_a else ""],
          url=eps_a["source_url"] if eps_a else "https://irbank.net/1840", title="derived", location="", key_extra=today,
          note="" if pe_t else ("Actual EPS ≤ 0: P/E not meaningful." if eps_a else "Inputs missing."))
    feps = fc.get("eps") if fc else None
    pe_f = price / feps if price and feps and feps > 0 else None
    w.put("P/E (forward)", "derived-current", "", pe_f if pe_f else ("not meaningful" if feps is not None and price else None), "times",
          "Forward P/E = price / company-forecast EPS", [price_id, fids.get("eps")], url=fc.get("source_url", "https://www.tsuchiya.co.jp/ir/news/"),
          title="derived", location="", key_extra=today,
          note="" if pe_f else (f"Company-forecast EPS = {feps} (≤ 0): forward P/E not meaningful." if feps is not None else "Forecast EPS missing."))
    if pe_f and ib.get("forward_per"):
        if abs(pe_f / ib["forward_per"] - 1) > 0.10:
            out["warnings"].append(f"P/E sources disagree >10%: computed {pe_f:.2f} vs irbank {ib['forward_per']:.2f}")
    dps = fc.get("dps") if fc else None
    dy = dps / price if dps is not None and price else None
    w.put("Dividend Yield (forecast)", "derived-current", "", dy, "ratio", "= company-forecast DPS / price",
          [fids.get("dps"), price_id], url=fc.get("source_url", "https://www.tsuchiya.co.jp/ir/news/"), title="derived", location="",
          key_extra=today, note="" if dy is not None else "Forecast DPS or price missing.")
    da = g("Depreciation")
    fop = fc.get("operating_profit") * (fc.get("unit_scale") or 1_000_000) if fc and fc.get("operating_profit") is not None else None
    febitda = fop + _v(da) if fop is not None and da else None
    w.put("EBITDA (forward)", "derived-current", "", febitda, "JPY",
          "Forward EBITDA = company-forecast 営業利益 + latest actual 減価償却費 (assumes D&A flat; stated, not hidden)",
          [fids.get("operating_profit"), da["record_id"] if da else ""], url=fc.get("source_url", "https://www.tsuchiya.co.jp/ir/news/"),
          title="derived", location="", key_extra=today, note="" if febitda is not None else "Forecast OP or D&A missing.")
    peers = peer_metrics(st, base) if peers_too else {}
    eps_rows = [L.get((pe_, "EPS (basic)")) for pe_ in CONFIG["fiscal_years"].values()]
    eps_rows = [r for r in eps_rows if r]
    norm = statistics.mean(_v(r) for r in eps_rows) if eps_rows else None
    norm_id = w.put("EPS (5-year average, actual)", "derived", "", norm, "JPY/share",
                    "= mean of reported basic EPS for the fiscal years extracted (normalised EPS; Addendum A18)",
                    [r["record_id"] for r in eps_rows], url=eps_rows[-1]["source_url"] if eps_rows else "https://disclosure2.edinet-fsa.go.jp/",
                    title="有価証券報告書 (5 years)", location="EPS per filing", period_label=f"{len(eps_rows)} fiscal years",
                    note="" if len(eps_rows) == 5 else f"Only {len(eps_rows)} of 5 fiscal years available; average over those.")
    targets(st, base, w, price, price_id, feps, fids, dy, febitda, debt, cash, shares, shares_ids, peers, today,
            norm_eps=norm, norm_id=norm_id, eps_actual=_v(eps_a) if eps_a else None, eps_actual_id=eps_a["record_id"] if eps_a else None)
    return out

# ---------------------------------------------------------------- peers
def peer_metrics(st, base: CsvStore) -> dict:
    res = {}
    today = iso()[:10]
    for p in CONFIG["peers"]:
        t, name = p["ticker"], p["name"]
        w = Writer(st, base, t, name)
        r = {"ticker": t}
        try:
            px = market.best_price(st, t)
        except BudgetStop:
            break
        pid = None
        if px:
            pid = w.put("Stock Price", "market", "", px["price"], "JPY/share", "quoted price", [], url=px["source_url"],
                        title=px["source_type"], location=px["location"], source_type=px["source_type"],
                        quote_time=px["quote_time"], price_type=px["price_type"], key_extra=px["quote_time"][:10],
                        period_label=f"quote {px['quote_time'][:10]}")
        fc = {}
        try:
            fc = market.latest_forecast(st, t)
        except (FetchError, GuardrailError) as e:
            log(st, "forecast", {"ticker": t}, str(e))
        except BudgetStop:
            pass
        eid = None
        if fc:
            eid = w.put("EPS (company forecast)", "company forecast", "", fc.get("eps"), "JPY/share", "= 会社予想 EPS (決算短信)", [],
                        url=fc["source_url"], title=fc["source_title"], location=f"p.{fc.get('page')} row {fc.get('row')}",
                        source_type="決算短信 / 適時開示 (company)", key_extra=fc.get("fiscal_year", ""),
                        period_label=fc.get("fiscal_year") or "current FY forecast")
            sc = fc.get("unit_scale") or 1_000_000
            if fc.get("operating_profit") is not None:
                r["fop"] = fc["operating_profit"] * sc
                r["fop_id"] = w.put("Operating Profit (company forecast)", "company forecast", "", r["fop"], "JPY",
                                    "= 会社予想 営業利益 (決算短信)", [], url=fc["source_url"], title=fc["source_title"],
                                    location=f"p.{fc.get('page')}", source_type="決算短信 / 適時開示 (company)",
                                    key_extra=fc.get("fiscal_year", ""))
        ib = {}
        try:
            ib = market.irbank_summary(st, t)
            w.put("Forward P/E (irbank cross-check)", "market", "", ib.get("forward_per"), "times", "irbank 予想PER (third-party)", [],
                  url=ib["url"], title="irbank", location=f"as of {ib.get('as_of')}", source_type="market data (irbank, third-party)",
                  key_extra=today)
            r["irbank_pe"] = ib.get("forward_per")
        except (FetchError, GuardrailError, BudgetStop) as e:
            log(st, "irbank", {"ticker": t}, str(e))
        price, feps = (px or {}).get("price"), fc.get("eps") if fc else None
        eps_src, eps_url = "決算短信 (company)", (fc or {}).get("source_url", "")
        if feps is None and ib.get("forecast_eps") is not None:
            # Addendum A14: the peer's 決算短信 can't be read automatically -> irbank 予想EPS, labeled third-party
            feps, eps_src, eps_url = ib["forecast_eps"], "irbank 予想EPS (third-party)", ib["url"]
            eid = w.put("EPS (company forecast)", "company forecast", "", feps, "JPY/share",
                        "= 会社予想 EPS as shown by irbank (third-party copy of the 決算短信 forecast)", [], url=ib["url"],
                        title="irbank", location=f"EPS（連）予 (as of {ib.get('as_of')})",
                        source_type="market data (irbank, third-party)", key_extra=f"irbank {today}",
                        note="Third-party fallback (Addendum A14): the company's 決算短信 list cannot be read automatically.")
        if price and feps is not None:
            pe = price / feps if feps > 0 else None
            r["pe"] = pe
            r["pe_id"] = w.put("P/E (forward)", "derived-current", "", pe if pe else "not meaningful", "times",
                               f"Forward P/E = price / company-forecast EPS (EPS source: {eps_src})", [pid, eid], url=eps_url or ib.get("url", ""),
                               title="derived", location="", key_extra=today,
                               note="" if pe else f"Forecast EPS {feps} ≤ 0; excluded from target P/E.")
        else:
            w.put("P/E (forward)", "derived-current", "", None, "times", "Forward P/E = price / company-forecast EPS", [pid, eid],
                  url=CONFIG["peer_ir"].get(t, "https://irbank.net/"), title="derived", location="", key_extra=today,
                  note="Price or forecast EPS missing.")
        # EV/EBITDA inputs from the peer's latest 有報 (cached facts only; discovery runs in P6 with a cursor)
        inp = st["documents"].get(f"peer:{t}", {}).get("latest_facts")
        if inp and price and inp.get("op") is not None:
            shares = (inp["shares_issued"] - (inp.get("treasury") or 0)) if inp.get("shares_issued") else None
            mcap = price * shares if shares and shares > 0 else None
            debt, cash, da = inp.get("debt"), inp.get("cash"), inp.get("da")
            if None not in (mcap, debt, cash, da):
                ev = mcap + debt - cash
                eb = inp["op"] + da
                r["ev_ebitda"] = ev / eb if eb > 0 and ev > 0 else None  # negative/n.m. multiples are excluded
                r["ev_ebitda_id"] = w.put("EV/EBITDA (trailing)", "derived-current", "", r["ev_ebitda"] or "not meaningful", "times",
                                          "= (price × shares ex-treasury + interest-bearing debt − cash) / (営業利益 + 減価償却費), latest 有報 actuals",
                                          [pid], url=inp.get("source_url", "https://disclosure2.edinet-fsa.go.jp/"),
                                          title=f"有価証券報告書 {inp.get('docID')}", location="PL/BS/CF of latest 有報",
                                          key_extra=today, note="" if r["ev_ebitda"] else "EV or EBITDA ≤ 0; not meaningful, excluded from the target.")
        res[t] = r
    return res

def targets(st, base, w: Writer, price, price_id, feps, fids, dy, febitda, debt, cash, shares, shares_ids, peers, today,
            norm_eps=None, norm_id=None, eps_actual=None, eps_actual_id=None):
    pes = {t: r["pe"] for t, r in peers.items() if r.get("pe")}
    excluded = [f"{t} ({'n/m' if 'pe' in r else 'missing'})" for t, r in peers.items() if not r.get("pe")]
    tpe = statistics.mean(pes.values()) if pes else None
    tpe_id = w.put("Target P/E", "derived-current", "", tpe, "times", "Target P/E = arithmetic mean of peers' forward P/E",
                   [r.get("pe_id") for r in peers.values() if r.get("pe")], url="https://irbank.net/", title="derived from peer rows",
                   location="", key_extra=today,
                   note=(f"Peers used: {', '.join(pes)}; excluded: {', '.join(excluded) or 'none'}; "
                         f"median = {statistics.median(pes.values()):.2f}" if pes else "No peer forward P/E available."))
    if tpe is not None and feps is not None:
        tp = tpe * feps if feps > 0 else "not meaningful"
        note = "" if feps > 0 else f"Company-forecast EPS = {feps} (≤ 0); a P/E target price would be zero or negative and is not reported."
    else:
        tp, note = None, "Target P/E or forecast EPS missing."
    method = {}  # basis -> (price, record_id) for the average valuated price
    tp_id = w.put("Target Price (P/E)", "derived-current", "", tp, "JPY/share", "= Target P/E × company-forecast EPS",
                  [tpe_id, fids.get("eps")], url="https://www.tsuchiya.co.jp/ir/news/", title="derived", location="", key_extra=today, note=note)
    if isinstance(tp, float):
        method["forecast"] = (tp, tp_id)
    if isinstance(tp, float) and price:
        er = tp / price - 1 + (dy or 0)
        w.put("Expected Return (P/E)", "derived-current", "", er, "ratio", "= Target Price / Price − 1 + dividend yield",
              [tp_id, price_id], url="https://www.tsuchiya.co.jp/ir/news/", title="derived", location="", key_extra=today,
              note="" if dy is not None else "Dividend yield missing; treated as not available (not added).")
    else:
        w.put("Expected Return (P/E)", "derived-current", "", "not meaningful" if tp == "not meaningful" else None, "ratio",
              "= Target Price / Price − 1 + dividend yield", [tp_id, price_id], url="https://www.tsuchiya.co.jp/ir/news/",
              title="derived", location="", key_extra=today, note="Target price by P/E not available.")
    for label, eps, eid, basis_note in (
            ("Target Price (P/E, trailing EPS)", eps_actual, eps_actual_id, "latest reported annual EPS"),
            ("Target Price (P/E, 5-year average EPS)", norm_eps, norm_id,
             "normalised EPS = mean of the five reported years; used because forecast and trailing EPS are not positive (A18)")):
        if tpe is None or eps is None:
            w.put(label, "derived-current", "", None, "JPY/share", f"= Target P/E × {basis_note}", [tpe_id, eid],
                  url="https://disclosure2.edinet-fsa.go.jp/", title="derived", location="", key_extra=today, note="Target P/E or EPS missing.")
            continue
        tpx = tpe * eps if eps > 0 else "not meaningful"
        tpx_id = w.put(label, "derived-current", "", tpx, "JPY/share", f"= Target P/E × {basis_note}", [tpe_id, eid],
                       url="https://disclosure2.edinet-fsa.go.jp/", title="derived", location="", key_extra=today,
                       note="" if eps > 0 else f"EPS = {eps:.2f} (≤ 0); a P/E price would be zero or negative.")
        if isinstance(tpx, float):
            method["trailing" if "trailing" in label else "5-year average"] = (tpx, tpx_id)
        er = (tpx / price - 1 + (dy or 0)) if isinstance(tpx, float) and price else ("not meaningful" if tpx == "not meaningful" else None)
        w.put(label.replace("Target Price", "Expected Return"), "derived-current", "", er, "ratio",
              "= Target Price / current market price − 1 + dividend yield", [tpx_id, price_id],
              url="https://disclosure2.edinet-fsa.go.jp/", title="derived", location="", key_extra=today,
              note="" if isinstance(er, float) else ("Target price not meaningful." if er else "Price missing."))
    evs = {t: r["ev_ebitda"] for t, r in peers.items() if r.get("ev_ebitda")}
    tm = statistics.mean(evs.values()) if evs else None
    tm_id = w.put("Target EV/EBITDA", "derived-current", "", tm, "times",
                  "= arithmetic mean of peers' EV/EBITDA on latest 有報 actuals (trailing; Addendum A14), applied to Tsuchiya's forward EBITDA",
                  [r.get("ev_ebitda_id") for r in peers.values() if r.get("ev_ebitda")], url="https://disclosure2.edinet-fsa.go.jp/",
                  title="derived from peer rows", location="", key_extra=today,
                  note=(f"Peers used: {', '.join(evs)}; median = {statistics.median(evs.values()):.2f}" if evs else "No peer EV/EBITDA yet (peer 有報 inputs pending)."))
    if tm and febitda and febitda > 0 and debt is not None and cash and shares:
        tev = febitda * tm
        teq = tev - (debt - _v(cash))
        tps = teq / shares
        method["ev_ebitda"] = (tps, None)
        tps_id = w.put("Target Price (EV/EBITDA)", "derived-current", "", tps, "JPY/share",
                       "= (Forward EBITDA × Target EV/EBITDA − Net debt) / shares outstanding (ex-treasury)",
                       [tm_id] + shares_ids + [cash["record_id"]], url=cash["source_url"], title="derived", location="", key_extra=today)
        if price:
            w.put("Expected Return (EV/EBITDA)", "derived-current", "", tps / price - 1 + (dy or 0), "ratio",
                  "= Target Price / Price − 1 + dividend yield", [tps_id, price_id], url=cash["source_url"], title="derived",
                  location="", key_extra=today)
    else:
        w.put("Target Price (EV/EBITDA)", "derived-current", "", "not meaningful" if febitda is not None and febitda <= 0 else None,
              "JPY/share", "= (Forward EBITDA × Target EV/EBITDA − Net debt) / shares", [tm_id],
              url="https://disclosure2.edinet-fsa.go.jp/", title="derived", location="", key_extra=today,
              note="Inputs missing (target multiple, forward EBITDA, net debt or shares).")
    # Average valuated price = mean of the P/E method price and the EV/EBITDA method price (Addendum A18).
    pe_basis = next((b for b in ("forecast", "trailing", "5-year average") if b in method), None)
    if "ev_ebitda" in method:
        method["ev_ebitda"] = (method["ev_ebitda"][0], tps_id)
    parts = [(f"P/E ({pe_basis} EPS)", *method[pe_basis])] if pe_basis else []
    if "ev_ebitda" in method:
        parts.append(("EV/EBITDA", *method["ev_ebitda"]))
    avgp = statistics.mean(p for _, p, _ in parts) if len(parts) == 2 else None
    w.put("Average Valuated Price", "derived-current", "", avgp, "JPY/share",
          "= (P/E method price + EV/EBITDA method price) / 2; P/E method uses forecast EPS, else trailing EPS, else 5-year average EPS",
          [i for _, _, i in parts], url="https://disclosure2.edinet-fsa.go.jp/", title="derived", location="", key_extra=today,
          note=("Methods: " + "; ".join(f"{n} = {p:.2f}" for n, p, _ in parts)) if avgp is not None
          else "Needs both a P/E method price and an EV/EBITDA price; not computed (not imputed).")

# ---------------------------------------------------------------- peer EV inputs from EDINET (cursor)
def latest_filed_fy_end(today, month: int):
    """Most recent fiscal-year end whose annual report deadline (3 months) has passed."""
    import datetime as dt
    best = None
    for y in (today.year, today.year - 1, today.year - 2):
        nxt = dt.date(y + (month // 12), month % 12 + 1, 1)
        fy_end = nxt - dt.timedelta(days=1)
        if fy_end + dt.timedelta(days=95) <= today and (best is None or fy_end > best):
            best = fy_end
    return best

def tanshin_actuals(st, base: CsvStore) -> dict:
    """Headline actuals from the latest full-year 決算短信 (for the cross-source check)."""
    import re as _re
    import urllib.parse
    w = Writer(st, base, TS, NAME)
    docs = [d for d in market.find_forecast_docs(st, TS) if "決算短信" in d["title"] and not _re.search(r"四半期|中間", d["title"])]
    if not docs:
        return {"found": False}
    d = docs[0]
    m = _re.search(r"(20\d\d)年(\d{1,2})月期", d["title"])
    if not m:
        return {"found": False}
    body, meta = fetch_pdf(st, d["url"])
    act = market.parse_actuals_pdf(body, f"{m.group(1)}年{int(m.group(2))}月期")
    pe = (__import__("datetime").date(int(m.group(1)) + (int(m.group(2)) // 12), int(m.group(2)) % 12 + 1, 1)
          - __import__("datetime").timedelta(days=1)).isoformat()
    sc = act.get("unit_scale") or 1_000_000
    for metric, key in (("Revenue (決算短信)", "revenue"), ("Operating profit (決算短信)", "operating_profit"),
                        ("Net profit (決算短信)", "net_income")):
        v = act.get(key)
        w.put(metric, "actual (決算短信)", pe, None if v is None else v * sc, "JPY", "= 決算短信 連結経営成績 (百万円, rounded)", [],
              url=d["url"] + "#page=1", title=d["title"], location="1. 連結経営成績", source_type="決算短信",
              note="" if v is not None else "Not found in 決算短信 table.")
    return {"found": True, "fy": pe}

def fetch_pdf(st, url):
    from lib import fetch, write_bytes
    import urllib.parse
    body, meta = fetch(st, url, binary=True, purpose="決算短信 PDF")
    write_bytes(f"raw/web/{urllib.parse.urlsplit(url).hostname}/{meta['sha256']}.pdf", body)
    return body, meta

def peer_inputs(st, max_peers: int = 2) -> dict:
    """Discover each peer's latest 有報 and cache EV inputs in state (not a CSV of its own)."""
    import datetime as dt
    out = {}
    done = 0
    for p in CONFIG["peers"]:
        key = f"peer:{p['ticker']}"
        d = st["documents"].setdefault(key, {"fy": {}, "scanned": {}, "corrections": []})
        if d.get("latest_facts") and d["latest_facts"].get("fetched", "")[:7] == iso()[:7] and "op" in d["latest_facts"]:
            continue
        if done >= max_peers:
            break
        t = dt.date.fromisoformat(iso()[:10])
        fy_end = latest_filed_fy_end(t, p["fy_end_month"]).isoformat()
        try:
            edinet.discover(st, edinet.PEER_EDINET[p["ticker"]], {"latest": fy_end}, key)
        except (FetchError, BudgetStop) as e:
            out[p["ticker"]] = f"discovery paused: {e}"
            break
        doc = d["fy"].get("latest", {}).get("asr")
        if not doc:
            out[p["ticker"]] = "annual report not found yet"
            continue
        facts = edinet.read_xbrl_csv(edinet.download(st, doc["docID"], 5))
        def f(els, ctx):
            for c in (ctx, ctx + "_NonConsolidatedMember"):  # share counts are parent-company items
                for el in els:
                    for x in facts:
                        if x["element"] == el and x["context"] == c and x["value"] not in ("", "－"):
                            return float(x["value"])
            return None
        debt_parts = [f([e], "CurrentYearInstant") for e in ("jppfs_cor:ShortTermLoansPayable", "jppfs_cor:CurrentPortionOfLongTermLoansPayable",
                                                              "jppfs_cor:LongTermLoansPayable", "jppfs_cor:BondsPayable", "jppfs_cor:CurrentPortionOfBonds",
                                                              "jppfs_cor:LeaseObligationsCL", "jppfs_cor:LeaseObligationsNCL")]
        d["latest_facts"] = {
            "docID": doc["docID"], "source_url": edinet.doc_url(doc["docID"]), "fetched": iso(),
            "debt": sum(x for x in debt_parts if x is not None) if any(x is not None for x in debt_parts) else None,
            "cash": f(["jppfs_cor:CashAndCashEquivalents"], "CurrentYearInstant"),
            "op": f(["jppfs_cor:OperatingIncome"], "CurrentYearDuration"),
            "da": f(["jppfs_cor:DepreciationAndAmortizationOpeCF", "jppfs_cor:DepreciationOpeCF"], "CurrentYearDuration"),
            "shares_issued": f(["jpcrp_cor:TotalNumberOfIssuedSharesSummaryOfBusinessResults",
                                "jpcrp_cor:NumberOfIssuedSharesAsOfFiscalYearEndIssuedSharesTotalNumberOfSharesEtc"], "CurrentYearInstant"),
            "treasury": f(["jpcrp_cor:TotalNumberOfSharesHeldTreasurySharesEtc"], "CurrentYearInstant"),
        }
        out[p["ticker"]] = "ok"
        done += 1
    return out
