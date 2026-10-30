"""M1 statistical comparables, the panel backtest and proposed weights (Addenda A39-A40, v2.006).

Panel: one observation per company and fiscal year, dated when that year's annual figures became public
(universe.read_fundamentals: filing date, or fiscal year end + 3 months). Price = last close on or before that date.

M1 (P/B equation, the one that can always be used for Tsuchiya):
    log(P/B) = a_year + b1·ROE_norm + b2·growth + b3·log(revenue) + e
  ROE_norm = mean ROE of the last three fiscal years; growth = 3-year revenue growth rate (a year);
  size = log revenue (no price inside a regressor). Regressors winsorised at 5/95%; Huber regression
  (IRLS, c = 1.345 × robust scale); a_year = calendar-year effect. The EV/EBITDA equation needs full statements
  for every year and comes later. Tsuchiya's fitted P/B uses the latest year effect and its latest regressors;
  the interval is the fitted value × exp(±1.2816 × residual scale) (80%).

Backtest (rules fixed before the first run; Addendum A40):
  month-ends from the first month with a usable M1 fit to 12 months before the last price;
  at each month-end, only figures published by then; M1 refitted on observations dated before the month-end;
  signals = value ÷ price − 1 for: M1 (P/B regression), own-history P/B (median past year-end P/B × BPS),
  peer median P/B (universe median × BPS), peer median trailing P/E (universe median × EPS > 0);
  outcome = 12-month total return (price change + dividends);
  measures per method: mean monthly rank IC (Spearman) with a Newey-West t (11 lags), top-minus-bottom quintile
  return, hit rate (relative to the month's median), median absolute value error vs the price 12 months later.
  Acceptance: mean IC > 0 and t ≥ 2. Proposed weight = ½ × equal share + ½ × share by accepted IC.
Nothing here changes the live valuation: the result is written to state/model_params.json as "pending approval"
and an approval request is queued; valuation.py uses it only after the student approves.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import statistics

import numpy as np

import pricestore
import universe
from lib import ROOT, iso, log, queue_approval, approved, consume, write_text

PARAMS = "state/model_params.json"
TAX = 0.304                                                    # statutory effective rate used for clean ROE
REGRESSORS = ["roe_norm", "growth", "log_revenue"]            # P/B equation
EV_REGRESSORS = ["margin", "growth", "log_revenue"]           # EV/EBITDA equation
MIN_OBS, MIN_IC_N, Z80 = 30, 6, 1.2816


# ---------------------------------------------------------------- data
def _fund_by_ticker() -> dict:
    out: dict = {}
    for r in universe.read_fundamentals():
        out.setdefault(r["ticker"], {}).setdefault(r["fy_end"], {})[r["item"]] = (r["value"], r["published"])
    return out


def _prices() -> dict:
    store = pricestore.load_store()
    return {s.split(".")[0]: v for s, v in store.items()}


def _close_on(rows, day: str):
    lo, hi, best = 0, len(rows) - 1, None
    while lo <= hi:  # rows sorted by date: last close on or before day
        mid = (lo + hi) // 2
        if rows[mid][0] <= day:
            best, lo = rows[mid], mid + 1
        else:
            hi = mid - 1
    return best


def _features(fy: dict, years: list[str], i: int) -> dict | None:
    """Regressors for fiscal year years[i] from that year and earlier ones (published no later than it)."""
    cur = fy[years[i]]
    if not all(k in cur for k in ("bps", "revenue")) or cur["bps"][0] <= 0 or cur["revenue"][0] <= 0:
        return None
    roes = []
    for y in years[max(0, i - 2): i + 1]:
        # v2.008: clean ROE = ordinary profit × (1 − 30.4%) ÷ net assets, so one-off gains and losses and unusual
        # tax charges drop out (like F3); reported ROE (or net income ÷ net assets) only if ordinary profit is missing
        g = fy[y]
        if "ordinary_profit" in g and g.get("net_assets", (0,))[0] > 0:
            roes.append(g["ordinary_profit"][0] * (1 - TAX) / g["net_assets"][0])
        elif "roe" in g:
            roes.append(g["roe"][0])
        elif "net_income" in g and g.get("net_assets", (0,))[0] > 0:
            roes.append(g["net_income"][0] / g["net_assets"][0])
    if not roes:
        return None
    growth = None
    if i >= 3 and "revenue" in fy[years[i - 3]] and fy[years[i - 3]]["revenue"][0] > 0:
        growth = (cur["revenue"][0] / fy[years[i - 3]]["revenue"][0]) ** (1 / 3) - 1
    elif i >= 1 and "revenue" in fy[years[i - 1]] and fy[years[i - 1]]["revenue"][0] > 0:
        growth = cur["revenue"][0] / fy[years[i - 1]]["revenue"][0] - 1
    if growth is None:
        return None
    pub = max(v[1] for v in cur.values())
    out = {"fy_end": years[i], "published": pub, "bps": cur["bps"][0], "eps": cur.get("eps", (None,))[0],
           "roe_norm": statistics.mean(roes), "growth": growth, "log_revenue": math.log(cur["revenue"][0])}
    # EV/EBITDA inputs (full statements; v2.006): shares = net assets ÷ BPS (no treasury history needed)
    if all(k in cur for k in ("operating_profit", "depreciation", "debt", "cash", "net_assets")):
        margins = [fy[y]["operating_profit"][0] / fy[y]["revenue"][0] for y in years[max(0, i - 2): i + 1]
                   if "operating_profit" in fy[y] and fy[y].get("revenue", (0,))[0] > 0]
        eb3 = [fy[y]["operating_profit"][0] + fy[y]["depreciation"][0] for y in years[max(0, i - 2): i + 1]
               if "operating_profit" in fy[y] and "depreciation" in fy[y]]
        out.update(ebitda=cur["operating_profit"][0] + cur["depreciation"][0], debt=cur["debt"][0], cash=cur["cash"][0],
                   shares=cur["net_assets"][0] / cur["bps"][0], margin=statistics.mean(margins) if margins else None,
                   ebitda3=statistics.mean(eb3) if len(eb3) >= 2 else None)
    return out


def panel(members: list[str] | None = None) -> list[dict]:
    """One row per company and fiscal year with regressors, the price on the publication date and log(P/B).
    Only universe members (screened) when a member list is given."""
    F, P = _fund_by_ticker(), _prices()
    rows = []
    for t, fy in F.items():
        if members is not None and t not in members:
            continue
        years = sorted(fy)
        for i in range(len(years)):
            f = _features(fy, years, i)
            px = _close_on(P.get(t, []), f["published"]) if f else None
            if not f or not px or px[0] < (dt.date.fromisoformat(f["published"]) - dt.timedelta(days=10)).isoformat():
                continue
            row = {"ticker": t, **f, "price": px[1], "date": px[0], "year": int(f["published"][:4]),
                   "log_pb": math.log(px[1] / f["bps"])}
            # v2.008: log(EV ÷ 3-year average EBITDA); years where EBITDA dipped below half its 3-year average are left
            # out, because a temporary dip inflates the multiple without saying anything about value
            if f.get("ebitda3") and f["ebitda3"] > 0 and f.get("margin") is not None and f["ebitda"] >= 0.5 * f["ebitda3"]:
                ev = px[1] * f["shares"] + f["debt"] - f["cash"]
                if ev > 0:
                    row["log_ev_ebitda"] = math.log(ev / f["ebitda3"])
            rows.append(row)
    return rows


# ---------------------------------------------------------------- Huber regression with year effects
def _winsor(x: np.ndarray, lo, hi) -> np.ndarray:
    return np.clip(x, lo, hi)


def fit(rows: list[dict], y_key: str = "log_pb", regs: list[str] = REGRESSORS) -> dict | None:
    rows = [r for r in rows if r.get(y_key) is not None and all(r.get(k) is not None for k in regs)]
    if len(rows) < MIN_OBS:
        return None
    X = np.array([[r[k] for k in regs] for r in rows], float)
    q = {k: (float(np.percentile(X[:, j], 5)), float(np.percentile(X[:, j], 95))) for j, k in enumerate(regs)}
    for j, k in enumerate(regs):
        X[:, j] = _winsor(X[:, j], *q[k])
    years = sorted({r["year"] for r in rows})
    D = np.array([[1.0 if r["year"] == y else 0.0 for y in years] for r in rows])
    A = np.hstack([D, X])
    y = np.array([r[y_key] for r in rows])
    w = np.ones(len(y))
    beta = np.linalg.lstsq(A, y, rcond=None)[0]
    for _ in range(50):  # IRLS with Huber weights
        res = y - A @ beta
        s = 1.4826 * float(np.median(np.abs(res - np.median(res)))) or 1e-6
        u = np.abs(res) / (1.345 * s)
        w = np.where(u <= 1, 1.0, 1.0 / np.maximum(u, 1e-12))
        W = np.sqrt(w)[:, None]
        nb = np.linalg.lstsq(A * W, y * W[:, 0], rcond=None)[0]
        if np.max(np.abs(nb - beta)) < 1e-8:
            beta = nb
            break
        beta = nb
    res = y - A @ beta
    scale = 1.4826 * float(np.median(np.abs(res - np.median(res))))
    counts = {y_: sum(1 for r in rows if r["year"] == y_) for y_ in years}
    return {"year_effects": {str(y_): float(b) for y_, b in zip(years, beta[:len(years)])},
            "year_counts": {str(k): v for k, v in counts.items()},
            "coef": {k: float(b) for k, b in zip(regs, beta[len(years):])}, "y": y_key,
            "winsor": q, "scale": scale, "n": len(rows), "r2_weighted": _r2(y, A @ beta, w)}


def _r2(y, yhat, w) -> float:
    ybar = float(np.average(y, weights=w))
    return 1 - float(np.sum(w * (y - yhat) ** 2)) / float(np.sum(w * (y - ybar) ** 2) or 1)


def predict(model: dict, feats: dict) -> dict:
    """Fitted P/B for one company: the latest year effect with at least 5 observations (else the last one)."""
    ye = model["year_effects"]
    good = [y for y in sorted(ye) if model["year_counts"].get(y, 0) >= 5] or sorted(ye)
    a = ye[good[-1]]
    if any(feats.get(k) is None for k in model["coef"]):
        return None
    xb = sum(c * min(max(feats[k], model["winsor"][k][0]), model["winsor"][k][1]) for k, c in model["coef"].items())
    y = model.get("y", "log_pb")
    v = math.exp(a + xb) if y.startswith("log_") else a + xb
    lo, hi = (v * math.exp(-Z80 * model["scale"]), v * math.exp(Z80 * model["scale"])) if y.startswith("log_") else \
             (v - Z80 * model["scale"], v + Z80 * model["scale"])
    key = "pb" if y == "log_pb" else "multiple"
    return {key: v, "low": lo, "high": hi, "year_effect": good[-1]}


def latest_features(ticker: str) -> dict | None:
    F = _fund_by_ticker().get(ticker)
    if not F:
        return None
    years = sorted(F)
    for i in range(len(years) - 1, -1, -1):
        f = _features(F, years, i)
        if f:
            return f
    return None


# ---------------------------------------------------------------- backtest
def _month_ends(start: str, end: str) -> list[str]:
    d = dt.date.fromisoformat(start[:7] + "-01")
    out = []
    while True:
        nxt = (d.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        me = nxt - dt.timedelta(days=1)
        if me.isoformat() > end:
            return out
        out.append(me.isoformat())
        d = nxt


def _rank(x: list[float]) -> list[float]:
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def _spearman(a, b) -> float | None:
    if len(a) < MIN_IC_N:
        return None
    ra, rb = np.array(_rank(a)), np.array(_rank(b))
    if ra.std() == 0 or rb.std() == 0:
        return None
    return float(np.corrcoef(ra, rb)[0, 1])


def _nw_t(x: list[float], lags: int = 11) -> float | None:
    n = len(x)
    if n < 12:
        return None
    a = np.array(x) - np.mean(x)
    g0 = float(a @ a) / n
    v = g0 + 2 * sum((1 - L / (lags + 1)) * float(a[L:] @ a[:-L]) / n for L in range(1, min(lags, n - 1) + 1))
    return float(np.mean(x)) / math.sqrt(v / n) if v > 0 else None


def _t_plain(x: list[float]) -> float | None:
    if len(x) < 3:
        return None
    sd = statistics.stdev(x)
    return statistics.mean(x) / (sd / math.sqrt(len(x))) if sd > 0 else None


def backtest(rows: list[dict] | None = None, freq: str = "annual") -> dict:
    """freq "annual": one cross-section a year (end of July, after the main filing season), 12-month returns that do
    not overlap, plain t. freq "monthly": every month-end, overlapping returns, Newey-West t (reference only)."""
    rows = rows if rows is not None else panel()
    P = _prices()
    F = {t: v for t, v in _fund_by_ticker().items() if t in {r["ticker"] for r in rows}}
    if not rows:
        return {"error": "no panel rows yet"}
    last_px = max(v[-1][0] for v in P.values() if v)
    first = min(r["published"] for r in rows)
    end = (dt.date.fromisoformat(last_px) - dt.timedelta(days=366)).isoformat()
    methods = ["M1 (P/B regression)", "P/B (own history)", "P/B (peers)", "P/E (peers, trailing)",
               "M1 (EV/EBITDA regression)", "EV/EBITDA (own history)", "EV/EBITDA (peers)"]
    ics = {m: [] for m in methods}
    spread = {m: [] for m in methods}
    hits = {m: [] for m in methods}
    errs = {m: [] for m in methods}
    months = 0
    dates = _month_ends(first, end)
    if freq == "annual":
        dates = [d for d in dates if d[5:7] == "07"]
    for me in dates:
        train = [r for r in rows if r["date"] < me]
        enough = len(train) >= MIN_OBS and len({r["year"] for r in train}) >= 3
        model = fit(train) if enough else None
        model_ev = fit(train, "log_ev_ebitda", EV_REGRESSORS) if enough else None
        later = (dt.date.fromisoformat(me) + dt.timedelta(days=365)).isoformat()
        snap = []
        for t, fy in F.items():
            years = [y for y in sorted(fy) if max(v[1] for v in fy[y].values()) <= me]
            if not years:
                continue
            f = _features(fy, years, len(years) - 1)
            p0, p1 = _close_on(P.get(t, []), me), _close_on(P.get(t, []), later)
            if not f or not p0 or not p1 or p0[0] < (dt.date.fromisoformat(me) - dt.timedelta(days=10)).isoformat():
                continue
            divs = sum(dv for d, _, dv in P[t] if me < d <= later)
            own, own_ev = [], []
            for y in years:
                b = fy[y].get("bps")
                c = _close_on(P.get(t, []), y)
                if b and b[0] > 0 and c and c[0] >= (dt.date.fromisoformat(y) - dt.timedelta(days=10)).isoformat():
                    own.append(c[1] / b[0])
                    g = fy[y]
                    if all(k in g for k in ("operating_profit", "depreciation", "debt", "cash", "net_assets")):
                        eb = g["operating_profit"][0] + g["depreciation"][0]
                        ev = c[1] * g["net_assets"][0] / b[0] + g["debt"][0] - g["cash"][0]
                        if eb > 0 and ev > 0:
                            own_ev.append(ev / eb)
            ev_now = (p0[1] * f["shares"] + f["debt"] - f["cash"]) if f.get("ebitda") else None
            snap.append({"t": t, "f": f, "price": p0[1], "ret": (p1[1] + divs) / p0[1] - 1, "p_later": p1[1],
                         "own": statistics.median(own) if len(own) >= 3 else None,
                         "own_ev": statistics.median(own_ev) if len(own_ev) >= 3 else None,
                         "ev_mult": ev_now / f["ebitda"] if ev_now and f.get("ebitda", 0) > 0 and ev_now > 0 else None})
        if len(snap) < MIN_IC_N:
            continue
        months += 1
        med_pb = statistics.median(s["price"] / s["f"]["bps"] for s in snap)
        pes = [s["price"] / s["f"]["eps"] for s in snap if s["f"]["eps"] and s["f"]["eps"] > 0]
        med_pe = statistics.median(pes) if pes else None
        evm = [s["ev_mult"] for s in snap if s["ev_mult"] and s["ev_mult"] < 60]
        med_ev = statistics.median(evm) if len(evm) >= MIN_IC_N else None
        def per_share(mult, f):  # equity value per share from an EV/EBITDA multiple
            return (mult * f["ebitda"] - f["debt"] + f["cash"]) / f["shares"] if f.get("ebitda", 0) > 0 and f.get("shares") else None
        for s in snap:
            v = {}
            if model:
                v["M1 (P/B regression)"] = predict(model, s["f"])["pb"] * s["f"]["bps"]
            if s["own"]:
                v["P/B (own history)"] = s["own"] * s["f"]["bps"]
            v["P/B (peers)"] = med_pb * s["f"]["bps"]
            if med_pe and s["f"]["eps"] and s["f"]["eps"] > 0:
                v["P/E (peers, trailing)"] = med_pe * s["f"]["eps"]
            if model_ev and s["f"].get("margin") is not None and s["f"].get("ebitda3"):
                pr = predict(model_ev, s["f"])   # multiple on 3-year average EBITDA, applied to it
                ps = ((pr["multiple"] * s["f"]["ebitda3"] - s["f"]["debt"] + s["f"]["cash"]) / s["f"]["shares"]) if pr else None
                if ps and ps > 0:
                    v["M1 (EV/EBITDA regression)"] = ps
            if s["own_ev"]:
                ps = per_share(s["own_ev"], s["f"])
                if ps and ps > 0:
                    v["EV/EBITDA (own history)"] = ps
            if med_ev:
                ps = per_share(med_ev, s["f"])
                if ps and ps > 0:
                    v["EV/EBITDA (peers)"] = ps
            s["v"] = v
        for m in methods:
            pts = [(s["v"][m] / s["price"] - 1, s["ret"], abs(s["v"][m] - s["p_later"]) / s["p_later"]) for s in snap if m in s["v"]]
            if len(pts) < MIN_IC_N:
                continue
            gaps, rets, e = [p[0] for p in pts], [p[1] for p in pts], [p[2] for p in pts]
            ic = _spearman(gaps, rets)
            if ic is not None:
                ics[m].append(ic)
            k = max(1, len(pts) // 5)
            order = sorted(range(len(pts)), key=lambda i: gaps[i])
            spread[m].append(statistics.mean(rets[i] for i in order[-k:]) - statistics.mean(rets[i] for i in order[:k]))
            mg, mr = statistics.median(gaps), statistics.median(rets)
            hits[m].append(statistics.mean(1.0 if (g - mg) * (r - mr) > 0 else 0.0 for g, r in zip(gaps, rets)))
            errs[m].extend(e)
    res = {"freq": freq, "months": months, "period": [first[:7], end[:7]], "methods": {}}
    tf = _t_plain if freq == "annual" else _nw_t
    for m in methods:
        x = ics[m]
        t = tf(x)
        res["methods"][m] = {"months": len(x), "mean_ic": round(statistics.mean(x), 4) if x else None,
                             "t": round(t, 2) if t is not None else None,
                             "top_minus_bottom": round(statistics.mean(spread[m]), 4) if spread[m] else None,
                             "hit_rate": round(statistics.mean(hits[m]), 3) if hits[m] else None,
                             "median_abs_error": round(statistics.median(errs[m]), 3) if errs[m] else None}
    return res


# ---------------------------------------------------------------- weights and parameters
LIVE = {"M1 (P/B regression)": "M1 (P/B regression)", "P/B (own history)": "P/B (own history)",
        "P/B (peers)": "P/B (peers)", "P/E (peers, trailing)": "P/E", "M1 (EV/EBITDA regression)": "M1 (EV/EBITDA regression)",
        "EV/EBITDA (own history)": "EV/EBITDA (own history)", "EV/EBITDA (peers)": "EV/EBITDA"}
UNTESTED: list[str] = []


def propose_weights(bt: dict) -> dict:
    tested = {LIVE[m]: r for m, r in bt["methods"].items()}
    accepted = {k: r["mean_ic"] for k, r in tested.items() if r["mean_ic"] and r["mean_ic"] > 0 and (r["t"] or 0) >= 2}
    names = list(tested) + UNTESTED
    eq = 1 / len(names)
    tot = sum(accepted.values())
    w = {k: 0.5 * eq + 0.5 * (accepted.get(k, 0) / tot if tot else eq) for k in names}
    s = sum(w.values())
    return {"weights": {k: round(v / s, 4) for k, v in w.items()}, "accepted": sorted(accepted),
            "rule": "accepted if mean IC > 0 and t ≥ 2 in the annual backtest (non-overlapping years); weight = ½ equal share + ½ share by accepted IC "
                    "(equal if none accepted). The live P/E and EV/EBITDA methods use forward figures; their backtest uses "
                    "trailing figures (no point-in-time forecast history)."}


def load_params() -> dict:
    p = ROOT / PARAMS
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def live_params(st) -> dict | None:
    """The approved parameters, if any. An approval of the pending version promotes it (consumed once)."""
    p = load_params()
    pend = p.get("pending")
    if pend:
        ar = approved(st, "model_params", {"version": pend["version"]})
        if ar:
            consume(st, ar)
            p["approved"] = {**pend, "status": "approved", "approval": ar, "approved_at": iso()}
            p.pop("pending")
            write_text(PARAMS, json.dumps(p, ensure_ascii=False, indent=1))
            write_text("reports/backtest_latest.html", report_html(p["approved"], ar))   # the report shows the new status
            log(st, "model_params", {"version": pend["version"]}, f"approved ({ar})")
    return p.get("approved")


def research(st) -> dict:
    """Fit M1 on the full panel, run the backtest, propose weights; store as pending and queue an approval."""
    import mdfit   # A53: Md's measured variables are fitted every research run; accepted ones apply at once (standing approval)
    p0 = load_params()
    p0["md"] = mdfit.fit(st)
    write_text(PARAMS, json.dumps(p0, ensure_ascii=False, indent=1))
    rows = panel(universe.members(st))
    model = fit(rows)
    model_ev = fit(rows, "log_ev_ebitda", EV_REGRESSORS)
    if not model:
        return {"panel_rows": len(rows), "fit": f"not enough observations ({len(rows)} < {MIN_OBS})"}
    bt = backtest(rows, "annual")              # decides acceptance and weights (v2.008)
    bt["monthly_reference"] = backtest(rows, "monthly")["methods"]
    wts = propose_weights(bt) if "methods" in bt else {}
    feats = latest_features("1840")
    tsu = predict(model, feats) if feats else None
    tsu_ev = predict(model_ev, feats) if feats and model_ev else None
    version = iso()[:10] + f"-n{model['n']}"
    p = load_params()
    p["pending"] = {"version": version, "status": "pending approval", "m1": model, "m1_ev": model_ev, "backtest": bt,
                    "weights": wts, "tsuchiya": {"features": feats, "fitted_pb": tsu, "fitted_ev": tsu_ev}, "created": iso(),
                    "screen": universe._state(st)["screen"],
                    "panel": {"rows": len(rows), "companies": len({r["ticker"] for r in rows})}}
    write_text(PARAMS, json.dumps(p, ensure_ascii=False, indent=1))
    import lib
    for r in lib.pending_requests():  # an older research run's request is superseded by this one (only the newest can be approved)
        if r["kind"] == "model_params" and r["id"] not in st["consumed_approvals"] and r["params"].get("version") != version:
            consume(st, r["id"])
            log(st, "research", {"approval": r["id"]}, f"superseded by {version}")
    ar = queue_approval(st, "model_params", "Use the new M1 coefficients and method weights in the daily valuation",
                        PARAMS, {"version": version},
                        "Research run: see reports/backtest_latest.html for the backtest, the M1 fit and the proposed weights.")
    write_text("reports/backtest_latest.html", report_html(p["pending"], ar))
    log(st, "research", {"version": version}, {"n": model["n"], "accepted": wts.get("accepted")})
    return {"version": version, "approval": ar, "panel_rows": len(rows), "m1": {k: round(v, 4) for k, v in model["coef"].items()},
            "m1_ev": {k: round(v, 4) for k, v in model_ev["coef"].items()} if model_ev else "not enough EV/EBITDA rows",
            "tsuchiya_fitted_pb": round(tsu["pb"], 3) if tsu else None, "backtest_months": bt.get("months"),
            "weights": wts.get("weights"), "accepted": wts.get("accepted")}


def _ev_section(p: dict) -> str:
    me = p.get("m1_ev")
    if not me:
        return "<h2>M1: EV/EBITDA equation</h2><p>Not fitted: not enough company-years with full statements yet.</p>"
    fe = (p.get("tsuchiya") or {}).get("fitted_ev") or {}
    return ("<h2>M1: log(EV ÷ 3-year average EBITDA) = year effect + c1·margin + c2·growth + c3·log(revenue)</h2><table><tr><th>Term</th><th>Coefficient</th></tr>"
            + "".join(f"<tr><td>{k}</td><td>{v:.4f}</td></tr>" for k, v in me["coef"].items())
            + f"</table><p>n = {me['n']}; robust residual scale {me['scale']:.3f}; weighted R² {me['r2_weighted']:.3f}. "
              f"Tsuchiya fitted EV/EBITDA {fe.get('multiple', float('nan')):.2f} (80% interval {fe.get('low', float('nan')):.2f}–{fe.get('high', float('nan')):.2f}).</p>")


def _screen_section(p: dict) -> str:
    import html
    rows = []
    for t, s in sorted((p.get("screen") or {}).items()):
        if t not in universe.CANDIDATES:
            continue
        share = "" if s.get("share") is None else f"{s['share']:.0%}"
        rows.append(f"<tr><td>{t}</td><td>{html.escape(universe.CANDIDATES[t][1])}</td><td>{share}</td>"
                    f"<td>{'yes' if s.get('pass') else 'no'}</td><td>{html.escape(s.get('note') or '')}</td></tr>")
    return ("<h2>Universe screen (residential share of segment revenue ≥ 50%)</h2><table><tr><th>Ticker</th><th>Company</th>"
            "<th>Residential share</th><th>In universe</th><th>Note</th></tr>" + "".join(rows) + "</table>")


def report_html(p: dict, ar: str) -> str:
    """A plain report of one research run: panel, M1 coefficients, backtest measures, proposed weights."""
    import html
    m, bt, w = p["m1"], p["backtest"], p["weights"]
    rows_bt = "".join(f"<tr><td>{html.escape(k)}</td><td>{r['months']}</td><td>{r['mean_ic']}</td><td>{r['t']}</td>"
                      f"<td>{r['top_minus_bottom']}</td><td>{r['hit_rate']}</td><td>{r['median_abs_error']}</td></tr>"
                      for k, r in bt.get("methods", {}).items())
    rows_w = "".join(f"<tr><td>{html.escape(k)}</td><td>{v:.1%}</td></tr>" for k, v in (w.get("weights") or {}).items())
    t = p["tsuchiya"]
    fp = t.get("fitted_pb") or {}
    if p.get("status") == "approved":
        status = (f"<p>Status: <b>approved</b> ({html.escape(ar)}, {html.escape(str(p.get('approved_at', ''))[:16].replace('T', ' '))}). "
                  "These parameters and weights are used in the daily valuation.</p>")
    else:
        status = (f"<p>Status: <b>pending approval</b> ({html.escape(ar)}). Nothing below is used in the daily valuation until the "
                  f"student writes <code>APPROVE {html.escape(ar)}</code> in state/approvals.md.</p>")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Backtest {html.escape(p['version'])}</title><style>body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 16px;line-height:1.5}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem}}td,th{{border-bottom:1px solid #ddd;padding:4px 8px;text-align:left}}th{{background:#f4f4f4}}
@media (prefers-color-scheme:dark){{body{{background:#141917;color:#e2e8e5}}th{{background:#222a28}}td,th{{border-color:#2c3532}}}}</style></head><body>
<h1>Research run {html.escape(p['version'])}</h1>
{status}
<h2>Panel</h2><p>{p['panel']['rows']} company-years from {p['panel']['companies']} companies. Survivorship: delisted housebuilders are missing (no price history).</p>
<h2>M1: log(P/B) = year effect + b1·ROE_norm + b2·growth + b3·log(revenue)</h2>
<table><tr><th>Term</th><th>Coefficient</th></tr>{''.join(f"<tr><td>{k}</td><td>{v:.4f}</td></tr>" for k, v in m['coef'].items())}</table>
<p>n = {m['n']}; robust residual scale {m['scale']:.3f}; weighted R² {m['r2_weighted']:.3f}.</p>
<p>Tsuchiya: ROE_norm {t['features']['roe_norm']:.4f}, growth {t['features']['growth']:.4f}, revenue ¥{math.exp(t['features']['log_revenue']) / 1e6:,.0f}m
→ fitted P/B {fp.get('pb', float('nan')):.3f} (80% interval {fp.get('low', float('nan')):.3f}–{fp.get('high', float('nan')):.3f}).</p>
{_ev_section(p)}
{_screen_section(p)}
<h2>Backtest ({bt.get('freq', 'monthly')}: {bt.get('months')} cross-sections, {' to '.join(bt.get('period', []))})</h2>
<table><tr><th>Method</th><th>Months</th><th>Mean rank IC</th><th>t (Newey-West)</th><th>Top − bottom quintile</th><th>Hit rate</th><th>Median abs. error</th></tr>{rows_bt}</table>
<h2>Proposed weights</h2><p>{html.escape(w.get('rule', ''))}. Accepted: {html.escape(', '.join(w.get('accepted') or []) or 'none')}.</p>
<table><tr><th>Method</th><th>Weight</th></tr>{rows_w}</table></body></html>"""
