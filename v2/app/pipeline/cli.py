"""Pipeline CLI for the Tsuchiya research-data agent (v2). The LLM orchestrator (v2/app/agent.py) calls start and
finish; the baseline subagent's tools call run / steady / peek / refusal / injection. Usage (from the repo root):

  python3 v2/app/pipeline/cli.py start --mode scheduled|test|phase=P3
  python3 v2/app/pipeline/cli.py plan
  python3 v2/app/pipeline/cli.py run P1 ... P9
  python3 v2/app/pipeline/cli.py steady            # every-run refresh + due daily tasks
  python3 v2/app/pipeline/cli.py news-ingest tmp/news.json
  python3 v2/app/pipeline/cli.py integrated-record tmp/integrated.json
  python3 v2/app/pipeline/cli.py refusal "<what was requested>" --source <url|instruction>
  python3 v2/app/pipeline/cli.py injection --url <url> --excerpt "<short quote>"
  python3 v2/app/pipeline/cli.py peek "<csv name>" [--where col=value] [--limit 5]
  python3 v2/app/pipeline/cli.py headline         # 1840 headline, peer inputs, 7-day history (no active run needed)
  python3 v2/app/pipeline/cli.py finish --plan tmp/plan.txt

Every command prints one JSON object. Exit code 0 unless the input was invalid.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import lib
from lib import (CONFIG, ROOT, BudgetStop, CsvStore, FetchError, GuardrailError, base_row, iso, load_state,
                 log, now, out, record_id, run_ctx, save_state, unresolved, write_text)

PHASES = ["P1", "P2", "P3", "P4", "P5", "P6", "P7", "P8", "P9"]
# v2 (Addendum A19): P7 (alternative data) and P8 (legal score) are parked for a future alternative-data
# subagent. Their code stays in altdata.py / legal.py; existing Alternative Data rows are kept, not refreshed.
PARKED = ["P7", "P8"]
ACTIVE = [p for p in PHASES if p not in PARKED]
DAILY_TASKS = ("filings",)
LOCK = "state/run.lock"

def stores() -> dict[str, CsvStore]:
    return {n: CsvStore(n) for n in lib.SCHEMAS}

# ---------------------------------------------------------------- lock & start
def cmd_start(a):
    st = load_state()
    mode = a.mode
    lockp = ROOT / LOCK
    if lockp.exists():
        info = json.loads(lockp.read_text(encoding="utf-8") or "{}")
        age_h = (now() - dt.datetime.fromisoformat(info.get("started", iso()))).total_seconds() / 3600
        if age_h < CONFIG["budgets"]["lock_stale_hours"]:
            log(None, "lock", {"lock": info}, "skipped: previous run active")
            write_text("logs/run_summaries.md", f"\n## {iso()} — skipped\n\nskipped: previous run active ({info.get('run_id')}).\n", mode="a")
            return out({"proceed": False, "reason": "skipped: previous run active", "lock": info})
        log(None, "lock", {"lock": info}, f"stale lock ({age_h:.1f} h) taken over")
        st.setdefault("history", []).append({"run_id": info.get("run_id"), "ended": "stale lock taken over"})
    run_id = "R-" + now().strftime("%Y%m%d-%H%M%S")
    lib.write_text(LOCK, json.dumps({"run_id": run_id, "started": iso(), "mode": mode}))
    st["current_run"] = {"run_id": run_id, "started": iso(), "mode": "test" if mode == "test" else mode,
                         "iterations": 0, "domain_requests": {}, "url_requests": {}, "attention": [],
                         "phases_worked": [], "counts": {}, "refusals": [], "injections": [], "notes": []}
    # approvals: report decisions; each is consumed by the action that uses it
    decisions = lib.read_approvals()
    pending = [r for r in lib.pending_requests() if r["id"] not in st["consumed_approvals"]]
    for r in pending:
        if decisions.get(r["id"]) == "REJECT":
            lib.consume(st, r["id"])
            log(st, "approval_decision", {"id": r["id"]}, "rejected by user")
    save_state(st)
    log(st, "run", {"mode": mode}, "started")
    dash = _dashboard_start(st, mode)
    if dash.get("paused"):
        st["current_run"] = None
        save_state(st)
        if lockp.exists():
            lockp.unlink()
        return out({"proceed": False, "reason": "paused on the dashboard (press Start there to resume)", "dashboard": dash})
    return out({"proceed": True, "run_id": run_id, "mode": mode, "dashboard": dash, **_plan(st)})

DASH_EVERY = 14 * 3600  # scheduled twice a day (06:17 and 16:17 JST); the longest gap is 14 hours

def _dashboard_start(st, mode) -> dict:
    """Check in with the seminar dashboard (not in test mode). Paused -> the run does no work."""
    if mode == "test":
        return {"enabled": False}
    try:
        import dashboard
        if not dashboard.enabled():
            return {"enabled": False}
        import inbox   # A49: files the student uploads to Main's or C&A's Inbox become citable evidence
        for prof in ("main", "ca"):
            c = dashboard.client(prof)
            c.accept_inbox(lambda item, path, c=c: inbox.store(st, item, path, c.report), check=False)
        reply = dashboard.check_in(every=DASH_EVERY)
        if dashboard.is_paused():
            dashboard.report("Paused on the dashboard: run skipped", "status")
            dashboard.flush()
            log(st, "dashboard", {}, "paused: run skipped")
            return {"enabled": True, "paused": True}
        dashboard.report(f"Agent online: run {st['current_run']['run_id']} ({mode})", "status")
        # the other dashboard agents (A35): pausing C&A or a debater skips the consensus debate, not the run
        others, paused = [], []
        for prof in dashboard.configured():
            if prof == "main":
                continue
            c = dashboard.client(prof)
            c.check_in(every=DASH_EVERY)
            others.append(prof)
            if c.is_paused():
                paused.append(prof)
                c.report("Paused on the dashboard: debate skipped this run", "status")
            else:
                c.report(f"Online: run {st['current_run']['run_id']} ({mode})", "status")
        return {"enabled": True, "paused": False, "reachable": bool(reply), "buffered": dashboard.pending(),
                "agents": ["main"] + others, "paused_profiles": paused}
    except Exception as e:  # the dashboard must never stop the agent
        log(st, "dashboard", {}, f"check-in failed: {type(e).__name__}")
        return {"enabled": False}

def _due_daily(st, task: str) -> bool:
    t = now()
    last = st["daily"].get(task)
    return t.hour >= 5 and last != t.date().isoformat()

def _plan(st) -> dict:
    incomplete = [p for p in ACTIVE if st["phases"][p]["status"] != "complete"]
    daily = [t for t in DAILY_TASKS if _due_daily(st, t)] if not incomplete else []
    pend = [r["id"] for r in lib.pending_requests() if r["id"] not in st["consumed_approvals"]]
    return {"initial_build_remaining": incomplete, "next_phase": incomplete[0] if incomplete else "steady",
            "parked_phases": PARKED,
            "daily_due": daily, "pending_approvals": pend, "budgets": CONFIG["budgets"],
            "env": {k: bool(os.environ.get(k)) for k in ("EDINET_API_KEY", "GOOGLE_SHEET_ID", "GOOGLE_APPLICATION_CREDENTIALS", "REINFOLIB_API_KEY")}}

def cmd_plan(a):
    st = load_state()
    return out(_plan(st))

# ---------------------------------------------------------------- phases
def _phase(st, p, fn):
    cr = run_ctx(st)
    cr["phases_worked"].append(p)
    ph = st["phases"][p]
    try:
        result = fn(st)
        if result.get("_incomplete"):
            ph["status"] = "in_progress"
        else:
            ph["status"] = "complete"
            ph["last_success"] = iso()
        ph["fail_streak"] = 0
    except BudgetStop as e:
        ph["status"] = "in_progress"
        result = {"budget_stop": str(e)}
        cr["notes"].append(f"{p}: budget stop — {e}; cursor saved")
    except (FetchError, GuardrailError) as e:
        ph["status"] = "blocked" if isinstance(e, GuardrailError) else "in_progress"
        ph["fail_streak"] += 1
        result = {"error": str(e)}
        cr["notes"].append(f"{p}: {e}")
        if ph["fail_streak"] >= 3:
            cr["attention"].append(f"{p} has failed in 3 consecutive runs: {e}")
    cr["counts"][p] = result
    save_state(st)
    log(st, "phase", {"phase": p}, result)
    return out({"phase": p, "status": ph["status"], "result": result})

def p1(st):
    import edinet
    from bs4 import BeautifulSoup
    docs = edinet.discover(st, CONFIG["edinet_code"], CONFIG["fiscal_years"], "tsuchiya")
    missing = [t for t, d in docs["fy"].items() if not d["asr"]]
    # 統合報告書: scan the company IR pages (the agent adds its own WebSearch via integrated-record)
    searched = st["documents"].setdefault("integrated_search", {"urls": [], "found": [], "date": None})
    for url in ("https://www.tsuchiya.co.jp/ir/", "https://www.tsuchiya.co.jp/ir/news/"):
        if url in searched["urls"]:
            continue
        body, meta = lib.fetch(st, url, purpose="search for 統合報告書")
        soup = BeautifulSoup(body, "lxml")
        for a in soup.find_all("a", href=True):
            if "統合報告" in a.get_text() or "integrated" in a["href"].lower():
                searched["found"].append({"url": a["href"], "title": a.get_text(strip=True)})
        searched["urls"].append(url)
    searched["date"] = iso()[:10]
    return {"annual_reports": {t: (d["asr"] or {}).get("docID") for t, d in docs["fy"].items()},
            "corrections": [c["docID"] for c in docs["corrections"]], "integrated_found": searched["found"],
            "_incomplete": bool(missing)}

def p2(st):
    import edinet
    sec = CsvStore("Securities Reports")
    docs = st["documents"].get("tsuchiya", {"fy": {}})
    done = st["phases"]["P2"].setdefault("cursor", None) or []
    if not isinstance(done, list):
        done = []
    terms = list(CONFIG["fiscal_years"])
    stats = []
    try:
        for i, term in enumerate(terms):
            d = docs["fy"].get(term, {}).get("asr")
            if not d or d["docID"] in done:
                continue
            prior = terms[i - 1] if i > 0 else None  # stay within the five target fiscal years
            stats.append(edinet.extract_doc(st, sec, term, d, prior))
            done.append(d["docID"])
            st["phases"]["P2"]["cursor"] = done
        for c in docs.get("corrections", []):
            if c["docID"] in done:
                continue
            term = next((t for t, e in CONFIG["fiscal_years"].items() if e == c.get("periodEnd")), None)
            if term and c.get("xbrlFlag") == "1":
                stats.append(edinet.extract_doc(st, sec, term, c, None))
            done.append(c["docID"])
            st["phases"]["P2"]["cursor"] = done
    finally:
        saved = sec.save(st)
    missing = [t for t in terms if not docs["fy"].get(t, {}).get("asr")]
    return {"docs": stats, "csv": saved, "_incomplete": bool(missing) or not saved.get("saved")}

def p3(st):
    ir = CsvStore("Integrated Reports")
    run_id = run_ctx(st)["run_id"]
    s = st["documents"].get("integrated_search", {"urls": [], "found": [], "date": iso()[:10]})
    web = st["documents"].get("integrated_websearch", [])
    found = s.get("found", []) + [w for w in web if w.get("is_integrated_report")]
    searched_urls = s.get("urls", []) + [w["query_or_url"] for w in web if w.get("query_or_url")]
    n = 0
    for term, pe in CONFIG["fiscal_years"].items():
        hit = [f for f in found if str(int(pe[:4])) in (f.get("title", "") + f.get("url", ""))]
        row = base_row(record_id=record_id("integrated", term), report_title=hit[0]["title"] if hit else "not published",
                       section="(whole report)", line_item_ja="統合報告書の発行", line_item_en="Integrated report published",
                       value="published" if hit else "not published", unit="", scale="", period_label=f"FY ending {pe}",
                       period_end=pe, source_type="company IR site search",
                       source_title="Searched: " + "; ".join(searched_urls)[:400],
                       source_url=hit[0]["url"] if hit else (searched_urls[0] if searched_urls else "https://www.tsuchiya.co.jp/ir/"),
                       location_in_source="IR pages and web search results", as_reported_in="",
                       extraction_method="HTML/web search",
                       correction_note=("" if hit else f"No 統合報告書 found for this FY (searched {s.get('date')}). "
                                        "株主通信/決算説明資料 are not substituted (Addendum A4)."))
        ir.upsert(row, run_id)
        n += 1
    saved = ir.save(st)
    if any(w.get("candidate_substitute") for w in web):
        run_ctx(st)["notes"].append("Candidate substitutes (not used, A4): " +
                                    "; ".join(w["title"] for w in web if w.get("candidate_substitute"))[:300])
    return {"rows": n, "csv": saved, "integrated_found": len(found)}

def p4(st):
    import validate
    S = stores()
    res = validate.run_checks(st, S["Securities Reports"], S["Alternative Data"], S["Baseline Metrics"])
    for f in res["headline_failures"]:
        run_ctx(st)["attention"].append(f"Validation failed on headline figure: {f['note']}")
    saved = [S[n].save(st) for n in ("Securities Reports", "Alternative Data")]
    return {"summary": res["summary"], "failures": len(res["failures"]), "headline_failures": len(res["headline_failures"]),
            "csv": saved}

def p5(st):
    import sheets
    S = stores()
    extra = [{"kind": "pending approval", "detail": f"{r['id']}: {r['kind']}"} for r in lib.pending_requests()
             if r["id"] not in st["consumed_approvals"]]
    extra += [{"kind": "needs attention", "detail": x} for x in run_ctx(st)["attention"]]
    res = sheets.sync(st, S, extra, test=run_ctx(st).get("mode") == "test")
    for n in S:
        if S[n].changed:
            S[n].save(st)
    if res.get("error"):
        run_ctx(st)["attention"].append(res["error"])
        res["_incomplete"] = True
    if res.get("pending"):
        res["_incomplete"] = True
    return res

def p6(st):
    import metrics
    S = stores()
    sec, base = S["Securities Reports"], S["Baseline Metrics"]
    r1 = metrics.historical(st, sec, base)
    try:
        r0 = metrics.tanshin_actuals(st, base)
    except (FetchError, GuardrailError) as e:
        r0 = {"error": str(e)}
    pin = {}
    try:
        pin = metrics.peer_inputs(st)
    except (FetchError, BudgetStop) as e:
        pin = {"paused": str(e)}
    try:
        r2 = metrics.current(st, sec, base)
    finally:
        saved = base.save(st)
    run_ctx(st)["attention"].extend(w for w in r2["warnings"] if "moved" in w or "disagree" in w)
    run_ctx(st)["notes"].extend(w for w in r2["warnings"] if "moved" not in w and "disagree" not in w)
    peers_done = all(st["documents"].get(f"peer:{p['ticker']}", {}).get("latest_facts") for p in CONFIG["peers"])
    return {"headline": headline(base), "historical": r1, "tanshin": r0, "peer_inputs": pin, "csv": saved, "_incomplete": not peers_done}

def p7(st):
    import altdata
    alt = CsvStore("Alternative Data")
    res = {}
    observed = set()
    steps = [
        ("iest", lambda: _scan(altdata.scan_iest(st, alt, observed), alt, observed, "https://iestdot.com/")),
        ("rentals", lambda: _scan(altdata.scan_rentals(st, alt, observed), alt, observed, altdata.RNP)),
        ("fudosan_count", lambda: altdata.scan_fudosan_count(st, alt)),
        ("manual", lambda: altdata.ingest_manual(st, alt)),
        ("homepages", lambda: altdata.snapshot_homepages(st, alt)),
        ("wayback", lambda: altdata.wayback(st, alt)),
        ("benchmark", lambda: altdata.benchmark(st, alt)),
    ]
    try:
        for name, fn in steps:
            try:
                res[name] = fn()
            except (FetchError, GuardrailError) as e:
                res[name] = {"error": str(e)}
            except BudgetStop as e:  # per-domain budget: move on to sources on other domains
                res[name] = {"budget_stop": str(e)}
                if "iteration" in str(e) or "time" in str(e):
                    raise
    finally:
        res["csv"] = alt.save(st)
    st["daily"]["listings"] = now().date().isoformat()
    # The Wayback backlog continues in the daily P7 runs; it does not hold back the steady-state refresh.
    res["wayback_backlog"] = len(altdata.HOMEPAGES) * 5 - len(st["daily"].get("wayback_done", []))
    scans = [res.get("iest", {}), res.get("rentals", {})]
    res["_incomplete"] = not all(s.get("complete") for s in scans)
    return res

def _scan(stats, alt, observed, prefix):
    import altdata
    stats["removed"] = altdata.mark_removed(alt, observed, prefix, stats.get("complete", False))
    return stats

def p8(st):
    import legal
    alt = CsvStore("Alternative Data")
    res = {}
    for name, fn in (("dispositions", lambda: legal.search_dispositions(st, alt)),
                     ("regulations", lambda: legal.check_regulations(st, alt))):
        try:
            res[name] = fn()
        except (FetchError, GuardrailError) as e:
            res[name] = {"error": str(e)}
    before = {r["record_id"]: r["value"] for r in alt.rows.values() if r["record_type"] == "legal_score"}
    try:
        res["score"] = legal.score(st, alt)
    finally:
        alt.save(st)
    after = {r["record_id"]: r["value"] for r in alt.rows.values() if r["record_type"] == "legal_score"}
    res["score_changed"] = before != after
    if res["dispositions"].get("hits_group"):
        run_ctx(st)["attention"].append(f"Administrative disposition record(s) found for a Tsuchiya group entity: {res['dispositions']['hits_group']}")
    res["csv"] = alt.save(st)
    st["daily"]["legal"] = now().date().isoformat()
    return res

def p9(st):
    import dictionary
    S = stores()
    r = dictionary.write(S)
    r["coverage"] = dictionary.coverage(S)
    return r

PHASE_FN = {"P1": p1, "P2": p2, "P3": p3, "P4": p4, "P5": p5, "P6": p6, "P7": p7, "P8": p8, "P9": p9}

def cmd_run(a):
    st = load_state()
    run_ctx(st)
    if a.phase in PARKED:
        raise GuardrailError(f"{a.phase} is parked in v2 (Addendum A19); it belongs to a future alternative-data subagent")
    return _phase(st, a.phase, PHASE_FN[a.phase])

def cmd_steady(a):
    """Every run: price + derived metrics; daily: new-filings check. (v2: listings P7 and legal P8 are parked.)"""
    import edinet
    import metrics
    st = load_state()
    cr = run_ctx(st)
    res = {"headline": None}
    S = stores()
    # Peers' latest 有報 (EV/EBITDA inputs): re-checked once a month, at most 2 peers per run, so a peer's new
    # annual report (or a new peer, A30) is picked up without re-running P6.
    try:
        res["peer_inputs"] = metrics.peer_inputs(st)
    except (FetchError, GuardrailError, BudgetStop) as e:
        res["peer_inputs"] = {"error": str(e)[:160]}
    try:
        r = metrics.current(st, S["Securities Reports"], S["Baseline Metrics"])
        cr["attention"].extend(w for w in r["warnings"] if "moved" in w or "disagree" in w)
        res["metrics"] = S["Baseline Metrics"].save(st)
    except BudgetStop as e:
        res["metrics"] = {"budget_stop": str(e)}
    if _due_daily(st, "filings"):
        try:
            new = edinet.scan_recent(st, days=1)
            known = {d["docID"] for d in st["documents"].get("seen_recent", [])}
            fresh = [d for d in new if d["docID"] not in known]
            st["documents"].setdefault("seen_recent", []).extend(fresh)
            for d in fresh:
                cr["attention"].append(f"New EDINET filing: {d['docDescription']} ({d['docID']})")
                if d["docTypeCode"] in ("120", "130"):
                    st["phases"]["P1"]["status"] = "pending"
                    st["phases"]["P2"]["status"] = "pending"
            res["filings"] = {"new": fresh}
        except (FetchError, BudgetStop) as e:
            res["filings"] = {"error": str(e)}
        st["daily"]["filings"] = now().date().isoformat()
        save_state(st)
    if _due_daily(st, "marketing"):   # A51: marketing data daily; the peers' sites once a week
        import marketing
        last = (st["documents"].get("marketing") or {}).get("last_peers", "")
        weekly = last <= (now().date() - dt.timedelta(days=7)).isoformat()
        res["marketing"] = marketing.refresh(st, S["Alternative Data"], weekly)
        S["Alternative Data"].save(st)
        st["daily"]["marketing"] = now().date().isoformat()
        save_state(st)
    save_state(st)
    res["headline"] = headline(S["Baseline Metrics"])
    return out(res)

def _capture(fn):
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue().strip().splitlines()[-1]

# ---------------------------------------------------------------- agent-supplied inputs
def cmd_news(a):
    import legal
    st = load_state()
    alt = CsvStore("Alternative Data")
    r = legal.ingest_news(st, alt, a.path)
    st["daily"]["news_searched"] = now().date().isoformat()
    r["csv"] = alt.save(st)
    save_state(st)
    return out(r)

def cmd_integrated(a):
    """JSON list: {query_or_url, title, url, is_integrated_report, candidate_substitute}."""
    st = load_state()
    items = json.loads((ROOT / a.path).read_text(encoding="utf-8"))
    st["documents"].setdefault("integrated_websearch", []).extend(items)
    save_state(st)
    log(st, "integrated_search", {"n": len(items)}, "recorded")
    return out({"recorded": len(items)})

def cmd_refusal(a):
    st = load_state()
    run_ctx(st)["refusals"].append({"request": a.text[:200], "source": a.source})
    log(st, "refusal", {"request": a.text[:200], "source": a.source}, "declined: out of scope or prohibited")
    save_state(st)
    return out({"logged": True})

def cmd_injection(a):
    st = load_state()
    run_ctx(st)["injections"].append({"url": a.url, "excerpt": a.excerpt[:120]})
    log(st, "injection_attempt", {"url": a.url, "excerpt": a.excerpt[:120]}, "ignored")
    save_state(st)
    return out({"logged": True})

def cmd_peek(a):
    s = CsvStore(a.csv)
    rows = list(s.rows.values())
    for w in a.where or []:
        k, v = w.split("=", 1)
        rows = [r for r in rows if r.get(k) == v]
    keep = ["record_id", "line_item_en", "metric", "record_type", "period_end", "value", "unit", "agent_status",
            "validation_status", "source_url", "correction_note"]
    res = {"total": len(s.rows), "matched": len(rows),
           "sample": [{k: r[k][:120] for k in keep if k in r} for r in rows[: a.limit]]}
    if not rows and a.where:  # a miss: show what the filtered columns actually contain, instead of nothing
        res["available"] = {k: sorted({r.get(k, "") for r in s.rows.values()} - {""})[:60]
                            for k in (w.split("=", 1)[0] for w in a.where)}
    return out(res)

# ---------------------------------------------------------------- finish
SUMMARY_TMPL = """
## {run_id} — {mode}

