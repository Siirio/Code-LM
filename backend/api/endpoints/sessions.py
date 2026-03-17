"""API endpoints for chat sessions and agent personas.

Sessions provide persistent conversation history per project.
Agent personas allow custom system prompt extensions.
"""
import logging

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from storage.memory_service import (
    create_session,
    list_sessions,
    get_session,
    get_messages,
    delete_session,
    create_persona,
    list_personas,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Request / Response schemas ────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    project_id: str
    agent_id: str | None = None


class CreatePersonaRequest(BaseModel):
    project_id: str
    name: str
    description: str | None = None
    system_prompt_extra: str | None = None


# ── Session endpoints ─────────────────────────────────────────────────────────

@router.post("")
async def create_session_endpoint(request: CreateSessionRequest):
    """Create a new chat session for a project."""
    result = await create_session(
        project_id=request.project_id,
        agent_id=request.agent_id,
    )
    return result


@router.get("")
async def list_sessions_endpoint(project_id: str = Query(...)):
    """List all chat sessions for a project, ordered by most recent."""
    return await list_sessions(project_id)


@router.get("/{session_id}")
async def get_session_endpoint(session_id: str):
    """Get a single chat session by ID."""
    result = await get_session(session_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return result


@router.delete("/{session_id}")
async def delete_session_endpoint(session_id: str):
    """Delete a chat session and all its messages."""
    deleted = await delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return {"deleted": True, "session_id": session_id}


@router.get("/{session_id}/messages")
async def get_messages_endpoint(session_id: str):
    """Get all messages for a chat session."""
    session = await get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return await get_messages(session_id)


# ── Agent persona endpoints ──────────────────────────────────────────────────

@router.post("/agents")
async def create_persona_endpoint(request: CreatePersonaRequest):
    """Create a custom agent persona for a project."""
    result = await create_persona(
        project_id=request.project_id,
        name=request.name,
        description=request.description,
        system_prompt_extra=request.system_prompt_extra,
    )
    return result


@router.get("/agents")
async def list_personas_endpoint(project_id: str = Query(...)):
    """List all agent personas for a project."""
    return await list_personas(project_id)
