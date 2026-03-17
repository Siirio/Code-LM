import logging

import anthropic
import google.api_core.exceptions
import openai
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

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


@router.post("", response_model=ChatResponse)
async def chat(request: ChatRequest):
    try:
        result = await orchestrator_chat(
            project_id=request.project_id,
            message=request.message,
            conversation_id=request.conversation_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
        )
        return ChatResponse(**result)

    except anthropic.AuthenticationError as e:
        # Invalid or missing API key — 401 so the plugin can surface a clear message
        logger.error("Anthropic authentication error: %s", e)
        raise HTTPException(
            status_code=401,
            detail="Anthropic API key is invalid or missing. Check ANTHROPIC_API_KEY in your .env file.",
        )

    except anthropic.BadRequestError as e:
        # 400 from Anthropic — most common cause: insufficient credits or bad request payload.
        # Parse out the human-readable message from the structured error body when possible.
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
        # Catch-all for other Anthropic HTTP errors (e.g. 529 overloaded)
        logger.error("Anthropic API error (status %s): %s", e.status_code, e)
        raise HTTPException(
            status_code=502,
            detail=f"Anthropic API returned an error ({e.status_code}). Please try again.",
        )

    except openai.AuthenticationError:
        logger.error("OpenAI authentication error")
        raise HTTPException(
            status_code=401,
            detail="OpenAI API key is invalid or missing. Check OPENAI_API_KEY in your .env file.",
        )

    except openai.RateLimitError:
        logger.warning("OpenAI rate limit hit")
        raise HTTPException(
            status_code=429,
            detail="OpenAI API rate limit reached. Please wait a moment and try again.",
        )

    except openai.APIStatusError as e:
        logger.error("OpenAI API error (status %s): %s", e.status_code, e)
        raise HTTPException(
            status_code=502,
            detail=f"OpenAI API returned an error ({e.status_code}). Please try again.",
        )

    except google.api_core.exceptions.ResourceExhausted as e:
        # Gemini free-tier quota exhausted (HTTP 429 from Google).
        logger.warning("Gemini ResourceExhausted (quota exceeded): %s", e)
        raise HTTPException(
            status_code=429,
            detail=(
                "Gemini API quota exceeded. Try switching LLM_PROVIDER=anthropic in .env "
                "or upgrade your Google AI plan."
            ),
        )

    except Exception as e:
        # Unexpected errors: log the full traceback server-side, return a safe message
        logger.exception("Unexpected error in chat endpoint")
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error: {type(e).__name__}: {e}",
        )


@router.post("/stream")
async def chat_stream(request: ChatRequest):
    """Stream chat responses as Server-Sent Events.

    Returns the same logical response as POST /chat but streamed as SSE:
      - data: {"chunk": "..."}                    text fragments
      - data: {"tool": "...", "status": "running"} tool call status
      - data: {"done": true}                      end of response
    """
    return StreamingResponse(
        orchestrator_chat_stream(
            project_id=request.project_id,
            message=request.message,
            conversation_id=request.conversation_id,
            session_id=request.session_id,
            agent_id=request.agent_id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