- Start / end (JST): {start} / {end}
- Iterations: {iters}; web requests by domain: {domains}
{attention}
### Plan
{plan}

### Phases worked
{phases}

### Rows added / changed
{rows}

### Validation
{validation}

### Pending approvals
{pending}

### Refusals and injection attempts
{refusals}

### Notes
{notes}

### Next planned
{next}
"""

def cmd_finish(a):
    st = load_state()
    cr = run_ctx(st)
    S = stores()
    rows = []
    for n, s in S.items():
        added = sum(1 for r in s.rows.values() if r["run_id"] == cr["run_id"])
        rows.append(f"- {n}: {len(s.rows)} rows total; {added} written this run")
    val = cr["counts"].get("P4", {}).get("summary") if cr["counts"].get("P4") else None
    pend = [f"- {r['id']}: {r['kind']} — {r['text'].splitlines()[0][len(r['id']) + 3:]}" for r in lib.pending_requests()
            if r["id"] not in st["consumed_approvals"]]
    plan = (ROOT / a.plan).read_text(encoding="utf-8").strip() if a.plan and (ROOT / a.plan).exists() else "(no plan recorded)"
    att = ""
    if cr["attention"]:
        att = "\n**NEEDS ATTENTION**\n" + "\n".join(f"- {x}" for x in cr["attention"]) + "\n"
    phases = "\n".join(f"- {p}: {st['phases'][p]['status']}" for p in dict.fromkeys(cr["phases_worked"])) or "- steady-state refresh only"
    body = SUMMARY_TMPL.format(
        run_id=cr["run_id"], mode=cr["mode"], start=cr["started"], end=iso(), iters=cr["iterations"],
        domains=json.dumps(cr["domain_requests"], ensure_ascii=False), attention=att, plan=plan, phases=phases,
        rows="\n".join(rows), validation=json.dumps(val, ensure_ascii=False) if val else "checks not run this run",
        pending="\n".join(pend) or "- none",
        refusals="\n".join([f"- refused: {r['request']} (source: {r['source']})" for r in cr["refusals"]] +
                           [f"- injection ignored at {i['url']}: \"{i['excerpt']}\"" for i in cr["injections"]]) or "- none",
        notes="\n".join(f"- {x}" for x in cr["notes"][:20]) + (f"\n- … {len(cr['notes']) - 20} more in logs/run_log.jsonl" if len(cr["notes"]) > 20 else "") or "- none",
        next=_plan(st)["next_phase"])
    lines = body.strip().splitlines()
    if len(lines) > 60:
        body = "\n".join(lines[:55]) + f"\n\n(… summary truncated; see logs/run_log.jsonl for run {cr['run_id']})\n"
    write_text("logs/run_summaries.md", "\n" + body.strip() + "\n", mode="a")
    st.setdefault("history", []).append({"run_id": cr["run_id"], "ended": iso(), "iterations": cr["iterations"],
                                         "attention": len(cr["attention"])})
    st["history"] = st["history"][-200:]
    st["current_run"] = None
    save_state(st)
    lockp = ROOT / LOCK
    if lockp.exists():
        lockp.unlink()
    log(None, "run", {"run_id": cr["run_id"]}, "finished")
    dash = _dashboard_finish(st, cr, S) if cr["mode"] != "test" else "test mode: no dashboard"
    committed = _git_commit(cr) if cr["mode"] != "test" else "test mode: no commit"
    return out({"finished": cr["run_id"], "summary_lines": len(body.splitlines()), "attention": cr["attention"], "git": committed,
                "dashboard": dash})

REPO_DATA = "https://github.com/renishly/tsuchiya_holdings/tree/main/v2/app/data"
CHANGELOG_DIR = ROOT.parent / "changelog"   # v2/changelog/v2.NNN.html (tracked)

def current_version() -> tuple[str, Path | None]:
    """The latest changelog entry, e.g. ("v2.004", path). Every dashboard agent shows it as its version (A35)."""
    best = None
    for f in CHANGELOG_DIR.glob("v*.html") if CHANGELOG_DIR.exists() else []:
        try:
            key = tuple(int(x) for x in f.stem.lstrip("v").split("."))
        except ValueError:
            continue
        if best is None or key > best[0]:
            best = (key, f)
    return (best[1].stem, best[1]) if best else ("unknown", None)

def _vkey(name: str):
    try:
        return tuple(int(x) for x in name.lstrip("v").split("."))
    except ValueError:
        return None

def changelog_since(last: str | None) -> Path | None:
    """One page with every changelog newer than `last` (the one the dashboard last received), newest first (A50).
    A single new version is posted as it is; without a record of `last`, only the newest is posted."""
    files = sorted((k, f) for f in (CHANGELOG_DIR.glob("v*.html") if CHANGELOG_DIR.exists() else []) if (k := _vkey(f.stem)))
    lk = _vkey(last) if last else None
    newer = [f for k, f in files if k > lk] if lk else [f for _, f in files[-1:]]
    if len(newer) <= 1:
        return newer[0] if newer else None
    newer.reverse()                                   # newest first
    texts = [f.read_text(encoding="utf-8") for f in newer]
    head = texts[0][:texts[0].find("<body>")]
    head = re.sub(r"<title>.*?</title>", f"<title>Changelog {newer[-1].stem}–{newer[0].stem}</title>", head, count=1, flags=re.S)
    parts = []
    for f, t in zip(newer, texts):
        m = re.search(r"<main>(.*)</main>", t, re.S)
        parts.append(f'<section id="{f.stem}" style="display:flex;flex-direction:column;gap:2.2rem">{m.group(1) if m else ""}</section>')
    toc = " · ".join(f'<a href="#{f.stem}">{f.stem}</a>' for f in newer)
    intro = (f'<header style="display:flex;flex-direction:column;gap:.6rem"><div class="eyebrow">Tsuchiya Holdings (1840) agent · changelog</div>'
             f'<h1>Changes in {newer[-1].stem}–{newer[0].stem}</h1><p>This page collects {len(newer)} versions: {toc}. '
             f'Newest first.</p></header>')
    page = head + "<body>\n<main>\n" + intro + '\n<hr style="border:0;border-top:1px solid var(--rule);width:100%">\n'.join([""] + parts) + "\n</main>\n</body>\n</html>\n"
    rel = f"reports/changelog_{newer[-1].stem}_{newer[0].stem}.html"
    write_text(rel, page)
    return ROOT / rel

def _latest(base, ticker, metric):
    rows = [r for r in base.rows.values() if r["ticker"] == ticker and r["metric"] == metric and r["value"] != ""]
    rows.sort(key=lambda r: (r["quote_time_jst"] or r["period_end"] or "", r["access_date_jst"]))
    return rows[-1] if rows else None

HEADLINE = [("Stock Price", "stock_price_jpy"), ("Market Cap", "market_cap_jpy"), ("P/E (trailing)", "pe_trailing"),
            ("P/E (forward)", "pe_forward"), ("EPS (company forecast)", "eps_forecast"),
            ("EPS (5-year average, actual)", "eps_5y_avg"), ("Target P/E", "target_pe"),
            ("Target Price (P/E)", "price_pe_forecast_eps"), ("Target Price (P/E, trailing EPS)", "price_pe_trailing_eps"),
            ("Target Price (P/E, 5-year average EPS)", "price_pe_5y_avg_eps"), ("Target EV/EBITDA", "target_ev_ebitda"),
            ("Target Price (EV/EBITDA)", "price_ev_ebitda"), ("Average Valuated Price", "average_valuated_price"),
            ("Expected Return (P/E, 5-year average EPS)", "return_pe_5y_avg_eps"),
            ("Expected Return (EV/EBITDA)", "return_ev_ebitda"), ("Dividend Yield (forecast)", "dividend_yield_forecast"),
            ("Consensus Valuated Price", "consensus_valuated_price"),
            # v2.005 (A37-A38): combined value, range, weights and Md
            ("Valuation range low", "valuation_range_low"), ("Valuation range high", "valuation_range_high"),
            ("Expected Return (combined)", "return_combined"), ("Method weights", "method_weights"),
            ("Md (operating profit)", "md_operating_profit"), ("Md (EPS)", "md_eps")]

def headline(base) -> dict:
    """The key 1840 baseline numbers in one small block, so a model never has to search the CSV for them.
    A value that is missing or not meaningful is given with its reason, never filled in."""
    res = {}
    for metric, key in HEADLINE:
        r = _latest(base, "1840", metric)
        if r is None:
            res[key] = "not available"
            continue
        try:
            res[key] = round(float(r["value"]), 4)
        except ValueError:
            res[key] = f"{r['value'] or 'unresolved'}: {r['correction_note'][:80]}"
    px = _latest(base, "1840", "Stock Price")
    res["price_date_source"] = f"{px['quote_time_jst'][:10]} / {px['source_type']}" if px else "not available"
    return res

PEER_KEYS = [("Stock Price", "price"), ("EPS (company forecast)", "eps_forecast"), ("P/E (forward)", "pe_forward"),
             ("EV/EBITDA (trailing)", "ev_ebitda")]
WATCH = [("Average Valuated Price", "average_valuated_price"), ("Target P/E", "target_pe"),
         ("Target EV/EBITDA", "target_ev_ebitda"), ("Stock Price", "stock_price_jpy")]

def _num(r):
    try:
        return round(float(r["value"]), 4)
    except (TypeError, ValueError, KeyError):
        return None

def review_view(base) -> dict:
    """What the orchestrator's review needs: the 1840 headline, each peer's inputs to the target multiples, and the
    last 7 days of the watched values (one value per day)."""
    peers = {}
    for p in CONFIG["peers"]:
        peers[p["ticker"]] = {"name": p["name"], **{k: (_num(r) if (r := _latest(base, p["ticker"], m)) else None)
                                                   for m, k in PEER_KEYS}}
    hist = {}
    for metric, key in WATCH:
        days = {}
        for r in sorted((r for r in base.rows.values() if r["ticker"] == "1840" and r["metric"] == metric),
                        key=lambda r: (r["quote_time_jst"] or r["access_date_jst"])):
            v = _num(r)
            if v is not None:
                days[(r["quote_time_jst"] or r["access_date_jst"])[:10]] = v
        hist[key] = [[d, v] for d, v in sorted(days.items())[-7:]]
    return {"headline": headline(base), "peers": peers, "history_7d": hist}

def cmd_research(a):
    """Research run (A39-A40): universe discovery and filings (resumable), price history, then M1 fit + backtest."""
    import modelfit
    import pricestore
    import universe
    st = load_state()
    cr = run_ctx(st)
    res = {}
    try:
        # within the research budget of 900 EDINET requests per run: up to 700 date lists + 150 downloads
        res["universe"] = universe.refresh(st, max_days=int(os.environ.get("RESEARCH_MAX_DAYS", "700")),
                                           max_downloads=int(os.environ.get("RESEARCH_MAX_DOWNLOADS", "150")))
    except (FetchError, GuardrailError, BudgetStop) as e:
        res["universe"] = {"paused": str(e)[:200]}
    save_state(st)
    if cr.get("mode") != "test":
        try:
            res["prices"] = pricestore.update_prices(st)
        except Exception as e:  # Yahoo unavailable: the backtest waits for the next run
            res["prices"] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
        save_state(st)
    u = res["universe"]
    # fit whenever the panel is large enough; the coverage (filings still missing) is reported with it
    res["model"] = modelfit.research(st)
    if isinstance(res["model"], dict):
        res["model"]["filings_still_missing"] = (u.get("discovery") or {}).get("remaining") if isinstance(u, dict) else None
    save_state(st)
    log(st, "research", {}, {k: (v if k != "universe" else "see result") for k, v in res.items() if k != "prices"})
    return out(res)

def cmd_evidence(a):
    """Gate 0 (A41): collect new items onto the evidence board; list the ones not yet considered by a debate."""
    import evidence
    st = load_state()
    run_ctx(st)
    res = evidence.collect(st)
    save_state(st)
    return out(res)

def cmd_evidence_items(a):
    """Items from the board for the scouts and debaters (by id, or by topic among the gate-0 items)."""
    import evidence
    b = evidence.load_board()
    ids = set(a.ids.split(",")) if a.ids else None
    items = [v for v in b["items"].values() if (ids is None or v["id"] in ids) and (not a.topic or v["topic"] == a.topic)]
    if ids is None and not a.topic:
        items = items[-30:]
    return out({"items": sorted(items, key=lambda x: x["date"], reverse=True)[:30]})

def cmd_evidence_close(a):
    import evidence
    st = load_state()
    run_ctx(st)
    evidence.close_gate(st)
    save_state(st)
    return out({"closed": True})

def cmd_linkcheck(a):
    """A43: resolve, snapshot and excerpt each flagged item's source (code, no model)."""
    import linkcheck
    st = load_state()
    run_ctx(st)
    res = linkcheck.check(st, [i for i in a.ids.split(",") if i])
    save_state(st)
    return out(res)

