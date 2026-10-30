"""Fitting Md's measured variables (Addendum A53, v2.014+). Code only; no model.

Md (operating profit, yen) = Discount Baseline + Σ coefficient × (change since the guidance date, %) × months left / 12
+ Debate Signal (live mode). A positive Md lowers the company's forecast. This module fits the coefficients on the
M1 universe in the weekly research run:
  orders   y = change in operating profit in year t ÷ revenue in t−1;  x = growth of order intake in year t
           (受注実績 in each 有報; a value is used only if orders are 30–300% of revenue that year)
  supply   same y;  x = change of the BOJ lumber and wood products index over fiscal year t (one x per month-end year,
           shared by all companies, so the t-statistic is computed from the year averages)
  land     not fitted while no land-price series exists (REINFOLIB_API_KEY not set)
  trends   share of search: logged with coefficient 0 (too few years and too small for Tsuchiya to fit)
A slope b (operating profit per unit of x, as a share of revenue) becomes an Md coefficient in yen per 1% change:
coef = −b × Tsuchiya's latest revenue × 0.01. Acceptance (fixed in advance, as for the M1 weights): expected sign
(orders b > 0, supply b < 0), |t| ≥ 2, at least 15 observations. The student approved accepted coefficients in
advance on 30 Oct 2026 (STANDING_APPROVAL); a rejected coefficient stays 0.
"""
from __future__ import annotations

import csv
import datetime as dt
import math
import statistics
from pathlib import Path

from lib import ROOT, iso, log

STANDING_APPROVAL = {"date": "2026-10-30", "by": "student", "scope": ["orders", "supply", "land"],
                     "rule": "applied automatically when accepted (expected sign, |t| ≥ 2, n ≥ 15); otherwise 0"}
MIN_N = 15


