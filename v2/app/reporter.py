"""Reporter agent (Addendum A42, v2.007): the weekly 1–2 page summary.

  code   `cli.py digest`: the condensed record since the last report (values, weights, Md, debates, research)
  agent  reads the digest and writes the narrative (at most 500 words); describes, never decides
  code   checks every number against the digest (unverified ones are flagged in the text), builds the tables and
         the HTML page (`cli.py report-write`), and the close-out posts it to Tsuchibot Main once
Runs at the daily check (first run after the TSE close) when the last summary is 7+ days old, or after 5 Md
debates since it.
"""
from __future__ import annotations

import json
import os

import consensus
from llm import Tool, load_prompt, pipeline, run_agent

REPORTER_MODEL = (os.getenv("REPORTER_MODEL") or "").strip() or None


def write(root) -> str:
    digest = pipeline("digest")
    tools = [Tool("read_digest", "Read the condensed record since the last summary.", {"type": "object", "properties": {}},
                  lambda: digest, max_chars=12000)]
    res = run_agent("reporter", load_prompt("reporter"), "Write this week's summary narrative. Read the digest first.",
                    tools, 3, skills=["log-digest", "summary-report", "number-check", "reporting", "guardrails"], model=REPORTER_MODEL)
    text = res.text.strip()
    try:
        nums = consensus._board_numbers(consensus._flatten_all(json.loads(digest)))
        bad = consensus._unverified_numbers(text, nums)
    except (json.JSONDecodeError, TypeError):
        bad = []
    if bad:
        text += f"\n\n(Code check: {len(bad)} number(s) not found in the record and not verified: {', '.join(bad[:8])})"
    (root / "tmp").mkdir(exist_ok=True)
    (root / "tmp" / "summary.md").write_text(text, encoding="utf-8")
    return pipeline("report-write", "tmp/summary.md")
