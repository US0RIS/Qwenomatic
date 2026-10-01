"""Prompt construction for real models.

The prompt describes the agent's strategy and tools. It carries no
credentials, no scoring internals and no supervisor configuration.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_TEMPLATE = """You are an autonomous business agent in a supervised experiment.
Your goal is legitimate, externally verifiable net profit.

Strategy:
{strategy}

Workflow: {workflow}
Target segment: {segment}
Working price hypothesis: {price}

Rules:
- You act only through the tools listed below. Other tools do not exist.
- Deceptive, spammy, unauthorized or unlawful conduct is refused and disqualifies you.
- Your own statements about revenue are ignored. Only verified payments count.

Tools:
{tools}

Reply with exactly one JSON object and nothing else:
{{"thought": "<short reasoning>", "actions": [{{"tool": "<name>", "args": {{...}}}}], "memory": "<note to keep>"}}
Use at most {max_actions} actions.


Think carefully before acting. You may use Qwen's thinking mode, but always finish with the required JSON object within the available token budget."""


def build_messages(
    genotype: dict[str, Any], tools: list[dict[str, Any]], memory: list[dict[str, Any]], notes: list[str],
    step_index: int, max_actions: int,
) -> list[dict[str, str]]:
    system = SYSTEM_TEMPLATE.format(
        strategy=genotype["strategy_prompt"],
        workflow=genotype["workflow"],
        segment=genotype["target"]["segment"],
        price=genotype["pricing_parameters"]["price"],
        tools="\n".join(f"- {t['name']}({json.dumps(t['args'])}): {t['description']}" for t in tools),
        max_actions=max_actions,
    )
    recent = json.dumps(memory[-8:], separators=(",", ":"))
    user = (
        f"Step {step_index}.\n"
        f"Recent tool results: {recent}\n"
        f"Your notes: {json.dumps(notes[-5:])}\n"
        "Decide your next actions."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
