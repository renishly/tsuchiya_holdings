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
import subprocess
import sys

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
        reply = dashboard.check_in(every=DASH_EVERY)
        if dashboard.is_paused():
            dashboard.report("Paused on the dashboard: run skipped", "status")
            dashboard.flush()
            log(st, "dashboard", {}, "paused: run skipped")
            return {"enabled": True, "paused": True}
        dashboard.report(f"Agent online: run {st['current_run']['run_id']} ({mode})", "status")
        return {"enabled": True, "paused": False, "reachable": bool(reply), "buffered": dashboard.pending()}
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
            # factor subagent (A28-A29): its own numbers, shown next to the baseline, not in the average
            ("Factor-implied Price", "factor_implied_price"), ("Cost of Equity (factor model)", "cost_of_equity_factor"),
            ("Residual Income Value (reference)", "residual_income_value_reference"),
            ("Consensus Valuated Price", "consensus_valuated_price")]

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
         ("Target EV/EBITDA", "target_ev_ebitda"), ("Stock Price", "stock_price_jpy"),
         ("Factor-implied Price", "factor_implied_price")]

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

def cmd_factors(a):
    """Fama-French-style factor model (Addenda A28-A29): price store, risk-free, betas, r, implied price."""
    import factors
    st = load_state()
    run_ctx(st)
    S = stores()
    try:
        res = factors.run(st, S["Securities Reports"], S["Baseline Metrics"])
    except (FetchError, GuardrailError, BudgetStop) as e:
        res = {"error": str(e)[:200]}
    res["csv"] = S["Baseline Metrics"].save(st)
    save_state(st)
    log(st, "factors", {}, {k: res.get(k) for k in ("cost_of_equity", "factor_implied_price", "error")})
    return out(res)

def cmd_consensus_record(a):
    """Write the consensus result (computed by v2/consensus.py) as Baseline Metrics rows (Addendum A32)."""
    from metrics import Writer
    st = load_state()
    run_ctx(st)
    res = json.loads((ROOT / a.path).read_text(encoding="utf-8"))
    base = CsvStore("Baseline Metrics")
    ids = [r["record_id"] for m in ("Average Valuated Price", "Factor-implied Price") if (r := _latest(base, "1840", m))]
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
        dashboard.output({"run_id": cr["run_id"], "mode": cr["mode"], "phases_worked": list(dict.fromkeys(cr["phases_worked"])),
                          "rows_total": rows, "needs_attention": cr["attention"], "pending_approvals": pend,
                          "notes": cr["notes"][:5], "data": REPO_DATA}, "run summary")
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
            "target P/E (peer mean)": show("Target P/E"),
            "price via P/E, forecast EPS (JPY)": show("Target Price (P/E)"),
            "price via P/E, trailing EPS (JPY)": show("Target Price (P/E, trailing EPS)"),
            "price via P/E, 5-year average EPS (JPY)": show("Target Price (P/E, 5-year average EPS)"),
            "target EV/EBITDA (peer mean)": show("Target EV/EBITDA"),
            "price via EV/EBITDA (JPY)": show("Target Price (EV/EBITDA)"),
            "average valuated price (JPY)": show("Average Valuated Price"),
            "average uses": (avg or {}).get("correction_note", "") or "not available",
            "return via P/E, 5-year average EPS": show("Expected Return (P/E, 5-year average EPS)"),
            "return via EV/EBITDA": show("Expected Return (EV/EBITDA)"),
            "inputs: forecast EPS / 5-year average EPS": f"{show('EPS (company forecast)')} / {show('EPS (5-year average, actual)')}",
            "dividend yield (forecast)": show("Dividend Yield (forecast)"),
            "note": "Agent baseline calculated from public data; the target and view are the student's (syllabus 2.5).",
        }
        dashboard.set_monitor(val, "Valuation (agent baseline)")
        fac = {
            "factor-implied price (JPY)": show("Factor-implied Price"),
            "by start date (1y / 2y / 3y / 5y)": " / ".join(str(show(f"Factor-implied Price ({h}y start)")) for h in (1, 2, 3, 5)),
            "cost of equity r": show("Cost of Equity (factor model)"),
            "risk-free (JGB 1y)": show("Risk-free rate (JGB 1y)"),
            "betas market / SMB / HML": " / ".join(str(show(f"Beta: {k} (factor model)")) for k in ("market", "size SMB", "value HML")),
            "alpha (annual)": show("Alpha, annualised (factor model)"),
            "residual income value (reference only)": show("Residual Income Value (reference)"),
            "note": "Factor proxies from Tokyo-listed ETFs, not Kenneth French's factors. Shown next to the baseline; "
                    "it enters the final number only through the planned consensus.",
        }
        dashboard.set_monitor(fac, "Factor model (Fama-French style)")
        con = _latest(base, "1840", "Consensus Valuated Price")
        if con:  # A32: the final number; the student's own target and view remain separate
            dashboard.set_monitor({
                "consensus price (JPY)": show("Consensus Valuated Price"),
                "range (JPY)": f"{show('Consensus range low')} - {show('Consensus range high')}",
                "current market price (JPY)": show("Stock Price"),
                "date": con["access_date_jst"][:10],
                "details": con["correction_note"][:600],
                "note": "Agent consensus between the baseline and factor subagents; the target and view are the student's.",
            }, "Consensus valuation")
        legal = S["Alternative Data"].get(record_id("legal_score", "total"))
        if legal and legal["value"]:
            dashboard.set_monitor(f"{legal['value']} / 100 (exposure measure, not a finding of wrongdoing)", "Legal penalty score")
        left = dashboard.flush()
        return f"sent; {left} item(s) buffered" if left else "sent"
    except Exception as e:
        return f"dashboard error: {type(e).__name__}"

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
    s = sub.add_parser("factors"); s.set_defaults(fn=cmd_factors)
    s = sub.add_parser("consensus-record"); s.add_argument("path"); s.set_defaults(fn=cmd_consensus_record)
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
