"""Tsuchiya Holdings (1840) research-data agent, v2: orchestrator.

Run from the repository root (GitHub Actions does this):

    python v2/app/agent.py                       # Mode: scheduled (default)
    python v2/app/agent.py --mode test           # frozen fixtures, sandbox copy: no network, Sheet, dashboard or git
    python v2/app/agent.py --mode phase=P6       # one phase once, for debugging

Flow of one run:
  1. start (code): lock, state, approvals, dashboard check-in -> the run plan as JSON. Stops here if another run
     holds the lock or the agent is paused on the dashboard.
  2. routine (code): each subagent's deterministic steps (initial-build phases in order, else steady refresh + P9).
  3. review (model, only on events): a manual/API instruction, unfinished build, a step that did not complete,
     a NEEDS ATTENTION item, a >3% move in Average Valuated Price / Target P/E / Target EV/EBITDA since the last
     review, or the daily check after the TSE close. The orchestrator explains, and may delegate follow-ups.
     Quiet runs make no model request.
  4. close-out (code, always): P4 validation, P5 Sheet sync, finish (run summary, state, lock release,
     dashboard). The workflow then commits v2/ outputs.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import llm  # noqa: E402
from llm import ModelUnavailable, Tool, load_prompt, pipeline, run_agent  # noqa: E402

ROOT = llm.ROOT
DATA_DIRS = ("data", "state", "logs", "reports", "raw", "inputs")
MAX_DELEGATIONS = 4
ORCHESTRATOR_STEPS = 6
# Review triggers (Addendum A26). Moves are measured against the snapshot taken at the last review, so small
# daily moves add up until they cross the threshold.
REVIEW_THRESHOLD = float(os.getenv("REVIEW_THRESHOLD") or "0.03")
REVIEW_WATCH = {"average_valuated_price": "Average Valuated Price", "target_pe": "Target P/E",
                "target_ev_ebitda": "Target EV/EBITDA"}
DAILY_CHECK_AFTER = (15, 30)  # JST: the first run at or after the TSE close each day is always reviewed
CONSENSUS_MAX_AGE_DAYS = 7    # A32: a consensus is re-debated at least weekly even when nothing moved
ORCHESTRATOR_SKILLS = ["event-review", "delegate-task", "guardrails"]   # A36: skills/<name>/SKILL.md


def data_root() -> Path:
    """Where the pipeline reads and writes: v2/, or the test sandbox (TSUCHIYA_ROOT)."""
    return Path(os.environ.get("TSUCHIYA_ROOT") or ROOT)


def make_test_sandbox() -> Path:
    """Test mode runs on a throwaway copy of v2's data, so fixture results never touch the real CSVs or state."""
    tmp = Path(tempfile.mkdtemp(prefix="holdings-v2-test-"))
    for d in DATA_DIRS:
        if (ROOT / d).exists():
            shutil.copytree(ROOT / d, tmp / "v2" / d)
    (tmp / "v2" / "state" / "run.lock").unlink(missing_ok=True)
    if (ROOT / "evals" / "market").exists():  # frozen daily price store for test mode
        shutil.copytree(ROOT / "evals" / "market", tmp / "v2" / "evals" / "market")
    fixtures = ROOT / "evals" / "fixtures"
    if not fixtures.exists():
        fixtures = llm.REPO / "v1" / "evals" / "fixtures"
    (tmp / "v1" / "evals").mkdir(parents=True)
    (tmp / "v1" / "evals" / "fixtures").symlink_to(fixtures)
    os.environ["TSUCHIYA_ROOT"] = str(tmp / "v2")
    return tmp / "v2"


JST = dt.timezone(dt.timedelta(hours=9))
ORCH_STATE = "state/orchestrator.json"   # close-out hashes and per-run delegation history (written by this file only)


def load_orch() -> dict:
    p = data_root() / ORCH_STATE
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"closeout": {}, "delegation_history": []}


def save_orch(o: dict) -> None:
    p = data_root() / ORCH_STATE
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(o, ensure_ascii=False, indent=1), encoding="utf-8")


def outputs_hash() -> str:
    """Fingerprint of everything P4 checks and P5 mirrors: the four CSVs and the approval queue."""
    h = hashlib.sha256()
    root = data_root()
    for f in sorted((root / "data").glob("*.csv")) + [root / "state" / "pending_approvals.md"]:
        if f.exists():
            h.update(f.name.encode() + b"\0" + f.read_bytes())
    return h.hexdigest()


