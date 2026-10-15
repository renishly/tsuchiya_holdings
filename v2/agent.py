"""Tsuchiya Holdings (1840) research-data agent, v2: orchestrator.

Run from the repository root (GitHub Actions does this):

    python v2/agent.py                       # Mode: scheduled (default)
    python v2/agent.py --mode test           # frozen fixtures, sandbox copy: no network, Sheet, dashboard or git
    python v2/agent.py --mode phase=P6       # one phase once, for debugging

Flow of one run:
  1. start (code): lock, state, approvals, dashboard check-in -> the run plan as JSON. Stops here if another run
     holds the lock or the agent is paused on the dashboard.
  2. orchestrate (model): read the plan, write a 3-5 line run plan, delegate work to subagents, review reports.
     If the model API is unavailable, each subagent's deterministic fallback runs instead.
  3. close-out (code, always): P4 validation, P5 Sheet sync, finish (run summary, state, lock release,
     dashboard). The workflow then commits v2/ outputs.
"""
from __future__ import annotations

import argparse
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
    fixtures = ROOT / "evals" / "fixtures"
    if not fixtures.exists():
        fixtures = llm.REPO / "v1" / "evals" / "fixtures"
    (tmp / "v1" / "evals").mkdir(parents=True)
    (tmp / "v1" / "evals" / "fixtures").symlink_to(fixtures)
    os.environ["TSUCHIYA_ROOT"] = str(tmp / "v2")
    return tmp / "v2"
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

    # --- orchestrator tools ---------------------------------------------------------------------------------
    def write_plan(self, plan: str) -> str:
        lines = [ln for ln in plan.strip().splitlines() if ln.strip()][:5]
        self.plan_text = "\n".join(lines)
        return json.dumps({"saved": True, "lines": len(lines)})

    def delegate(self, subagent: str, task: str) -> str:
        from subagents import SUBAGENTS
        spec = SUBAGENTS.get(subagent)
        if spec is None:
            return json.dumps({"error": f"unknown subagent; available: {list(SUBAGENTS)}"})
        if len(self.reports) >= MAX_DELEGATIONS:
            return json.dumps({"error": f"delegation limit reached ({MAX_DELEGATIONS} per run)"})
        print(f"Delegating to {subagent}: {task[:200]}", flush=True)
        brief = (f"Mode: {self.mode}\nRun plan JSON from start:\n{json.dumps(self._plan_view(), ensure_ascii=False)}"
                 f"\n\nTask from the orchestrator:\n{task}")
        res = run_agent(subagent, load_prompt(spec.prompt), brief, spec.tools, spec.max_steps)
        self.executed.extend(_as_cmd(c) for c in res.tool_calls)
        report = {"subagent": subagent, "task": task[:300], "steps": res.steps, "tool_calls": len(res.tool_calls),
                  "stopped": res.stopped, "report": res.text[:2000]}
        self.reports.append(report)
        return json.dumps(report, ensure_ascii=False)

    def log_refusal(self, request: str, source: str = "") -> str:
        return pipeline("refusal", request[:200], "--source", source[:200])

    def _plan_view(self) -> dict:
        keep = ("run_id", "mode", "initial_build_remaining", "next_phase", "parked_phases", "daily_due",
                "pending_approvals", "env")
        return {k: self.start[k] for k in keep if k in self.start}

    # --- the two ways of doing the work ---------------------------------------------------------------------
    def orchestrate(self) -> None:
        from subagents import SUBAGENTS
        llm.check_settings()
        tools = [
            Tool("write_plan", "Save the 3-5 line plan for this run (phases, targets, budget). Call this first.",
                 {"type": "object", "properties": {"plan": {"type": "string"}}, "required": ["plan"]},
                 self.write_plan),
            Tool("delegate", "Give one subagent a task and get back its report. Subagents: " +
                 "; ".join(f"{s.name}: {s.description}" for s in SUBAGENTS.values()),
                 {"type": "object", "properties": {
                     "subagent": {"type": "string", "enum": list(SUBAGENTS)},
                     "task": {"type": "string", "description": "what to do this run, and any context it needs "
                                                               "(including other subagents' findings)"}},
                  "required": ["subagent", "task"]}, self.delegate),
            Tool("log_refusal", "Record a request you declined as out of scope or prohibited.",
                 {"type": "object", "properties": {"request": {"type": "string"}, "source": {"type": "string"}},
                  "required": ["request"]}, self.log_refusal),
        ]
        task = (f"Mode: {self.mode}\n"
                f"Run plan JSON from start:\n{json.dumps(self._plan_view(), ensure_ascii=False, indent=1)}\n"
                + (f"\nInstruction from the user's manual/API trigger (still bound by your guardrails):\n"
                   f"{self.instruction}\n" if self.instruction else ""))
        res = run_agent("orchestrator", load_prompt("orchestrator"), task, tools, ORCHESTRATOR_STEPS)
        self.orchestrator_summary = res.text.strip()[:1500]

    def fallback(self) -> None:
        """Deterministic work when the model is unavailable: each subagent's fallback commands, in order."""
        from subagents import SUBAGENTS
        if not self.plan_text:
            self.plan_text = ("Deterministic fallback (model unavailable).\n"
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
                print(f"Fallback ({spec.name}): cli.py {' '.join(cmd)}", flush=True)
                r = j(pipeline(*cmd))
                self.executed.append(cmd)
                self.reports.append({"subagent": spec.name, "task": "fallback: " + " ".join(cmd),
                                     "report": json.dumps({k: r.get(k) for k in ("phase", "status", "error")
                                                           if k in r} or r, ensure_ascii=False)[:300]})
                if "budget_stop" in json.dumps(r):
                    break

    def close_out(self) -> dict:
        for phase in ("P4", "P5"):
            if not self.mode.startswith("phase=") or self.mode.endswith(phase):  # phase mode: only that phase
                print(f"Close-out: cli.py run {phase}", flush=True)
                pipeline("run", phase)
        plan_file = data_root() / "tmp" / "plan.txt"
        plan_file.parent.mkdir(parents=True, exist_ok=True)
        body = [self.plan_text or "(the orchestrator wrote no plan)"]
        if self.model_error:
            body.append(f"Model unavailable: {self.model_error} (deterministic fallback used).")
        for r in self.reports:
            body.append(f"Subagent {r['subagent']}: {r['task'][:120]} -> "
                        f"{(r.get('stopped') + '; ') if r.get('stopped') else ''}{r['report'][:400]}")
        if self.orchestrator_summary:
            body.append("Orchestrator: " + self.orchestrator_summary[:600])
        u = llm.usage
        body.append(f"Model calls: {u['requests']}; tokens in/out: {u['prompt_tokens']}/{u['completion_tokens']}; "
                    f"models: {', '.join(sorted(u['models'])) or '(none)'}")
        plan_file.write_text("\n".join(body) + "\n", encoding="utf-8")
        return j(pipeline("finish", "--plan", "tmp/plan.txt"))


def _as_cmd(sig: str) -> tuple[str, ...]:
    """Map a subagent tool call signature to the pipeline command it ran (for the fallback's no-repeat rule)."""
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
    if mode not in ("scheduled", "test") and not (mode.startswith("phase=P") and mode[7:] in "123456789"
                                                   and len(mode) == 8):
        print(f"Unknown mode {mode!r}; use scheduled, test or phase=P1..P9", file=sys.stderr)
        return 2
    if mode == "test":
        os.environ.setdefault("TSUCHIYA_FIXTURE_SET", "default")
        os.environ["TSUCHIYA_GIT"] = "0"
        print(f"Test mode: sandbox copy at {make_test_sandbox()} (v2/ itself is not changed)", flush=True)

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
        run.orchestrate()
    except ModelUnavailable as e:
        run.model_error = str(e)
        print(f"::warning::Model unavailable: {e} Running the deterministic fallback.", flush=True)
        run.fallback()
    except Exception as e:  # never skip the close-out
        run.model_error = f"orchestrator crashed: {type(e).__name__}: {str(e)[:200]}"
        print(f"::warning::{run.model_error}", flush=True)
        run.fallback()
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
