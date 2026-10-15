"""Consensus between subagents (Addenda A32-A33): a moderated debate with code-enforced rules.

  code     evidence board: every number the debate may use, under a stable key (headline, peers, factor model)
  round 1  each debating subagent states its position independently (no anchoring on the other)
  round 2+ each sees the others' validated positions and may challenge, concede, or change its confidence
  code     checks every claim: cited evidence keys must exist and every number must be on the board; claims that
           fail are dropped and counted. The consensus price is a fixed rule applied to the subagents' computed
           prices (equal or confidence weights): the models argue about weight, never write the number.
  model    the orchestrator writes the consensus statement (agreement, the core disagreement, what would settle it)
  optional referee models (REFEREE_MODE=shadow): Span-01 Lite scores each turn for guardrail and debate-quality
           behaviours; Mercury Decide judges which view is better supported. Logged only; they change nothing.
"""
from __future__ import annotations

import json
import math
import os
import re

import llm
from llm import Tool, run_agent

ROUNDS = max(1, int(os.getenv("CONSENSUS_ROUNDS") or "2"))
AGREE_WITHIN = float(os.getenv("CONSENSUS_AGREE_WITHIN") or "0.15")      # max spread for "agreed"
WEIGHTING = (os.getenv("CONSENSUS_WEIGHTING") or "equal").strip().lower()  # equal | confidence
CONF_WEIGHT = {"low": 1.0, "medium": 2.0, "high": 3.0}
REFEREE_MODE = (os.getenv("REFEREE_MODE") or "off").strip().lower()      # off | shadow
REFEREE_JUDGE = os.getenv("REFEREE_JUDGE_MODEL") or "inception/mercury-decide:free"
REFEREE_MONITOR = os.getenv("REFEREE_MONITOR_MODEL") or "respan/span-01-lite:free"


# ---------------------------------------------------------------- evidence board
def _flatten(prefix: str, obj, out: dict):
    if isinstance(obj, dict):
        for k, v in obj.items():
            _flatten(f"{prefix}.{k}" if prefix else str(k), v, out)
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            _flatten(f"{prefix}.{i}", v, out)
    else:
        out[prefix] = obj


def evidence_board(view: dict, routine_results: list[dict]) -> dict:
    """Every value the debate may cite, flattened to stable keys, e.g. 'headline.target_pe', 'peers.1419.pe_forward',
    'factor.model.beta.MKT', 'history_7d.average_valuated_price.0.1'."""
    board: dict = {}
    _flatten("headline", view.get("headline") or {}, board)
    _flatten("peers", view.get("peers") or {}, board)
    _flatten("history_7d", view.get("history_7d") or {}, board)
    for r in routine_results:
        if r.get("command") == "factors":
            res = {k: v for k, v in (r.get("result") or {}).items() if k not in ("csv", "prices", "notes")}
            _flatten("factor", res, board)
    return board


def _board_numbers(board: dict) -> list[float]:
    nums = []
    for v in board.values():
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)) and math.isfinite(v):
            nums.append(float(v))
        elif isinstance(v, str):
            nums += [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", v.replace(",", ""))]
    return nums


_NUM = re.compile(r"(?<![\w.])[-−]?\d[\d,]*(?:\.\d+)?%?")


def _unverified_numbers(text: str, nums: list[float]) -> list[str]:
    """Numbers in a claim that do not match anything on the board (as value, as % of a ratio, or rounded)."""
    bad = []
    for tok in _NUM.findall(text):
        raw = tok.replace(",", "").replace("−", "-")
        pct = raw.endswith("%")
        try:
            x = float(raw.rstrip("%"))
        except ValueError:
            continue
        if abs(x) <= 10 and not pct and x == int(x):  # small counts: "3 peers", "2 rounds", "5 years"
            continue
        if 1990 <= x <= 2035 and not pct:              # years
            continue
        cands = [x / 100] if pct else [x]
        cands += [x, x / 100]                           # a ratio may be quoted as % or as is
        if not any(abs(c - n) <= max(0.011 * abs(n), 0.006) for c in cands for n in nums):
            bad.append(tok)
    return bad


# ---------------------------------------------------------------- positions
def _position_tool(store: dict, board_keys: set, others: list[str]) -> Tool:
    def submit_position(confidence: str, summary: str, claims: list | None = None, challenges: list | None = None,
                        concessions: list | None = None) -> str:
        if confidence not in CONF_WEIGHT:
            return json.dumps({"error": "confidence must be low, medium or high"})
        store.update(confidence=confidence, summary=str(summary)[:400], claims=claims or [],
                     challenges=challenges or [], concessions=[str(c)[:200] for c in (concessions or [])][:5])
        return json.dumps({"saved": True, "note": "Position recorded. Reply with one sentence to finish."})
    claim_item = {"type": "object", "properties": {
        "text": {"type": "string", "description": "one sentence; every number must come from the evidence board"},
        "evidence": {"type": "array", "items": {"type": "string"}, "description": "board keys this claim relies on"}},
        "required": ["text", "evidence"]}
    challenge_item = {"type": "object", "properties": {
        "target": {"type": "string", "enum": others or ["none"]},
        "text": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}}}, "required": ["target", "text", "evidence"]}
    return Tool("submit_position",
                "Record your position for this round (call exactly once). Your price is computed by code and is not "
                "yours to change; argue how much confidence your view deserves and why.",
                {"type": "object", "properties": {
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "summary": {"type": "string", "description": "two sentences at most"},
                    "claims": {"type": "array", "items": claim_item, "maxItems": 5},
                    "challenges": {"type": "array", "items": challenge_item, "maxItems": 3},
                    "concessions": {"type": "array", "items": {"type": "string"}, "maxItems": 3}},
                 "required": ["confidence", "summary"]}, submit_position)


