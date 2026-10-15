"""Fama-French-style factor model for 1840 (Addenda A28-A29), and the shared daily price store.

Shared daily price store (A28: yfinance guardrail loosened by the student, replacing A16's per-run fetch)
  - raw/market/daily_prices.csv: daily close and dividends for the tickers in STORE_SYMBOLS only.
  - Updated at most once per JST day, after the TSE close (15:30), in ONE yfinance batch; the first fill
    downloads the history once. No other Yahoo data, no retries around a block.
  - The baseline's share prices (market.yf_prices) and this model both read from the store.
Risk-free rate: the 1-year JGB yield from the Ministry of Finance CSVs (www.mof.go.jp), once per day.

Factor model (proxies built from Tokyo-listed ETFs; NOT Kenneth French's published factors)
  MKT = TOPIX ETF (1306.T) total return - risk-free
  SMB = JPX-Nikkei Mid Small ETF (1493.T) - TOPIX Core 30 ETF (1311.T)
  HML = Nomura Japan Equity High Dividend 70 ETF (1577.T) - TOPIX ETF (1306.T)   (value proxy: high dividend)
  Weekly (Friday) returns over the last 5 years; Dimson adjustment (one lag of MKT) for thin trading.
  Cost of equity r = rf + sum(beta x long-run average factor premium).
  Factor-implied price = today's price x predicted / actual cumulative total return since the start date
    (= the price if 1840 had earned exactly its factor-predicted return), averaged over 1, 2, 3, 5-year starts.
  Residual income value (reference only) = BPS + (normalised ROE - r) x BPS / r.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import re

from lib import (CONFIG, ROOT, BudgetStop, FetchError, GuardrailError, fetch, iso, log, now, run_ctx, write_text)

STORE_FILE = "raw/market/daily_prices.csv"
RF_FILE = "raw/market/jgb_1y.csv"
STORE_SYMBOLS = {  # ticker or factor leg -> Yahoo symbol (A28 scope; changing it needs approval)
    "1840": "1840.T", "205A": "205A.T", "1873": "1873.T", "1419": "1419.T", "2976": "2976.S",
    "TOPIX": "1306.T", "CORE30": "1311.T", "MIDSMALL": "1493.T", "HIDIV": "1577.T",
}
HISTORY_START = "2016-10-01"
UPDATE_AFTER = (15, 30)      # JST: the daily batch runs at the first run after the TSE close
MOF_ALL = "https://www.mof.go.jp/jgbs/reference/interest_rate/data/jgbcm_all.csv"
MOF_MONTH = "https://www.mof.go.jp/jgbs/reference/interest_rate/jgbcm.csv"
WINDOW_WEEKS = 260           # 5 years of weekly returns for the betas
MIN_WEEKS = 104
HORIZONS = (1, 2, 3, 5)      # start dates for the factor-implied price, in years
YAHOO_URL = "https://finance.yahoo.com/quote/{sym}/history/"


# ---------------------------------------------------------------- store I/O
def _read_csv(rel) -> list[dict]:
    p = ROOT / rel
    if not p.exists():
        return []
    with open(p, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _write_csv(rel, rows: list[dict], cols: list[str]):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols)
    w.writeheader()
    w.writerows(rows)
    write_text(rel, buf.getvalue())


def load_store() -> dict[str, list[tuple[str, float, float]]]:
    """symbol -> [(date, close, dividend)] sorted by date."""
    rel = STORE_FILE
    if run_ctx_mode() == "test":
        rel = _fixture("daily_prices.csv")
    out: dict[str, list] = {}
    for r in _read_csv(rel):
        out.setdefault(r["symbol"], []).append((r["date"], float(r["close"]), float(r["dividend"] or 0)))
    for v in out.values():
        v.sort()
    return out


def load_rf() -> list[tuple[str, float]]:
    rel = RF_FILE if run_ctx_mode() != "test" else _fixture("jgb_1y.csv")
    return sorted((r["date"], float(r["yield_pct"])) for r in _read_csv(rel))


_MODE = {"mode": None}


def run_ctx_mode():
    return _MODE["mode"]


def _fixture(name):
    return f"evals/market/{name}"   # frozen copies for Mode: test (the test sandbox copies evals/market)


def latest_close(ticker: str) -> dict | None:
    """Latest stored close for the baseline (no network). None if the store has nothing for it."""
    sym = STORE_SYMBOLS.get(ticker)
    rows = load_store().get(sym or "", [])
    if not rows:
        return None
    d, c, _ = rows[-1]
    return {"close": c, "date": d, "symbol": sym, "url": YAHOO_URL.format(sym=sym)}


# ---------------------------------------------------------------- daily update (one yfinance batch per day)
def _due(st, key) -> bool:
    t = now()
    last = st.setdefault("daily", {}).get(key)
    return last != t.date().isoformat() and (t.hour, t.minute) >= UPDATE_AFTER


def update_prices(st) -> dict:
    """Once per JST day after 15:30 (or once at any time if the store is empty): one yfinance batch."""
    cr = run_ctx(st)
    _MODE["mode"] = cr.get("mode")
    if cr.get("mode") == "test":
        return {"source": "fixture", "fetched": False}
    store = load_store()
    empty = not store
    if not empty and not _due(st, "price_store"):
        return {"fetched": False, "reason": "already updated today, or before 15:30 JST",
                "last_date": max(v[-1][0] for v in store.values())}
    if cr.get("price_store_tried"):
        return {"fetched": False, "reason": "already tried this run"}
    cr["price_store_tried"] = True
    starts = {s: (HISTORY_START if s not in store else
                  (dt.date.fromisoformat(store[s][-1][0]) - dt.timedelta(days=10)).isoformat())
              for s in STORE_SYMBOLS.values()}
    start = min(starts.values())
    try:
        import yfinance as yf
        syms = list(STORE_SYMBOLS.values())
        df = yf.download(syms, start=start, interval="1d", progress=False, auto_adjust=False, actions=True,
                         group_by="ticker", threads=False)
    except Exception as e:  # Yahoo block or rate limit: keep the store as it is, no workaround (A16/A28)
        log(st, "yfinance", {"symbols": len(STORE_SYMBOLS)}, f"unavailable: {type(e).__name__}: {str(e)[:120]}")
        return {"fetched": False, "error": f"yfinance unavailable: {type(e).__name__}"}
    added, refetch = 0, []
    for sym in STORE_SYMBOLS.values():
        try:
            sub = df[sym]
            closes = sub["Close"]
            divs = sub["Dividends"] if "Dividends" in sub else None
        except (KeyError, TypeError):
            continue
        new = []
        for idx, c in closes.items():
            if c is None or (isinstance(c, float) and math.isnan(c)) or float(c) <= 0:
                continue
            d = idx.strftime("%Y-%m-%d")
            dv = float(divs.get(idx, 0) or 0) if divs is not None else 0.0
            new.append((d, round(float(c), 4), 0.0 if math.isnan(dv) else dv))
        old = {d: (c, v) for d, c, v in store.get(sym, [])}
        # a split re-bases Yahoo's whole close series: if the overlap disagrees, replace this symbol's history
        clash = [d for d, c, _ in new if d in old and abs(old[d][0] / c - 1) > 0.01]
        if clash and starts[sym] != HISTORY_START:
            refetch.append(sym)
            continue
        for d, c, v in new:
            if d not in old:
                added += 1
            old[d] = (c, v)
        store[sym] = sorted((d, c, v) for d, (c, v) in old.items())
    rows = [{"date": d, "symbol": s, "close": c, "dividend": v} for s, v_ in store.items() for d, c, v in v_]
    _write_csv(STORE_FILE, sorted(rows, key=lambda r: (r["symbol"], r["date"])), ["date", "symbol", "close", "dividend"])
    st["daily"]["price_store"] = now().date().isoformat()
    if refetch:  # history re-based (split): drop it so the next daily update downloads it again
        rows = [r for r in rows if r["symbol"] not in refetch]
        _write_csv(STORE_FILE, sorted(rows, key=lambda r: (r["symbol"], r["date"])), ["date", "symbol", "close", "dividend"])
    log(st, "yfinance", {"symbols": len(STORE_SYMBOLS), "start": start}, f"{added} new daily rows; re-based: {refetch}")
    return {"fetched": True, "new_rows": added, "rebased": refetch}


_ERA = {"R": 2018, "H": 1988, "S": 1925}


def _parse_mof(text: str) -> list[tuple[str, float]]:
    out = []
    for line in text.splitlines():
        m = re.match(r"([RHS])(\d+)\.(\d+)\.(\d+),([^,]*)", line.strip())
        if not m:
            continue
        y = _ERA[m.group(1)] + int(m.group(2))
        try:
            v = float(m.group(5))
        except ValueError:
            continue  # "-": no 1-year yield that day
        out.append((f"{y:04d}-{int(m.group(3)):02d}-{int(m.group(4)):02d}", v))
    return out


def update_rf(st) -> dict:
    """1-year JGB yield (MOF). Full history once, then the current-month file once per day after 15:30."""
    if run_ctx(st).get("mode") == "test":
        return {"source": "fixture"}
    have = load_rf()
    if have and not _due(st, "rf_store"):
        return {"fetched": False, "last_date": have[-1][0]}
    rows = dict(have)
    got = 0
    for url in ((MOF_ALL, MOF_MONTH) if not have else (MOF_MONTH,)):  # first fill: history, then this month
        try:
            body, meta = fetch(st, url, binary=True, snapshot=False, purpose="1-year JGB yield (risk-free rate)")
        except (FetchError, GuardrailError, BudgetStop) as e:
            log(st, "rf", {"url": url}, f"unavailable: {e}")
            return {"fetched": bool(got), "error": str(e)[:160]}
        new = [(d, v) for d, v in _parse_mof(body.decode("cp932", "replace")) if d >= HISTORY_START]
        rows.update(new)
        got += len(new)
        _write_csv(RF_FILE, [{"date": d, "yield_pct": v, "source": MOF_ALL} for d, v in sorted(rows.items())],
                   ["date", "yield_pct", "source"])
    st["daily"]["rf_store"] = now().date().isoformat()
    return {"fetched": True, "rows": got}


# ---------------------------------------------------------------- returns and regression
def _daily_returns(rows) -> dict[str, float]:
    """Total return per day: (close + dividend) / previous close - 1."""
    out = {}
    for (d0, c0, _), (d1, c1, v1) in zip(rows, rows[1:]):
        out[d1] = (c1 + v1) / c0 - 1
    return out


def _weekly(daily: dict[str, float]) -> dict[str, float]:
    """Compound daily returns into weeks ending on Friday (key = that Friday's date)."""
    out: dict[str, float] = {}
    for d, r in sorted(daily.items()):
        day = dt.date.fromisoformat(d)
        fri = (day + dt.timedelta(days=(4 - day.weekday()) % 7)).isoformat()
        out[fri] = (1 + out.get(fri, 0.0)) * (1 + r) - 1
    return out


def _rf_weekly(rf: list[tuple[str, float]], weeks: list[str]) -> dict[str, float]:
    out, j, last = {}, 0, None
    for w in weeks:
        while j < len(rf) and rf[j][0] <= w:
            last = rf[j][1]
            j += 1
        if last is not None:
            out[w] = (1 + last / 100) ** (1 / 52) - 1
    return out


def factor_series(store, rf) -> dict:
    W = {k: _weekly(_daily_returns(store.get(STORE_SYMBOLS[k], []))) for k in ("1840", "TOPIX", "CORE30", "MIDSMALL", "HIDIV")}
    weeks = sorted(set.intersection(*(set(v) for v in W.values())))
    rfw = _rf_weekly(rf, weeks)
    weeks = [w for w in weeks if w in rfw]
    return {
        "weeks": weeks, "rf": [rfw[w] for w in weeks],
        "stock": [W["1840"][w] for w in weeks],
        "MKT": [W["TOPIX"][w] - rfw[w] for w in weeks],
        "SMB": [W["MIDSMALL"][w] - W["CORE30"][w] for w in weeks],
        "HML": [W["HIDIV"][w] - W["TOPIX"][w] for w in weeks],
    }


def estimate(fs: dict) -> dict:
    """OLS of 1840's weekly excess return on MKT, MKT(t-1) (Dimson), SMB, HML over the last WINDOW_WEEKS weeks."""
    import numpy as np
    n_all = len(fs["weeks"])
    if n_all < MIN_WEEKS + 1:
        return {"error": f"only {n_all} weeks of overlapping data (need {MIN_WEEKS})"}
    idx = list(range(max(1, n_all - WINDOW_WEEKS), n_all))
    y = np.array([fs["stock"][i] - fs["rf"][i] for i in idx])
    X = np.column_stack([np.ones(len(idx)), [fs["MKT"][i] for i in idx], [fs["MKT"][i - 1] for i in idx],
                         [fs["SMB"][i] for i in idx], [fs["HML"][i] for i in idx]])
    b, *_ = np.linalg.lstsq(X, y, rcond=None)
    resid = y - X @ b
    dof = len(y) - X.shape[1]
    s2 = float(resid @ resid) / dof
    cov = s2 * np.linalg.inv(X.T @ X)
    se = np.sqrt(np.diag(cov))
    r2 = 1 - float(resid @ resid) / float(((y - y.mean()) ** 2).sum())
    # Dimson market beta = beta(t) + beta(t-1); its variance includes their covariance
    bm = float(b[1] + b[2])
    se_bm = float(math.sqrt(cov[1, 1] + cov[2, 2] + 2 * cov[1, 2]))
    beta = {"MKT": bm, "SMB": float(b[3]), "HML": float(b[4])}
    beta_se = {"MKT": se_bm, "SMB": float(se[3]), "HML": float(se[4])}
    # long-run average premia over ALL available weeks, annualised
    prem, prem_se = {}, {}
    for k in ("MKT", "SMB", "HML"):
        v = np.array(fs[k])
        prem[k] = float(v.mean() * 52)
        prem_se[k] = float(v.std(ddof=1) / math.sqrt(len(v)) * 52)
    # covariance of the Dimson market beta with SMB/HML betas, for the r range (betas' uncertainty only)
    g = np.zeros((len(b), 3))
    g[1, 0] = g[2, 0] = 1
    g[3, 1] = 1
    g[4, 2] = 1
    cov_beta = g.T @ cov @ g
    p = np.array([prem["MKT"], prem["SMB"], prem["HML"]])
    return {"beta": beta, "beta_se": beta_se, "alpha_annual": float(b[0] * 52), "alpha_se_annual": float(se[0] * 52),
            "r2": r2, "n_weeks": len(y), "window": [fs["weeks"][idx[0]], fs["weeks"][idx[-1]]],
            "premia": prem, "premia_se": prem_se, "premia_since": fs["weeks"][0],
            "risk_premium_se": float(math.sqrt(p @ cov_beta @ p)), "estimated": iso()}


def implied_prices(fs: dict, est: dict, price_today: float) -> dict:
    """Price if 1840 had earned exactly its factor-predicted return since each start date (rf cancels out)."""
    out = {}
    weeks = fs["weeks"]
    b = est["beta"]
    last = dt.date.fromisoformat(weeks[-1])
    for h in HORIZONS:
        start = (last - dt.timedelta(days=365 * h)).isoformat()
        idx = [i for i, w in enumerate(weeks) if w > start and i >= 1]
        if len(idx) < 40 * h:
            out[f"{h}y"] = None
            continue
        pred = act = 1.0
        for i in idx:
            mkt = fs["MKT"][i]
            pred *= 1 + fs["rf"][i] + b["MKT"] * mkt + b["SMB"] * fs["SMB"][i] + b["HML"] * fs["HML"][i]
            act *= 1 + fs["stock"][i]
        out[f"{h}y"] = price_today * pred / act
    return out


# ---------------------------------------------------------------- run (cli.py factors)
def _bps_and_roe(sec) -> dict:
    rows = [r for r in sec.rows.values() if r["scope"] == "consolidated" and r["value"] != ""
            and "Prior-year" not in r["correction_note"]]
    def series(ja):
        s = {}
        for r in rows:
            if r["line_item_ja"] == ja:
                s[r["period_end"]] = (float(r["value"]), r)
        return dict(sorted(s.items()))
    bps, eps = series("1株当たり純資産額"), series("1株当たり当期純利益")
    if not bps:
        return {}
    last_pe, (bps_now, bps_row) = list(bps.items())[-1]
    common = [pe for pe in bps if pe in eps]
    roe = (sum(eps[pe][0] for pe in common) / sum(bps[pe][0] for pe in common)) if common else None
    return {"bps": bps_now, "bps_row": bps_row, "bps_period": last_pe, "roe_norm": roe, "years": len(common),
            "roe_ids": [eps[pe][1]["record_id"] for pe in common] + [bps[pe][1]["record_id"] for pe in common]}


def run(st, sec, base) -> dict:
    from metrics import Writer
    cr = run_ctx(st)
    _MODE["mode"] = cr.get("mode")
    res = {"prices": update_prices(st), "risk_free": update_rf(st)}
    store, rf = load_store(), load_rf()
    if not store.get(STORE_SYMBOLS["1840"]):
        return {**res, "error": "no price history for 1840 yet"}
    fs = factor_series(store, rf)
    fm = st["documents"].setdefault("factor_model", {})
    stale = (not fm.get("estimated") or
             (now() - dt.datetime.fromisoformat(fm["estimated"])).days >= 7 or cr.get("mode") == "test")
    if stale:
        est = estimate(fs)
        if est.get("error"):
            return {**res, "error": est["error"]}
        st["documents"]["factor_model"] = fm = est
    d_last, p_last, _ = store[STORE_SYMBOLS["1840"]][-1]
    rf_now = rf[-1][1] / 100 if rf else None
    premium = sum(fm["beta"][k] * fm["premia"][k] for k in ("MKT", "SMB", "HML"))
    r = rf_now + premium if rf_now is not None else None
    imp = implied_prices(fs, fm, p_last)
    vals = [v for v in imp.values() if v]
    fip = sum(vals) / len(vals) if vals else None

    w = Writer(st, base, "1840", CONFIG["company_name"])
    today = iso()[:10]
    yurl = YAHOO_URL.format(sym=STORE_SYMBOLS["1840"])
    src = "market data (Yahoo Finance via yfinance, third-party; Addendum A28) + MOF JGB yields"
    note_proxy = "Factor proxies from Tokyo-listed ETFs (1306/1311/1493/1577), not Kenneth French's published factors."
    common = dict(url=yurl, title="factor model (weekly returns)", location=f"weeks {fm['window'][0]}..{fm['window'][1]}",
                  source_type=src, key_extra=today)
    basis = "derived (factor model)"
    ids = {}
    for k, label in (("MKT", "market"), ("SMB", "size SMB"), ("HML", "value HML")):
        ids[k] = w.put(f"Beta: {label} (factor model)", basis, "", fm["beta"][k], "times",
                       f"OLS on weekly excess returns{' (Dimson: beta(t)+beta(t-1))' if k == 'MKT' else ''}", [],
                       note=f"SE {fm['beta_se'][k]:.3f}; R² {fm['r2']:.2f}; n = {fm['n_weeks']} weeks. {note_proxy}", **common)
        w.put(f"Factor premium: {label} (annual)", basis, "", fm["premia"][k], "ratio",
              "mean weekly factor return × 52, all available weeks", [],
              note=f"SE {fm['premia_se'][k]:.3f}; since {fm['premia_since']}. {note_proxy}", **common)
    rf_id = w.put("Risk-free rate (JGB 1y)", "market", "", rf_now, "ratio", "1-year JGB yield / 100 (latest day)", [],
                  url=MOF_ALL, title="財務省 国債金利情報", location=f"1年 column, {rf[-1][0] if rf else 'n/a'}",
                  source_type="Ministry of Finance (official)", key_extra=today,
                  note="" if rf else "MOF yields not available yet.")
    w.put("Alpha, annualised (factor model)", basis, "", fm["alpha_annual"], "ratio", "regression intercept × 52", [],
          note=f"SE {fm['alpha_se_annual']:.3f}", **common)
    lo = hi = None
    if r is not None:
        lo, hi = r - 1.96 * fm["risk_premium_se"], r + 1.96 * fm["risk_premium_se"]
    r_id = w.put("Cost of Equity (factor model)", basis, "", r, "ratio",
                 "= rf + β_MKT×premium_MKT + β_SMB×premium_SMB + β_HML×premium_HML", [rf_id, *ids.values()],
                 note=(f"95% range from beta uncertainty only: {lo:.3f} to {hi:.3f} (premia uncertainty not included). {note_proxy}"
                       if r is not None else "Risk-free rate missing; not computed."), **common)
    for h, v in imp.items():
        w.put(f"Factor-implied Price ({h} start)", basis, "", v, "JPY/share",
              "= current price × predicted / actual cumulative total return since the start date", [*ids.values()],
              note="" if v else "Not enough history for this start date.", **common)
    fip_id = w.put("Factor-implied Price", basis, "", fip, "JPY/share",
                   "= mean of the factor-implied prices for 1, 2, 3 and 5-year start dates", [*ids.values()],
                   note=(f"Price {p_last} on {d_last}; starts used: {', '.join(k for k, v in imp.items() if v)}. "
                         "Enters the final valuation only through the consensus (A29)."), **common)
    ri = _bps_and_roe(sec)
    riv, ri_note = None, ""
    if ri and r and ri.get("roe_norm") is not None and r > 0:
        riv = ri["bps"] + (ri["roe_norm"] - r) * ri["bps"] / r
        ri_note = (f"BPS {ri['bps']} ({ri['bps_period']}), normalised ROE {ri['roe_norm']:.4f} "
                   f"(= {ri['years']}-year average EPS / average BPS), r {r:.4f}. Reference only (A29).")
    w.put("Residual Income Value (reference)", basis, "", riv, "JPY/share",
          "= BPS + (normalised ROE − r) × BPS / r (no-growth residual income)",
          [r_id, ri.get("bps_row", {}).get("record_id", "")] if ri else [r_id],
          url="https://disclosure2.edinet-fsa.go.jp/", title="derived", location="",
          source_type="derived", key_extra=today, note=ri_note or "Inputs missing (r, BPS or EPS history).")
    return {**res, "price": p_last, "price_date": d_last, "estimated_this_run": stale,
            "model": {"beta": {k: round(v, 3) for k, v in fm["beta"].items()},
                      "beta_se": {k: round(v, 3) for k, v in fm["beta_se"].items()}, "r2": round(fm["r2"], 3),
                      "n_weeks": fm["n_weeks"], "alpha_annual": round(fm["alpha_annual"], 4),
                      "premia": {k: round(v, 4) for k, v in fm["premia"].items()}},
            "risk_free": rf_now, "cost_of_equity": round(r, 4) if r is not None else None,
            "cost_of_equity_range_betas_only": [round(lo, 4), round(hi, 4)] if lo is not None else None,
            "factor_implied_price": round(fip, 2) if fip else None,
            "factor_implied_by_start": {k: (round(v, 2) if v else None) for k, v in imp.items()},
            "residual_income_value_reference": round(riv, 2) if riv else None,
            "notes": [note_proxy]}