def cmd_claimcheck(a):
    """A43: every number in a claim must appear in the text of an item it cites; returns annotated positions."""
    import linkcheck
    positions = json.loads((ROOT / a.path).read_text(encoding="utf-8"))
    summary = linkcheck.check_claims(positions)
    return out({"summary": summary, "positions": positions})

def cmd_md_context(a):
    st = load_state()
    return out(st["documents"].get("md_context") or {})

def cmd_report_due(a):
    """Weekly summary due? 7+ days since the last one, or 5+ Md debates since it (A42)."""
    import evidence
    st = load_state()
    last = st.get("reports", {}).get("last_summary", "")
    n = sum(1 for c in evidence.read_cases() if c.get("recorded", "") > last and c.get("stopped_at") != "gate 1 (scouts flagged nothing)")
    age = (now() - dt.datetime.fromisoformat(last)).days if last else None
    due = last == "" or (age is not None and age >= 7) or n >= 5
    return out({"due": due, "days_since": age, "debates_since": n})

def cmd_debate_memory(a):
    import evidence
    return out(evidence.memory(a.name))

def cmd_debate_record(a):
    """Store a debate's result (computed in agent.py by the consensus rule) as the Debate Signal for Md (A42)."""
    import evidence
    st = load_state()
    run_ctx(st)
    res = json.loads((ROOT / a.path).read_text(encoding="utf-8"))
    sig = evidence.record(st, res)
    save_state(st)
    return out({"recorded": True, "debate_signal": sig})

