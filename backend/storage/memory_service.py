"""Service layer for reading and writing Project Memory to PostgreSQL.

All DB access goes through here — endpoints and the orchestrator never
touch SQLAlchemy directly.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from storage.postgres import get_pg_session
from storage.models import (
    Project, ProjectMemory, MemoryProposal, ArchRule,
    ChatSession, ChatMessage, AgentPersona,
)


# ── Projects ──────────────────────────────────────────────────────────────────

async def get_or_create_project(project_id: str, name: str = "", root_path: str = "") -> dict:
    async with get_pg_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if not project:
            project = Project(id=project_id, name=name or project_id, root_path=root_path)
            session.add(project)
        return {
            "id": project.id,
            "name": project.name,
            "root_path": project.root_path,
            "branch": project.branch,
            "indexed": project.indexed,
            "files_indexed": project.files_indexed,
            "last_scanned_at": project.last_scanned_at.isoformat() if project.last_scanned_at else None,
        }


async def mark_project_scanned(project_id: str, files_count: int) -> None:
    async with get_pg_session() as session:
        result = await session.execute(select(Project).where(Project.id == project_id))
        project = result.scalar_one_or_none()
        if project:
            project.indexed = True
            project.files_indexed = files_count
            project.last_scanned_at = datetime.now(timezone.utc)


# ── Project Memory (Layer 1) ──────────────────────────────────────────────────

async def load_memory(project_id: str) -> dict | None:
    """Load Layer 1 Project Memory. Returns None if project not yet indexed."""
    async with get_pg_session() as session:
        result = await session.execute(
            select(ProjectMemory).where(ProjectMemory.project_id == project_id)
        )
        mem = result.scalar_one_or_none()
        return mem.to_dict() if mem else None


async def save_memory(
    project_id: str,
    summary: str,
    architecture_type: str,
    modules: list[str],
    domain_entities: list[str],
) -> dict:
    """Create or fully replace the Layer 1 memory for a project."""
    async with get_pg_session() as session:
        result = await session.execute(
            select(ProjectMemory).where(ProjectMemory.project_id == project_id)
        )
        mem = result.scalar_one_or_none()
        if not mem:
            mem = ProjectMemory(project_id=project_id)
            session.add(mem)
        mem.summary = summary
        mem.architecture_type = architecture_type
        mem.modules = "\n".join(modules)
        mem.domain_entities = "\n".join(domain_entities)
        # Flush so SQLAlchemy resolves server-side defaults (updated_at) and
        # assigns the primary key before we serialise the row.  The surrounding
        # get_pg_session context manager will commit after the yield.
        await session.flush()
        return mem.to_dict()


# ── Memory Proposals ──────────────────────────────────────────────────────────

async def create_proposal(project_id: str, category: str, content: str, reason: str) -> dict:
    async with get_pg_session() as session:
        proposal = MemoryProposal(
            project_id=project_id,
            category=category,
            content=content,
            reason=reason,
        )
        session.add(proposal)
        await session.flush()  # get the generated id
        return {
            "id": proposal.id,
            "project_id": project_id,
            "category": category,
            "content": content,
            "reason": reason,
            "status": "pending",
            "created_at": proposal.created_at.isoformat(),
        }


async def list_proposals(project_id: str, status: str = "pending") -> list[dict]:
    async with get_pg_session() as session:
        result = await session.execute(
            select(MemoryProposal)
            .where(MemoryProposal.project_id == project_id, MemoryProposal.status == status)
            .order_by(MemoryProposal.created_at.desc())
        )
        return [
            {
                "id": p.id,
                "category": p.category,
                "content": p.content,
                "reason": p.reason,
                "status": p.status,
                "created_at": p.created_at.isoformat(),
            }
            for p in result.scalars().all()
        ]


async def resolve_proposal(proposal_id: str, approved: bool) -> dict:
    """Approve or reject a memory proposal. If approved, applies the change to Layer 1 memory."""
    async with get_pg_session() as session:
        result = await session.execute(
            select(MemoryProposal).where(MemoryProposal.id == proposal_id)
        )
        proposal = result.scalar_one_or_none()
        if not proposal:
            raise ValueError(f"Proposal {proposal_id} not found")

        proposal.status = "approved" if approved else "rejected"
        proposal.resolved_at = datetime.now(timezone.utc)

        if approved:
            await _apply_proposal(session, proposal)

        return {"id": proposal_id, "status": proposal.status}


async def _apply_proposal(session, proposal: MemoryProposal) -> None:
    """Merge an approved proposal into the live project memory."""
    result = await session.execute(
        select(ProjectMemory).where(ProjectMemory.project_id == proposal.project_id)
    )
    mem = result.scalar_one_or_none()
    if not mem:
        mem = ProjectMemory(project_id=proposal.project_id)
        session.add(mem)

    if proposal.category == "module":
        existing = mem.modules_list()
        if proposal.content not in existing:
            mem.modules = "\n".join(existing + [proposal.content])

    elif proposal.category == "domain_entity":
        existing = mem.entities_list()
        if proposal.content not in existing:
            mem.domain_entities = "\n".join(existing + [proposal.content])

    elif proposal.category in ("architectural_decision", "rule"):
        # Append to summary
        mem.summary = (mem.summary + f"\n\nDecision: {proposal.content}").strip()


# ── Architectural Rules ───────────────────────────────────────────────────────

async def list_rules(project_id: str) -> list[dict]:
    async with get_pg_session() as session:
        result = await session.execute(
            select(ArchRule)
            .where(ArchRule.project_id == project_id, ArchRule.active == True)
            .order_by(ArchRule.created_at)
        )
        return [
            {"id": r.id, "name": r.name, "description": r.description, "severity": r.severity}
            for r in result.scalars().all()
        ]


async def add_rule(project_id: str, name: str, description: str, severity: str = "error") -> dict:
    async with get_pg_session() as session:
        # Use an upsert so repeated scans finding the same rule do not crash
        # with a UniqueViolationError on the (project_id, name) constraint.
        stmt = (
            pg_insert(ArchRule)
            .values(project_id=project_id, name=name, description=description, severity=severity)
            .on_conflict_do_nothing(index_elements=["project_id", "name"])
        )
        await session.execute(stmt)
        await session.flush()

        # on_conflict_do_nothing returns no row, so always fetch the live row.
        result = await session.execute(
            select(ArchRule).where(ArchRule.project_id == project_id, ArchRule.name == name)
        )
        rule = result.scalar_one()
        return {"id": rule.id, "name": rule.name, "description": rule.description, "severity": rule.severity}


# ── Chat Sessions ─────────────────────────────────────────────────────────────

async def create_session(project_id: str, agent_id: str | None = None) -> dict:
    """Create a new chat session for the given project, auto-creating the project record if needed."""
    async with get_pg_session() as session:
        # Ensure project row exists (foreign key requirement)
        result = await session.execute(select(Project).where(Project.id == project_id))
        if not result.scalar_one_or_none():
            session.add(Project(id=project_id, name=project_id, root_path=""))

        chat_session = ChatSession(project_id=project_id, agent_id=agent_id)
        session.add(chat_session)
        await session.flush()
        return {
            "id": chat_session.id,
            "project_id": chat_session.project_id,
            "agent_id": chat_session.agent_id,
            "title": chat_session.title,
            "created_at": chat_session.created_at.isoformat(),
        }


async def list_sessions(project_id: str) -> list[dict]:
    """Return all sessions for a project, ordered by most recently updated first."""
    async with get_pg_session() as session:
        from sqlalchemy import func as sa_func
        result = await session.execute(
            select(
                ChatSession,
                sa_func.count(ChatMessage.id).label("message_count"),
            )
            .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
            .where(ChatSession.project_id == project_id)
            .group_by(ChatSession.id)
            .order_by(ChatSession.updated_at.desc())
        )
        rows = result.all()
        return [
            {
                "id": cs.id,
                "project_id": cs.project_id,
                "agent_id": cs.agent_id,
                "title": cs.title,
                "created_at": cs.created_at.isoformat(),
                "updated_at": cs.updated_at.isoformat() if cs.updated_at else None,
                "message_count": count,
            }
            for cs, count in rows
        ]


async def get_session(session_id: str) -> dict | None:
    """Return a single session by ID, or None if not found."""
    async with get_pg_session() as session:
        result = await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        cs = result.scalar_one_or_none()
        if not cs:
            return None
        return {
            "id": cs.id,
            "project_id": cs.project_id,
            "agent_id": cs.agent_id,
            "title": cs.title,
            "created_at": cs.created_at.isoformat(),
            "updated_at": cs.updated_at.isoformat() if cs.updated_at else None,
        }


async def add_message(session_id: str, role: str, content: str) -> dict:
    """Save a chat message. Auto-sets session title from the first user message."""
    async with get_pg_session() as session:
        msg = ChatMessage(session_id=session_id, role=role, content=content)
        session.add(msg)

        # Auto-title from the first user message
        if role == "user":
            result = await session.execute(
                select(ChatSession).where(ChatSession.id == session_id)
            )
            chat_session = result.scalar_one_or_none()
            if chat_session and not chat_session.title:
                chat_session.title = content[:60]

        await session.flush()
        return {
            "id": msg.id,
            "session_id": msg.session_id,
            "role": msg.role,
            "content": msg.content,
            "created_at": msg.created_at.isoformat(),
        }


async def delete_session(session_id: str) -> bool:
    """Delete a chat session and all its messages. Returns True if found and deleted."""
    async with get_pg_session() as session:
        result = await session.execute(
            select(ChatSession).where(ChatSession.id == session_id)
        )
        chat_session = result.scalar_one_or_none()
        if not chat_session:
            return False
        await session.execute(
            ChatMessage.__table__.delete().where(ChatMessage.session_id == session_id)
        )
        await session.delete(chat_session)
        return True


async def get_messages(session_id: str) -> list[dict]:
    """Return all messages for a session, ordered chronologically."""
    async with get_pg_session() as session:
        result = await session.execute(
            select(ChatMessage)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at)
        )
        return [
            {
                "role": m.role,
                "content": m.content,
                "created_at": m.created_at.isoformat(),
            }
            for m in result.scalars().all()
        ]


# ── Agent Personas ────────────────────────────────────────────────────────────

async def create_persona(
    project_id: str, name: str, description: str | None, system_prompt_extra: str | None
) -> dict:
    """Create a custom agent persona for a project."""
    async with get_pg_session() as session:
        persona = AgentPersona(
            project_id=project_id,
            name=name,
            description=description,
            system_prompt_extra=system_prompt_extra,
        )
        session.add(persona)
        await session.flush()
        return {
            "id": persona.id,
            "project_id": persona.project_id,
            "name": persona.name,
            "description": persona.description,
            "system_prompt_extra": persona.system_prompt_extra,
            "created_at": persona.created_at.isoformat(),
        }


async def list_personas(project_id: str) -> list[dict]:
    """Return all agent personas for a project."""
    async with get_pg_session() as session:
        result = await session.execute(
            select(AgentPersona)
            .where(AgentPersona.project_id == project_id)
            .order_by(AgentPersona.created_at)
        )
        return [
            {
                "id": p.id,
                "project_id": p.project_id,
                "name": p.name,
                "description": p.description,
                "system_prompt_extra": p.system_prompt_extra,
                "created_at": p.created_at.isoformat(),
            }
            for p in result.scalars().all()
        ]


async def get_persona(persona_id: str) -> dict | None:
    """Return a single agent persona by ID, or None if not found."""
    async with get_pg_session() as session:
        result = await session.execute(
            select(AgentPersona).where(AgentPersona.id == persona_id)
        )
        p = result.scalar_one_or_none()
        if not p:
            return None
        return {
            "id": p.id,
            "project_id": p.project_id,
            "name": p.name,
            "description": p.description,
            "system_prompt_extra": p.system_prompt_extra,
            "created_at": p.created_at.isoformat(),
        }
