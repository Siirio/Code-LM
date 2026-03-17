from fastapi import APIRouter

from api.endpoints import chat, projects, memory, graph, sessions, files

router = APIRouter()

router.include_router(chat.router, prefix="/chat", tags=["chat"])
router.include_router(projects.router, prefix="/projects", tags=["projects"])
router.include_router(memory.router, prefix="/memory", tags=["memory"])
router.include_router(graph.router, prefix="/graph", tags=["graph"])
router.include_router(sessions.router, prefix="/sessions", tags=["sessions"])
router.include_router(files.router, prefix="/files", tags=["files"])

# Top-level /agents alias — matches POST /api/v1/agents used by IntelliJ client
_agents_alias = APIRouter()

from pydantic import BaseModel
from storage.memory_service import create_persona


class _CreatePersonaRequest(BaseModel):
    project_id: str
    name: str
    description: str | None = None
    system_prompt_extra: str | None = None


@_agents_alias.post("/agents", tags=["agents"])
async def create_agent_alias(request: _CreatePersonaRequest):
    return await create_persona(
        project_id=request.project_id,
        name=request.name,
        description=request.description,
        system_prompt_extra=request.system_prompt_extra,
    )


router.include_router(_agents_alias)
