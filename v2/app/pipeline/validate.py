"""Scraping-accuracy checks (phase P4, and the end-of-run validation step).

Failed values are marked validation_status = failed:<check>; nothing is dropped or "fixed".
"""
from __future__ import annotations

from collections import defaultdict

from lib import CONFIG, CsvStore, log, run_ctx, sample

HEADLINE = {"Revenue", "Operating profit", "Profit attributable to owners of parent", "EPS (basic)", "Total assets"}

def _v(r):
    try:
        return float(r["value"]) * float(r["scale"] or 1)
    except (ValueError, TypeError):
        return None

def _index(store: CsvStore):
    """(as_reported_in, period_end, scope, line_item_en) -> row, for consolidated + segment rows."""
    ix = {}
    for r in store.rows.values():
        if r["agent_status"] == "unresolved" or r["value"] == "":
            continue
        ix[(r["as_reported_in"], r["period_end"], r["scope"], r["line_item_en"])] = r
    return ix

def _tol(*vals, rel=1e-4, unit=1000.0):
    """Rounding tolerance: values reported in 千円 can differ by one unit per component."""
    big = max(abs(v) for v in vals if v is not None)
    return max(rel * big, unit * len(vals))

def run_checks(st, store: CsvStore, alt: CsvStore | None = None, base: CsvStore | None = None) -> dict:
    ix = _index(store)
    results = defaultdict(lambda: {"passed": 0, "failed": 0, "not_checked": 0})
    failures = []
    docs = {(r["as_reported_in"], r["period_end"]) for r in store.rows.values() if r["scope"] == "consolidated"}

    def mark(check, rows, ok, note):
        status = "passed" if ok else f"failed:{check}"
        for r in rows:
            cur = r["validation_status"]
            if ok and cur.startswith("failed:"):
                continue  # one failing check keeps the row failed
            store.set_validation(r["record_id"], status, None if ok else note)
        results[check]["passed" if ok else "failed"] += 1
        if not ok:
            failures.append({"check": check, "note": note, "rows": [r["record_id"] for r in rows],
                             "headline": any(r["line_item_en"] in HEADLINE for r in rows)})

    for doc, pe in docs:
        g = lambda item, scope="consolidated": ix.get((doc, pe, scope, item))
        # 1. 総資産 = 負債 + 純資産
        a, l, n = g("Total assets"), g("Total liabilities"), g("Net assets")
        if a and l and n:
            ok = abs(_v(a) - (_v(l) + _v(n))) <= _tol(_v(a), _v(l), _v(n))
            mark("bs_identity", [a, l, n], ok, f"総資産 {_v(a):.0f} ≠ 負債 {_v(l):.0f} + 純資産 {_v(n):.0f} ({doc}, {pe})")
        else:
            results["bs_identity"]["not_checked"] += 1
        # 2. 売上高 − 売上原価 = 売上総利益
        s, c, gp = g("Revenue"), g("Cost of sales"), g("Gross profit")
        if s and c and gp:
            ok = abs(_v(s) - _v(c) - _v(gp)) <= _tol(_v(s), _v(c), _v(gp))
            mark("gross_profit_identity", [s, c, gp], ok, f"売上高 − 売上原価 ≠ 売上総利益 ({doc}, {pe})")
        else:
            results["gross_profit_identity"]["not_checked"] += 1
        # 3. segment revenue (external) + reconciling items = consolidated revenue
        segs = [r for (d, p, sc, it), r in ix.items() if d == doc and p == pe and sc.startswith("segment:")
                and it == "Segment revenue (external customers)" and sc not in ("segment:合計",)]
        if s and segs:
            total = sum(_v(r) for r in segs)
            ok = abs(total - _v(s)) <= _tol(total, _v(s), *[_v(r) for r in segs])
            mark("segment_sum", segs + [s], ok, f"Σ segment external revenue {total:.0f} ≠ 売上高 {_v(s):.0f} ({doc}, {pe})")
        else:
            results["segment_sum"]["not_checked"] += 1
        # 4. cash roll-forward: begin + OCF + ICF + FCF + FX (+other) = end
        end = g("Cash and equivalents, end of period")
        prior_pe = _prior_pe(pe)
        begin = ix.get((doc, prior_pe, "consolidated", "Cash and equivalents, end of period"))
        o, i, f = g("Operating cash flow"), g("Investing cash flow"), g("Financing cash flow")
        if end and begin and o and i and f:
            fx = _v(g("FX effect on cash")) if g("FX effect on cash") else 0.0
            calc = _v(begin) + _v(o) + _v(i) + _v(f) + fx
            ok = abs(calc - _v(end)) <= _tol(calc, _v(end), _v(o), _v(i), _v(f))
            note = f"期首 + 営業CF + 投資CF + 財務CF + 換算差額 = {calc:.0f} ≠ 期末 {_v(end):.0f} ({doc}, {pe})"
            if not ok and g("Net change in cash"):
                other = _v(end) - _v(begin) - _v(g("Net change in cash"))
                note += f"; other reconciling items (e.g. 連結範囲の変動) = {other:.0f}"
                ok = abs((_v(begin) + _v(g('Net change in cash')) + other) - _v(end)) < 1 and abs(other) > 0 and \
                    abs(_v(g("Net change in cash")) - (_v(o) + _v(i) + _v(f) + fx)) <= _tol(_v(o), _v(i), _v(f))
            mark("cash_rollforward", [end, o, i, f], ok, note)
        else:
            results["cash_rollforward"]["not_checked"] += 1
        # 5. EPS plausibility: NI / average shares within 1% of reported EPS
        ni, eps, avg = g("Profit attributable to owners of parent"), g("EPS (basic)"), g("Average shares outstanding")
        if ni and eps and avg and _v(avg):
            calc = _v(ni) / _v(avg)
            ok = abs(calc - _v(eps)) <= max(0.01 * abs(_v(eps)), 0.01)
            mark("eps_plausibility", [ni, eps, avg], ok, f"NI/avg shares = {calc:.2f} vs EPS {_v(eps):.2f} ({doc}, {pe})")
        else:
            results["eps_plausibility"]["not_checked"] += 1
        # 6. unit/scale vs 5-year summary
        for item in ("Revenue", "Ordinary profit", "Profit attributable to owners of parent", "Total assets", "Net assets",
                     "Operating cash flow", "Cash and equivalents, end of period"):
            a_, b_ = g(item), g(item + " (5-year summary)")
            if a_ and b_ and _v(b_):
                ratio = abs(_v(a_) / _v(b_)) if _v(b_) else 0
                ok = not any(abs(ratio - k) / k < 0.05 for k in (1e3, 1e6, 1e-3, 1e-6))
                mark("unit_scale", [a_], ok, f"{item}: statement {_v(a_):.0f} vs 5-year summary {_v(b_):.0f} (ratio {ratio:g})")
    # 7. cross-document: same FY in consecutive reports (current vs prior-year column)
    by_key = defaultdict(list)
    for r in store.rows.values():
        if r["value"] != "" and r["agent_status"] != "unresolved" and r["scope"] == "consolidated" \
                and "(5-year summary)" not in r["line_item_en"]:
            by_key[(r["period_end"], r["line_item_en"])].append(r)
    for (pe, item), rows in by_key.items():
        vals = {r["as_reported_in"]: _v(r) for r in rows}
        if len(vals) < 2:
            continue
        ok = max(vals.values()) - min(vals.values()) <= _tol(*vals.values())
        if not ok:
            restated = any("訂正" in r["source_type"] or "restat" in r["correction_note"].lower() for r in rows)
            note = (f"{item} for FY ending {pe} differs across filings {vals}" +
                    ("; a correction/restatement is present — both values kept" if restated else "; no restatement noted"))
            mark("cross_document", rows, restated, note) if restated else mark("cross_document", rows, False, note)
        else:
            results["cross_document"]["passed"] += 1
    # 8. cross-source: headline figures vs 決算短信 actuals (Baseline Metrics rows basis=actual, source 決算短信)
    if base is not None:
        for b in base.rows.values():
            if b["basis"] != "actual (決算短信)" or b["value"] == "":
                continue
            item = {"Revenue (決算短信)": "Revenue", "Operating profit (決算短信)": "Operating profit",
                    "Net profit (決算短信)": "Profit attributable to owners of parent"}.get(b["metric"])
            if not item:
                continue
            cands = [r for r in store.rows.values() if r["line_item_en"] == item and r["period_end"] == b["period_end"]
                     and r["scope"] == "consolidated" and r["value"] != ""]
            for r in cands:
                vb = _v(b)
                ok = abs(_v(r) - vb) < max(1e6, 1e-3 * abs(vb))  # 短信 rounds or truncates to 百万円
                mark("cross_source", [r], ok, f"{item} {_v(r):.0f} vs 決算短信 {vb:.0f} ({b['source_url']})")
    # 9. re-extraction sample (10% of rows written this run, from cached raw files)
    results["reextract"] = reextract_sample(st, store)
    # listings (Alternative Data): dedupe + source_url at first_seen
    if alt is not None:
        for r in alt.rows.values():
            if r["record_type"] == "listing" and not r["source_url"].startswith("http"):
                alt.set_validation(r["record_id"], "failed:listing_source_url", "No reachable source_url recorded at first_seen.")
                results["listing_source"]["failed"] += 1
    summary = {k: dict(v) if isinstance(v, dict) else v for k, v in results.items()}
    log(st, "validate", {"rows": len(store.rows)}, summary)
    return {"summary": summary, "failures": failures,
            "headline_failures": [f for f in failures if f["headline"]]}