def cmd_digest(a):
    """The reporter's input (A42): condensed record since the last report; tables are built from this by code."""
    import evidence
    import htmlpages
    import modelfit
    st = load_state()
    base = CsvStore("Baseline Metrics")
    since = st.setdefault("reports", {}).get("last_summary", "")
    hist = {}
    for metric in ("Stock Price", "Average Valuated Price", "Md (operating profit)", "Target Price (P/E, v2.005)",
                   "Target Price (EV/EBITDA, forward, F1)", "Target Price (own history, P/B, M2)",
                   "Target Price (M1, P/B regression)", "Target Price (M1, EV/EBITDA regression)"):
        days = {}
        for r in base.rows.values():
            if r["ticker"] == "1840" and r["metric"] == metric and r["value"] not in ("", "not meaningful"):
                try:
                    days[(r["quote_time_jst"] or r["access_date_jst"])[:10]] = round(float(r["value"]), 2)
                except ValueError:
                    pass
        hist[metric] = sorted(days.items())[-7:]
    w = _latest(base, "1840", "Method weights")
    prm = modelfit.load_params()
    cases = [c for c in evidence.read_cases() if c.get("recorded", "") > since]
    return out({"since": since or "first report", "values_last_7_days": hist,
                "weights": json.loads(w["value"]) if w and w["value"].startswith("{") else None,
                "md": {m: (lambda r: r["value"] if r else None)(_latest(base, "1840", m)) for m in
                       ("Md (operating profit)", "Md: Discount Baseline", "Md: Supply Costs (% since guidance)",
                        "Md: Land Price Ave (% since guidance)", "Md: Debate Signal (shadow)")},
                "debates": [{"date": c["recorded"][:10], "items": c.get("items"), "signal": c.get("signal"),
                             "mode": c.get("mode"), "stopped_at": c.get("stopped_at"), "note": c.get("note")} for c in cases],
                "evidence_sources": {k: {"items": v["items"], "newest": v["newest"]}
                                     for k, v in htmlpages.source_counts(evidence.load_board()).items()},
                "research": {"pending": (prm.get("pending") or {}).get("version"), "approved": (prm.get("approved") or {}).get("version"),
                             "accepted_methods": ((prm.get("pending") or prm.get("approved") or {}).get("weights") or {}).get("accepted")},
                "attention": [x for r in st.get("history", [])[-14:] for x in ([] if not r.get("attention") else [r["run_id"]])]})