def _ols(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    a = my - b * mx
    resid = [y - a - b * x for x, y in zip(xs, ys)]
    s2 = sum(r * r for r in resid) / (n - 2) if n > 2 else float("nan")
    se = math.sqrt(s2 / sxx) if s2 == s2 and sxx else float("nan")
    return {"b": b, "a": a, "t": b / se if se else float("nan"), "n": n}


def _fundamentals() -> dict:
    F = {}
    p = ROOT / "data" / "universe_fundamentals.csv"
    if not p.exists():
        return F
    for r in csv.DictReader(open(p, encoding="utf-8")):
        if r["item"] in ("revenue", "operating_profit"):
            F.setdefault(r["ticker"], {}).setdefault(r["fy_end"], {})[r["item"]] = float(r["value"])
    return F


def _orders_panel(st, members: list[str]) -> dict:
    """{ticker: {fy_end: orders}} from every cached 有報 of the universe members."""
    import edinet
    import marketing
    docs = st["documents"].get("universe", {}).get("docs", {})
    out = {}
    for t in members:
        for d in (docs.get(t) or {}).values():
            z = ROOT / "raw" / "edinet" / d["docID"] / f"{d['docID']}_type5.csv.zip"
            if not z.exists():
                continue
            facts = edinet.read_xbrl_csv(z)
            text = next((x["value"] for x in facts if x["element"].endswith(
                "ManagementAnalysisOfFinancialPositionOperatingResultsAndCashFlowsTextBlock")), "")
            o = marketing.orders_from_text(t, text) if t in marketing.ORDER_ROWS or t == "1873" else marketing.orders_generic(text)
            row = next((x for k, x in o.items() if k in ("住宅事業", "小計", "合計") and x.get("orders")), None) or \
                next((x for x in o.values() if x.get("orders")), None)
            if row:
                out.setdefault(t, {})[d["periodEnd"]] = row
    return out


def _prev_fy(fy_end: str, years: dict) -> str | None:
    d = dt.date.fromisoformat(fy_end)
    return next((k for k in years if abs((d - dt.date.fromisoformat(k)).days - 365) <= 20), None)


def fit_orders(st, members: list[str], F: dict) -> dict:
    orders = _orders_panel(st, members)
    xs, ys, used = [], [], set()
    for t, by in orders.items():
        f = F.get(t, {})
        for fy, row in by.items():
            prev = _prev_fy(fy, f)
            if not prev or fy not in f:
                continue
            rev, rev0 = f[fy].get("revenue"), f[prev].get("revenue")
            op, op0 = f[fy].get("operating_profit"), f[prev].get("operating_profit")
            if not all((rev, rev0)) or op is None or op0 is None:
                continue
            if not 0.3 <= row["orders"] / rev <= 3.0:   # misread units or a partial segment
                continue
            g = row.get("growth")   # the year-on-year change printed in the table; else from two filings
            if g is None and prev in by and by[prev].get("orders"):
                g = row["orders"] / by[prev]["orders"] - 1
            if g is None or abs(g) > 1.5:
                continue
            xs.append(g)
            ys.append((op - op0) / rev0)
            used.add(t)
    r = _ols(xs, ys) or {}
    r.update(companies=len(used), n=len(xs), x="order intake growth in year t",
             y="operating-profit change in year t ÷ revenue in t−1", expected_sign="+")
    return r


def _lumber_change(series: dict, fy_end: str) -> float | None:
    d = dt.date.fromisoformat(fy_end)
    end = f"{d.year}{d.month:02d}"
    start = f"{d.year - 1}{d.month:02d}"
    if end in series and start in series and series[start]:
        return series[end] / series[start] - 1
    return None


def fit_supply(st, members: list[str], F: dict) -> dict:
    series = ((st["documents"].get("md_series") or {}).get("lumber") or {}).get("series") or {}
    by_year: dict[int, list] = {}
    xs_all, ys_all = [], []
    for t in members:
        f = F.get(t, {})
        for fy, v in f.items():
            prev = _prev_fy(fy, f)
            x = _lumber_change(series, fy)
            if x is None or not prev or v.get("operating_profit") is None or f[prev].get("operating_profit") is None or not f[prev].get("revenue"):
                continue
            y = (v["operating_profit"] - f[prev]["operating_profit"]) / f[prev]["revenue"]
            xs_all.append(x)
            ys_all.append(y)
            by_year.setdefault(int(fy[:4]) * 100 + int(fy[5:7]), []).append((x, y))
    # x is shared within a fiscal-year end: the honest t uses one point per year-end (mean y)
    pts = [(v[0][0], statistics.mean(y for _, y in v)) for v in by_year.values() if v]
    r_years = _ols([p[0] for p in pts], [p[1] for p in pts]) or {}
    r_all = _ols(xs_all, ys_all) or {}
    return {"b": r_years.get("b"), "t": r_years.get("t"), "n": len(xs_all), "year_points": len(pts),
            "pooled_t": r_all.get("t"), "x": "BOJ lumber index change over fiscal year t",
            "y": "operating-profit change in year t ÷ revenue in t−1", "expected_sign": "−",
            "series_from": min(series) if series else None}


def fit(st) -> dict:
    """All Md coefficients for one research run, with acceptance and the standing approval applied."""
    import universe
    members = universe.members(st)
    F = _fundamentals()
    rev = (sorted(F.get("1840", {}).items())[-1][1].get("revenue") if F.get("1840") else None)
    res = {"fitted": iso(), "tsuchiya_revenue": rev, "standing_approval": STANDING_APPROVAL, "coef": {}, "terms": {}}
    fits = {"orders": fit_orders(st, members, F), "supply": fit_supply(st, members, F),
            "land": {"note": "not fitted: no land-price series (set the REINFOLIB_API_KEY secret to collect it)"},
            "trends": {"note": "logged with coefficient 0: Trends has too few years and Tsuchiya's values are near zero"}}
    for k, r in fits.items():
        b, t, n = r.get("b"), r.get("t"), r.get("n", 0)
        sign_ok = b is not None and ((k == "orders" and b > 0) or (k == "supply" and b < 0))
        accepted = bool(sign_ok and t is not None and t == t and abs(t) >= 2 and n >= MIN_N and rev)
        coef = round(-b * rev * 0.01) if accepted else 0.0
        reason = ("accepted: expected sign, |t| ≥ 2, n ≥ 15; applied under the standing approval of 30 Oct 2026" if accepted else
                  r.get("note") or ("rejected: " + ", ".join(x for x, ok in (("unexpected sign", sign_ok),
                                                                            (f"|t| = {abs(t):.2f} < 2" if t == t and t is not None else "no t", t is not None and t == t and abs(t) >= 2),
                                                                            (f"n = {n} < {MIN_N}", n >= MIN_N)) if not ok)))
        res["terms"][k] = {**{kk: (round(v, 5) if isinstance(v, float) else v) for kk, v in r.items()},
                           "accepted": accepted, "coef_yen_per_pct": coef, "reason": reason}
        res["coef"][k] = coef
    log(st, "mdfit", {}, {k: (v["accepted"], v.get("t") if isinstance(v.get("t"), (int, float)) else None) for k, v in res["terms"].items()})
    return res
