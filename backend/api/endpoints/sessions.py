"""API endpoints for chat sessions and agent personas."""
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


class CreateSessionRequest(BaseModel):
    project_id: str
    agent_id: str | None = None


class CreatePersonaRequest(BaseModel):
    project_id: str
    name: str
    description: str | None = None
    system_prompt_extra: str | None = None


def _db_error(exc: Exception) -> HTTPException:
    logger.error("DB error: %s", exc)
    return HTTPException(status_code=503, detail="Database unavailable — check that Docker is running")


# ── Session endpoints ─────────────────────────────────────────────────────────

@router.post("")
async def create_session_endpoint(request: CreateSessionRequest):
    try:
        return await create_session(project_id=request.project_id, agent_id=request.agent_id)
    except Exception as exc:
        raise _db_error(exc)


@router.get("")
async def list_sessions_endpoint(project_id: str = Query(...)):
    try:
        return await list_sessions(project_id)
    except Exception:
        return []


@router.get("/{session_id}")
async def get_session_endpoint(session_id: str):
    try:
        result = await get_session(session_id)
    except Exception as exc:
        raise _db_error(exc)
    if not result:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return result


@router.delete("/{session_id}")
async def delete_session_endpoint(session_id: str):
    try:
        deleted = await delete_session(session_id)
    except Exception as exc:
        raise _db_error(exc)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
    return {"deleted": True, "session_id": session_id}


@router.get("/{session_id}/messages")
async def get_messages_endpoint(session_id: str):
    try:
        session = await get_session(session_id)
        if not session:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")
        return await get_messages(session_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise _db_error(exc)


# ── Agent persona endpoints ───────────────────────────────────────────────────

@router.post("/agents")
async def create_persona_endpoint(request: CreatePersonaRequest):
    try:
        return await create_persona(
            project_id=request.project_id,
            name=request.name,
            description=request.description,
            system_prompt_extra=request.system_prompt_extra,
        )
    except Exception as exc:
        raise _db_error(exc)


@router.get("/agents")
async def list_personas_endpoint(project_id: str = Query(...)):
    try:
        return await list_personas(project_id)
    except Exception:
        return []
