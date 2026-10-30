"""Data gaps and errors report (Addendum A52, v2.014): an HTML monitor on Tsuchibot Main. Code only; no model.

Every run, code gathers what could not be retrieved, what is outdated and what was cut short, and says for each item
how much it matters to the evaluation and why:
  high    the valuation itself is missing or wrong this run (e.g. no market price)
  medium  the valuation is computed but rests on less than it should (a method, Md's baseline, research coverage)
  low     only supporting data is affected (debate sources in shadow mode, marketing data, the dashboard)
  info    a known, permanent gap (e.g. a company does not disclose an item)
Sources: this run's log lines (fetch errors, skipped sites, budget stops), the build phases, Baseline Metrics rows,
the evidence-board and marketing source status, the research parameters and universe, Google Trends uploads and
the dashboard buffer.
"""
from __future__ import annotations

import datetime as dt
import json
import os

import htmlpages
from htmlpages import esc
from lib import ROOT, iso

SEV = {"high": 0, "medium": 1, "low": 2, "info": 3}
SEV_TAG = {"high": "no", "medium": "wa", "low": "in", "info": "ok"}
SITE_ROLE = {  # domain -> (area, severity when it fails, why)
    "irbank.net": ("Market price", "high", "the market price feeds every method, the expected return and the gap to the estimate"),
    "api.edinet-fsa.go.jp": ("Filings (EDINET)", "medium", "new filings are not read: forecasts, actuals and the universe may be out of date"),
    "disclosure2dl.edinet-fsa.go.jp": ("Filings (EDINET)", "medium", "filing downloads failed; figures stay at the last version read"),
    "www.tsuchiya.co.jp": ("Tsuchiya IR", "medium", "the company forecast (決算短信) may be out of date, which moves every forward method"),
    "www.stat-search.boj.or.jp": ("Md: supply costs", "low", "logged with coefficient 0, so Md is unchanged"),
    "www.reinfolib.mlit.go.jp": ("Md: land prices", "low", "logged with coefficient 0, so Md is unchanged"),
    "www.tamahome.jp": ("Peers", "medium", "peer 決算短信 feed the peer multiples; marketing pages are supporting data only"),
    "www.nihonhouse-hd.co.jp": ("Peers", "medium", "peer 決算短信 feed the peer multiples; marketing pages are supporting data only"),
    "www.grandy.co.jp": ("Peers", "medium", "peer 決算短信 feed the peer multiples; marketing pages are supporting data only"),
}
PEER_SITES = {"www.tamahome.jp", "www.nihonhouse-hd.co.jp", "www.grandy.co.jp"}
KEY_METHODS = ["Target Price (P/E, v2.005)", "Target Price (EV/EBITDA, forward, F1)", "Target Price (own history, P/B, M2)",
               "Target Price (own history, EV/EBITDA, M2)", "Target Price (M1, P/B regression)", "Target Price (M1, EV/EBITDA regression)"]


def _issue(out, area, what, severity, why, when=""):
    out.append({"area": area, "what": what, "severity": severity, "why": why, "when": when or iso()[:16].replace("T", " ")})


def _run_log(run_id: str, tail: int = 6000) -> list[dict]:
    p = ROOT / "logs" / "run_log.jsonl"
    if not p.exists() or not run_id:
        return []
    lines = p.read_text(encoding="utf-8").splitlines()[-tail:]
    out = []
    for l in lines:
        try:
            r = json.loads(l)
        except ValueError:
            continue
        if r.get("run_id") == run_id:
            out.append(r)
    return out


