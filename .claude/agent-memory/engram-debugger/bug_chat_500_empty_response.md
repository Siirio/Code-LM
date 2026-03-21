---
name: Chat 500 / Empty Response Bug
description: Root cause, fix, and lesson from the POST /api/v1/chat 500 error that showed "Backend returned an empty response" in the plugin
type: project
---

**Date fixed:** 2026-03-12

**Symptom:** POST /api/v1/chat returned HTTP 500. IntelliJ plugin showed "Backend returned an empty response."

**Root cause chain (two separate bugs):**

1. **Backend error suppression** — chat.py caught ALL exceptions as `except Exception as e` and re-raised as HTTPException(500). The actual cause was anthropic.BadRequestError (HTTP 400: "credit balance too low"). This got stringified and placed in the FastAPI `{"detail": "..."}` JSON envelope. The backend was sending the error message, but the status code was wrong.

2. **Plugin error silencing** — BackendClient.kt's `post()` method called `it.body!!.string()` unconditionally without checking `response.isSuccessful`. So when the backend returned 500 with `{"detail": "..."}`, the plugin tried to Gson-deserialize that as `ChatResponse`. Gson found no `reply` field and set it to null. EngramToolWindowFactory.kt then hit `response.reply?.takeIf { it.isNotBlank() } ?: "Backend returned an empty response."` — swallowing the real error.

3. **Secondary bug in anthropic_provider.py** — stop_reason was inferred from `tool_calls` presence instead of reading `response.stop_reason` from the API. This could misclassify max_tokens or stop_sequence responses as end_turn.

**Fixes applied:**
- backend/api/endpoints/chat.py: Added typed catches for anthropic.AuthenticationError (401), anthropic.BadRequestError (400, extracts e.body["error"]["message"]), anthropic.RateLimitError (429), anthropic.APIStatusError (502), generic Exception (500 with logger.exception for full traceback).
- backend/llm/anthropic_provider.py: stop_reason now reads `response.stop_reason or "end_turn"` directly from the API response.
- clients/intellij/src/.../BackendClient.kt: post() now checks `response.isSuccessful`; on failure it parses `detail` from the JSON body and throws BackendException(statusCode, detail).
- clients/intellij/src/.../EngramToolWindowFactory.kt: Added BackendException catch branch that shows "CodeLM Error (N): <message>" to the user.

**Why:** To prevent future errors from being silently swallowed and to make the plugin show actionable messages (e.g. "credit balance too low") instead of generic fallbacks.
