---
name: Orchestrator project_id flow bug (fixed)
description: Root cause and fix for get_project_memory returning not_indexed despite successful scan
type: project
---

The orchestrator's `_execute_tool` was reading `project_id` from `tool_input` (whatever the LLM put in its tool call JSON) rather than from the authoritative `project_id` parameter that the `chat()` function receives from the HTTP request. The LLM had no reliable basis for the UUID-format project_id, so it either hallucinated one or used a garbage value, causing `load_memory(wrong_id)` to return `None`.

**Fix applied (2026-03-15):** `_execute_tool` now accepts `project_id` as an explicit third parameter, and `chat()` injects the correct `project_id` into the system prompt so the LLM knows the exact value to use in tool calls. The call-site was updated to pass `project_id` from the outer `chat()` scope.

**File:** `/mnt/c/EngramAI/backend/orchestrator/orchestrator.py`

**Why:** `ProjectSession.projectId` is computed in Kotlin via `UUID.nameUUIDFromBytes(basePath.toByteArray())` — this is a version-3 UUID derived from the project path. The LLM has no way to know this value without it being told.

**How to apply:** Any future tool added to `_execute_tool` must use the `project_id` parameter, not `tool_input.get("project_id")`.
