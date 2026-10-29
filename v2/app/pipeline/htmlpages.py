"""Script-free HTML reports and plain-language overview pages for the four reporting pages (Addendum A44, v2.008).

Every report the agent posts is an HTML file with inline CSS only (no scripts, no external resources), readable in
the dashboard's HTML preview and in any browser, in light or dark mode:
  reports/runs/run_<run id>.html          run summary (every run)
  reports/research_summary_latest.html    the research agent's summary
  reports/debates/<date>_<time>.html      one Md debate (C&A) and one page per debater's turns
  reports/pages/{main,ca,debater_a,debater_b}.html   the overview of each reporting page ("read me first")
The overview pages explain what each value means, in which direction, compared with what, and how fresh it is.
"""
from __future__ import annotations

import html
import json

from lib import ROOT, iso, write_text

CSS = """
:root{--bg:#f7f8f7;--card:#fff;--fg:#1b2220;--mut:#5a6662;--rule:#d8dedb;--acc:#0f6b62;--accs:#e0eeeb;--bad:#a3401f;--bads:#f6e5de;
--good:#2d6a3a;--goods:#e1efe3;--warn:#8a6a12;--warns:#f4edd6;--blue:#2f5d8a;--blues:#e3ebf3;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#111514;--card:#181e1c;--fg:#e2e8e5;--mut:#98a6a1;--rule:#2c3532;--acc:#5cc2b4;--accs:#163029;
--bad:#f08e6a;--bads:#3a2119;--good:#7cc98b;--goods:#17301d;--warn:#e0bd5c;--warns:#33290f;--blue:#86b3e0;--blues:#1a2633;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI","Noto Sans JP",sans-serif;padding:24px 16px 48px}
main{max-width:860px;margin:0 auto;display:flex;flex-direction:column;gap:18px}
h1{font-size:1.6rem;margin:0}h2{font-size:1.15rem;margin:0 0 6px;padding-bottom:4px;border-bottom:1px solid var(--rule)}h3{font-size:1rem;margin:8px 0 4px}
p{margin:0 0 6px;max-width:72ch}.mut{color:var(--mut);font-size:.88rem}.card{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:14px 16px}
.eyebrow{font:500 .72rem ui-monospace,Menlo,monospace;letter-spacing:.08em;text-transform:uppercase;color:var(--acc)}
.big{font:600 1.7rem ui-monospace,Menlo,monospace}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px}
.kpi{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:10px 12px}.kpi .l{font-size:.78rem;color:var(--mut)}
.kpi .v{font:600 1.25rem ui-monospace,Menlo,monospace}.kpi .s{font-size:.78rem;color:var(--mut)}
table{border-collapse:collapse;width:100%;font-size:.86rem}td,th{text-align:left;vertical-align:top;padding:6px 8px;border-bottom:1px solid var(--rule)}
th{font-size:.76rem;color:var(--mut);font-weight:600}.num{font-family:ui-monospace,Menlo,monospace;white-space:nowrap}
.bar{height:10px;background:var(--rule);border-radius:5px;overflow:hidden;min-width:80px}.bar>i{display:block;height:100%;background:var(--acc)}
.tag{font:.7rem ui-monospace,Menlo,monospace;padding:1px 6px;border-radius:3px;white-space:nowrap}
.ok{background:var(--goods);color:var(--good)}.no{background:var(--bads);color:var(--bad)}.wa{background:var(--warns);color:var(--warn)}.in{background:var(--blues);color:var(--blue)}
.scale{position:relative;height:46px;margin:8px 0 30px}.scale .line{position:absolute;left:0;right:0;top:22px;height:2px;background:var(--rule)}
.scale .rng{position:absolute;top:16px;height:14px;background:var(--accs);border:1px solid var(--acc);border-radius:7px}
.scale .pt{position:absolute;top:13px;width:4px;height:20px;margin-left:-2px;border-radius:2px}
.scale .lab{position:absolute;top:36px;transform:translateX(-50%);font:.72rem ui-monospace,Menlo,monospace;white-space:nowrap}
.note{border-left:3px solid var(--acc);background:var(--accs);padding:8px 12px;border-radius:4px;font-size:.9rem}
.warnbox{border-left:3px solid var(--warn);background:var(--warns);padding:8px 12px;border-radius:4px;font-size:.9rem}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;margin:0;font-size:.88rem}dt{font-weight:600}dd{margin:0;color:var(--mut)}
"""


