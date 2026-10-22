"""Shared model layer for the v2 agent: OpenRouter (or any OpenAI-compatible) client, the free-only guard,
fallback models, a per-run request budget, and the tool loop used by the orchestrator and every subagent.

Settings come only from environment variables (GitHub repo secrets / variables), so changing the model or
limits never needs a code change. Secrets are never printed or written to files.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from openai import APIStatusError, BadRequestError, NotFoundError, OpenAI, RateLimitError

APP = Path(__file__).resolve().parent           # v2/app/ (code)
ROOT = APP.parent                               # v2/ (data, state, logs)
REPO = ROOT.parent                              # repository root

# --- Settings -----------------------------------------------------------------------------------------------
BASE_URL = os.getenv("LLM_BASE_URL") or "https://openrouter.ai/api/v1"
IS_OPENROUTER = "openrouter.ai" in BASE_URL
API_KEY = (os.getenv("LLM_API_KEY") or "").strip()
MODEL = (os.getenv("MODEL") or "").strip()
FALLBACK_MODELS = [m.strip() for m in (os.getenv("FALLBACK_MODELS") or "").split(",") if m.strip()]
FREE_ONLY = (os.getenv("FREE_ONLY") or ("true" if IS_OPENROUTER else "false")).strip().lower() == "true"
MAX_STEPS = int(os.getenv("MAX_STEPS") or "8")                 # tool rounds per subagent
MAX_LLM_REQUESTS = int(os.getenv("MAX_LLM_REQUESTS") or "40")  # all model calls in one run
TOOL_RESULT_CHARS = 3000                                       # what the model sees of one tool result


class ModelUnavailable(Exception):
    """The model API cannot be used for this run (no key, bad model, rate limit, budget). The orchestrator
    falls back to the deterministic pipeline and still validates, syncs and finishes."""


def check_settings() -> None:
    if not API_KEY:
        raise ModelUnavailable("LLM_API_KEY is not set (store your OpenRouter key as the LLM_API_KEY secret).")
    if not MODEL:
        raise ModelUnavailable("MODEL is not set. Run the 'List models' workflow, pick a model that supports "
                               "tool calling, and set it as the MODEL repository variable.")
    if IS_OPENROUTER and FREE_ONLY:
        paid = [m for m in [MODEL, *FALLBACK_MODELS] if not m.endswith(":free")]
        if paid:
            raise ModelUnavailable(f"These models are not free and would spend credits: {', '.join(paid)}. "
                                   "Use IDs ending in :free, or set FREE_ONLY=false to allow paid models.")


_client: OpenAI | None = None
usage = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "models": set()}


def client() -> OpenAI:
    global _client
    if _client is None:
        check_settings()
        # The SDK retries 429 and 5xx with backoff before chat() sees them.
        _client = OpenAI(base_url=BASE_URL, api_key=API_KEY, max_retries=3,
                         default_headers={"X-Title": "Tsuchiya Holdings agent v2"} if IS_OPENROUTER else None)
    return _client


# --- Model call ---------------------------------------------------------------------------------------------
def chat(messages: list[dict], tools: list[dict] | None = None):
    kwargs = {"model": MODEL, "messages": messages}
    if tools:
        kwargs["tools"] = tools
    if FALLBACK_MODELS and IS_OPENROUTER:
        kwargs["extra_body"] = {"models": [MODEL, *FALLBACK_MODELS]}  # tried in order if one fails
    for attempt in range(4):
        if usage["requests"] >= MAX_LLM_REQUESTS:
            raise ModelUnavailable(f"per-run model request budget reached ({MAX_LLM_REQUESTS})")
        usage["requests"] += 1
        try:
            resp = client().chat.completions.create(**kwargs)
        except RateLimitError:
            if attempt == 3:
                raise ModelUnavailable("still rate limited: the daily limit is probably used up, or free "
                                       "capacity is busy")
            print("Rate limited; waiting 60s", flush=True)
            time.sleep(60)
            continue
        except NotFoundError as e:
            raise ModelUnavailable(f"the provider couldn't serve this request ({e.status_code}); check the "
                                   "model ID and that it supports tool calling")
        except BadRequestError as e:
            if attempt < 3:  # e.g. a malformed tool call from the model
                print(f"Request rejected ({e.status_code}); retrying", flush=True)
                time.sleep(5)
                continue
            raise ModelUnavailable(f"request rejected 4 times ({e.status_code})")
        except APIStatusError as e:
            if e.status_code == 402:
                raise ModelUnavailable("payment required (402): the OpenRouter balance is negative")
            if e.status_code == 401:
                raise ModelUnavailable("401: LLM_API_KEY is wrong or deleted")
            raise ModelUnavailable(f"provider error {e.status_code}")
        except Exception as e:  # network errors after the SDK's own retries
            raise ModelUnavailable(f"{type(e).__name__}")
        if not resp.choices:  # an upstream error sometimes arrives as an empty reply
            print("Empty response; retrying in 10s", flush=True)
            time.sleep(10)
            continue
        if resp.model:
            usage["models"].add(resp.model)
        if resp.usage:
            usage["prompt_tokens"] += resp.usage.prompt_tokens or 0
            usage["completion_tokens"] += resp.usage.completion_tokens or 0
        return resp
    raise ModelUnavailable("no usable response after 4 attempts")


# --- Tools --------------------------------------------------------------------------------------------------
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    fn: Callable[..., str]

    def schema(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": self.parameters}}


def pipeline(*args: str, timeout: int = 1800) -> str:
    """Run one guarded pipeline command (v2/app/pipeline/cli.py) and return its JSON result as text.
    The pipeline is our own code: it gets the real environment (EDINET key, Sheet credentials, dashboard key)
    and enforces the allowlist, write paths, locked rows and budgets itself. Nothing model-written runs."""
    try:
        r = subprocess.run([sys.executable, str(APP / "pipeline" / "cli.py"), *args], cwd=REPO,
                           capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "timeout", "detail": f"cli.py {args[0]} exceeded {timeout}s"})
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):  # the CLI prints exactly one JSON object; helpers may print notes before it
        if ln.lstrip().startswith("{"):
            return ln
    tail = (r.stderr or r.stdout).strip()[-400:]
    return json.dumps({"error": f"exit {r.returncode}", "detail": tail})


# --- Agent loop: think, call tools, repeat ------------------------------------------------------------------
@dataclass
class AgentResult:
    text: str
    steps: int
    tool_calls: list[str] = field(default_factory=list)
    stopped: str = ""  # "", "step limit", "loop detected"


def run_agent(name: str, system: str, task: str, tools: list[Tool], max_steps: int = MAX_STEPS) -> AgentResult:
    registry = {t.name: t for t in tools}
    schemas = [t.schema() for t in tools] or None
    messages = [{"role": "system", "content": system}, {"role": "user", "content": task}]
    calls: list[str] = []
    for step in range(1, max_steps + 1):
        msg = chat(messages, schemas).choices[0].message
        if not msg.tool_calls:
            return AgentResult(msg.content or "", step, calls)
        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [{"id": c.id, "type": "function",
                                         "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                        for c in msg.tool_calls]})
        for call in msg.tool_calls:
            sig = f"{call.function.name}({call.function.arguments or ''})"
            calls.append(sig)
            print(f"  [{name}] {sig[:160]}", flush=True)
            tool = registry.get(call.function.name)
            if tool is None:
                result = f"Error: unknown tool {call.function.name}"
            elif calls[-3:] == [sig] * 3:  # loop protection: 3 identical calls in a row
                result = "Error: loop detected (3 identical calls in a row). Stop this line of work."
            else:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    result = tool.fn(**args)
                except (json.JSONDecodeError, TypeError) as e:
                    result = f"Error: bad arguments for {tool.name}: {e}"
                except Exception as e:  # a tool must never crash the run
                    result = f"Error: {tool.name} failed: {type(e).__name__}: {str(e)[:200]}"
            messages.append({"role": "tool", "tool_call_id": call.id,
                             "content": str(result)[:TOOL_RESULT_CHARS]})
        if len(calls) >= 3 and calls[-3:] == [calls[-1]] * 3:
            return AgentResult("(stopped: loop detected)", step, calls, "loop detected")
    return AgentResult("(stopped: reached step limit)", max_steps, calls, "step limit")


# --- Decisions API (optional referee models, Addendum A33) -----------------------------------------------------
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"   # alpha endpoint: typed questions -> probabilities


def decide(model: str, state: dict | str, questions: dict, timeout: int = 60) -> dict:
    """One Decisions API call (OpenRouter /api/alpha/decisions). Returns {"answers": {...}} or {"error": ...}.
    Same free-only guard and per-run request budget as chat calls; never raises."""
    import requests
    if not API_KEY:
        return {"error": "LLM_API_KEY not set"}
    if IS_OPENROUTER and FREE_ONLY and not model.endswith(":free"):
        return {"error": f"{model} is not a free model (FREE_ONLY)"}
    if usage["requests"] >= MAX_LLM_REQUESTS:
        return {"error": f"per-run model request budget reached ({MAX_LLM_REQUESTS})"}
    usage["requests"] += 1
    try:
        r = requests.post(DECISIONS_URL, timeout=timeout,
                          headers={"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json",
                                   "X-Title": "Tsuchiya Holdings agent v2"},
                          json={"model": model, "state": state, "questions": questions})
    except Exception as e:
        return {"error": f"{type(e).__name__}"}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code}: {r.text[:400]}"}
    try:
        data = r.json()
    except ValueError:
        return {"error": "response was not JSON"}
    usage["models"].add(data.get("model") or model)
    return {"answers": data.get("answers") or {}, "usage": data.get("usage")}


def load_prompt(name: str) -> str:
    return (APP / "prompts" / f"{name}.md").read_text(encoding="utf-8")