def collect(st, cr: dict, base) -> list[dict]:
    from cli import PHASES, PARKED, _latest
    out: list[dict] = []
    today = dt.date.fromisoformat(iso()[:10])
    # ---- this run: cut short, fetch failures, skipped sites
    logs = _run_log(cr.get("run_id", ""))
    fails: dict[str, list] = {}
    for r in logs:
        o = str(r.get("outcome", ""))
        if r.get("tool") == "fetch" and o.startswith("error"):
            url = (r.get("inputs") or {}).get("url", "")
            host = url.split("/")[2] if "//" in url else "?"
            if host in PEER_SITES and not any(k in url.lower() for k in ("/ir", ".pdf")):
                continue   # a peer's marketing page: listed under "Marketing data" from the collector's status
            fails.setdefault(host, []).append(o[:80])
        elif "domain skipped" in o:
            _issue(out, "Run", f"{(r.get('inputs') or {}).get('domain')} skipped for the rest of the run after 3 failures", "medium",
                   "later steps that needed this site used the last stored values", r["ts"][:16].replace("T", " "))
        elif any(k in o for k in ("budget reached", "loop detected", "budget_stop", "paused:")):
            _issue(out, "Run", f"cut short: {o[:120]}", "medium",
                   "steps after this point did not run; the pages show values from the last complete run", r["ts"][:16].replace("T", " "))
    for host, errs in fails.items():
        area, sev, why = SITE_ROLE.get(host, ("Other source", "low", "supporting data only"))
        _issue(out, area, f"{host}: {len(errs)} request(s) failed (e.g. {errs[0]})", sev, why)
    for n in cr.get("notes", []):
        if any(k in str(n).lower() for k in ("budget", "timed out", "timeout", "skipped", "stopped", "failed")):
            _issue(out, "Run", str(n)[:160], "medium", "a step did not finish; its results are from an earlier run")
    for a in cr.get("attention", []):
        if not str(a).startswith("New EDINET filing"):
            _issue(out, "Run", str(a)[:160], "medium", "flagged by code as needing attention")
    # ---- build phases
    for p in PHASES:
        s = (st["phases"].get(p) or {}).get("status")
        if p in PARKED or s in (None, "complete"):
            continue
        sev = "high" if p in ("P2", "P6") else "medium"
        _issue(out, "Build phase", f"{p} is {s}: {(st['phases'][p].get('note') or '')[:100]}", sev,
               "P2 (annual report) and P6 (valuation) feed the estimate directly; other phases feed checks and context")
    # ---- valuation rows
    px = _latest(base, "1840", "Stock Price")
    if not px or not px.get("value"):
        _issue(out, "Market price", "no market price stored", "high", "every method is compared with it; no estimate gap or expected return")
    else:
        d = (px.get("quote_time_jst") or px.get("access_date_jst") or "")[:10]
        if d and (today - dt.date.fromisoformat(d)).days > 4:
            _issue(out, "Market price", f"latest price is from {d} ({(today - dt.date.fromisoformat(d)).days} days old)", "high",
                   "the gap between the estimate and the market is measured against an old price")
    for m in KEY_METHODS:
        r = _latest(base, "1840", m)
        if r and (r.get("agent_status") == "unresolved" or not r.get("value") or r["value"] == "not meaningful"):
            _issue(out, "Valuation method", f"{m.replace('Target Price ', '')}: not computed ({(r.get('correction_note') or '')[:90]})",
                   "medium", "the combined estimate uses the remaining methods, with weights re-scaled to 100%")
    r = _latest(base, "1840", "Md: Discount Baseline")
    if not r or not r.get("value"):
        _issue(out, "Md", "Discount Baseline not computed", "medium", "the company forecast is used without the track-record discount")
    for m in ("Md: Land Price Ave (% since guidance)", "Md: Supply Costs (% since guidance)"):
        r = _latest(base, "1840", m)
        if not r or not r.get("value"):
            note = ((r or {}).get("correction_note") or "")[:80]
            _issue(out, "Md", f"{m.split(' (')[0]} not available" + (f" ({note})" if note else ""), "low",
                   "coefficient is 0 until fitted on the panel, so Md does not change")
    # ---- research coverage
    try:
        import modelfit
        import universe
        prm = modelfit.load_params()
        cur = prm.get("approved") or prm.get("pending") or {}
        u = st["documents"].get("universe", {})
        members = universe.members(st)
        panel_n = (cur.get("panel") or {}).get("companies")
        if panel_n and panel_n < len(members):
            _issue(out, "Research", f"{len(members) - panel_n} of {len(members)} universe members have no usable price or figures in the panel",
                   "medium", "M1 and the backtest learn from fewer companies; results are noisier")
        missing = [t for t in universe.CANDIDATES if not (u.get("docs", {}).get(t) or {}).get("latest")]
        if missing:
            _issue(out, "Research", f"latest annual report not found for {len(missing)} candidate(s): {', '.join(missing[:8])}", "medium",
                   "their screen and panel rows are missing or out of date")
        v = (cur.get("version") or "")[:10]
        if v and (today - dt.date.fromisoformat(v)).days > 14:
            _issue(out, "Research", f"last research run is from {v}", "low", "weekly research did not refresh M1 and the weights")
    except Exception as e:
        _issue(out, "Research", f"research status could not be read ({type(e).__name__})", "low", "coverage unknown")
    # ---- evidence sources
    import evidence
    b = evidence.load_board()
    live = (os.getenv("DEBATE_SIGNAL_MODE") or "shadow").lower() == "live"
    for k, s in (b.get("sources") or {}).items():
        if s.get("error"):
            _issue(out, "Debate evidence", f"{k}: {s['error'][:100]}", "medium" if live else "low",
                   "fewer items reach the news scouts" + ("; the Debate Signal is live in Md" if live else "; the Debate Signal is in shadow mode, so Md is unchanged"),
                   (s.get("checked") or "")[:16].replace("T", " "))
    # ---- marketing
    mk = (st["documents"].get("marketing") or {}).get("sources") or {}
    for k, s in mk.items():
        if not s.get("ok"):
            _issue(out, "Marketing data", f"{k}: {s.get('detail', '')[:100]}", "low",
                   "marketing data is descriptive and not yet used in the valuation", (s.get("checked") or "")[:16].replace("T", " "))
        for g in s.get("gaps") or []:
            _issue(out, "Marketing data", g[:120], "info", "the company does not report it in that filing; nothing is estimated in its place")
    tr = st["documents"].get("trends")
    if not tr:
        _issue(out, "Marketing data", "no Google Trends export uploaded yet", "low",
               "share of search is not available; upload a Trends CSV to Main's or C&A's Inbox")
    else:
        last = tr["last_date"] if len(tr["last_date"]) == 10 else tr["last_date"] + "-28"
        age = (today - dt.date.fromisoformat(last)).days
        if age > 35:
            _issue(out, "Marketing data", f"Google Trends data ends {tr['last_date']} ({age} days ago; uploaded {tr['uploaded']})", "low",
                   "share of search is outdated; upload a fresh export (monthly)")
    # ---- dashboard
    try:
        import dashboard
        n = dashboard.pending()
        if n:
            _issue(out, "Dashboard", f"{n} item(s) waiting in the offline buffer", "low", "results reach the dashboard late, not lost")
    except Exception:
        pass
    return sorted(out, key=lambda x: (SEV[x["severity"]], x["area"]))