def esc(x) -> str:
    return html.escape("" if x is None else str(x))


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def yen(v, d=1) -> str:
    x = num(v)
    return "—" if x is None else f"¥{x:,.{d}f}"


def myen(v) -> str:
    x = num(v)
    return "—" if x is None else f"¥{x / 1e6:,.1f}m"


def pct(v, d=1, sign=True) -> str:
    x = num(v)
    return "—" if x is None else (f"{x:+.{d}%}" if sign else f"{x:.{d}%}")


def bar(frac) -> str:
    f = max(0.0, min(1.0, num(frac) or 0.0))
    return f'<div class="bar"><i style="width:{f * 100:.0f}%"></i></div>'


def page(title: str, eyebrow: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{esc(title)}</title><style>{CSS}</style></head><body><main>'
            f'<div><div class="eyebrow">{esc(eyebrow)}</div><h1>{esc(title)}</h1><p class="mut">Generated {esc(iso()[:16].replace("T", " "))} JST by the agent\'s code. '
            f'Agent baseline from public data; the target price and the view are the student\'s (syllabus 2.5). Not investment advice.</p></div>'
            f'{body}</main></body></html>')


def save(rel: str, text: str) -> str:
    write_text(rel, text)
    return rel


def scale(points: list[tuple[str, float, str]], rng: tuple | None = None) -> str:
    """A horizontal scale of prices (yen) with labelled markers and an optional range band; pure CSS."""
    vals = [v for _, v, _ in points if v is not None] + ([x for x in rng if x is not None] if rng else [])
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.12 or max(1.0, hi * 0.1)
    lo, hi = lo - pad, hi + pad
    pos = lambda v: (v - lo) / (hi - lo) * 100
    out = ['<div class="scale"><div class="line"></div>']
    if rng and None not in rng:
        out.append(f'<div class="rng" style="left:{pos(rng[0]):.1f}%;width:{pos(rng[1]) - pos(rng[0]):.1f}%"></div>')
    labelled = 0
    for lab, v, color in points:
        if v is None:
            continue
        out.append(f'<div class="pt" style="left:{pos(v):.1f}%;background:var(--{color})"></div>')
        if lab:  # unlabelled points are small ticks (the methods); labels alternate rows so they do not collide
            out.append(f'<div class="lab" style="left:{min(92, max(8, pos(v))):.1f}%;top:{36 + (labelled % 2) * 15}px">{esc(lab)} {yen(v, 0)}</div>')
            labelled += 1
    out.append("</div>")
    return "".join(out)