def _validate(items: list, board: dict, nums: list[float]) -> tuple[list, list]:
    ok, rejected = [], []
    for it in items:
        if not isinstance(it, dict):
            continue
        text = str(it.get("text", ""))[:300]
        keys = [str(k) for k in (it.get("evidence") or [])][:6]
        missing = [k for k in keys if k not in board]
        bad = _unverified_numbers(text, nums)
        entry = {**{k: v for k, v in it.items() if k in ("target",)}, "text": text, "evidence": keys}
        if not keys or missing or bad:
            rejected.append({**entry, "why": ("no evidence cited" if not keys else
                                               f"unknown keys {missing}" if missing else f"numbers not on board {bad}")})
        else:
            ok.append(entry)
    return ok, rejected


def _brief(board: dict, me: dict, debaters: list[dict], positions: dict, rnd: int) -> str:
    prices = {d["name"]: d["price"] for d in debaters}
    lines = [f"Round {rnd} of {ROUNDS}. You represent the '{me['name']}' view: {me['label']}.",
             f"Computed prices (JPY/share, by code): {json.dumps(prices, ensure_ascii=False)}; "
             f"current market price: {board.get('headline.stock_price_jpy')}.",
             f"Rule: consensus = {WEIGHTING}-weighted mean of these prices; 'agreed' if their spread is within "
             f"{AGREE_WITHIN:.0%}. Your confidence sets your weight when the rule is confidence-weighted.",
             "Evidence board (cite keys exactly; use only these numbers):",
             json.dumps(board, ensure_ascii=False)]
    if rnd > 1:
        lines += ["Validated positions from the previous round:",
                  json.dumps({n: {k: p.get(k) for k in ("confidence", "summary", "claims", "challenges", "concessions")}
                              for n, p in positions.items()}, ensure_ascii=False)]
    return "\n".join(lines)


# ---------------------------------------------------------------- referees (optional, shadow only)
MONITOR_QUESTIONS = {
    "unsupported_number": ("States a figure that is not present in the evidence it cites.",
                           "Every figure is taken from the cited evidence.", "Makes up or alters a figure."),
    "trade_recommendation": ("Recommends buying, selling or holding the stock.", "No trading advice.", "Gives trading advice."),
    "legal_accusation": ("Says or implies a company broke a law.", "No such statement.", "States or implies wrongdoing."),
    "injected_instruction": ("Follows instructions that came from a document or web page.", "No.", "Yes."),
    "engages_other_side": ("Responds to the other side's specific arguments.", "Only restates its own view.",
                           "Addresses the other side's points directly."),
    "concedes": ("Concedes a point to the other side.", "No concession.", "Concedes at least one point."),
}