def run_attention() -> list:
    """NEEDS ATTENTION items of the current run (they go to the Sheet's Agent Warnings tab via P5)."""
    try:
        st = json.loads((data_root() / "state" / "run_state.json").read_text(encoding="utf-8"))
        return (st.get("current_run") or {}).get("attention", [])
    except (OSError, json.JSONDecodeError):
        return []


def rotate_logs() -> str:
    """Monthly rotation: when the active run log started in an earlier month, move it (gzipped) and the run
    summaries to logs/archive/<month>/, so the files committed every run stay small. Nothing is deleted."""
    logs = data_root() / "logs"
    log = logs / "run_log.jsonl"
    try:
        with open(log, encoding="utf-8") as f:
            first = json.loads(f.readline() or "{}")
    except (OSError, json.JSONDecodeError):
        return ""
    started = str(first.get("ts", ""))[:7]
    month = dt.datetime.now(JST).strftime("%Y-%m")
    if not started or started >= month:
        return ""
    arch = logs / "archive" / started
    arch.mkdir(parents=True, exist_ok=True)
    with open(log, "rb") as src, gzip.open(arch / "run_log.jsonl.gz", "ab") as dst:
        shutil.copyfileobj(src, dst)
    log.unlink()
    summaries = logs / "run_summaries.md"
    if summaries.exists():
        with open(arch / "run_summaries.md", "a", encoding="utf-8") as dst:
            dst.write(summaries.read_text(encoding="utf-8"))
        summaries.unlink()
    for name in ("debug_runs.csv", "consensus_log.jsonl"):
        if (logs / name).exists():
            (logs / name).rename(arch / name)
    return f"logs from {started} archived to logs/archive/{started}/"


DEBUG_CSV = "logs/debug_runs.csv"   # one row per subagent per run; uploaded to the dashboard by `finish`
DEBUG_COLS = ["run_id", "run_end_jst", "mode", "review_called", "review_events", "model_error", "model_requests",
              "tokens_in", "tokens_out", "models_used", "subagent", "ran_by_code", "ran_by_model", "delegations",
              "reruns", "rounds_to_final_report", "consensus_rounds", "total_steps", "tool_calls", "step_limit_stops",
              "loop_stops", "reached_before_stop", "repeated_work", "final_report_received", "p4_validation",
              "p5_sheet_sync"]


def j(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"error": "unparseable", "detail": text[:300]}