def cmd_report_write(a):
    """Build the weekly summary report: code tables from the digest + the reporter's checked narrative (A42)."""
    import html as H
    st = load_state()
    run_ctx(st)
    d = json.loads(_capture(lambda: cmd_digest(a)))
    text = (ROOT / a.path).read_text(encoding="utf-8")[:8000]
    today = iso()[:10]
    rows = "".join(f"<tr><td>{H.escape(m)}</td>" + "".join(f"<td>{v}</td>" for _, v in s) + "</tr>"
                   for m, s in d["values_last_7_days"].items() if s)
    dates = max((s for s in d["values_last_7_days"].values()), key=len, default=[])
    deb = "".join(f"<tr><td>{c['date']}</td><td>{len(c.get('items') or [])}</td><td>{c.get('signal')}</td><td>{H.escape(c.get('mode') or '')}</td>"
                  f"<td>{H.escape(c.get('stopped_at') or '')}</td></tr>" for c in d["debates"]) or "<tr><td colspan=5>no debate</td></tr>"
    body = "".join(f"<p>{H.escape(p)}</p>" for p in text.split("\n\n") if p.strip())
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Summary {today}</title><style>body{{font-family:system-ui,sans-serif;max-width:860px;margin:2rem auto;padding:0 16px;line-height:1.55}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0 1.5rem;font-size:.85rem}}td,th{{border-bottom:1px solid #ddd;padding:4px 6px;text-align:left}}th{{background:#f4f4f4}}
@media (prefers-color-scheme:dark){{body{{background:#141917;color:#e2e8e5}}th{{background:#222a28}}td,th{{border-color:#2c3532}}}}</style></head><body>
<h1>Tsuchiya Holdings (1840): summary, {today}</h1><p><i>Written by the reporter agent from the agent's own records; numbers checked by code. Agent baseline from public data; the target and view are the student's.</i></p>
{body}
<h2>Values, last 7 data days (JPY)</h2><table><tr><th>Item</th>{''.join(f'<th>{x}</th>' for x, _ in dates)}</tr>{rows}</table>
<h2>Weights</h2><p>{H.escape(json.dumps(d['weights'], ensure_ascii=False))}</p>
<h2>Md</h2><p>{H.escape(json.dumps(d['md'], ensure_ascii=False))}</p>
<h2>Debates since the last report</h2><table><tr><th>Date</th><th>Items</th><th>Debate Signal (JPY)</th><th>Mode</th><th>Stopped at</th></tr>{deb}</table>
<h2>Research</h2><p>{H.escape(json.dumps(d['research'], ensure_ascii=False))}</p></body></html>"""
    rel = f"reports/summary_{today}.html"
    write_text(rel, page)
    write_text("reports/summary_latest.html", page)
    st.setdefault("reports", {})["last_summary"] = iso()
    st["reports"]["latest_file"] = rel
    save_state(st)
    return out({"written": rel})

def research_view() -> dict:
    """The latest research run, trimmed for the research agent (A40)."""
    import modelfit
    import universe
    prm = modelfit.load_params()
    cur = prm.get("pending") or prm.get("approved")
    if not cur:
        return {"status": "no research run yet"}
    r = lambda d: {k: round(v, 4) if isinstance(v, float) else v for k, v in (d or {}).items()}
    pend = [x for x in lib.pending_requests() if x["kind"] == "model_params" and x["params"].get("version") == cur["version"]]
    return {"version": cur["version"], "status": cur.get("status"), "approval_id": pend[0]["id"] if pend else cur.get("approval"),
            "panel": cur.get("panel"),
            "m1_pb": {"coef": r(cur["m1"]["coef"]), "n": cur["m1"]["n"], "scale": round(cur["m1"]["scale"], 3)},
            "m1_ev": ({"coef": r(cur["m1_ev"]["coef"]), "n": cur["m1_ev"]["n"], "scale": round(cur["m1_ev"]["scale"], 3)}
                      if cur.get("m1_ev") else "not fitted"),
            "tsuchiya": {"features": r({k: v for k, v in (cur["tsuchiya"].get("features") or {}).items() if k in ("roe_norm", "growth", "margin")}),
                         "fitted_pb": r(cur["tsuchiya"].get("fitted_pb")), "fitted_ev_ebitda": r(cur["tsuchiya"].get("fitted_ev"))},
            "backtest": cur.get("backtest"), "weights": cur.get("weights"),
            "screen": {t: {"company": universe.CANDIDATES[t][1], "share": s.get("share"), "in_universe": s.get("pass"), "note": s.get("note")}
                       for t, s in (cur.get("screen") or {}).items() if t in universe.CANDIDATES},
            "approved_in_use": (prm.get("approved") or {}).get("version")}

def cmd_research_view(a):
    return out(research_view())

def cmd_research_summary(a):
    """Store the research agent's summary (already number-checked by agent.py) with the pending parameters."""
    import modelfit
    st = load_state()
    run_ctx(st)
    text = (ROOT / a.path).read_text(encoding="utf-8")[:4000]
    prm = modelfit.load_params()
    cur = prm.get("pending") or prm.get("approved")
    if not cur:
        return out({"stored": False, "reason": "no research run"})
    cur["summary"] = text
    write_text(modelfit.PARAMS, json.dumps(prm, ensure_ascii=False, indent=1))
    import htmlpages
    htmlpages.research_summary(cur)  # A44: HTML, no scripts
    log(st, "research", {"version": cur["version"]}, "summary stored")
    return out({"stored": True, "version": cur["version"]})

def cmd_consensus_record(a):
    """Write the consensus result (computed by v2/consensus.py) as Baseline Metrics rows (Addendum A32)."""
    from metrics import Writer
    st = load_state()
    run_ctx(st)
    res = json.loads((ROOT / a.path).read_text(encoding="utf-8"))
    base = CsvStore("Baseline Metrics")
    ids = [r["record_id"] for m in ("Average Valuated Price",) if (r := _latest(base, "1840", m))]
    w = Writer(st, base, "1840", CONFIG["company_name"])
    today = iso()[:10]
    common = dict(url="https://disclosure2.edinet-fsa.go.jp/", title="consensus between subagents", location="",
                  source_type="derived", key_extra=today)
    prices = ", ".join(f"{k} {v:.2f}" for k, v in res["prices"].items())
    weights = ", ".join(f"{k} {v:.0%}" for k, v in res["weights"].items())
    note = (f"Status: {res['status']} (spread {res['spread']:.1%}; agreed if within the configured limit). "
            f"Prices: {prices}. Weights ({res['weighting']}): {weights}. Rounds: {res['rounds']}; "
            f"rejected claims: {res['rejected_claims']}. {res.get('statement', '')}")[:900]
    w.put("Consensus Valuated Price", "derived (consensus)", "", res["price"], "JPY/share",
          "= weighted mean of the subagents' computed prices (weights from the configured rule)", ids, note=note, **common)
    w.put("Consensus range low", "derived (consensus)", "", res["range"][0], "JPY/share", "= lowest subagent price", ids, **common)
    w.put("Consensus range high", "derived (consensus)", "", res["range"][1], "JPY/share", "= highest subagent price", ids, **common)
    saved = base.save(st)
    save_state(st)
    log(st, "consensus", {"status": res["status"]}, {"price": res["price"], "range": res["range"]})
    return out({"recorded": True, "csv": saved})

def cmd_headline(a):
    return out(review_view(CsvStore("Baseline Metrics")))

def _overview_values(st, base, cr, ver, pend) -> dict:
    """Values for the Main overview page (A44), read from Baseline Metrics, state and the research parameters."""
    import htmlpages
    import modelfit
    def v(metric):
        r = _latest(base, "1840", metric)
        return r["value"] if r else None
    px = _latest(base, "1840", "Stock Price")
    wr = _latest(base, "1840", "Method weights")
    weights = json.loads(wr["value"]) if wr and (wr["value"] or "").startswith("{") else {}
    methods = []
    for k, metric in htmlpages.METHOD_ROW.items():
        r = _latest(base, "1840", metric)
        if not r:
            continue
        note = r["correction_note"][:160] if r["correction_note"] else ""
        methods.append((k, r["value"], weights.get(k), note))
    mc = st["documents"].get("md_context") or {}
    ds = st["documents"].get("debate_signal") or {}
    prm = modelfit.load_params()
    cur = prm.get("pending") or prm.get("approved") or {}
    bt = cur.get("backtest") or {}
    ms = {k: r for k, r in (bt.get("methods") or {}).items() if isinstance(r, dict) and "mean_ic" in r}
    ms = {k: {**r, "t": r.get("t", r.get("t_nw"))} for k, r in ms.items()}  # runs before v2.008 stored t_nw (monthly)
    freq = bt.get("freq", "monthly")
    passed = [k for k, r in ms.items() if (r.get("mean_ic") or 0) > 0 and (r.get("t") or 0) >= 2]
    n = max((r.get("months") or 0 for r in ms.values()), default=0)
    sentence = (f"Latest test: {n} {freq} cross-sections ({'one per year' if freq == 'annual' else 'month-ends, overlapping'}); "
                + (f"passed: {', '.join(passed)}." if passed else "no method passed the bar yet, so all valid methods keep equal weights."))
    return {"price": v("Stock Price"), "price_date": px["quote_time_jst"][:10] if px else "", "combined": v("Average Valuated Price"),
            "range_low": v("Valuation range low"), "range_high": v("Valuation range high"),
            "expected_return": v("Expected Return (combined)"), "md": v("Md (operating profit)"), "methods": methods,
            "weights_rule": (wr or {}).get("formula", ""),
            "md_parts": {"forecast_op": mc.get("forecast_operating_profit"), "forecast_fy": mc.get("forecast_fy"),
                         "guidance_date": mc.get("guidance_date"), "months_left": mc.get("months_left"),
                         "baseline": v("Md: Discount Baseline"), "supply": v("Md: Supply Costs (% since guidance)"),
                         "land": v("Md: Land Price Ave (% since guidance)"),
                         "debate": v("Md: Debate Signal") or v("Md: Debate Signal (shadow)"), "debate_mode": ds.get("mode"),
                         "orders": v("Md: Order Momentum (% YTD orders vs last year)"),
                         "orders_note": ((_latest(base, "1840", "Md: Order Momentum (% YTD orders vs last year)") or {}).get("correction_note") or "")[:220],
                         "term": {k: v(f"Md term: {lab}") for k, lab in (("orders", "Order Momentum"), ("supply", "Supply Costs"), ("land", "Land Price Ave"))},
                         "fit": (prm.get("md") or {}).get("terms") or {}, "trends": st["documents"].get("trends")},
            "research": {"version": cur.get("version"), "status": cur.get("status"), "methods": ms, "sentence": sentence,
                         "companies": (cur.get("panel") or {}).get("companies"),
                         "approved": (prm.get("approved") or {}).get("version")},
            "pending": pend, "version": ver, "run_id": cr["run_id"]}

SHARED_SKILLS = ["guardrails", "peer-multiples", "forecast-inputs", "forecast-discount", "own-history", "pb-method",
                 "valuation-combine", "universe", "model-fit", "backtest", "evidence-board", "summary-report", "number-check",
                 "news-scout", "debate-position", "debater-memory"]   # A46: the student's choice (30 Oct 2026)
SKILL_SHARE = (os.environ.get("SKILL_SHARE") or "seminar").strip().lower()   # class | seminar
SKILL_FOOTER = ("\n\n---\nShared by Tsuchibot, a research agent valuing Tsuchiya Holdings (1840) from public data for a seminar "
                "thesis. Tool names in backticks are that agent's own. Benchmark from public data; not investment advice.\n")

def _dashboard_skills(st) -> str:
    """Post the shared skills (A46) when a SKILL.md changed since it was last posted, then the skill tree with their ids.
    Posting the same name again updates the skill (next version). Returns a short note, or '' when nothing changed."""
    import hashlib
    import dashboard
    sk_dir = Path(__file__).resolve().parent.parent / "skills"   # code directory (not the data root)
    sent = st.setdefault("dashboard_sent", {}).setdefault("skills", {})
    posted, failed = [], []
    for name in SHARED_SKILLS:
        f = sk_dir / name / "SKILL.md"
        if not f.exists():
            continue
        text = f.read_text(encoding="utf-8").rstrip() + SKILL_FOOTER
        h = hashlib.sha256((SKILL_SHARE + text).encode()).hexdigest()[:16]
        if sent.get(name, {}).get("hash") == h:
            continue
        m = re.search(r"(?ms)^description:(.*?)^(?:---|\w+:)", text)
        summary = " ".join(m.group(1).split())[:300] if m else None
        r = dashboard.post_skill(text, name=name, summary=summary, share=SKILL_SHARE)
        if r:
            sent[name] = {"hash": h, "identifier": r.get("identifier"), "version": r.get("version"), "address": r.get("address")}
            posted.append(name)
        else:
            failed.append(name)
    tree = (sk_dir / "SKILL_TREE.md").read_text(encoding="utf-8").rstrip()
    ids = "\n".join(f"- {n}: {v.get('identifier')} (v{v.get('version')})" for n, v in sent.items() if n in SHARED_SKILLS and isinstance(v, dict))
    tree += ("\n\n## Shared skills on the market\n" + ids + "\n") if ids else "\n"
    th = hashlib.sha256(tree.encode()).hexdigest()[:16]
    tree_set = False
    if st["dashboard_sent"].get("skill_tree") != th and dashboard.set_skill_tree(tree):
        st["dashboard_sent"]["skill_tree"], tree_set = th, True
    if posted or failed or tree_set:
        save_state(st)
    if not (posted or failed or tree_set):
        return ""
    return (f"skills: {len(posted)} posted ({SKILL_SHARE})" + (f", {len(failed)} failed ({', '.join(failed)})" if failed else "")
            + ("; skill tree updated" if tree_set else ""))

MONITORS = {  # A47: the monitors each dashboard agent keeps; any other monitor is removed at the end of a run
    "main": ["Overview (read me first)", "Data gaps and errors", "Status", "Version", "Build phases", "Rows per CSV", "Pending approvals",
             "Data (CSV files)", "Google Sheet", "Data dictionary (report_list.html)", "Debug log (debug_runs.csv)",
             "Current market price", "Average valuated price", "Valuation (agent baseline)", "Model weights",
             "Md (forecast discount)", "Research (M1 and backtest)", "Backtest report (backtest_latest.html)"],
    "ca": ["Overview (read me first)", "Version", "Md debate: Debate Signal", "Arbitration (decision models)", "Evidence sources"],
    "debater_a": ["Overview (read me first)", "Version", "Role", "Latest position"],
    "debater_b": ["Overview (read me first)", "Version", "Role", "Latest position"],
}

def _dashboard_prune():
    """Remove monitors left over from earlier versions (A47). Monitors set during this run are always kept; each
    removal is logged in that agent's activity log by the dashboard itself, plus one summary line here."""
    import dashboard
    for prof, keep in MONITORS.items():
        c = dashboard.client(prof)
        if not c.enabled() or c.is_paused():
            continue
        gone = c.prune_monitors(keep)
        if gone:
            c.report(("Removed old monitors: " + ", ".join(gone))[:300], "log")

def _dashboard_finish(st, cr, S) -> str:
    """Send this run's chosen results: one 'run summary' output, NEEDS ATTENTION items, and a few monitors."""
    try:
        import dashboard
        if not dashboard.enabled():
            return "not configured"
        dashboard.check_in(every=DASH_EVERY)
        phases = {p: ("parked (v2)" if p in PARKED else st["phases"][p]["status"]) for p in PHASES}
        rows = {n: len(s.rows) for n, s in S.items()}
        pend = [r["id"] + " " + r["kind"] for r in lib.pending_requests() if r["id"] not in st["consumed_approvals"]]
        for a_ in cr["attention"]:
            dashboard.report(f"NEEDS ATTENTION: {a_}"[:300], "error")
        import htmlpages  # A44: every report is a script-free HTML file
        ver, ver_file = current_version()
        dashboard.output(ROOT / htmlpages.run_summary(cr, phases, rows, pend, ver), "run summary")
        done = sum(1 for p in ACTIVE if phases[p] == "complete")
        dashboard.set_monitor(f"{done}/{len(ACTIVE)} baseline phases complete; last run {cr['run_id']}"
                              + (f"; {len(cr['attention'])} item(s) need attention" if cr["attention"] else ""), "Status")
        dashboard.set_monitor(phases, "Build phases")
        dashboard.set_monitor(rows, "Rows per CSV")
        dashboard.set_monitor(pend or "none", "Pending approvals")
        dashboard.set_monitor(REPO_DATA, "Data (CSV files)")
        sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
        if sheet_id:  # link built at run time from the env; the ID is never written to a file
            dashboard.set_monitor(f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit", "Google Sheet")
        dict_page = ROOT / "reports" / "report_list.html"
        if dict_page.exists():
            dashboard.set_monitor(dict_page, "Data dictionary (report_list.html)")
        debug_csv = ROOT / "logs" / "debug_runs.csv"  # written by v2/app/agent.py: per-subagent run debugging
        if debug_csv.exists():
            dashboard.set_monitor(debug_csv, "Debug log (debug_runs.csv)")
        base = S["Baseline Metrics"]
        px = _latest(base, "1840", "Stock Price")
        if px:  # single value: the latest market price (JPY)
            dashboard.set_monitor(round(float(px["value"]), 2), "Current market price")
        avg = _latest(base, "1840", "Average Valuated Price")
        if avg:  # single value: mean of the two valuation methods (JPY)
            try:
                dashboard.set_monitor(round(float(avg["value"]), 2), "Average valuated price")
            except ValueError:
                dashboard.set_monitor(avg["value"], "Average valuated price")

        def show(metric):
            r = _latest(base, "1840", metric)
            if not r:
                return "not available"
            try:
                return round(float(r["value"]), 4)
            except ValueError:
                return f"{r['value']}: {r['correction_note'][:80]}" if r["value"] else f"not computed: {r['correction_note'][:80]}"
        val = {
            "current market price (JPY)": show("Stock Price"),
            "market price date / source": f"{px['quote_time_jst'][:10]} / {px['source_type']}" if px else "not available",
            "combined value (JPY)": show("Average Valuated Price"),
            "range (JPY)": f"{show('Valuation range low')} - {show('Valuation range high')}",
            "expected return (combined)": show("Expected Return (combined)"),
            "P/E method (JPY)": show("Target Price (P/E, v2.005)"),
            "EPS basis used": show("EPS basis used (P/E)"),
            "EV/EBITDA method, forward (JPY)": show("Target Price (EV/EBITDA, forward, F1)"),
            "own history, P/B (JPY)": show("Target Price (own history, P/B, M2)"),
            "own history, EV/EBITDA (JPY)": show("Target Price (own history, EV/EBITDA, M2)"),
            "reference: P/B vs peers (JPY, weight 0)": show("Target Price (P/B, peers)"),
            "reference: EV/EBITDA trailing pair (JPY)": show("Target Price (EV/EBITDA, trailing pair, F1)"),
            "inputs: forward depreciation / normalised EPS": f"{show('Depreciation (forward, F7)')} / {show('EPS (normalised, ordinary profit, F3)')}",
            "note": "Agent baseline calculated from public data (v2.005); the target and view are the student's (syllabus 2.5).",
        }
        dashboard.set_monitor(val, "Valuation (agent baseline)")
        wr = _latest(base, "1840", "Method weights")
        dashboard.set_monitor({"weights": json.loads(wr["value"]) if wr and wr["value"].startswith("{") else "not available",
                               "methods": (wr or {}).get("correction_note", "")[:600],
                               "rule": "equal across valid methods, halved for a fallback basis, until the backtest sets weights (A38)"},
                              "Model weights")
        dashboard.set_monitor({
            "Md, operating profit (JPY)": show("Md (operating profit)"),
            "Md, EPS (JPY/share)": show("Md (EPS)"),
            "Discount Baseline (JPY)": show("Md: Discount Baseline"),
            "Land Price Ave (% since guidance)": show("Md: Land Price Ave (% since guidance)"),
            "Supply Costs (% since guidance)": show("Md: Supply Costs (% since guidance)"),
            "Debate Signal (JPY)": show("Md: Debate Signal (shadow)") if _latest(base, "1840", "Md: Debate Signal (shadow)") else show("Md: Debate Signal"),
            "note": "value = target multiple × (company forecast − Md); positive Md lowers the forecast. Land and supply "
                    "costs are logged with coefficient 0 until fitted on the panel (A37).",
        }, "Md (forecast discount)")
        try:  # A39-A40: the latest research run (M1 fit, backtest, proposed weights) and its approval state
            import modelfit
            prm = modelfit.load_params()
            cur = prm.get("pending") or prm.get("approved")
            if cur:
                bt = cur.get("backtest", {}).get("methods", {})
                dashboard.set_monitor({
                    "version": cur["version"], "status": cur.get("status"), "approval": cur.get("approval", "see Pending approvals"),
                    "panel": cur.get("panel"), "M1 coefficients": {k: round(v, 4) for k, v in cur["m1"]["coef"].items()},
                    "Tsuchiya fitted P/B": round((cur.get("tsuchiya", {}).get("fitted_pb") or {}).get("pb", float("nan")), 3),
                    "backtest, annual (mean IC / t)": {k: f"{r['mean_ic']} / {r.get('t', r.get('t_nw'))}" for k, r in bt.items() if isinstance(r, dict) and "mean_ic" in r},
                    "proposed weights": (cur.get("weights") or {}).get("weights"),
                    "approved version in use": (prm.get("approved") or {}).get("version", "none (equal weights)"),
                }, "Research (M1 and backtest)")
            rep_ = ROOT / "reports" / "backtest_latest.html"
            if rep_.exists():
                dashboard.set_monitor(rep_, "Backtest report (backtest_latest.html)")
            rp = (st.get("reports") or {}).get("latest_file")
            if rp and st.setdefault("dashboard_sent", {}).get("summary_report") != rp and (ROOT / rp).exists():
                if dashboard.output(ROOT / rp, "weekly summary"):  # A42: the reporter's summary, once per report
                    st["dashboard_sent"]["summary_report"] = rp
                    save_state(st)
            if cur and cur.get("summary") and st.setdefault("dashboard_sent", {}).get("research_summary") != cur["version"]:
                if dashboard.output(ROOT / htmlpages.research_summary(cur), "research summary"):
                    st["dashboard_sent"]["research_summary"] = cur["version"]
                    save_state(st)
        except Exception as e:
            dashboard.report(f"research monitor failed: {type(e).__name__}", "error")
        sent0 = st.setdefault("dashboard_sent", {})
        if not sent0.get("factor_monitor_removed"):  # v2.005 (A36): factor model removed; the parked legal score
            dashboard.delete_monitors("Factor model (Fama-French style)")   # (P8) waits for the alternative-data
            dashboard.delete_monitors("Legal penalty score")                # subagent and leaves the Main page
            sent0["factor_monitor_removed"] = True
            save_state(st)
        # A35: version on every dashboard agent; Main also outputs the stored changelog once per version
        try:  # A44: the plain-language overview of the Main page, first on the page
            dashboard.set_monitor(ROOT / htmlpages.overview_main(_overview_values(st, base, cr, ver, pend)), "Overview (read me first)")
        except Exception as e:
            dashboard.report(f"overview page failed: {type(e).__name__}: {str(e)[:120]}", "error")
        try:  # A52: what could not be retrieved, is outdated or was cut short, and how much it matters
            import errorreport
            dashboard.set_monitor(ROOT / errorreport.page(st, cr, base, ver), "Data gaps and errors")
        except Exception as e:
            dashboard.report(f"error report failed: {type(e).__name__}: {str(e)[:120]}", "error")
        dashboard.set_monitor(ver, "Version")
        sent = st.setdefault("dashboard_sent", {})
        if ver_file and sent.get("changelog") != ver:   # A50: every version since the last one posted, merged
            cl = changelog_since(sent.get("changelog"))
            if cl and dashboard.output(cl, "changelog"):
                sent["changelog"] = ver
                save_state(st)
        try:  # A46: shared skills and the skill tree on the seminar's skill market (only when their text changed)
            sk = _dashboard_skills(st)
        except Exception as e:
            sk = f"skills error: {type(e).__name__}"
        if sk:
            dashboard.report(sk[:300], "error" if "failed" in sk or "error" in sk else "log")
        others = _dashboard_debate(st, base, cr, show, ver)
        try:
            _dashboard_prune()
        except Exception as e:
            dashboard.report(f"monitor cleanup failed: {type(e).__name__}", "error")
        left = dashboard.flush()
        return (f"sent; {left} item(s) buffered" if left else "sent") + (f"; also {', '.join(others)}" if others else "")
    except Exception as e:
        return f"dashboard error: {type(e).__name__}"

def _dashboard_debate(st, base, cr, show, ver) -> list[str]:
    """C&A and Debater A/B show the Md debate (A41-A42). The valuation debate of v2.004-v2.006 (baseline vs factor)
    is retired: its monitors are removed once, so the pages never show factor-era results again."""
    import dashboard
    import evidence
    sent0 = st.setdefault("dashboard_sent", {})
    if not sent0.get("old_debate_monitors_removed"):
        for prof in ("ca", "debater_a", "debater_b"):
            c = dashboard.client(prof)
            if c.enabled():
                for name in ("Consensus valuation", "Arbitration (decision models)", "Role", "Latest position"):
                    c.delete_monitors(name)
        sent0["old_debate_monitors_removed"] = True
        save_state(st)
    cases = evidence.read_cases()
    return _dashboard_md_debate(st, cases[-1] if cases else None, ver)

def _dashboard_md_debate(st, case: dict | None, ver: str) -> list[str]:
    """C&A: the decision and the Debate Signal; Debater A/B: their own positions (A41-A42). Records sent once."""
    import dashboard
    import evidence
    import htmlpages
    board_full = evidence.load_board()
    board = board_full.get("items", {})
    case = case or {"recorded": "", "stopped_at": "no Md debate yet (gate 0 has not opened at a daily check)", "items": [],
                    "signal": 0.0, "mode": "shadow", "note": "", "positions": {}}
    sent_before = st.setdefault("dashboard_sent", {})
    new = sent_before.get("md_case") != case.get("recorded")
    sent = []
    ca = dashboard.client("ca")
    if ca.enabled():
        ca.set_monitor(ver, "Version")
        dec = case.get("decision") or {}
        ca.set_monitor({"date": case.get("recorded", "")[:10], "trigger": case.get("trigger"), "items": case.get("items"),
                        "stopped at": case.get("stopped_at"), "Debate Signal (JPY)": round(case.get("signal") or 0.0),
                        "mode": case.get("mode"), "rule": case.get("note"),
                        "note": "Positive lowers the company's operating-profit forecast (like Md). Shadow mode: logged, not applied."},
                       "Md debate: Debate Signal")
        ca.set_monitor({"judge": dec.get("model"), "evidence score A": dec.get("evidence_a"), "evidence score B": dec.get("evidence_b"),
                        "better supported": dec.get("supported"), "bucket": dec.get("bucket"), "error": dec.get("error"),
                        "turn monitor (Span-01 Lite)": case.get("monitor")}, "Arbitration (decision models)")
        ca.set_monitor(ROOT / htmlpages.overview_ca(case if case.get("recorded") else None, board, evidence.read_cases(), ver, board_full),
                       "Overview (read me first)")
        cnt, srcs = htmlpages.source_counts(board_full), board_full.get("sources") or {}
        ca.set_monitor({label: f"{(cnt.get(k) or {}).get('items', 0)} items; newest {(cnt.get(k) or {}).get('newest') or '—'}"
                        + (f"; ERROR {srcs[k]['error'][:60]}" if (srcs.get(k) or {}).get("error") else "")
                        for k, label, _, _ in htmlpages.SOURCE_ROWS}, "Evidence sources")
        if new and case.get("recorded"):
            ca.output(ROOT / htmlpages.debate_record(case, board), "md debate record")
        ca.flush()
        sent.append("C&A")
    for prof in ("debater_a", "debater_b"):
        c = dashboard.client(prof)
        if not c.enabled():
            continue
        pos = (case.get("positions") or {}).get(prof)
        c.set_monitor(ver, "Version")
        c.set_monitor(f"{'Debater A (proposer)' if prof == 'debater_a' else 'Debater B (sceptic)'} in the Md debate"
                      + (f"; model {pos.get('model')}" if pos else ""), "Role")
        c.set_monitor({"date": case.get("recorded", "")[:10], "direction": pos.get("direction"), "size (JPY)": pos.get("size_jpy"),
                       "confidence": pos.get("confidence"), "summary": pos.get("summary"), "claims kept": len(pos.get("claims") or []),
                       "claims rejected": pos.get("rejected_claims"), "concessions": pos.get("concessions")}
                      if pos else f"no position: debate stopped at {case.get('stopped_at')}", "Latest position")
        c.set_monitor(ROOT / htmlpages.overview_debater(prof, case if case.get("recorded") else None, evidence.memory(prof), ver),
                      "Overview (read me first)")
        if new and pos:
            c.output(ROOT / htmlpages.debate_turns(prof, case), "debate turns")
        c.flush()
        sent.append(dashboard.LABELS[prof].replace("Tsuchibot ", ""))
    if new and sent:
        sent_before["md_case"] = case.get("recorded")
        save_state(st)
    return sent

def _git_commit(cr) -> str:
    """Cloud routine (Addenda A1, A11): commit outputs; plain push of the current branch; never force."""
    top = ROOT.parent
    if not (top / ".git").exists() or os.environ.get("TSUCHIYA_GIT", "1") == "0":
        return "no git repository"
    paths = [str(ROOT / d) for d in ("data", "reports", "state", "logs", "raw", "inputs") if (ROOT / d).exists()]
    try:
        subprocess.run(["git", "-C", str(top), "add", "--"] + paths, check=True, capture_output=True)
        diff = subprocess.run(["git", "-C", str(top), "diff", "--cached", "--quiet"])
        if diff.returncode == 0:
            return "nothing to commit"
        subprocess.run(["git", "-C", str(top), "commit", "-m", f"agent run {cr['run_id']} ({cr['mode']})"], check=True, capture_output=True)
        # Cloud checkouts can be a detached HEAD: push HEAD to main explicitly. A normal push is refused by git
        # unless it is a fast-forward, so this never overwrites history (never --force).
        branch = subprocess.run(["git", "-C", str(top), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True).stdout.strip()
        target = branch if branch and branch != "HEAD" else "main"
        p = subprocess.run(["git", "-C", str(top), "push", "origin", f"HEAD:refs/heads/{target}"], capture_output=True, text=True)
        return f"committed and pushed to {target}" if p.returncode == 0 else f"committed; push failed: {p.stderr.strip()[:160]}"
    except subprocess.CalledProcessError as e:
        return f"git error: {e.stderr.decode()[:120] if e.stderr else e}"

def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("start"); s.add_argument("--mode", default="scheduled"); s.set_defaults(fn=cmd_start)
    s = sub.add_parser("plan"); s.set_defaults(fn=cmd_plan)
    s = sub.add_parser("run"); s.add_argument("phase", choices=PHASES); s.set_defaults(fn=cmd_run)
    s = sub.add_parser("steady"); s.set_defaults(fn=cmd_steady)
    s = sub.add_parser("news-ingest"); s.add_argument("path"); s.set_defaults(fn=cmd_news)
    s = sub.add_parser("integrated-record"); s.add_argument("path"); s.set_defaults(fn=cmd_integrated)
    s = sub.add_parser("refusal"); s.add_argument("text"); s.add_argument("--source", default=""); s.set_defaults(fn=cmd_refusal)
    s = sub.add_parser("injection"); s.add_argument("--url", required=True); s.add_argument("--excerpt", required=True); s.set_defaults(fn=cmd_injection)
    s = sub.add_parser("headline"); s.set_defaults(fn=cmd_headline)
    s = sub.add_parser("consensus-record"); s.add_argument("path"); s.set_defaults(fn=cmd_consensus_record)
    s = sub.add_parser("research"); s.set_defaults(fn=cmd_research)
    s = sub.add_parser("research-view"); s.set_defaults(fn=cmd_research_view)
    s = sub.add_parser("evidence"); s.set_defaults(fn=cmd_evidence)
    s = sub.add_parser("evidence-items"); s.add_argument("--ids", default=""); s.add_argument("--topic", default=""); s.set_defaults(fn=cmd_evidence_items)
    s = sub.add_parser("evidence-close"); s.set_defaults(fn=cmd_evidence_close)
    s = sub.add_parser("debate-memory"); s.add_argument("name"); s.set_defaults(fn=cmd_debate_memory)
    s = sub.add_parser("md-context"); s.set_defaults(fn=cmd_md_context)
    s = sub.add_parser("linkcheck"); s.add_argument("--ids", required=True); s.set_defaults(fn=cmd_linkcheck)
    s = sub.add_parser("claimcheck"); s.add_argument("path"); s.set_defaults(fn=cmd_claimcheck)
    s = sub.add_parser("report-due"); s.set_defaults(fn=cmd_report_due)
    s = sub.add_parser("debate-record"); s.add_argument("path"); s.set_defaults(fn=cmd_debate_record)
    s = sub.add_parser("digest"); s.set_defaults(fn=cmd_digest)
    s = sub.add_parser("report-write"); s.add_argument("path"); s.set_defaults(fn=cmd_report_write)
    s = sub.add_parser("research-summary"); s.add_argument("path"); s.set_defaults(fn=cmd_research_summary)
    s = sub.add_parser("peek"); s.add_argument("csv"); s.add_argument("--where", action="append"); s.add_argument("--limit", type=int, default=5); s.set_defaults(fn=cmd_peek)
    s = sub.add_parser("finish"); s.add_argument("--plan", default="tmp/plan.txt"); s.set_defaults(fn=cmd_finish)
    a = ap.parse_args()
    try:
        a.fn(a)
    except GuardrailError as e:
        st = load_state()
        log(st if st.get("current_run") else None, "guardrail", {"cmd": a.cmd}, f"refused: {e}")
        out({"error": "guardrail", "detail": str(e)})
    except SystemExit:
        raise
    except Exception as e:  # never crash silently: log and report
        log(None, "error", {"cmd": a.cmd}, f"{type(e).__name__}: {str(e)[:300]}")
        try:
            import dashboard
            if dashboard.enabled() and os.environ.get("TSUCHIYA_FIXTURE_SET") is None:
                dashboard.report(f"cli.py {a.cmd} failed: {type(e).__name__}", "error")
        except Exception:
            pass
        out({"error": type(e).__name__, "detail": str(e)[:300]})
        sys.exit(1)

if __name__ == "__main__":
    main()
