---
name: Backend Architecture
description: Key file locations, data flow, component roles, and fragile areas in the EngramAI Python FastAPI backend
type: project
---

EngramAI backend is a FastAPI app at /mnt/c/EngramAI/backend/.

**Entry point:** main.py — mounts /api/v1 router, CORS middleware, /health endpoint.

**Request flow for POST /api/v1/chat:**
api/endpoints/chat.py -> orchestrator/orchestrator.py -> llm/factory.py -> llm/<provider>_provider.py -> SDK

**Key files:**
- backend/config.py — Settings via pydantic-settings. Reads .env via an absolute path anchored to `Path(__file__).parent.parent / ".env"` (fixed 2026-03-12). Properties: active_api_key, active_model (resolved from LLM_PROVIDER).
- backend/api/endpoints/chat.py — FastAPI route. Calls orchestrator_chat(), returns ChatResponse. Catches anthropic.AuthenticationError, openai.AuthenticationError, and google quota errors individually.
- backend/orchestrator/orchestrator.py — Agentic loop: calls LLM, handles tool_use stop_reason, re-runs until end_turn.
- backend/llm/base.py — Abstract LLMProvider, LLMResponse, ToolCall dataclasses.
- backend/llm/factory.py — get_provider() dispatch by name string. Supports "anthropic", "gemini", "openai".
- backend/llm/anthropic_provider.py — Wraps anthropic.Anthropic SDK. Maps SDK response to LLMResponse.
- backend/llm/openai_provider.py — Wraps openai.OpenAI SDK. Guards against empty api_key with ValueError. Logs api_key[:8] at DEBUG on init.
- backend/llm/gemini_provider.py — Wraps Google Gemini SDK.

**Installed SDK versions (as of 2026-03-12):**
- anthropic==0.84.0 (in venv)
- openai==1.30.1 (in venv)
- fastapi==0.111.0

**LLM provider config (.env at /mnt/c/EngramAI/.env):**
- LLM_PROVIDER= sets which provider is active
- OPENAI_API_KEY, ANTHROPIC_API_KEY, GEMINI_API_KEY all present

**Fragile areas:**
- The orchestrator agentic loop has no iteration limit — a misbehaving LLM could loop forever on tool_use.
- All tool implementations are TODO stubs (get_project_memory, query_code_graph, search_files, etc.) returning not_indexed.
- openai SDK v1.x: passing api_key="" does NOT raise at construction time — it sends "Authorization: Bearer " and gets a 401 from OpenAI. The guard in OpenAIProvider.__init__ now catches this before the request is made.

**Bug fixed 2026-03-12 — OpenAI 401 "key missing":**
Root cause: config.py used `env_file="../.env"` (relative path). When the backend was started from any directory other than backend/ (e.g. repo root, VS Code task, Docker), the .env was not found, openai_api_key defaulted to "", and openai SDK sent "Authorization: Bearer " which OpenAI rejected with 401. Fix: changed to `Path(__file__).resolve().parent.parent / ".env"` — absolute, works from any cwd.

**Why:** To speed up future debugging by knowing exactly where to look first.
**How to apply:** When a chat error is reported, start at chat.py -> orchestrator.py -> <provider>_provider.py in that order. For any 401 from any provider, first confirm config.py is resolving the .env path correctly and the key field is non-empty.
