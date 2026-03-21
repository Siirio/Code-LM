"""Skill definitions — tool allowlists and graph depth per agent type.

Each skill restricts which tools the LLM can call and how deep the code graph
is traversed.  Narrower context = fewer tokens = faster, cheaper responses.

Agent → Skill mapping (same names as _detect_agent in orchestrator.py):
  debugger   — trace errors, read files, no code proposals
  codegen    — full write access, DRY check via graph
  architect  — graph + rules only, no file reads or edits
  explain    — read-only, shallow context
  main       — all tools (fallback when intent is unclear)
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Skill:
    allowed_tools: frozenset[str]
    graph_depth: int  # default depth passed to query_code_graph


# ── Skill definitions ─────────────────────────────────────────────────────────

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
    # Debugging: trace call chains, read files, check rules — no writes
    "debugger": Skill(
        allowed_tools=frozenset({
            "get_project_memory",
            "query_code_graph",
            "search_files",
            "read_file",
            "check_architecture_rules",
        }),
        graph_depth=3,   # need to follow import chains to the error site
    ),

    # Code generation: full write access + DRY graph check
    "codegen": Skill(
        allowed_tools=frozenset({
            "get_project_memory",
            "query_code_graph",
            "search_files",
            "read_file",
            "propose_file_edit",
            "suggest_memory_update",
        }),
        graph_depth=2,   # check direct neighbours for DRY, no need to go deeper
    ),

    # Architecture review: graph + rules only — no file content, no edits
    "architect": Skill(
        allowed_tools=frozenset({
            "get_project_memory",
            "query_code_graph",
            "check_architecture_rules",
            "suggest_memory_update",
        }),
        graph_depth=4,   # needs full dependency picture across layers
    ),

    # Explain / Q&A: read-only, shallow — just find and show the right file
    "explain": Skill(
        allowed_tools=frozenset({
            "get_project_memory",
            "search_files",
            "read_file",
        }),
        graph_depth=1,   # only the file itself, no relationship expansion
    ),

    # Main / fallback: all tools, standard depth
    "main": Skill(
        allowed_tools=_ALL_TOOLS,
        graph_depth=2,
    ),
}


def apply_skill(agent: str, tools: list[dict]) -> list[dict]:
    """Return a filtered + depth-patched copy of the tools list for this agent.

    - Only tools in the skill's allowlist are included.
    - The default `depth` in query_code_graph is overridden to the skill's value
      so the LLM uses the right traversal depth without being instructed explicitly.
    """
    skill = SKILLS.get(agent, SKILLS["main"])
    result: list[dict] = []
    for tool in tools:
        if tool["name"] not in skill.allowed_tools:
            continue
        if tool["name"] == "query_code_graph":
            tool = copy.deepcopy(tool)
            tool["input_schema"]["properties"]["depth"]["default"] = skill.graph_depth
        result.append(tool)
    return result
