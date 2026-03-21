---
name: load_memory Returns None After Successful Scan
description: Root cause and fix for load_memory returning None / get_project_memory status:not_indexed despite a successful scan
type: project
---

Scan completed and returned HTTP 200, but `get_project_memory` always returned `status: not_indexed` because `load_memory` returned `None`.

**Root cause (Bug 1 — fatal):** `storage/models.py` was never imported before `init_postgres()` called `Base.metadata.create_all`. SQLAlchemy's `DeclarativeBase` registry was empty, so `create_all` created zero tables. All subsequent DB writes either raised a "table does not exist" error (which `get_pg_session` rolled back silently) or the query returned no rows.

**Fix:** Added `import storage.models  # noqa: F401` as the first statement inside `init_postgres()` in `/mnt/c/CodeLM/backend/storage/postgres.py`. This is a side-effect import — the models register themselves with `Base` when the module loads. The import is placed inside the function body (not at module top-level) to avoid a circular import, since `models.py` already imports `Base` from `postgres.py`.

**Root cause (Bug 2 — correctness):** `save_memory` in `memory_service.py` called `mem.to_dict()` before `session.flush()`. On a new insert, `updated_at` is `None` until flush triggers the `default=_now` callable. This didn't cause a crash (the model guards with `if self.updated_at else None`), but the returned dict had `updated_at: null` and was built before the write was confirmed.

**Fix:** Added `await session.flush()` before `return mem.to_dict()` in `save_memory`.

**Why:** `onupdate=_now` fires on UPDATE statements only. `default=_now` fires at flush time (when SQLAlchemy generates the INSERT). Without flush, a newly created object has `None` for any column whose default is a Python callable rather than a server-side default.

**How to apply:** When debugging "data saved but not retrieved" bugs, always verify the import chain that feeds `Base.metadata` before `create_all` runs. This is the single most common cause of silent table-creation failures with SQLAlchemy's `DeclarativeBase`.

---

**Root cause (Bug 3 — 2026-03-15):** Tables exist and `project_memory` rows are present, but `_execute_tool("get_project_memory", ...)` still returns `status: not_indexed`. The bug is in `orchestrator.py`: `_execute_tool` was reading `project_id = tool_input.get("project_id", "unknown")` — i.e., whatever the LLM put in its tool call JSON. The LLM has no knowledge of the UUID-format `project_id` (derived from `UUID.nameUUIDFromBytes(basePath.toByteArray())` in Kotlin), so it hallucinated a value or used `"unknown"`, causing `load_memory(wrong_id)` to return `None`.

**Fix:** `_execute_tool` now accepts `project_id` as an explicit third parameter. `chat()` passes its `project_id` argument directly, bypassing whatever the LLM writes in `tool_input`. Additionally, `chat()` injects `project_id` into the system prompt so the LLM knows the correct value for `suggest_memory_update` and other tools that legitimately need it.

**Key architectural fact:** `ProjectSession.projectId` in the Kotlin plugin is computed via `UUID.nameUUIDFromBytes(basePath.toByteArray())` which produces a deterministic version-3 UUID. The backend never tells the LLM what this value is, so the LLM cannot reliably reproduce it.
