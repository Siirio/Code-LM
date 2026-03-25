"""Working Memory Layer — per-session ephemeral understanding store.

Persists UNDERSTANDING, not raw data:
- Which files have been read this session (paths only — no raw content)
- Extracted decisions and plan steps from assistant responses
- Last active agent and task (used by agent stickiness in orchestrator)
- Task Contract: structured intent shared across ALL agents in a session

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
class TaskContract:
    """Structured intent shared by ALL agents in a session.

    Goal evolves with clarifications — each user refinement appended to
    clarifications[] and goal updated to reflect the latest understanding.
    When a genuinely new unrelated topic appears, detect_topic_shift() flags it
    so the UI can suggest opening a new chat.
    """
    goal: str = ""                              # current goal (updated with refinements)
    clarifications: list[str] = field(default_factory=list)   # how goal evolved over time
    constraints: list[str] = field(default_factory=list)
    architecture_flow: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    recent_file_changes: list[str] = field(default_factory=list)
    # Core topic words extracted from the original goal for shift detection
    _topic_words: list[str] = field(default_factory=list)


@dataclass
class ModuleMemory:
    """What we know about one feature module this session."""
    classes_seen: list[str] = field(default_factory=list)
    summary: str = ""                # one-sentence summary of what was found
    key_findings: list[str] = field(default_factory=list)


@dataclass
class SessionState:
    files_read: list[str] = field(default_factory=list)       # ordered, most-recent last
    decisions: list[str] = field(default_factory=list)        # extracted decision sentences
    plan_steps: list[str] = field(default_factory=list)       # explicit plan steps
    modules_explored: dict[str, ModuleMemory] = field(default_factory=dict)
    last_agent: str | None = None
    last_agent_task: str | None = None  # first 80 chars of the last user message
    task_contract: TaskContract = field(default_factory=TaskContract)

    def file_modules(self) -> set[str]:
        """Return the set of modules whose files have been read."""
        return {
            mod for mod, mem in self.modules_explored.items()
            if mem.classes_seen
        }


def get_state(session_id: str) -> SessionState:
    if session_id not in _store:
        _store[session_id] = SessionState()
    return _store[session_id]


def update_from_file_read(session_id: str, file_path: str) -> None:
    """Record that a file was read this session."""
    state = get_state(session_id)
    if file_path in state.files_read:
        state.files_read.remove(file_path)
    state.files_read.append(file_path)
    if len(state.files_read) > _MAX_FILES:
        state.files_read = state.files_read[-_MAX_FILES:]


def update_module(
    session_id: str,
    module_name: str,
    classes_seen: list[str] | None = None,
    summary: str = "",
    key_finding: str = "",
) -> None:
    """Record or extend what we know about a module this session."""
    state = get_state(session_id)
    if module_name not in state.modules_explored:
        state.modules_explored[module_name] = ModuleMemory()
    mem = state.modules_explored[module_name]
    if classes_seen:
        for cls in classes_seen:
            if cls not in mem.classes_seen:
                mem.classes_seen.append(cls)
    if summary:
        mem.summary = summary
    if key_finding and key_finding not in mem.key_findings:
        mem.key_findings.append(key_finding)
        mem.key_findings = mem.key_findings[-5:]


# Stop-words excluded from topic word extraction
_STOP_WORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "is", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "that", "this", "it", "be", "was", "are", "has",
    "have", "do", "does", "i", "me", "my", "we", "our", "you", "your", "can",
    "will", "need", "want", "make", "get", "let", "add", "create", "new", "please",
    "so", "just", "also", "all", "some", "any", "now", "not", "no",
})

# Phrases that strongly signal a completely new unrelated topic
_TOPIC_SHIFT_STARTERS: tuple[str, ...] = (
    "let's now work on", "let us now work on",
    "now let's work on", "now let us work on",
    "completely different", "switch to", "move on to",
    "forget about that", "forget about this", "different topic",
    "new feature", "new module", "new task",
    "now implement", "now build", "now create",
    "can you now build", "can you now create", "can you now add",
    "i want to work on something else", "something completely different",
    "unrelated to", "change of topic", "different request",
)


def _extract_topic_words(text: str) -> list[str]:
    """Return meaningful content words from text (for topic comparison)."""
    words = re.findall(r"[a-z]+", text.lower())
    return [w for w in words if len(w) >= 4 and w not in _STOP_WORDS]


def update_task_contract(
    session_id: str,
    goal: str = "",
    refinement: str = "",
    constraint: str = "",
    arch_flow: str = "",
    decision: str = "",
    file_changed: str = "",
) -> None:
    """Record or extend the Task Contract for this session.

    goal=      : sets the initial goal (only on first message; ignored thereafter)
    refinement=: user clarification / correction — appended to clarifications and
                 the goal is updated to include the new understanding
    constraint=, arch_flow=, decision=, file_changed= : additive fields
    """
    state = get_state(session_id)
    tc = state.task_contract

    if goal and not tc.goal:
        tc.goal = goal[:300]
        # Seed topic words from the initial goal for later shift detection
        tc._topic_words = _extract_topic_words(goal)[:12]

    if refinement:
        short = refinement[:200]
        if short not in tc.clarifications:
            tc.clarifications.append(short)
            tc.clarifications = tc.clarifications[-6:]
            # Update the working goal to absorb the refinement
            # Append a concise note rather than replacing entirely
            if tc.goal:
                tc.goal = tc.goal + f" [updated: {short}]"
            else:
                tc.goal = short

    if constraint and constraint not in tc.constraints:
        tc.constraints.append(constraint)
        tc.constraints = tc.constraints[-5:]
    if arch_flow and arch_flow not in tc.architecture_flow:
        tc.architecture_flow.append(arch_flow)
        tc.architecture_flow = tc.architecture_flow[-3:]
    if decision and decision not in tc.decisions:
        tc.decisions.append(decision)
        tc.decisions = tc.decisions[-8:]
    if file_changed and file_changed not in tc.recent_file_changes:
        tc.recent_file_changes.append(file_changed)
        tc.recent_file_changes = tc.recent_file_changes[-10:]


def detect_topic_shift(session_id: str, message: str) -> bool:
    """Return True if the message looks like a completely new unrelated topic.

    Used to suggest opening a new chat. Only fires when:
    1. There is an active task contract (previous work exists), AND
    2. Either a shift-starter phrase is present, OR the message shares
       fewer than 2 words with the known topic of the current goal.

    Deliberately conservative — false negatives are fine (user just continues),
    false positives are annoying (wrongly telling them to start a new chat).
    """
    state = get_state(session_id)
    tc = state.task_contract
    if not tc.goal or not tc._topic_words:
        return False  # No goal established yet — nothing to shift from

    msg_lower = message.lower()

    # Fast path: explicit shift phrase
    if any(msg_lower.startswith(phrase) or (f" {phrase} " in msg_lower)
           for phrase in _TOPIC_SHIFT_STARTERS):
        return True

    # Build combined topic vocabulary from goal + all clarifications
    topic_vocab = list(tc._topic_words)
    for clarification in tc.clarifications:
        topic_vocab.extend(_extract_topic_words(clarification))

    msg_words = set(_extract_topic_words(msg_lower))
    if len(msg_words) < 4:
        return False  # Too short to judge (questions, short replies)

    # Use substring matching so "roles" matches "role", "manager" matches "manage", etc.
    def has_overlap(msg_set: set[str], vocab: list[str]) -> bool:
        for mw in msg_set:
            for tv in vocab:
                if mw in tv or tv in mw:
                    return True
        return False

    if not has_overlap(msg_words, topic_vocab) and len(msg_words) >= 4:
        return True

    return False


def update_from_response(session_id: str, text: str) -> None:
    """Extract decisions and plan steps from an assistant response."""
    if not text:
        return
    state = get_state(session_id)
    for m in _DECISION_RE.finditer(text):
        sentence = m.group(0).strip().rstrip(".,")
        if sentence and sentence not in state.decisions:
            state.decisions.append(sentence)
            # Mirror top decisions into the task contract
            update_task_contract(session_id, decision=sentence)
    state.decisions = state.decisions[-_MAX_DECISIONS:]


def format_task_contract(session_id: str) -> str | None:
    """Return a compact Task Contract block for injection into sub-agent context.

    Sub-agents (coder, debugger) receive this instead of full session history
    so they understand the evolving goal, clarifications, and what has changed.

    Returns None if no goal has been set yet (first message in session).
    """
    state = get_state(session_id)
    tc = state.task_contract
    if not tc.goal:
        return None

    lines = ["--- Task Contract ---"]
    # Strip inline [updated: ...] tags from the goal for cleaner display
    display_goal = re.sub(r"\s*\[updated:.*?\]", "", tc.goal).strip()
    lines.append(f"Goal: {display_goal}")

    if tc.clarifications:
        lines.append("User clarifications (goal refinements, in order):")
        for c in tc.clarifications:
            lines.append(f"  → {c}")

    if tc.constraints:
        lines.append("Constraints:")
        for c in tc.constraints:
            lines.append(f"  - {c}")

    if tc.architecture_flow:
        lines.append("Architecture:")
        for f in tc.architecture_flow:
            lines.append(f"  {f}")

    if tc.decisions:
        lines.append("Decisions made:")
        for d in tc.decisions[-5:]:
            lines.append(f"  • {d}")

    if tc.recent_file_changes:
        lines.append("Files changed this session:")
        for fp in tc.recent_file_changes:
            lines.append(f"  - {fp}")

    lines.append("--- End Task Contract ---")
    return "\n".join(lines)


def format_injection(session_id: str) -> str | None:
    """Return a compact working-memory block to inject before the LLM call.

    Module summaries replace per-file listings for files within explored modules.
    Returns None if there is nothing useful to inject.
    """
    state = get_state(session_id)
    parts: list[str] = []

    # Module summaries (richer than file list)
    if state.modules_explored:
        mod_lines: list[str] = []
        for mod_name, mem in sorted(state.modules_explored.items()):
            cls_str = f"[{', '.join(mem.classes_seen[:6])}{'…' if len(mem.classes_seen) > 6 else ''}]"
            line = f"  - {mod_name}: {cls_str}"
            if mem.summary:
                line += f" — {mem.summary}"
            if mem.key_findings:
                line += f"\n    Findings: {'; '.join(mem.key_findings)}"
            mod_lines.append(line)
        parts.append("Modules explored this session:\n" + "\n".join(mod_lines))

    # Files not belonging to any explored module
    explored_files: set[str] = set()
    for mem in state.modules_explored.values():
        # We don't track file→module here, so just deduplicate by listing
        # files that are NOT already summarised under a module
        pass  # covered below by checking files_read

    orphan_files = [
        p for p in state.files_read
        if not any(
            mem.classes_seen  # skip files in modules we have summaries for
            for mem in state.modules_explored.values()
            # heuristic: if we have a module summary, individual files are redundant
        )
    ]
    if not state.modules_explored and orphan_files:
        paths = "\n".join(f"  - {p}" for p in orphan_files)
        parts.append(f"Files read this session:\n{paths}")
    elif orphan_files and len(orphan_files) <= 5:
        paths = "\n".join(f"  - {p}" for p in orphan_files)
        parts.append(f"Additional files read (outside explored modules):\n{paths}")

    if state.decisions:
        recent = state.decisions[-5:]
        items = "\n".join(f"  • {d}" for d in recent)
        parts.append(f"Decisions made this session:\n{items}")

    if not parts:
        return None

    return (
        "--- Working Memory ---\n"
        + "\n\n".join(parts)
        + "\n--- End Working Memory ---"
    )
