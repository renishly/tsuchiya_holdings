"""The Md debate (Addenda A41-A42, v2.007): news scouts, Debater A and B, C&A decision calls, consensus rule.

  code      gate 0: new items on the evidence board (pipeline `evidence`); none -> stop, no model call
  agents    news scouts (one call per topic with new items) flag items that could move Tsuchiya's operating profit
            and that no measured Md variable covers; none flagged -> gate 1 closed, stop
  agents    Debater A (proposer) and Debater B (sceptic), separate instances on their own models (DEBATER_A_MODEL,
            DEBATER_B_MODEL), each with its own memory; round 1 independently; gate 2: immaterial or uncited
            proposal, or B agrees -> no round 2; round 2: each answers the other
  code      link check (pipeline/linkcheck.py): every flagged source is fetched, checked (status, type, allowlist),
            snapshotted with its SHA-256 and turned into a focused excerpt; unresolved items cannot be cited
  code      claims must cite link-checked ids; other claims are dropped and counted; claim check after each round:
            numbers in a claim must appear in the cited text
  decision  Span-01 Lite scores each turn (behaviour); Mercury Decide answers four typed questions in one call
            (evidence score A, evidence score B, better-supported side, Debate Signal bucket)
  code      consensus rule: signal = cap × Σ p(bucket) × share × evidence score of the better-supported side;
            0 if the answers point both ways (p(down side) > 35% and p(up side) > 35%)
The debaters argue; the decision models choose among fixed options; code sets the yen value. Positive lowers
the forecast (like Md). DEBATE_SIGNAL_MODE=shadow (default) logs it without applying it to Md.
"""
from __future__ import annotations

import json
import os

import consensus
import llm
from llm import Tool, load_prompt, pipeline, run_agent

DEBATER_A_MODEL = (os.getenv("DEBATER_A_MODEL") or "").strip() or None
DEBATER_B_MODEL = (os.getenv("DEBATER_B_MODEL") or "").strip() or (llm.FALLBACK_MODELS[0] if llm.FALLBACK_MODELS else None)
SCOUT_MODEL = (os.getenv("SCOUT_MODEL") or "").strip() or None
JUDGE = consensus.REFEREE_JUDGE
MONITOR = consensus.REFEREE_MONITOR
IMMATERIAL = 10_000_000          # gate 2: proposals under ¥10m of operating profit stop the debate
SPLIT = 0.35                      # consensus rule: both directions above 35% -> signal 0
SHARE = {"strong_down": 1.0, "down": 0.5, "none": 0.0, "up": -0.5, "strong_up": -1.0}
# share > 0 lowers the forecast ("down" = guidance looks too high), matching Md's sign


def j(s: str) -> dict:
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return {"error": "bad JSON", "raw": str(s)[:300]}


def _items(ids=None, topic="") -> list[dict]:
    args = ["evidence-items"] + (["--ids", ",".join(ids)] if ids else []) + (["--topic", topic] if topic else [])
    return j(pipeline(*args)).get("items", [])


# ---------------------------------------------------------------- scouts
def scout(topic: str, items: list[dict]) -> list[dict]:
    flagged: list[dict] = []
    ids = {i["id"] for i in items}
    def read_items() -> str:  # compact items; excerpts shortened so the JSON is never cut
        slim = [{k: i.get(k) for k in ("id", "date", "ticker", "source", "title")} | {"excerpt": (i.get("excerpt") or "")[:500]}
                for i in items]
        return json.dumps({"topic": topic, "items": slim}, ensure_ascii=False)
    def flag_items(flags: list) -> str:
        for f in flags or []:
            if isinstance(f, dict) and f.get("id") in ids:
                flagged.append({"id": f["id"], "reason": str(f.get("reason", ""))[:300]})
        return json.dumps({"flagged": len(flagged)})
    tools = [Tool("read_items", "Read the new evidence-board items in your topic.", {"type": "object", "properties": {}}, read_items,
                  max_chars=9000),
             Tool("flag_items", "Flag items that could move Tsuchiya's operating profit this fiscal year and are not covered "
                  "by the measured Md variables (land prices, building-material costs, Tsuchiya's guidance record). "
                  "Flag none if nothing qualifies.",
                  {"type": "object", "properties": {"flags": {"type": "array", "items": {"type": "object", "properties": {
                      "id": {"type": "string"}, "reason": {"type": "string"}}, "required": ["id", "reason"]}}},
                   "required": ["flags"]}, flag_items)]
    run_agent(f"scout:{topic}", load_prompt("scout"), f"Topic: {topic}. Read the new items, then flag or flag none.",
              tools, 3, skills=["news-scout", "evidence-board", "guardrails"], model=SCOUT_MODEL)
    return flagged


