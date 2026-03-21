# CodeLM — Backlog

Items are ordered by priority within each section.
Each item includes: root cause, proposed solution, and why it matters.

---

## High Priority

### read_file: range-based reading (from_line / to_line)

**Root cause:** `READ_FILE_MAX_CHARS = 12_000` truncates large files at ~400 lines.
The LLM never sees code beyond that boundary, but can still attempt to edit it —
leading to blind proposals where `original_snippet` doesn't match visible content.

**Current workaround:** Fix 2 (Critical issues session) blocks `propose_file_edit`
when `truncated=True` and warns the LLM. This prevents wrong edits but doesn't
give the LLM a way to actually read the code it needs.

**Proposed fix:** Add optional `from_line` / `to_line` parameters to the `read_file`
tool so the LLM can target exactly the lines it needs:

```python
# Tool schema addition
"from_line": {"type": "integer", "description": "First line to read (1-indexed). Default: 1."},
"to_line":   {"type": "integer", "description": "Last line to read (inclusive). Default: end of file."},
```

**Expected flow after fix:**
1. LLM calls `search_files("PaymentService.process_refund")` → gets file path + line hint
2. LLM calls `read_file(path, from_line=480, to_line=560)` → sees only the target method
3. LLM calls `propose_file_edit` with accurate `original_snippet`

**Why this closes the problem fully:**
The current `READ_FILE_MAX_CHARS` guard can stay as a safety cap per read call.
Range reading turns it from a hard limit into a sliding window — any file of any
size becomes readable in targeted chunks. No more truncation warnings, no more
blocked edits, no more "use search_files instead" fallback messages.

**Files to change:** `orchestrator.py` (`_execute_tool` → `read_file` branch),
tool schema in `TOOLS` list.

---

## Medium Priority

### Estimated cost before sending (UI)

Show `~$0.03–0.12` in the input bar before the user hits Send.
Gives users control over budget without surprising them after the fact.
One label next to the Send button, derived from message length × model pricing estimate.

### architect skill: cap read_file at 2 files

`architect` skill currently has no `read_file` (added in skills session).
When it's added, enforce a max of 2 `read_file` calls per turn — architect mode
should reason about structure, not read entire modules.
Implement as a counter in `_execute_tool` passed via request context.

### propose_file_edit: add line_start field

String matching for `original_snippet` fails when:
- File has two similar blocks
- LLM slightly misquotes whitespace
- File changed between `read_file` and `propose_file_edit`

Add optional `line_start: int` to the tool schema. When present, search for
`original_snippet` only within ±20 lines of that position.

---

## Deferred (acceptable debt for MVP)

### project_id tied to folder path

Moving or renaming the project folder orphans the entire graph and memory.
Fix: derive project identity from Git remote URL or a `.codelm-id` file in the root.
Requires DB migration — do after user base is established.

### Server-side budget enforcement

`X-Budget-Balance` is client-supplied. Users can spoof it.
Fix: store balance server-side keyed by a subscription token (see `chat.py` security note).
Do after real payment integration is in place.
