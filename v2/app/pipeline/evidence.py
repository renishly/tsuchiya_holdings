"""Evidence board, gate 0 and the debate record for Md's Debate Signal (Addenda A41-A42, v2.007).

Evidence board (code): dated items the scouts and debaters may cite, each with a stable id (E-xxxxxxxx).
  Sources today (already allowed): Tsuchiya's and the peers' IR notices (決算短信, 業績予想の修正 and other PDFs
  linked from their IR pages) and Tsuchiya's new EDINET filings. Other news sources need the student's approval
  (D18) and are added to SOURCES when approved.
  For a new Tsuchiya item, the first page of text is kept as an excerpt (at most 1,500 characters).
  Items older than 120 days drop off the board.
Gate 0 (code): items first seen after the last debate check. None -> no model is called.
Debate record (code): the C&A result and the consensus rule's Debate Signal, written to the case log
  (state/debate_cases.jsonl), to state (for Md) and as Baseline Metrics rows. DEBATE_SIGNAL_MODE=shadow (default)
  logs the signal without applying it to Md; live applies it (only after the student switches it on, D20).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re

import market
from lib import CONFIG, ROOT, BudgetStop, FetchError, GuardrailError, fetch, iso, log, run_ctx, write_text

BOARD = "state/evidence_board.json"
CASES = "state/debate_cases.jsonl"
KEEP_DAYS = 120
MODE = (os.getenv("DEBATE_SIGNAL_MODE") or "shadow").strip().lower()      # shadow | live
TOPICS = {  # topic -> title keywords (Japanese); company notices default to "company and peers"
    "housing policy and finance": r"住宅ローン|金利|補助金|減税|政策|日銀",
    "regional economy and disasters": r"地震|災害|豪雪|台風|北海道|東北",
    "materials and supply chain": r"資材|木材|原材料|納期|供給|物流",
    "politics and macro": r"選挙|税制|為替|景気",
}


def load_board() -> dict:
    p = ROOT / BOARD
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"items": {}, "last_check": ""}


def save_board(b: dict):
    write_text(BOARD, json.dumps(b, ensure_ascii=False, indent=1))


def _topic(title: str) -> str:
    for t, pat in TOPICS.items():
        if re.search(pat, title):
            return t
    return "company and peers"


def _id(url: str) -> str:
    return "E-" + hashlib.sha256(url.encode()).hexdigest()[:8]


def _excerpt(st, url: str) -> str:
    try:
        body, meta = fetch(st, url, binary=True, purpose="evidence board: notice text")
    except (FetchError, GuardrailError, BudgetStop) as e:
        return f"(text unavailable: {str(e)[:80]})"
    try:
        import io
        import pdfplumber
        with pdfplumber.open(io.BytesIO(body)) as d:
            text = "\n".join((p.extract_text() or "") for p in d.pages[:2])
    except Exception:
        return "(text unavailable)"
    from lib import flag_injection
    flag_injection(st, url, text)
    return re.sub(r"[ \t]+", " ", text)[:1500]


def collect(st, max_excerpts: int = 5) -> dict:
    """Add new items from the allowed sources; return the board and the items for gate 0."""
    b = load_board()
    today = iso()[:10]
    test = run_ctx(st).get("mode") == "test"
    added, excerpts = [], 0
    first_fill = not b["items"]
    tickers = [CONFIG["company_code"]] + [p["ticker"] for p in CONFIG["peers"]]
    for t in tickers:
        if test:
            break
        try:
            docs = market.find_forecast_docs(st, t)
        except (FetchError, GuardrailError, BudgetStop) as e:
            log(st, "evidence", {"ticker": t}, f"IR page unavailable: {e}")
            continue
        for d in docs:
            i = _id(d["url"])
            if i in b["items"] or not d.get("date"):
                continue
            item = {"id": i, "date": d["date"], "ticker": t, "title": d["title"][:200], "url": d["url"],
                    "source": "company IR page", "topic": _topic(d["title"]), "first_seen": iso()}
            # a first fill of the board takes the last 30 days only, so old notices do not open the gate
            if first_fill and d["date"] < (dt.date.fromisoformat(today) - dt.timedelta(days=30)).isoformat():
                item["first_seen"] = "backfill"
            if t == CONFIG["company_code"] and excerpts < max_excerpts and item["first_seen"] != "backfill":
                item["excerpt"] = _excerpt(st, d["url"])
                excerpts += 1
            b["items"][i] = item
            added.append(i)
    for d in st["documents"].get("seen_recent", []):  # Tsuchiya's new EDINET filings (found by the daily check)
        url = f"https://disclosure2.edinet-fsa.go.jp/WZEK0040.aspx?{d.get('docID')}"
        i = _id(url)
        if i not in b["items"]:
            b["items"][i] = {"id": i, "date": (d.get("submitDateTime") or today)[:10], "ticker": CONFIG["company_code"],
                             "title": d.get("docDescription", "EDINET filing")[:200], "url": url, "source": "EDINET",
                             "topic": "company and peers", "first_seen": iso()}
            added.append(i)
    cutoff = (dt.date.fromisoformat(today) - dt.timedelta(days=KEEP_DAYS)).isoformat()
    b["items"] = {k: v for k, v in b["items"].items() if v["date"] >= cutoff}
    new = [v for v in b["items"].values() if v["first_seen"] not in ("backfill",) and v["first_seen"] > (b.get("last_check") or "")]
    save_board(b)
    log(st, "evidence", {"added": len(added)}, f"{len(new)} item(s) for gate 0")
    return {"added": added, "gate0": sorted(new, key=lambda x: x["date"], reverse=True),
            "board_size": len(b["items"]), "company_notice": any(v["ticker"] == CONFIG["company_code"] for v in new)}


def close_gate(st):
    b = load_board()
    b["last_check"] = iso()
    save_board(b)


# ---------------------------------------------------------------- the debate record
def read_cases(n: int | None = None) -> list[dict]:
    p = ROOT / CASES
    if not p.exists():
        return []
    rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    return rows[-n:] if n else rows


def record(st, res: dict) -> dict:
    """Store one debate's result: case log line, the Debate Signal for Md, and a short memory per debater."""
    res = {**res, "mode": MODE, "recorded": iso(), "outcome": None}  # outcome filled once actual results are known
    write_text(CASES, json.dumps(res, ensure_ascii=False) + "\n", mode="a")
    st["documents"]["debate_signal"] = {"value": res.get("signal", 0.0), "date": iso()[:10], "mode": MODE,
                                        "expires": (dt.date.today() + dt.timedelta(days=30)).isoformat(),
                                        "items": res.get("items", []), "note": res.get("note", "")}
    close_gate(st)
    return st["documents"]["debate_signal"]


def memory(name: str) -> dict:
    """What a debater may read about itself: its last cases and a simple track record (outcomes when known)."""
    cases = read_cases()
    mine = [c for c in cases if name in (c.get("positions") or {})]
    done = [c for c in mine if c.get("outcome") is not None]
    return {"debates": len(mine), "with_outcome": len(done),
            "recent": [{"date": c["recorded"][:10], "items": c.get("items"), "your_position": c["positions"][name],
                        "signal": c.get("signal"), "evidence_score": (c.get("decision") or {}).get(f"evidence_{name[-1].lower()}")}
                       for c in mine[-3:]],
            "note": "Memory is not evidence: cite only items on the evidence board."}