def page(st, cr: dict, base, ver: str) -> str:
    items = collect(st, cr, base)
    n = {k: sum(1 for i in items if i["severity"] == k) for k in SEV}
    head = ("Nothing is missing or cut short in this run." if not items else
            f"{n['high']} high, {n['medium']} medium, {n['low']} low and {n['info']} informational item(s).")
    worst = next((i for i in items if i["severity"] in ("high", "medium")), None)
    verdict = ('<div class="warnbox"><b>The estimate is affected this run.</b> ' + esc(worst["what"]) + "</div>" if worst and worst["severity"] == "high"
               else '<div class="note">The estimate is computed; some inputs are thinner than usual (see medium items).</div>' if worst
               else '<div class="note">The estimate is not affected; only supporting data has gaps.</div>' if items else "")
    rows = "".join(f'<tr><td><span class="tag {SEV_TAG[i["severity"]]}">{i["severity"]}</span></td><td>{esc(i["area"])}</td>'
                   f'<td>{esc(i["what"])}<br><span class="mut">{esc(i["when"])}</span></td><td class="mut">{esc(i["why"])}</td></tr>'
                   for i in items)
    body = (f'<div class="card"><h2>Summary</h2><p>{esc(head)} Run {esc(cr.get("run_id"))}.</p></div>' + verdict
            + (f'<div class="card"><h2>Items</h2><table><tr><th>Severity</th><th>Area</th><th>What</th><th>Why it matters</th></tr>{rows}</table></div>' if items else "")
            + '<div class="card"><h2>What the severities mean</h2><dl>'
              '<dt><span class="tag no">high</span></dt><dd>the valuation itself is missing or wrong this run</dd>'
              '<dt><span class="tag wa">medium</span></dt><dd>the valuation is computed but rests on less than it should</dd>'
              '<dt><span class="tag in">low</span></dt><dd>only supporting data is affected (debate sources in shadow mode, marketing data, the dashboard)</dd>'
              '<dt><span class="tag ok">info</span></dt><dd>a known, permanent gap; nothing is estimated in its place</dd></dl></div>'
            + f'<p class="mut">Version {esc(ver)}. Missing values are never filled in: a gap stays a gap and is listed here.</p>')
    return htmlpages.save("reports/pages/errors.html", htmlpages.page("Data gaps and errors", "Tsuchibot Main · error report", body))
