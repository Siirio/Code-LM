"""Working Memory Layer — per-session ephemeral understanding store.

Persists UNDERSTANDING, not raw data:
- Which files have been read this session (paths only — no raw content)
- Extracted decisions and plan steps from assistant responses
- Last active agent and task (used by agent stickiness in orchestrator)

All state is in-process (no DB). Expires when the process restarts.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_store: dict[str, "SessionState"] = {}

# Sentence patterns that signal a decision or plan in assistant text
_DECISION_RE = re.compile(
    r"(?:I(?:'ll| will) \w[^.]{5,80}"
    r"|Let me \w[^.]{5,60}"
    r"|The (?:plan|approach|strategy) is[^.]{5,80}"
    r"|Next[,: ]+\w[^.]{5,60})",
    re.IGNORECASE,
)

_MAX_FILES = 20
_MAX_DECISIONS = 10


@dataclass
class SessionState:
    files_read: list[str] = field(default_factory=list)   # ordered, most-recent last
    decisions: list[str] = field(default_factory=list)    # extracted decision sentences
    plan_steps: list[str] = field(default_factory=list)   # explicit plan steps
    last_agent: str | None = None
    last_agent_task: str | None = None  # first 80 chars of the last user message


def get_state(session_id: str) -> SessionState:
    if session_id not in _store:
        _store[session_id] = SessionState()
    return _store[session_id]


def update_from_file_read(session_id: str, file_path: str) -> None:
    """Record that a file was read this session."""
    state = get_state(session_id)
    if file_path in state.files_read:
        # Move to end (most-recently-read)
        state.files_read.remove(file_path)
    state.files_read.append(file_path)
    if len(state.files_read) > _MAX_FILES:
        state.files_read = state.files_read[-_MAX_FILES:]


def update_from_response(session_id: str, text: str) -> None:
    """Extract decisions and plan steps from an assistant response."""
    if not text:
        return
    state = get_state(session_id)
    for m in _DECISION_RE.finditer(text):
        sentence = m.group(0).strip().rstrip(".,")
        if sentence and sentence not in state.decisions:
            state.decisions.append(sentence)
    state.decisions = state.decisions[-_MAX_DECISIONS:]


def format_injection(session_id: str) -> str | None:
    """Return a compact working-memory block to inject before the LLM call.

    Returns None if there is nothing useful to inject.
    """
    state = get_state(session_id)
    parts: list[str] = []

    if state.files_read:
        paths = "\n".join(f"  - {p}" for p in state.files_read)
        parts.append(f"Files already read this session:\n{paths}")

    if state.decisions:
        recent = state.decisions[-5:]
        items = "\n".join(f"  • {d}" for d in recent)
        parts.append(f"Decisions made this session:\n{items}")

    if not parts:
        return None

    return "--- Working Memory ---\n" + "\n\n".join(parts) + "\n--- End Working Memory ---"