# ---------------------------------------------------------------- debaters
def _position_tool(store: dict, ids: set, cap: float):
    def submit_position(direction: str, size_jpy: float, claims: list, confidence: str, summary: str = "",
                        concessions: list | None = None) -> str:
        store.clear()
        store.update(direction=direction if direction in ("down", "none", "up") else "none",
                     size_jpy=max(0.0, min(float(size_jpy or 0), cap)) if cap else 0.0,
                     claims=claims if isinstance(claims, list) else [], confidence=confidence if confidence in ("low", "medium", "high") else "low",
                     summary=str(summary)[:600], concessions=concessions or [])
        return json.dumps({"submitted": True, "size_capped_at": cap})
    return Tool("submit_position",
                "Submit your position: direction (down = the company's operating-profit forecast looks too high, up = too low, "
                "none), size in yen of operating profit (capped), claims each citing evidence ids, confidence, a one-line summary.",
                {"type": "object", "properties": {
                    "direction": {"type": "string", "enum": ["down", "none", "up"]},
                    "size_jpy": {"type": "number", "minimum": 0},
                    "claims": {"type": "array", "items": {"type": "object", "properties": {
                        "text": {"type": "string"}, "evidence": {"type": "array", "items": {"type": "string"}}},
                        "required": ["text", "evidence"]}},
                    "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                    "summary": {"type": "string"}, "concessions": {"type": "array", "items": {"type": "string"}}},
                 "required": ["direction", "size_jpy", "claims", "confidence"]}, submit_position)


def _fit(brief: dict, limit: int = 12000) -> str:
    """The brief as JSON within the limit: excerpts are shortened evenly, never the JSON itself."""
    n = max(1, len(brief.get("items", [])))
    for cap in (3000, 2000, 1200, 800, 500, 300):
        b = {**brief, "items": [{**i, "excerpt": (i.get("excerpt") or "")[:cap]} for i in brief.get("items", [])[:6]],
             "link_check": [{k: r.get(k) for k in ("id", "resolved", "pages", "sha256", "error")} for r in brief.get("link_check", [])]}
        s = json.dumps(b, ensure_ascii=False)
        if len(s) <= limit:
            return s
    return s


def _validate(pos: dict, ids: set) -> tuple[list, int]:
    ok, bad = [], 0
    for c in pos.get("claims", []):
        ev = [e for e in (c.get("evidence") or []) if e in ids] if isinstance(c, dict) else []
        if ev:
            ok.append({"text": str(c.get("text", ""))[:400], "evidence": ev})
        else:
            bad += 1
    return ok, bad


def debater(name: str, role: str, brief: dict, other: dict | None, cap: float, ids: set) -> dict:
    store: dict = {}
    mem = pipeline("debate-memory", name)
    tools = [Tool("read_evidence", "Read the flagged evidence items (with excerpts) and Md's measured parts.",
                  {"type": "object", "properties": {}}, lambda: _fit(brief), max_chars=12000),
             Tool("read_memory", "Read your own past debates and track record (not evidence).", {"type": "object", "properties": {}},
                  lambda: mem, max_chars=6000),
             _position_tool(store, ids, cap)]
    task = (f"You are {role}. Cap for the size: ¥{cap:,.0f} of operating profit. "
            + ("Round 1: state your position independently." if other is None else
               "Round 2: the other side's validated position is below; answer it, concede what the evidence does not support, "
               f"and resubmit.\n{json.dumps(other, ensure_ascii=False)[:4000]}"))
    model = DEBATER_A_MODEL if name == "debater_a" else DEBATER_B_MODEL
    res = run_agent(name, load_prompt(name), task, tools, 4,
                    skills=["debate-position", "evidence-board", "debater-memory", "guardrails"], model=model)
    pos = dict(store) or {"direction": "none", "size_jpy": 0.0, "claims": [], "confidence": "low",
                          "summary": "(no position submitted)", "concessions": []}
    pos["claims"], pos["rejected_claims"] = _validate(pos, ids)
    pos["model"], pos["steps"] = model or llm.MODEL, res.steps
    return pos


# ---------------------------------------------------------------- C&A and the consensus rule
def decide(positions: dict, brief: dict) -> dict:
    state = {"evidence": json.dumps([{k: i.get(k) for k in ("id", "date", "title", "excerpt")} for i in brief.get("items", [])],
                                    ensure_ascii=False)[:9000],
             "link_check": json.dumps([{k: r.get(k) for k in ("id", "resolved", "type", "pages", "sha256", "error")}
                                       for r in brief.get("link_check", [])], ensure_ascii=False)[:1500],
             "claim_check": json.dumps({k: {"claims_verified": v.get("claims_verified"),
                                            "claims_with_unverified_numbers": v.get("claims_with_unverified_numbers"),
                                            "unverified": [c.get("unverified_numbers") for c in v.get("claims", []) if c.get("unverified_numbers")]}
                                        for k, v in positions.items()}, ensure_ascii=False)[:1500],
             "measured_md": json.dumps(brief.get("md", {}), ensure_ascii=False)[:1500],
             "positions": json.dumps({k: {x: v.get(x) for x in ("direction", "size_jpy", "claims", "confidence", "summary", "concessions")}
                                      for k, v in positions.items()}, ensure_ascii=False)[:7000]}
    q = {
        "evidence_a": {"type": "noul", "instructions": "Do Debater A's cited items (their excerpts) support A's claims, using only "
                       "events not covered by land prices, building costs or Tsuchiya's guidance record? Claims whose numbers the "
                       "claim check could not find in the source count against the side.",
                       "criteria": {"true": "Well supported by the cited items.", "false": "Not supported, or about measured variables."}},
        "evidence_b": {"type": "noul", "instructions": "Same question for Debater B's claims.",
                       "criteria": {"true": "Well supported by the cited items.", "false": "Not supported, or about measured variables."}},
        "supported": {"type": "choice", "instructions": "Which side is better supported by the cited evidence?",
                      "criteria": {"A": "Debater A is better supported.", "B": "Debater B is better supported.", "neither": "Neither."}},
        "bucket": {"type": "choice", "instructions": "Which adjustment to the company's operating-profit forecast does the cited "
                   "evidence support? down = the forecast looks too high.",
                   "criteria": {"strong_down": "Clearly too high.", "down": "Somewhat too high.", "none": "No adjustment.",
                                "up": "Somewhat too low.", "strong_up": "Clearly too low."}},
    }
    r = llm.decide(JUDGE, state, q)
    if "error" in r:
        return {"error": r["error"]}
    a = r["answers"]
    out = {"evidence_a": (a.get("evidence_a") or {}).get("noul"), "evidence_b": (a.get("evidence_b") or {}).get("noul"),
           "supported": (a.get("supported") or {}).get("probabilities"), "bucket": (a.get("bucket") or {}).get("probabilities"),
           "model": JUDGE}
    return out


def monitor(transcript: list[dict]) -> list[dict]:
    qs = {k: {"type": "noul", "instructions": qq, "criteria": {"false": f, "true": t}}
          for k, (qq, f, t) in consensus.MONITOR_QUESTIONS.items() if k != "legal_accusation"}
    qs["out_of_scope"] = {"type": "noul", "instructions": "Argues from land prices, building-material costs or the company's "
                          "past guidance record (already measured), instead of new events.",
                          "criteria": {"false": "Argues from new events only.", "true": "Argues from measured variables."}}
    out = []
    for t in transcript:
        r = llm.decide(MONITOR, f"Speaker: {t['name']} (round {t['round']})\n"
                       + json.dumps({k: t.get(k) for k in ("summary", "claims", "concessions")}, ensure_ascii=False)[:5000], qs)
        out.append({"name": t["name"], "round": t["round"], **({"error": r["error"]} if "error" in r else
                    {k: round(float(v.get("noul", 0)), 3) for k, v in r["answers"].items() if isinstance(v, dict)})})
    return out


def consensus_rule(dec: dict, cap: float) -> tuple[float, str]:
    if not dec or dec.get("error") or not dec.get("bucket"):
        return 0.0, f"no C&A decision ({(dec or {}).get('error', 'missing')}): signal 0"
    p = dec["bucket"]
    down = p.get("strong_down", 0) + p.get("down", 0)
    up = p.get("strong_up", 0) + p.get("up", 0)
    if down > SPLIT and up > SPLIT:
        return 0.0, f"answers point both ways (down {down:.0%}, up {up:.0%}): signal 0"
    sup = dec.get("supported") or {}
    win = max(("A", "B"), key=lambda k: sup.get(k, 0)) if sup else "A"
    ev = dec.get(f"evidence_{win.lower()}") or 0.0
    expected = sum(p.get(k, 0) * s for k, s in SHARE.items())
    return cap * expected * ev, (f"cap ¥{cap:,.0f} × expected share {expected:+.2f} × evidence score of {win} {ev:.2f}")


def _claimcheck(a: dict, b: dict) -> tuple[dict, dict]:
    """Code checks every number in the claims against the cited sources' text (A43)."""
    from pathlib import Path
    path = "tmp/claimcheck.json"
    base = Path(os.environ.get("TSUCHIYA_ROOT") or llm.ROOT)   # the pipeline's data root (test sandbox or v2/app)
    (base / "tmp").mkdir(exist_ok=True)
    (base / path).write_text(json.dumps({"debater_a": a, "debater_b": b}, ensure_ascii=False), encoding="utf-8")
    r = j(pipeline("claimcheck", path))
    pos = r.get("positions") or {}
    return pos.get("debater_a", a), pos.get("debater_b", b)


# ---------------------------------------------------------------- the whole debate
def run(gate: dict, md_context: dict, trigger: str) -> dict:
    """Gates 1-3 and the decision. Returns the record (also stored by the pipeline)."""
    res = {"trigger": trigger, "items": [], "positions": {}, "stopped_at": "", "signal": 0.0}
    by_topic: dict[str, list] = {}
    for it in gate["gate0"]:
        by_topic.setdefault(it["topic"], []).append(it)
    flagged = []
    for topic, items in by_topic.items():
        flagged += scout(topic, items[:12])
    res["scouts"] = {t: len(v) for t, v in by_topic.items()}
    if not flagged:
        res["stopped_at"] = "gate 1 (scouts flagged nothing)"
        return res
    lc = j(pipeline("linkcheck", "--ids", ",".join(dict.fromkeys(f["id"] for f in flagged))))
    res["link_check"] = lc.get("report", [])
    ids = lc.get("resolved_ids") or []
    res["items"] = ids
    if not ids:
        res["stopped_at"] = "link check (no flagged source could be fetched and read)"
        return res
    cap = float(md_context.get("cap") or 0.0)
    brief = {"items": lc.get("items", []), "link_check": res["link_check"],
             "scout_reasons": [f for f in flagged if f["id"] in ids],
             "md": {k: md_context.get(k) for k in ("forecast_fy", "forecast_operating_profit", "guidance_date", "months_left",
                                                   "discount_baseline", "supply_costs_pct", "land_price_pct", "cap", "cap_rule")}}
    if cap <= 0:
        res["stopped_at"] = "cap is 0 (no guidance record yet, or the fiscal year has ended)"
        return res
    transcript = []
    a = debater("debater_a", "Debater A, the proposer", brief, None, cap, set(ids))
    b = debater("debater_b", "Debater B, the sceptic", brief, None, cap, set(ids))
    a, b = _claimcheck(a, b)
    transcript += [{"name": "debater_a", "round": 1, **a}, {"name": "debater_b", "round": 1, **b}]
    size_a = a["size_jpy"] if a["direction"] != "none" else 0.0
    if size_a < IMMATERIAL or not a["claims"]:
        res["stopped_at"] = "gate 2 (A's proposal immaterial or uncited)"
    elif b["direction"] == a["direction"]:
        res["stopped_at"] = "gate 2 (B agrees with A after round 1)"
    else:
        a = debater("debater_a", "Debater A, the proposer", brief, {k: b.get(k) for k in ("direction", "size_jpy", "claims", "summary")}, cap, set(ids))
        b = debater("debater_b", "Debater B, the sceptic", brief, {k: a.get(k) for k in ("direction", "size_jpy", "claims", "summary")}, cap, set(ids))
        a, b = _claimcheck(a, b)
        transcript += [{"name": "debater_a", "round": 2, **a}, {"name": "debater_b", "round": 2, **b}]
        res["stopped_at"] = "after round 2"
    res["positions"] = {"debater_a": a, "debater_b": b}
    res["transcript"] = transcript
    if res["stopped_at"].startswith("gate 2 (A's"):
        res["note"] = "no material, cited proposal: signal 0"
        return res
    res["monitor"] = monitor(transcript)
    res["decision"] = decide(res["positions"], brief)
    res["signal"], res["note"] = consensus_rule(res["decision"], cap)
    return res
