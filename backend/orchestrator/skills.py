"""Skill definitions — tool allowlists and graph depth per agent type.

Each skill restricts which tools the LLM can call and how deep the code graph
is traversed.  Narrower context = fewer tokens = faster, cheaper responses.

Agent → Skill mapping (same names as _detect_agent in orchestrator.py):
  coder      — full write access, DRY check via graph, handles all coding + explanation
  debugger   — trace errors, read files, no code proposals
  main       — all tools (fallback when intent is unclear)

Architect is NOT a user-facing skill. It runs as an internal validator
inside propose_file_edit before any edit proposal is accepted.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass


@dataclass(frozen=True)
class Skill:
    allowed_tools: frozenset[str]
    graph_depth: int  # default depth passed to query_code_graph


_ALL_TOOLS: frozenset[str] = frozenset({
    "get_project_memory",
    "query_code_graph",
    "search_files",
    "read_file",
    "propose_file_edit",
    "suggest_memory_update",
    "check_architecture_rules",
})

SKILLS: dict[str, Skill] = {
    # Coder: full write access + DRY graph check. Handles coding AND explanation.
    "coder": Skill(
        allowed_tools=frozenset({
            "get_project_memory",
            "query_code_graph",
            "search_files",
            "read_file",
            "propose_file_edit",
            "suggest_memory_update",
            "check_architecture_rules",
        }),
        graph_depth=2,
    ),

    # Debugger: trace call chains, read files, check rules — no writes
    "debugger": Skill(
        allowed_tools=frozenset({
            "get_project_memory",
            "query_code_graph",
            "search_files",
            "read_file",
            "check_architecture_rules",
        }),
        graph_depth=3,
    ),

    # Main / fallback: all tools, standard depth
    "main": Skill(
        allowed_tools=_ALL_TOOLS,
        graph_depth=2,
    ),
}


def apply_skill(agent: str, tools: list[dict]) -> list[dict]:
    """Return a filtered + depth-patched copy of the tools list for this agent.

    get_project_memory is excluded from all skills — it is pre-loaded before
    the LLM loop and injected as a synthetic tool result. The LLM never needs
    to call it explicitly.
    """
    skill = SKILLS.get(agent, SKILLS["main"])
    result: list[dict] = []
    for tool in tools:
        if tool["name"] == "get_project_memory":
            continue  # always pre-loaded, never exposed to LLM
        if tool["name"] not in skill.allowed_tools:
            continue
        if tool["name"] == "query_code_graph":
            tool = copy.deepcopy(tool)
            tool["input_schema"]["properties"]["depth"]["default"] = skill.graph_depth
        result.append(tool)
    return result
