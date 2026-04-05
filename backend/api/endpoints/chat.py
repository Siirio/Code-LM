import logging

import anthropic
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from config import settings
from orchestrator.orchestrator import chat as orchestrator_chat
from orchestrator.orchestrator import chat_stream as orchestrator_chat_stream

logger = logging.getLogger(__name__)

router = APIRouter()


class ChatRequest(BaseModel):
    project_id: str
    message: str
    conversation_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None


class ChatResponse(BaseModel):
    reply: str
    conversation_id: str
    memory_update_proposed: bool = False
    memory_update_proposal: dict | None = None


def _resolve_provider_and_key(user_key: str, x_provider: str) -> tuple[str, str]:
    """Determine the LLM provider and API key to use for this request.

    Priority:
    1. Explicit X-Provider header (if provided by the client)
    2. Auto-detect from key prefix (sk-ant- → anthropic, sk- → deepseek)
    3. Server default from settings.llm_provider

    Key falls back to the server .env key for the resolved provider.
    """
    key = user_key.strip()
    explicit_provider = x_provider.strip().lower()

    if explicit_provider in ("anthropic", "deepseek"):
        provider = explicit_provider
    elif key.startswith("sk-ant-"):
        provider = "anthropic"
    elif key:
        provider = "deepseek"
    else:
        provider = settings.llm_provider

    if not key:
        key = settings.deepseek_api_key if provider == "deepseek" else settings.anthropic_api_key

    return provider, key


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    x_api_key: str = Header(default="", alias="X-Api-Key"),
    x_provider: str = Header(default="", alias="X-Provider"),
):
    provider, api_key = _resolve_provider_and_key(x_api_key, x_provider)
    try:
        result = await orchestrator_chat(
            project_id=request.project_id,
            message=request.message,
            conversation_id=request.conversation_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            api_key=api_key,
            provider_name=provider,
            model=settings.llm_model or None,
        )
        return ChatResponse(**result)

    except anthropic.AuthenticationError as e:
        logger.error("Anthropic authentication error: %s", e)
        raise HTTPException(status_code=401, detail="Anthropic API key is invalid or missing.")

    except anthropic.BadRequestError as e:
        logger.error("Anthropic bad request: %s", e)
        try:
            user_message = e.body["error"]["message"]  # type: ignore[index]
        except Exception:
            user_message = str(e)
        raise HTTPException(status_code=400, detail=user_message)

    except anthropic.RateLimitError as e:
        logger.warning("Anthropic rate limit hit: %s", e)
        raise HTTPException(
            status_code=429,
            detail="Anthropic API rate limit reached. Please wait a moment and try again.",
        )

    except anthropic.APIStatusError as e:
        logger.error("Anthropic API error (status %s): %s", e.status_code, e)
        raise HTTPException(
            status_code=502,
            detail=f"Anthropic API returned an error ({e.status_code}). Please try again.",
        )

    except Exception as e:
        logger.exception("Unexpected error in chat endpoint")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {type(e).__name__}: {e}",
        )


@router.post("/stream")
async def chat_stream(
    request: ChatRequest,
    x_api_key: str = Header(default="", alias="X-Api-Key"),
    x_provider: str = Header(default="", alias="X-Provider"),
    x_budget_balance: float = Header(default=999.0, alias="X-Budget-Balance"),
):
    """Stream chat responses as Server-Sent Events.

    Returns the same logical response as POST /chat but streamed as SSE:
      - data: {"chunk": "..."}                        text fragments
      - data: {"tool": "...", "status": "running"}    tool call status
      - data: {"cost_usd": 0.0023, "balance_usd": 14.9977}  per-turn cost
      - data: {"done": true}                          end of response

    X-Provider: "anthropic" | "deepseek" (optional — auto-detected from key if omitted)
    X-Budget-Balance: caller's current USD balance (credits mode only).
    When the user supplies their own API key the balance is ignored (999).

    SECURITY NOTE (MVP): balance is client-supplied and not verified server-side.
    A user can send X-Budget-Balance: 999 to bypass the limit.
    This is acceptable while payments are mocked (localStorage).
    Before enabling real payments: store balance in the database keyed by a
    server-issued subscription token, verify it here, and reject requests that
    exceed the stored balance without relying on the client-sent value.
    """
    provider, api_key = _resolve_provider_and_key(x_api_key, x_provider)
    user_key = x_api_key.strip()
    # Users with their own key have unlimited budget (billed directly to them)
    budget = 999.0 if user_key else max(x_budget_balance, -999.0)

    return StreamingResponse(
        orchestrator_chat_stream(
            project_id=request.project_id,
            message=request.message,
            conversation_id=request.conversation_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
            api_key=api_key,
            provider_name=provider,
            model=settings.llm_model or None,
            budget_usd=budget,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