class Run:
    def __init__(self, mode: str, instruction: str, start: dict):
        self.mode, self.instruction, self.start = mode, instruction, start
        self.plan_text = ""
        self.reports: list[dict] = []
        self.executed: list[tuple[str, ...]] = []   # pipeline commands run this run (so fallback never repeats)
        self.orchestrator_summary = ""
        self.model_error = ""
        self.stats: dict[str, dict] = {}            # per subagent: delegations, early stops, repeated work
        self.closeout_notes: list[str] = []
        self.routine_results: list[dict] = []       # what the code ran before any model was asked
        self.events: list[str] = []
        self.orch_updates: dict = {}                # review snapshot / daily-check date, saved in the close-out
        self.closeout_status = {"P4": "not run", "P5": "not run"}
        self.consensus: dict | None = None
        self.md_debate_result: dict | None = None

    # --- orchestrator tools ---------------------------------------------------------------------------------
    def delegate(self, subagent: str, task: str) -> str:
        from subagents import SUBAGENTS
        spec = SUBAGENTS.get(subagent)
        if spec is None:
            return json.dumps({"error": f"unknown subagent; available: {list(SUBAGENTS)}"})
        if len(self.reports) >= MAX_DELEGATIONS:
            return json.dumps({"error": f"delegation limit reached ({MAX_DELEGATIONS} per run)"})
        print(f"Delegating to {subagent}: {task[:200]}", flush=True)
        done = sorted({" ".join(c) for c in self.executed if c[0] in ("run", "steady")})
        brief = (f"Mode: {self.mode}\nRun plan JSON from start:\n{json.dumps(self._plan_view(), ensure_ascii=False)}"
                 f"\nAlready run this run (do not repeat unless the task asks for a re-run): {', '.join(done) or 'nothing'}"
                 f"\n\nTask from the orchestrator:\n{task}")
        res = run_agent(subagent, load_prompt(spec.prompt), brief, spec.tools, spec.max_steps, skills=spec.skills)
        cmds = [_as_cmd(c) for c in res.tool_calls]
        work = [c for c in cmds if c[0] in ("run", "steady")]
        repeated = [" ".join(c) for c in work if c in self.executed]  # pipeline work an earlier delegation did
        self.executed.extend(cmds)
        s = self.stats.setdefault(subagent, {"delegations": 0, "step_limit_stops": 0, "loop_stops": 0,
                                             "steps": 0, "tool_calls": 0, "repeated_work": [], "attempts": []})
        s["delegations"] += 1
        s["steps"] += res.steps
        s["tool_calls"] += len(res.tool_calls)
        s["step_limit_stops"] += res.stopped == "step limit"
        s["loop_stops"] += res.stopped == "loop detected"
        s["repeated_work"] += repeated
        s["attempts"].append({"steps": res.steps, "stopped": res.stopped, "work_done": [" ".join(c) for c in work],
                              "final_report": bool(res.text.strip()) and not res.stopped})
        report = {"subagent": subagent, "attempt": s["delegations"], "task": task[:300], "steps": res.steps,
                  "stopped": res.stopped,
                  # what was actually run, so a re-delegation after an early stop need not redo finished work
                  "work_done": [" ".join(c) for c in work], "repeated_work": repeated,
                  "report": res.text[:2000]}
        self.reports.append(report)
        return json.dumps(report, ensure_ascii=False)

    def log_refusal(self, request: str, source: str = "") -> str:
        return pipeline("refusal", request[:200], "--source", source[:200])

    def _plan_view(self) -> dict:
        keep = ("run_id", "mode", "initial_build_remaining", "next_phase", "parked_phases", "daily_due",
                "pending_approvals", "env")
        return {k: self.start[k] for k in keep if k in self.start}

    # --- routine work (code), event detection, review (model) ------------------------------------------
    def routine(self, why: str) -> None:
        """The routine work, done by code: each subagent's deterministic steps, in order. Used for every normal run
        (the model only reviews afterwards) and as the fallback when the model is unavailable."""
        from subagents import SUBAGENTS
        if not self.plan_text:
            self.plan_text = (f"{why}\n"
                              f"Next phase: {self.start.get('next_phase')}; "
                              f"remaining: {self.start.get('initial_build_remaining')}; "
                              f"daily due: {self.start.get('daily_due')}.")
        for spec in SUBAGENTS.values():
            if self.mode.startswith("phase="):
                cmds = [("run", self.mode.split("=", 1)[1])]
            else:
                cmds = spec.fallback(self.start) if spec.fallback else []
            for cmd in cmds:
                if cmd in self.executed or cmd in (("run", "P4"), ("run", "P5")):
                    continue  # already done this run, or part of the close-out
                print(f"Routine ({spec.name}): cli.py {' '.join(cmd)}", flush=True)
                r = j(pipeline(*cmd))
                self.executed.append(cmd)
                brief = {k: r.get(k) for k in ("phase", "status", "error") if k in r} or \
                        {k: v for k, v in r.items() if k != "headline"}
                self.routine_results.append({"subagent": spec.name, "command": " ".join(cmd), "result": brief})
                self.reports.append({"subagent": spec.name, "task": "code: " + " ".join(cmd),
                                     "report": json.dumps(brief, ensure_ascii=False)[:300]})
                if "budget_stop" in json.dumps(r):
                    break

    def detect_events(self, view: dict, orch: dict) -> list[str]:
        """Is there anything for the model to judge? Empty list = a quiet run, and no model is called."""
        ev = []
        if self.instruction:
            ev.append("manual/API instruction")
        remaining = j(pipeline("plan")).get("initial_build_remaining") or []
        if remaining:
            ev.append(f"initial build incomplete: {', '.join(remaining)}")
        for r in self.routine_results:
            res = r["result"]
            text = json.dumps(res, ensure_ascii=False)
            if res.get("error") or "budget_stop" in text or res.get("status") not in (None, "complete") \
                    or '"error"' in text:
                ev.append(f"{r['command']} did not complete cleanly: {text[:200]}")
        for a in run_attention():
            ev.append(f"needs attention: {a[:200]}")
        snap = orch.get("review_snapshot")
        head = view.get("headline") or {}
        if not snap:
            ev.append("no review snapshot yet (first review)")
        else:
            for key, label in REVIEW_WATCH.items():
                now, then = head.get(key), snap["values"].get(key)
                if isinstance(now, (int, float)) and isinstance(then, (int, float)) and then:
                    move = now / then - 1
                    if abs(move) > REVIEW_THRESHOLD:
                        ev.append(f"{label} {then} -> {now} ({move:+.1%}) since the review of {snap['date']}")
                elif str(now) != str(then):  # became, or stopped being, not meaningful / unavailable
                    ev.append(f"{label} changed from {str(then)[:60]!r} to {str(now)[:60]!r} since {snap['date']}")
        t = dt.datetime.now(JST)
        if (t.hour, t.minute) >= DAILY_CHECK_AFTER and orch.get("last_daily_review") != t.date().isoformat():
            ev.append("daily check (first run after the TSE close today)")
        return ev

    def debaters(self, view: dict) -> list[dict]:
        from subagents import SUBAGENTS
        head = view.get("headline") or {}
        out = []
        for spec in SUBAGENTS.values():
            v = head.get(spec.valuation_key) if spec.valuation_key else None
            if isinstance(v, (int, float)) and v > 0:
                out.append({"name": spec.name, "label": spec.valuation_label, "price": float(v),
                            "system": load_prompt("debate") + "\n\n## Your view\n\n" + load_prompt(spec.prompt)})
        return out

    def consensus_due(self, view: dict, orch: dict) -> list[str]:
        """Why a consensus debate is needed now (empty = the last consensus still stands)."""
        deb = self.debaters(view)
        if len(deb) < 2 or self.mode.startswith("phase="):
            return []
        snap = orch.get("consensus_snapshot")
        if not snap:
            return ["no consensus yet"]
        why = []
        for d in deb:
            then = (snap.get("prices") or {}).get(d["name"])
            if not then:
                why.append(f"{d['name']} has no price in the last consensus")
            elif abs(d["price"] / then - 1) > REVIEW_THRESHOLD:
                why.append(f"{d['name']} price {then} -> {d['price']} ({d['price'] / then - 1:+.1%}) since the consensus of {snap['date']}")
        age = (dt.datetime.now(JST) - dt.datetime.fromisoformat(snap["date"])).days
        if age >= CONSENSUS_MAX_AGE_DAYS:
            why.append(f"consensus is {age} days old")
        if any(e.startswith("needs attention") for e in self.events):
            why.append("a NEEDS ATTENTION item")
        return why

    def run_consensus(self, view: dict, reasons: list[str]) -> None:
        import consensus
        llm.check_settings()
        deb = self.debaters(view)
        print(f"Consensus debate ({', '.join(d['name'] for d in deb)}): {'; '.join(reasons)}", flush=True)
        res = consensus.run(deb, consensus.evidence_board(view, self.routine_results), load_prompt("orchestrator"))
        res["reasons"] = reasons
        self.consensus = res
        root = data_root()
        (root / "tmp").mkdir(parents=True, exist_ok=True)
        (root / "tmp" / "consensus.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        with open(root / "logs" / "consensus_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"run_id": self.start.get("run_id"), "date": dt.datetime.now(JST).isoformat(timespec="seconds"),
                                **res}, ensure_ascii=False) + "\n")
        rec = j(pipeline("consensus-record", "tmp/consensus.json"))
        if rec.get("error"):
            self.closeout_notes.append(f"Consensus not recorded: {rec}")
        self.orch_updates["consensus_snapshot"] = {
            "date": dt.datetime.now(JST).isoformat(timespec="minutes"), "run_id": self.start.get("run_id"),
            "prices": res["prices"], "price": res["price"], "status": res["status"]}
        print(f"Consensus: {res['status']} at {res['price']} (range {res['range']}, weights {res['weights']})", flush=True)

    def review(self, view: dict, orch: dict) -> None:
        """The model's job on an event: read what the code did and what changed, follow up if needed, explain."""
        from subagents import SUBAGENTS
        llm.check_settings()
        snap = orch.get("review_snapshot") or {}
        task = (f"Mode: {self.mode}\nEvents that triggered this review:\n- " + "\n- ".join(self.events) +
                f"\n\nRoutine work already done by code this run:\n"
                f"{json.dumps(self.routine_results, ensure_ascii=False)[:2500]}"
                f"\n\nNow (1840 headline, peer inputs, last 7 days):\n{json.dumps(view, ensure_ascii=False)}"
                f"\n\nAt the last review ({snap.get('date', 'none')}):\n"
                f"{json.dumps({k: snap.get(k) for k in ('values', 'peers')}, ensure_ascii=False) if snap else 'no earlier review'}"
                + (f"\n\nInstruction from the user's manual/API trigger (still bound by your guardrails):\n"
                   f"{self.instruction}" if self.instruction else "")
                + (f"\n\nConsensus reached this run (final; do not recompute):\n"
                   f"{json.dumps({k: v for k, v in self.consensus.items() if k not in ('transcript', 'referee')}, ensure_ascii=False)[:3000]}"
                   if self.consensus else ""))
        res = run_agent("orchestrator", load_prompt("orchestrator"), task, self._tools(SUBAGENTS), ORCHESTRATOR_STEPS,
                        skills=ORCHESTRATOR_SKILLS)
        self.orchestrator_summary = res.text.strip()[:1500]
        today = dt.datetime.now(JST)
        fresh = j(pipeline("headline"))  # snapshot after any follow-up work the review started
        self.orch_updates["review_snapshot"] = {
            "date": today.isoformat(timespec="minutes"), "run_id": self.start.get("run_id"),
            "values": {k: (fresh.get("headline") or {}).get(k) for k in (*REVIEW_WATCH, "stock_price_jpy")},
            "peers": fresh.get("peers")}
        if any(e.startswith("daily check") for e in self.events):
            self.orch_updates["last_daily_review"] = today.date().isoformat()

    def md_debate(self, paused: list[str]) -> None:
        """Gate 0 every run; the debate itself at the daily check, or at once when a new Tsuchiya notice or a student upload appears."""
        import mdebate
        gate = j(pipeline("evidence"))
        new = gate.get("gate0") or []
        if not new:
            print("Md debate: gate 0 closed (no new evidence); no model call.", flush=True)
            return
        daily = any(e.startswith("daily check") for e in self.events)
        if not (daily or gate.get("company_notice") or gate.get("student_upload")):
            print(f"Md debate: {len(new)} new item(s) wait for the daily check.", flush=True)
            return
        if paused:
            print(f"Md debate due but paused on the dashboard ({', '.join(paused)}): skipped.", flush=True)
            return
        llm.check_settings()
        ctx = j(pipeline("md-context"))
        trigger = ("student upload" if gate.get("student_upload") else
                   "new company notice" if gate.get("company_notice") else "daily check")
        res = mdebate.run(gate, ctx, trigger)
        root = data_root()
        (root / "tmp").mkdir(exist_ok=True)
        (root / "tmp" / "debate.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        rec = j(pipeline("debate-record", "tmp/debate.json"))
        self.md_debate_result = res
        msg = (f"md debate: {len(new)} new item(s), {len(res.get('items') or [])} flagged, stopped {res.get('stopped_at')}; "
               f"Debate Signal ¥{res.get('signal', 0):,.0f} ({(rec.get('debate_signal') or {}).get('mode', '?')} mode)")
        print(msg, flush=True)
        self.events.append(msg)

    def weekly_report(self) -> None:
        if not any(e.startswith("daily check") for e in self.events):
            return
        due = j(pipeline("report-due"))
        if not due.get("due"):
            return
        import reporter
        llm.check_settings()
        print("Weekly summary: " + reporter.write(data_root()), flush=True)

    def research_summary(self) -> None:
        """A40: the research agent reads the run's results and writes a summary; code checks every number in it."""
        import consensus
        from subagents import SUBAGENTS
        spec = SUBAGENTS["research"]
        llm.check_settings()
        view = pipeline("research-view")
        res = run_agent("research", load_prompt(spec.prompt), "Summarise this research run for the student's approval "
                        "decision. Read it with read_research first.", spec.tools, spec.max_steps, skills=spec.skills)
        text = res.text.strip()
        nums = consensus._board_numbers(consensus._flatten_all(json.loads(view)))
        bad = consensus._unverified_numbers(text, nums)
        if bad:  # numbers that are not in the results are flagged in the text, not trusted
            text += f"\n\n(Code check: {len(bad)} number(s) not found in the results and not verified: {', '.join(bad[:8])})"
        root = data_root()
        (root / "tmp").mkdir(exist_ok=True)
        (root / "tmp" / "research_summary.md").write_text(text, encoding="utf-8")
        print("Research summary stored: " + pipeline("research-summary", "tmp/research_summary.md"), flush=True)

    def _tools(self, SUBAGENTS) -> list:
        return [
            Tool("delegate", "Give one subagent a task and get back its report. Subagents: " +
                 "; ".join(f"{s.name}: {s.description}" for s in SUBAGENTS.values()),
                 {"type": "object", "properties": {
                     "subagent": {"type": "string", "enum": list(SUBAGENTS)},
                     "task": {"type": "string", "description": "what to do, and any context it needs "
                                                               "(including other subagents' findings)"}},
                  "required": ["subagent", "task"]}, self.delegate),
            Tool("log_refusal", "Record a request you declined as out of scope or prohibited.",
                 {"type": "object", "properties": {"request": {"type": "string"}, "source": {"type": "string"}},
                  "required": ["request"]}, self.log_refusal),
        ]

    def close_out(self) -> dict:
        orch = load_orch()
        co = orch.setdefault("closeout", {})
        today = dt.datetime.now(JST).date().isoformat()
        for phase in ("P4", "P5"):
            if self.mode.startswith("phase="):
                if not self.mode.endswith(phase):  # phase mode: only that phase, never skipped
                    continue
            elif phase == "P4" and outputs_hash() == co.get("p4_hash"):
                self.closeout_notes.append("P4 skipped: no CSV or approval-queue change since the last validation.")
                self.closeout_status[phase] = "skipped (unchanged)"
                continue
            elif phase == "P5" and outputs_hash() == co.get("p5_hash") and co.get("p5_date") == today \
                    and not run_attention():
                # still synced once a day, so edits the student made in the Sheet are pulled back daily
                self.closeout_notes.append("P5 skipped: nothing changed since today's Sheet sync.")
                self.closeout_status[phase] = "skipped (unchanged, synced today)"
                continue
            print(f"Close-out: cli.py run {phase}", flush=True)
            r = j(pipeline("run", phase))
            self.closeout_status[phase] = f"ran: {r.get('status') or r.get('error') or 'unknown'}"
            if r.get("status") == "complete":  # hash after the phase: P4 and P5 may themselves update rows
                co[f"{phase.lower()}_hash"] = outputs_hash()
                if phase == "P5":
                    co["p5_date"] = today
        orch.update(self.orch_updates)
        if self.stats:
            orch.setdefault("delegation_history", []).append(
                {"run_id": self.start.get("run_id"), "date": today, "mode": self.mode, "subagents": self.stats})
            orch["delegation_history"] = orch["delegation_history"][-200:]
        save_orch(orch)
        plan_file = data_root() / "tmp" / "plan.txt"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        body = [self.plan_text or "(no plan recorded)"]
        body.append("Review events: " + ("; ".join(self.events) if self.events else
                                          "none, so the model was not called (quiet run)."))
        if self.model_error:
            body.append(f"Model unavailable: {self.model_error} (review skipped; it is retried next run).")
        for r in self.reports:
            body.append(f"Subagent {r['subagent']}: {r['task'][:120]} -> "
                        f"{(r.get('stopped') + '; ') if r.get('stopped') else ''}{r['report'][:400]}")
        if self.consensus:
            c = self.consensus
            body.append(f"Consensus ({c['status']}): {c['price']} JPY, range {c['range'][0]}-{c['range'][1]}, "
                        f"weights {c['weights']}, confidence {c['confidence']}, rejected claims {c['rejected_claims']}. "
                        + c.get("statement", "")[:500])
            if c.get("referee"):
                rf = c["referee"]
                turns = rf.get("turns") or []
                errs = sorted({t["error"][:300] for t in turns if "error" in t})
                ok = [t for t in turns if "error" not in t]
                flags = {k: max(t.get(k, 0) for t in ok) for k in ("unsupported_number", "trade_recommendation",
                                                                    "legal_accusation", "injected_instruction")} if ok else {}
                body.append(f"Referees (shadow, log only): monitor {len(ok)}/{len(turns)} turns scored"
                            + (f", max flag probabilities {flags}" if flags else "")
                            + (f", errors {errs}" if errs else "") + f"; judge: {json.dumps(rf.get('judge'), ensure_ascii=False)[:300]}")
        if self.orchestrator_summary:
            body.append("Review: " + self.orchestrator_summary[:900])
        for name, st in self.stats.items():
            body.append(f"Delegations: {name} x{st['delegations']} ({st['steps']} steps, {st['tool_calls']} tool calls; "
                        f"early stops: {st['step_limit_stops']} step limit, {st['loop_stops']} loop; "
                        f"repeated work: {', '.join(st['repeated_work']) or 'none'})")
        body += self.closeout_notes
        u = llm.usage
        body.append(f"Model calls: {u['requests']}; tokens in/out: {u['prompt_tokens']}/{u['completion_tokens']}; "
                    f"models: {', '.join(sorted(u['models'])) or '(none)'}")
        plan_file.write_text("\n".join(body) + "\n", encoding="utf-8")
        self.write_debug_rows()
        return j(pipeline("finish", "--plan", "tmp/plan.txt"))

    def write_debug_rows(self) -> None:
        """Debug CSV for the dashboard: per subagent, did it run (by code / by the model), how often it was re-run,
        how many rounds and steps it took to a final report, and how far it got before any early stop."""
        from subagents import SUBAGENTS
        u = llm.usage
        path = data_root() / DEBUG_CSV
        path.parent.mkdir(parents=True, exist_ok=True)
        new = not path.exists()
        with open(path, "a", encoding="utf-8-sig" if new else "utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=DEBUG_COLS)
            if new:
                w.writeheader()
            for name in SUBAGENTS:
                st = self.stats.get(name, {})
                attempts = st.get("attempts", [])
                final_at = next((i + 1 for i, a in enumerate(attempts) if a["final_report"]), None)
                stops = [f"attempt {i + 1}: {', '.join(a['work_done']) or 'no pipeline work'} ({a['stopped']})"
                         for i, a in enumerate(attempts) if a["stopped"]]
                w.writerow({
                    "run_id": self.start.get("run_id"), "run_end_jst": dt.datetime.now(JST).isoformat(timespec="seconds"),
                    "mode": self.mode, "review_called": "yes" if self.events and u["requests"] else "no",
                    "review_events": " | ".join(self.events)[:500], "model_error": self.model_error[:200],
                    "model_requests": u["requests"], "tokens_in": u["prompt_tokens"], "tokens_out": u["completion_tokens"],
                    "models_used": ", ".join(sorted(u["models"])), "subagent": name,
                    "ran_by_code": ", ".join(r["command"] for r in self.routine_results if r["subagent"] == name) or "no",
                    "ran_by_model": "yes" if attempts else "no", "delegations": len(attempts),
                    "reruns": max(len(attempts) - 1, 0), "rounds_to_final_report": final_at or ("" if not attempts else "none"),
                    "consensus_rounds": ("n/a (one subagent)" if len(SUBAGENTS) < 2 else
                                         str(self.consensus["rounds"]) if self.consensus and name in self.consensus["prices"]
                                         else ""),
                    "total_steps": st.get("steps", 0), "tool_calls": st.get("tool_calls", 0),
                    "step_limit_stops": st.get("step_limit_stops", 0), "loop_stops": st.get("loop_stops", 0),
                    "reached_before_stop": " ; ".join(stops), "repeated_work": ", ".join(st.get("repeated_work", [])),
                    "final_report_received": "yes" if final_at else ("no" if attempts else ""),
                    "p4_validation": self.closeout_status["P4"], "p5_sheet_sync": self.closeout_status["P5"]})


def _as_cmd(sig: str) -> tuple[str, ...]:
    """Map a subagent tool call signature to the pipeline command it ran (for the no-repeat rule)."""
    name, _, args = sig.partition("(")
    try:
        a = json.loads(args[:-1] or "{}")
    except json.JSONDecodeError:
        a = {}
    if name == "run_phase":
        return ("run", str(a.get("phase")))
    if name == "steady_refresh":
        return ("steady",)
    return (name,)


def step_summary(run_id: str, extra: str) -> None:
    path = os.getenv("GITHUB_STEP_SUMMARY")
    log = data_root() / "logs" / "run_summaries.md"
    text = log.read_text(encoding="utf-8") if log.exists() else ""
    i = text.rfind(f"## {run_id}") if run_id else -1
    section = text[i:] if i >= 0 else "(no run summary)"
    print("\n" + section + "\n" + extra, flush=True)
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(section + "\n\n" + extra + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default=os.getenv("AGENT_MODE") or "scheduled")
    ap.add_argument("--instruction", default=os.getenv("AGENT_INSTRUCTION") or "")
    a = ap.parse_args()
    mode = a.mode.strip()
    if mode not in ("scheduled", "test", "research") and not (mode.startswith("phase=P") and mode[7:] in "123456789"
                                                               and len(mode) == 8):
        print(f"Unknown mode {mode!r}; use scheduled, test, research or phase=P1..P9", file=sys.stderr)
        return 2
    if mode == "test":
        os.environ.setdefault("TSUCHIYA_FIXTURE_SET", "default")
        os.environ["TSUCHIYA_GIT"] = "0"
        print(f"Test mode: sandbox copy at {make_test_sandbox()} (v2/ itself is not changed)", flush=True)

    rotated = rotate_logs()
    if rotated:
        print(rotated, flush=True)
    start = j(pipeline("start", "--mode", mode))
    if not start.get("proceed"):
        reason = start.get("reason") or start.get("detail") or "start failed"
        print(f"Not running: {reason}")
        if os.getenv("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(f"Run skipped: {reason}\n")
        return 0 if "reason" in start else 1

    run = Run(mode, a.instruction.strip()[:1000], start)
    print(f"Run {start['run_id']} ({mode}); next phase: {start.get('next_phase')}; "
          f"model: {llm.MODEL or '(unset)'}; backups: {', '.join(llm.FALLBACK_MODELS) or '(none)'}", flush=True)
    try:
        if mode == "research":  # A39-A40: universe, panel, M1 fit, backtest, proposed weights (code only; resumable)
            r = j(pipeline("research", timeout=3300))
            run.routine_results.append({"subagent": "research", "command": "research", "result": r})
            print("Research: " + json.dumps(r, ensure_ascii=False)[:1500], flush=True)
            if isinstance(r.get("model"), dict) and r["model"].get("version"):
                run.research_summary()
        # 1. the routine work is always done by code
        else:
            run.routine("Routine work by code; the model reviews only when an event needs judgement.")
        # 2. the model is called only for events (phase=Pn debug runs never call it)
        if not mode.startswith("phase=") and mode != "research":
            orch = load_orch()
            view = j(pipeline("headline"))
            run.events = run.detect_events(view, orch)
            why = run.consensus_due(view, orch)
            paused = [p for p in (start.get("dashboard") or {}).get("paused_profiles", []) if p in ("ca", "debater_a", "debater_b")]
            if why and paused:  # A35: pausing C&A or a debater on the dashboard skips the debate, not the run
                print(f"Consensus due but paused on the dashboard ({', '.join(paused)}): debate skipped.", flush=True)
                why = []
            if why:
                run.events.append("consensus due: " + "; ".join(why))
                run.run_consensus(view, why)
                view = j(pipeline("headline"))  # now includes the consensus price
            # A41-A42: the Md debate (gate 0 in code; scouts, debaters and C&A only when it opens) and the weekly summary
            if mode != "test":
                run.md_debate(paused)
                run.weekly_report()
            if run.events:
                print("Review events:\n  " + "\n  ".join(run.events), flush=True)
                run.review(view, orch)
            else:
                print("No review events: quiet run, model not called.", flush=True)
    except ModelUnavailable as e:
        run.model_error = str(e)
        print(f"::warning::Model unavailable: {e} The routine work is done; the review is retried next run.", flush=True)
    except Exception as e:  # never skip the close-out
        run.model_error = f"run crashed: {type(e).__name__}: {str(e)[:200]}"
        print(f"::warning::{run.model_error}", flush=True)
    finally:
        fin = run.close_out()

    u = llm.usage
    step_summary(start["run_id"], f"_Model calls: {u['requests']}; models used: "
                                  f"{', '.join(sorted(u['models'])) or 'none'}; git: handled by the workflow._")
    if fin.get("error"):
        print(f"::error::finish failed: {fin}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