def _prior_pe(pe: str) -> str:
    y, m, d = pe.split("-")
    return f"{int(y) - 1}-{m}-{d}"

def reextract_sample(st, store: CsvStore) -> dict:
    import edinet
    run_id = run_ctx(st)["run_id"]
    written = [r for rid, r in store.rows.items() if r["run_id"] == run_id and r["extraction_method"] == "XBRL"
               and r["value"] != "" and r["xbrl_element"]]
    res = {"sampled": 0, "matched": 0, "mismatched": 0}
    cache = {}
    for r in sample(written, 0.10, run_id):
        doc = r["as_reported_in"]
        if doc not in cache:
            cache[doc] = edinet.facts_for(doc)
        facts = cache[doc]
        ctx = r["location_in_source"].split("context ")[-1].split(",")[0].strip()
        if r["xbrl_element"].endswith("TextBlock"):
            by_el = {}
            for f in facts:
                by_el.setdefault(f["element"], []).append(f)
            v = edinet._per_share_note(by_el, ctx.split("Year")[0], r["line_item_en"])
            vals = [v] if v is not None else []
        else:
            vals = [f["value"] for f in facts if f["element"] == r["xbrl_element"] and f["context"] == ctx]
        res["sampled"] += 1
        if vals and vals[0].replace(",", "") == r["value"]:
            res["matched"] += 1
        else:
            res["mismatched"] += 1
            store.set_validation(r["record_id"], "failed:reextract", f"Re-extraction from cached file gave {vals[:1]}")
    return res
