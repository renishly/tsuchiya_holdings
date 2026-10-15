"""Subagent registry. The orchestrator can delegate only to subagents listed here.

To add a subagent: create subagents/<name>.py with a SPEC (see baseline.py), write prompts/<name>.md, and add it
to SUBAGENTS below. The orchestrator's `delegate` tool lists the registered names automatically, and it can pass
one subagent's report into another's task, which is where cross-communication between subagents will go.
"""
from __future__ import annotations

from dataclasses import dataclass

from llm import Tool


@dataclass
class SubagentSpec:
    name: str
    description: str        # shown to the orchestrator: what this subagent owns
    prompt: str             # prompts/<prompt>.md is its system prompt
    tools: list[Tool]
    max_steps: int = 8
    # Deterministic work used when the model API is unavailable: (start_plan) -> list of pipeline arg tuples.
    fallback: object = None


def _registry() -> dict[str, SubagentSpec]:
    from subagents import baseline, factor
    return {s.name: s for s in (baseline.SPEC, factor.SPEC)}


SUBAGENTS = _registry()
