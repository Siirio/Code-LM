from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from storage.memory_service import (
    load_memory, save_memory,
    create_proposal, list_proposals, resolve_proposal,
    list_rules, add_rule,
)

router = APIRouter()


# ── Memory read/write ─────────────────────────────────────────────────────────

@router.get("/{project_id}")
async def get_memory(project_id: str):
    mem = await load_memory(project_id)
    if not mem:
        return {
            "project_id": project_id,
            "summary": "Project memory not yet initialized. Run a project scan first.",
            "architecture_type": "unknown",
            "modules": [],
            "domain_entities": [],
            "rules": [],
        }
    rules = await list_rules(project_id)
    return {**mem, "rules": rules}


class MemoryWriteRequest(BaseModel):
    summary: str
    architecture_type: str = "unknown"
    modules: list[str] = []
    domain_entities: list[str] = []


@router.put("/{project_id}")
async def write_memory(project_id: str, body: MemoryWriteRequest):
    """Directly write/overwrite Layer 1 memory (used by scanner after indexing)."""
    return await save_memory(
        project_id=project_id,
        summary=body.summary,
        architecture_type=body.architecture_type,
        modules=body.modules,
        domain_entities=body.domain_entities,
    )


# ── Memory proposals ──────────────────────────────────────────────────────────

@router.get("/{project_id}/proposals")
async def get_proposals(project_id: str, status: str = "pending"):
    """See all pending memory update proposals for a project."""
    return await list_proposals(project_id, status)


class ApproveRequest(BaseModel):
    proposal_id: str
    approved: bool


@router.post("/approve-update")
async def approve_memory_update(body: ApproveRequest):
    """Approve or reject a memory update proposal. Approved proposals are immediately applied."""
    try:
        return await resolve_proposal(body.proposal_id, body.approved)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ── Architectural rules ───────────────────────────────────────────────────────

@router.get("/{project_id}/rules")
async def get_rules(project_id: str):
    return await list_rules(project_id)


class RuleRequest(BaseModel):
    name: str
    description: str
    severity: str = "error"


@router.post("/{project_id}/rules")
async def create_rule(project_id: str, body: RuleRequest):
    return await add_rule(project_id, body.name, body.description, body.severity)