# ---------------------------------------------------------------- reports
def run_summary(cr: dict, phases: dict, rows: dict, pend: list, ver: str) -> str:
    att = "".join(f"<li>{esc(a)}</li>" for a in cr.get("attention", [])) or "<li>none</li>"
    notes = "".join(f"<li>{esc(n)}</li>" for n in cr.get("notes", [])[:8]) or "<li>none</li>"
    body = (f'<div class="kpis"><div class="kpi"><div class="l">Run</div><div class="v">{esc(cr["run_id"])}</div><div class="s">mode {esc(cr["mode"])} · version {esc(ver)}</div></div>'
            f'<div class="kpi"><div class="l">Needs attention</div><div class="v">{len(cr.get("attention", []))}</div><div class="s">items to look at</div></div>'
            f'<div class="kpi"><div class="l">Pending approvals</div><div class="v">{len(pend)}</div><div class="s">waiting for the student</div></div></div>'
            f'<div class="card"><h2>Needs attention</h2><ul>{att}</ul></div>'
            f'<div class="card"><h2>Pending approvals</h2><ul>{"".join(f"<li>{esc(p)}</li>" for p in pend) or "<li>none</li>"}</ul>'
            f'<p class="mut">Approve by writing <code>APPROVE &lt;id&gt;</code> under "## Decisions" in v2/app/state/approvals.md.</p></div>'
            f'<div class="card"><h2>Rows per data file</h2><table><tr><th>File</th><th class="num">Rows</th></tr>'
            + "".join(f'<tr><td>{esc(k)}</td><td class="num">{v:,}</td></tr>' for k, v in rows.items()) + '</table></div>'
            f'<div class="card"><h2>Build phases</h2><table>' + "".join(f'<tr><td>{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in phases.items())
            + f'</table></div><div class="card"><h2>Notes</h2><ul>{notes}</ul></div>')
    return save(f"reports/runs/run_{cr['run_id']}.html", page(f"Run summary {cr['run_id']}", "Tsuchibot Main · run summary", body))


def research_summary(cur: dict) -> str:
    text = "".join(f"<p>{esc(p)}</p>" for p in (cur.get("summary") or "").split("\n") if p.strip())
    body = (f'<div class="card"><h2>Summary written by the research agent</h2>{text}'
            f'<p class="mut">Every number was checked by code against the research results; unverified ones are listed at the end.</p></div>'
            f'<div class="note">Status: <b>{esc(cur.get("status"))}</b>. Full tables: reports/backtest_latest.html.</div>')
    return save("reports/research_summary_latest.html", page(f"Research run {cur.get('version')}", "Tsuchibot Main · research", body))


STOP_TEXT = {
    "gate 1": "The news scouts read the new items and flagged none as able to move Tsuchiya's operating profit beyond what Md already measures, so no debate was held.",
    "link check": "The scouts flagged items, but none of their documents could be fetched and read, so they could not be cited.",
    "gate 2 (A's": "Debater A's proposal was below ¥10m or had no valid citation, so the debate ended after round 1 with no adjustment.",
    "gate 2 (B agrees": "Debater B agreed with Debater A after round 1, so no second round was needed.",
    "after round 2": "Both debaters argued two rounds; the decision models then judged their evidence.",
    "cap is 0": "There is no room for an adjustment: the cap is zero (no guidance record yet, or the fiscal year has ended).",
    "no Md debate yet": "No Md debate has been triggered yet.",
}


BUCKET = {"strong_down": "lower it a lot", "down": "lower it somewhat", "none": "leave it", "up": "raise it somewhat", "strong_up": "raise it a lot"}


def signal_words(v) -> str:
    x = num(v) or 0.0
    if not x:
        return "no change to the forecast"
    return f"lowers the operating-profit forecast by {myen(abs(x))}" if x > 0 else f"raises the operating-profit forecast by {myen(abs(x))}"


def stop_text(s: str) -> str:
    return next((v for k, v in STOP_TEXT.items() if (s or "").startswith(k)), esc(s))


def debate_record(case: dict, board: dict) -> str:
    items = "".join(f'<tr><td class="num">{esc(i)}</td><td>{esc((board.get(i) or {}).get("title", ""))}</td><td>{esc((board.get(i) or {}).get("date", ""))}</td></tr>'
                    for i in case.get("items") or []) or '<tr><td colspan="3">none</td></tr>'
    body = (f'<div class="card"><h2>Outcome</h2><p>{stop_text(case.get("stopped_at", ""))}</p>'
            f'<p>Debate Signal: <b class="num">{myen(case.get("signal"))}</b>, which {signal_words(case.get("signal"))} ({esc(case.get("mode"))} mode). '
            f'<span class="mut">How it was computed: {esc(case.get("note", ""))}</span></p></div>'
            f'<div class="card"><h2>Evidence considered</h2><table><tr><th>Id</th><th>Title</th><th>Date</th></tr>{items}</table></div>'
            + _linkcheck_block(case)
            + _decision_block(case) + _positions_block(case))
    rel = f"reports/debates/{(case.get('recorded') or iso())[:16].replace(':', '').replace('T', '_')}.html"
    return save(rel, page("Md debate record", "Tsuchibot C&A · debate record", body))


def _linkcheck_block(case: dict) -> str:
    lc = case.get("link_check") or []
    if not lc:
        return ""
    rows = "".join(f'<tr><td class="num">{esc(r.get("id"))}</td><td>{esc(r.get("title"))}</td>'
                   f'<td>{"<span class=\'tag ok\'>read (" + esc(r.get("type")) + ", " + esc(r.get("pages")) + " p.)</span>" if r.get("resolved") else "<span class=\'tag no\'>" + esc(r.get("error")) + "</span>"}</td></tr>'
                   for r in lc)
    return (f'<div class="card"><h2>Link check</h2><p class="mut">Code fetched each flagged item and kept a snapshot; only items it could read may be cited.</p>'
            f'<table><tr><th>Id</th><th>Title</th><th>Result</th></tr>{rows}</table></div>')


def _decision_block(case: dict) -> str:
    d = case.get("decision") or {}
    if not d or d.get("error"):
        return f'<div class="card"><h2>Decision models</h2><p class="mut">{esc(d.get("error") or "not called (the debate stopped before C&A)")}</p></div>'
    b = d.get("bucket") or {}
    rows = "".join(f'<tr><td>{esc(BUCKET[k])}</td><td class="num">{num(b.get(k)) or 0:.0%}</td><td>{bar(b.get(k))}</td></tr>'
                   for k in BUCKET)
    s = d.get("supported") or {}
    return (f'<div class="card"><h2>What the decision models answered</h2>'
            f'<p>Evidence supports Debater A: <b class="num">{pct(d.get("evidence_a"), 0, False)}</b> · Debater B: <b class="num">{pct(d.get("evidence_b"), 0, False)}</b>. '
            f'Better supported: A {pct(s.get("A"), 0, False)}, B {pct(s.get("B"), 0, False)}, neither {pct(s.get("neither"), 0, False)}.</p>'
            f'<h3>What should happen to the company\'s operating-profit forecast? (probabilities)</h3><table>{rows}</table>'
            f'<p class="mut">Code turns these probabilities into the Debate Signal: '
            f'cap × (probability-weighted direction) × evidence score of the better-supported side; 0 if both directions are above 35%.</p></div>')


def _positions_block(case: dict) -> str:
    out = []
    for name, pos in (case.get("positions") or {}).items():
        claims = "".join(f'<tr><td>{esc(c.get("text"))}</td><td class="num">{esc(", ".join(c.get("evidence") or []))}</td>'
                         f'<td>{"<span class=\'tag ok\'>numbers found in source</span>" if not c.get("unverified_numbers") else "<span class=\'tag no\'>not in source: " + esc(", ".join(c["unverified_numbers"])) + "</span>"}</td></tr>'
                         for c in pos.get("claims") or []) or '<tr><td colspan="3">no valid claims</td></tr>'
        out.append(f'<div class="card"><h2>{"Debater A (proposer)" if name == "debater_a" else "Debater B (sceptic)"}</h2>'
                   f'<p>Direction <b>{esc(pos.get("direction"))}</b>, size <b class="num">{myen(pos.get("size_jpy"))}</b>, confidence {esc(pos.get("confidence"))}. {esc(pos.get("summary"))}</p>'
                   f'<table><tr><th>Claim</th><th>Cites</th><th>Claim check</th></tr>{claims}</table>'
                   f'<p class="mut">Claims dropped for citing nothing valid: {esc(pos.get("rejected_claims", 0))}. Model: {esc(pos.get("model"))}.</p></div>')
    return "".join(out)


# ---------------------------------------------------------------- overview pages ("read me first")
METHOD_TEXT = {
    "P/E": "Peers' price-to-earnings ratio × Tsuchiya's earnings per share (the company forecast minus Md, or a normalised EPS when the forecast is too small).",
    "EV/EBITDA": "Peers' enterprise value ÷ EBITDA, applied to Tsuchiya's forecast EBITDA (minus Md), plus its net cash, per share.",
    "P/B (own history)": "The price-to-book ratio the market has usually given Tsuchiya itself at past year-ends × its current book value per share.",
    "EV/EBITDA (own history)": "Tsuchiya's own past EV/EBITDA, applied to its adjusted forecast EBITDA.",
    "P/B (peers)": "The peers' median price-to-book × Tsuchiya's book value. Ignores differences in profitability, so it is for reference.",
    "M1 (P/B regression)": "The price-to-book a listed housebuilder with Tsuchiya's profitability, growth and size usually trades at (a regression on about 18 companies over ten years) × book value.",
    "M1 (EV/EBITDA regression)": "The EV/EBITDA a housebuilder with Tsuchiya's margin, growth and size usually trades at, applied to its adjusted forecast EBITDA.",
}
METHOD_ROW = {"P/E": "Target Price (P/E, v2.005)", "EV/EBITDA": "Target Price (EV/EBITDA, forward, F1)",
              "P/B (own history)": "Target Price (own history, P/B, M2)", "EV/EBITDA (own history)": "Target Price (own history, EV/EBITDA, M2)",
              "P/B (peers)": "Target Price (P/B, peers)", "M1 (P/B regression)": "Target Price (M1, P/B regression)",
              "M1 (EV/EBITDA regression)": "Target Price (M1, EV/EBITDA regression)"}


def overview_main(v: dict) -> str:
    """v: values gathered by cli.py from Baseline Metrics, state and the research parameters."""
    price, comb = num(v.get("price")), num(v.get("combined"))
    gap = (comb / price - 1) if price and comb else None
    words = ("below" if gap < 0 else "above") if gap is not None else ""
    head = (f'<div class="card"><h2>In one sentence</h2><p>The agent\'s combined estimate for Tsuchiya is <b class="num">{yen(comb)}</b> per share, '
            f'{abs(gap):.0%} {words} the latest market price of <b class="num">{yen(price)}</b> ({esc(v.get("price_date"))}). '
            if gap is not None else '<div class="card"><h2>In one sentence</h2><p>No combined estimate this run (not enough valid methods). ')
    head += ('This is a benchmark calculated from public data, not a forecast of the share price and not a recommendation.</p>'
             + scale([("market", price, "fg"), ("combined", comb, "acc")] + [("", num(p), "blue") for k, p, w, _ in v.get("methods", []) if num(p) and w],
                     (num(v.get("range_low")), num(v.get("range_high"))))
             + '<p class="mut">The shaded band runs from the lowest to the highest method that counts in the estimate; each blue tick is one of '
               'those methods (named in the table below). Reference methods with weight 0 are left off the scale.</p></div>')
    kpis = (f'<div class="kpis"><div class="kpi"><div class="l">Market price</div><div class="v">{yen(price)}</div><div class="s">{esc(v.get("price_date"))}, Yahoo daily close</div></div>'
            f'<div class="kpi"><div class="l">Combined estimate</div><div class="v">{yen(comb)}</div><div class="s">range {yen(v.get("range_low"), 0)}–{yen(v.get("range_high"), 0)}</div></div>'
            f'<div class="kpi"><div class="l">Expected return</div><div class="v">{pct(v.get("expected_return"))}</div><div class="s">estimate ÷ price − 1 + dividend yield</div></div>'
            f'<div class="kpi"><div class="l">Md (forecast discount)</div><div class="v">{myen(v.get("md"))}</div><div class="s">subtracted from the operating-profit forecast</div></div></div>')
    rows = "".join(f'<tr><td><b>{esc(k)}</b><br><span class="mut">{esc(METHOD_TEXT.get(k, ""))}</span></td><td class="num">{yen(p)}</td>'
                   f'<td class="num">{pct((num(p) / price - 1) if num(p) and price else None, 0)}</td><td class="num">{pct(w, 0, False) if w else "0%"}</td><td>{bar(w)}</td>'
                   f'<td class="mut">{esc(note)}</td></tr>' for k, p, w, note in v.get("methods", []))
    methods = (f'<div class="card"><h2>How the estimate is built</h2><p>Each method values Tsuchiya a different way. The combined estimate is their weighted average; '
               f'a method on a fallback basis (e.g. normalised instead of forecast earnings) counts half. Rule now: {esc((v.get("weights_rule") or "")[:1].upper() + (v.get("weights_rule") or "")[1:])}. '
               f'Weight 0 means the method is shown for reference only (pending approval, or not comparable).</p>'
               f'<table><tr><th>Method</th><th class="num">Price</th><th class="num">vs market</th><th class="num">Weight</th><th></th><th>Basis</th></tr>{rows}</table></div>')
    md = v.get("md_parts", {})
    mdb = (f'<div class="card"><h2>Md: the forecast discount</h2><p>Before valuing earnings, the agent adjusts the company\'s own forecast by Md. '
           f'A positive Md lowers the forecast (the company has tended to over-promise); a negative one raises it.</p><dl>'
           f'<dt>Company forecast, operating profit</dt><dd>{myen(md.get("forecast_op"))} for {esc(md.get("forecast_fy"))} (set {esc(md.get("guidance_date"))}; {esc(md.get("months_left"))} months left)</dd>'
           f'<dt>Discount Baseline</dt><dd>{myen(md.get("baseline"))} — from Tsuchiya\'s past forecast misses, scaled by the months left in the year</dd>'
           f'<dt>Supply costs</dt><dd>{pct((num(md.get("supply")) or 0) / 100 if md.get("supply") is not None else None)} since the forecast — timber price index (BOJ); recorded, not yet used in Md</dd>'
           f'<dt>Land prices</dt><dd>{pct((num(md.get("land")) or 0) / 100 if md.get("land") is not None else None)} since the forecast — Hokkaido land transactions (MLIT); recorded, not yet used in Md</dd>'
           f'<dt>Debate Signal</dt><dd>{myen(md.get("debate"))} — from the latest news debate ({esc(md.get("debate_mode") or "shadow")} mode: '
           f'{"added to Md" if md.get("debate_mode") == "live" else "shown only, not added to Md"})</dd>'
           f'<dt>Md in total</dt><dd><b>{myen(v.get("md"))}</b></dd></dl></div>')
    r = v.get("research") or {}
    res = (f'<div class="card"><h2>Weekly research: is each method any good?</h2><p>Every week the agent tests the methods on about 18 listed housebuilders over ten years: '
           f'did a method\'s "cheap" or "expensive" verdict predict the next year\'s return? {esc(r.get("sentence"))}</p>'
           f'<table><tr><th>Method</th><th class="num">Rank correlation with later returns</th><th class="num">t</th><th>Passed?</th></tr>'
           + "".join(f'<tr><td>{esc(k)}</td><td class="num">{esc(m.get("mean_ic"))}</td><td class="num">{esc(m.get("t"))}</td>'
                     f'<td>{"<span class=\'tag ok\'>yes</span>" if (num(m.get("mean_ic")) or 0) > 0 and (num(m.get("t")) or 0) >= 2 else "<span class=\'tag no\'>no</span>"}</td></tr>'
                     for k, m in (r.get("methods") or {}).items())
           + f'</table><p class="mut">A rank correlation of 0 means no predictive power; t ≥ 2 is the bar fixed in advance. Version {esc(r.get("version"))}, '
             f'status {esc(r.get("status"))}. Approved weights in use: {esc(r.get("approved") or "none (equal weights)")}.</p></div>')
    pend = v.get("pending") or []
    todo = (f'<div class="{"warnbox" if pend else "note"}"><b>Waiting for you:</b> '
            + ("; ".join(esc(p) for p in pend) + ". Approve by writing <code>APPROVE &lt;id&gt;</code> under \"## Decisions\" in v2/app/state/approvals.md."
               if pend else "nothing.") + '</div>')
    gl = ('<div class="card"><h2>Words used here</h2><dl><dt>EPS</dt><dd>earnings per share</dd><dt>P/E</dt><dd>share price ÷ EPS</dd>'
          '<dt>P/B</dt><dd>share price ÷ book value (net assets) per share</dd><dt>EV</dt><dd>market value of the shares + debt − cash</dd>'
          '<dt>EBITDA</dt><dd>operating profit + depreciation</dd><dt>Md</dt><dd>the forecast discount: an adjustment to the company\'s profit forecast</dd>'
          '<dt>M1</dt><dd>statistical comparables: what similar listed housebuilders trade at, adjusted for profitability, growth and size</dd></dl></div>')
    body = head + kpis + todo + methods + mdb + res + gl + f'<p class="mut">Version {esc(v.get("version"))}. Last run {esc(v.get("run_id"))}.</p>'
    return save("reports/pages/main.html", page("Tsuchiya Holdings (1840): overview", "Tsuchibot Main · read me first", body))


def overview_ca(case: dict | None, board: dict, history: list[dict], ver: str) -> str:
    intro = ('<div class="card"><h2>What this page is</h2><p>When new company notices or filings appear, the agent asks whether they should change '
             'Tsuchiya\'s operating-profit forecast before it is valued. News scouts pick relevant items, two debater agents argue for and against an '
             'adjustment, and two decision models (C&amp;A) judge the evidence. A fixed rule in code turns their answers into the <b>Debate Signal</b>, '
             'which becomes part of Md.</p></div>')
    if not case:
        last = '<div class="note">No Md debate has been triggered yet.</div>'
    else:
        last = (f'<div class="card"><h2>Latest debate: {esc((case.get("recorded") or "")[:10])}</h2><p>{stop_text(case.get("stopped_at", ""))}</p>'
                f'<p>Debate Signal: <b class="num">{myen(case.get("signal"))}</b>, which {signal_words(case.get("signal"))} — {esc(case.get("mode"))} mode '
                f'({"added to Md" if case.get("mode") == "live" else "shown only; Md is unchanged until the student switches to live"}).</p>'
                f'<p class="mut">{esc(case.get("note", ""))}</p></div>' + _decision_block(case))
    hist = "".join(f'<tr><td>{esc((c.get("recorded") or "")[:10])}</td><td>{esc(c.get("trigger"))}</td><td class="num">{len(c.get("items") or [])}</td>'
                   f'<td>{esc(c.get("stopped_at"))}</td><td class="num">{myen(c.get("signal"))}</td></tr>' for c in reversed(history[-10:]))
    body = intro + last + (f'<div class="card"><h2>Recent debates</h2><table><tr><th>Date</th><th>Trigger</th><th class="num">Items</th><th>Stopped at</th>'
                           f'<th class="num">Signal</th></tr>{hist}</table></div>' if hist else "") + f'<p class="mut">Version {esc(ver)}.</p>'
    return save("reports/pages/ca.html", page("Consensus and arbitration", "Tsuchibot C&A · read me first", body))


def overview_debater(prof: str, case: dict | None, memory: dict, ver: str) -> str:
    role = ("Debater A is the <b>proposer</b>: it argues for an adjustment to the company's operating-profit forecast when the new evidence supports one."
            if prof == "debater_a" else
            "Debater B is the <b>sceptic</b>: it tests the case for an adjustment and argues for a smaller one, or none, where the evidence is weak.")
    intro = (f'<div class="card"><h2>What this page is</h2><p>{role} It may cite only items on the evidence board whose documents code has fetched and read; '
             f'every number in its claims is checked against those documents. It never sets the final number.</p></div>')
    pos = ((case or {}).get("positions") or {}).get(prof)
    if pos:
        claims = "".join(f'<tr><td>{esc(c.get("text"))}</td><td>{"<span class=\'tag ok\'>found in source</span>" if not c.get("unverified_numbers") else "<span class=\'tag no\'>not in source</span>"}</td></tr>'
                         for c in pos.get("claims") or []) or '<tr><td colspan="2">no valid claims</td></tr>'
        lw = {"down": "the forecast looks too high and should be lowered", "up": "the forecast looks too low and should be raised",
              "none": "no adjustment is needed"}.get(pos.get("direction"), "")
        latest = (f'<div class="card"><h2>Latest position ({esc((case.get("recorded") or "")[:10])})</h2><p>It argued that {esc(lw)}, by '
                  f'<b class="num">{myen(pos.get("size_jpy"))}</b> of operating profit, with {esc(pos.get("confidence"))} confidence. {esc(pos.get("summary"))}</p>'
                  f'<table><tr><th>Claim</th><th>Claim check</th></tr>{claims}</table>'
                  f'<p class="mut">Dropped for citing nothing valid: {esc(pos.get("rejected_claims", 0))}. Model: {esc(pos.get("model"))}.</p></div>')
    else:
        latest = f'<div class="note">No position yet. {stop_text((case or {}).get("stopped_at", "no Md debate yet"))}</div>'
    track = (f'<div class="card"><h2>Track record</h2><p>Debates taken part in: <b>{esc(memory.get("debates", 0))}</b>; with a known outcome: '
             f'{esc(memory.get("with_outcome", 0))}. Outcomes are filled in once the fiscal year\'s actual results are published.</p></div>')
    name = "Debater A" if prof == "debater_a" else "Debater B"
    return save(f"reports/pages/{prof}.html", page(name, f"Tsuchibot {name} · read me first", intro + latest + track + f'<p class="mut">Version {esc(ver)}.</p>'))


def debate_turns(prof: str, case: dict) -> str:
    """One debater's turns in one Md debate, round by round."""
    name = "Debater A (proposer)" if prof == "debater_a" else "Debater B (sceptic)"
    turns = [t for t in case.get("transcript") or [] if t.get("name") == prof]
    body = "".join(_positions_block({"positions": {prof: t}}).replace(f"<h2>{name}</h2>", f"<h2>Round {esc(t.get('round'))}</h2>", 1)
                   + (f'<p class="mut">Conceded: {esc(t.get("concessions"))}</p>' if t.get("concessions") else "") for t in turns)
    body = (f'<div class="note">{stop_text(case.get("stopped_at", ""))} Debate Signal {myen(case.get("signal"))} ({esc(case.get("mode"))} mode).</div>'
            + (body or '<p class="mut">No turns recorded.</p>'))
    rel = f"reports/debates/{(case.get('recorded') or iso())[:16].replace(':', '').replace('T', '_')}_{prof}.html"
    return save(rel, page(f"{name}: turns", f"Tsuchibot {name.split(' (')[0]} · debate turns", body))
