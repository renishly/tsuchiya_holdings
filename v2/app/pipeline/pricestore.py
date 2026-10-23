"""Shared daily price store (Addendum A28; the factor model that also used it was removed in v2.005, A36).

  - raw/market/daily_prices.csv: daily close and dividends for the tickers in STORE_SYMBOLS only.
  - Updated at most once per JST day, after the TSE close (15:30), in ONE yfinance batch; the first fill
    downloads the history once. No other Yahoo data, no retries around a block.
  - The baseline's share prices (market.yf_prices), the own-history band (M2) and later the M1 panel read it.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import math

from lib import ROOT, log, now, run_ctx, write_text

STORE_FILE = "raw/market/daily_prices.csv"
STORE_SYMBOLS = {  # ticker -> Yahoo symbol (A28 scope; the M1 universe extends it, A37)
    "1840": "1840.T", "1873": "1873.T", "1419": "1419.T", "8999": "8999.T",
}
HISTORY_START = "2016-10-01"
UPDATE_AFTER = (15, 30)      # JST: the daily batch runs at the first run after the TSE close
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


_MODE = {"mode": None}


def run_ctx_mode():
    return _MODE["mode"]


def _fixture(name):
    return f"evals/market/{name}"   # frozen copies for Mode: test (the test sandbox copies evals/market)


def symbols(st=None) -> dict[str, str]:
    """Tickers in the daily batch: Tsuchiya and peers, plus the M1 universe once it is screened (A39)."""
    out = dict(STORE_SYMBOLS)
    if st is not None:
        try:
            import universe
            out.update({t: f"{t}.T" for t in universe.members(st)})
        except Exception:
            pass
    return out


def latest_close(ticker: str) -> dict | None:
    """Latest stored close for the baseline (no network). None if the store has nothing for it."""
    sym = STORE_SYMBOLS.get(ticker) or f"{ticker}.T"
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
    missing = [s for s in symbols(st).values() if s not in store]   # a new universe member: fetch its history now
    if not empty and not missing and not _due(st, "price_store"):
        return {"fetched": False, "reason": "already updated today, or before 15:30 JST",
                "last_date": max(v[-1][0] for v in store.values())}
    if cr.get("price_store_tried"):
        return {"fetched": False, "reason": "already tried this run"}
    cr["price_store_tried"] = True
    syms_map = symbols(st)
    starts = {s: (HISTORY_START if s not in store else
                  (dt.date.fromisoformat(store[s][-1][0]) - dt.timedelta(days=10)).isoformat())
              for s in syms_map.values()}
    start = min(starts.values())
    try:
        import yfinance as yf
        syms = list(syms_map.values())
        df = yf.download(syms, start=start, interval="1d", progress=False, auto_adjust=False, actions=True,
                         group_by="ticker", threads=False)
    except Exception as e:  # Yahoo block or rate limit: keep the store as it is, no workaround (A16/A28)
        log(st, "yfinance", {"symbols": len(syms_map)}, f"unavailable: {type(e).__name__}: {str(e)[:120]}")
        return {"fetched": False, "error": f"yfinance unavailable: {type(e).__name__}"}
    added, refetch = 0, []
    for sym in syms_map.values():
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
    log(st, "yfinance", {"symbols": len(syms_map), "start": start}, f"{added} new daily rows; re-based: {refetch}")
    return {"fetched": True, "new_rows": added, "rebased": refetch}