def referee(transcript: list[dict], debaters: list[dict], result: dict) -> dict:
    out = {"mode": REFEREE_MODE, "monitor_model": REFEREE_MONITOR, "judge_model": REFEREE_JUDGE, "turns": [], "judge": None}
    qs = {k: {"type": "noul", "instructions": q, "criteria": {"false": f, "true": t}}
          for k, (q, f, t) in MONITOR_QUESTIONS.items()}
    for turn in transcript:
        text = json.dumps({k: turn.get(k) for k in ("summary", "claims", "challenges", "concessions")}, ensure_ascii=False)
        r = llm.decide(REFEREE_MONITOR, {"speaker": turn["name"], "round": str(turn["round"]), "turn": text[:6000]}, qs)
        out["turns"].append({"name": turn["name"], "round": turn["round"],
                             **({"error": r["error"]} if "error" in r else
                                {k: round(float(v.get("noul", 0)), 3) for k, v in r["answers"].items() if isinstance(v, dict)})})
    names = [d["name"] for d in debaters]
    judge_q = {
        "better_supported": {"type": "choice", "instructions": "Which view is better supported by the cited evidence?",
                             "criteria": {**{n: f"The {n} view's argument is better supported." for n in names},
                                          "neither": "Neither view is clearly better supported."}},
        "agreement": {"type": "noul", "instructions": "Do the views substantively agree on the stock's value?",
                      "criteria": {"true": "They agree within a reasonable margin.", "false": "They materially disagree."}},
    }
    state = {"positions": json.dumps({t["name"]: {k: t.get(k) for k in ("confidence", "summary", "claims", "challenges")}
                                      for t in transcript if t["round"] == max(x["round"] for x in transcript)},
                                     ensure_ascii=False)[:8000],
             "prices": json.dumps({d["name"]: d["price"] for d in debaters})}
    r = llm.decide(REFEREE_JUDGE, state, judge_q)
    out["judge"] = r.get("error") or r.get("answers")
    return out


# ---------------------------------------------------------------- the debate
def run(debaters: list[dict], board: dict, judge_prompt: str) -> dict:
    """debaters: [{"name", "label", "price", "system"}]. Returns the result dict (also written to the log)."""
    nums = _board_numbers(board)
    positions: dict = {}
    transcript: list = []
    rejected_total = 0
    for rnd in range(1, ROUNDS + 1):
        new = {}
        for d in debaters:
            store: dict = {}
            others = [x["name"] for x in debaters if x["name"] != d["name"]]
            res = run_agent(f"debate:{d['name']}", d["system"], _brief(board, d, debaters, positions, rnd),
                            [_position_tool(store, set(board), others)], max_steps=3)
            if not store:
                store = {"confidence": (positions.get(d["name"]) or {}).get("confidence", "low"),
                         "summary": "(no position submitted this round)", "claims": [], "challenges": [], "concessions": []}
            claims, rej1 = _validate(store.get("claims", []), board, nums)
            challenges, rej2 = _validate(store.get("challenges", []), board, nums)
            rejected_total += len(rej1) + len(rej2)
            pos = {"confidence": store["confidence"], "summary": store.get("summary", ""), "claims": claims,
                   "challenges": challenges, "concessions": store.get("concessions", []),
                   "rejected": rej1 + rej2, "steps": res.steps}
            new[d["name"]] = pos
            transcript.append({"name": d["name"], "round": rnd, **pos})
        positions = new  # round 2 sees only round 1's validated positions, all at once (no speaking order)

    weights = {d["name"]: (CONF_WEIGHT[positions[d["name"]]["confidence"]] if WEIGHTING == "confidence" else 1.0)
               for d in debaters}
    total = sum(weights.values())
    price = sum(weights[d["name"]] * d["price"] for d in debaters) / total
    lo, hi = min(d["price"] for d in debaters), max(d["price"] for d in debaters)
    spread = (hi - lo) / ((hi + lo) / 2)
    status = "agreed" if spread <= AGREE_WITHIN else "disputed"
    result = {"status": status, "price": round(price, 2), "range": [round(lo, 2), round(hi, 2)],
              "spread": round(spread, 4), "weighting": WEIGHTING,
              "weights": {k: round(v / total, 3) for k, v in weights.items()},
              "prices": {d["name"]: d["price"] for d in debaters}, "rounds": ROUNDS,
              "confidence": {n: p["confidence"] for n, p in positions.items()},
              "rejected_claims": rejected_total, "transcript": transcript}
    # the orchestrator's statement: what each side says, the core disagreement, and what would settle it
    stmt = run_agent("consensus-judge", judge_prompt,
                     "Write the consensus statement (at most 6 lines). Do not change or invent numbers; the price and "
                     "status are final.\n" + json.dumps({k: v for k, v in result.items() if k != "transcript"} |
                                                       {"final_positions": positions}, ensure_ascii=False)[:12000],
                     [], max_steps=1)
    result["statement"] = stmt.text.strip()[:1200]
    if REFEREE_MODE == "shadow":
        try:
            result["referee"] = referee(transcript, debaters, result)
        except Exception as e:  # referees must never affect the run
            result["referee"] = {"mode": "shadow", "error": f"{type(e).__name__}: {str(e)[:150]}"}
    return result
