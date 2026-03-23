"""SQLAlchemy ORM models for CodeLM's PostgreSQL store.

Tables:
  projects          — one row per indexed project
  project_memory    — Layer 1: the live project summary (one row per project)
  memory_proposals  — pending memory updates awaiting developer approval
  arch_rules        — architectural rules and ADRs per project
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from storage.postgres import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> str:
    return str(uuid.uuid4())


# ── Projects ──────────────────────────────────────────────────────────────────

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    root_path: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[str] = mapped_column(String(128), default="main")
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)
    files_indexed: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    memory: Mapped["ProjectMemory | None"] = relationship(back_populates="project", uselist=False)
    proposals: Mapped[list["MemoryProposal"]] = relationship(back_populates="project")
    rules: Mapped[list["ArchRule"]] = relationship(back_populates="project")


# ── Project Memory (Layer 1) ──────────────────────────────────────────────────

class ProjectMemory(Base):
    """Layer 1 — the persistent project summary loaded in every chat."""
    __tablename__ = "project_memory"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), unique=True)
    # High-level summary paragraph
    summary: Mapped[str] = mapped_column(Text, default="")
    architecture_type: Mapped[str] = mapped_column(String(64), default="unknown")
    # Stored as newline-separated values for simplicity; serialised/deserialised by the service
    modules: Mapped[str] = mapped_column(Text, default="")          # "AuthModule\nBillingModule\n..."
    domain_entities: Mapped[str] = mapped_column(Text, default="")  # "User\nInvoice\nPayment\n..."
    # JSON-encoded list of pattern strings discovered by the post-scan analysis pass.
    discovered_patterns: Mapped[str] = mapped_column(Text, default="")
    # Trustworthiness of this memory entry.
    # "medium" = derived from static analysis (regex parsers, heuristics).
    # "high"   = manually confirmed by a developer.
    # "low"    = inferred from incomplete data (< 5 files, unknown stack, etc.)
    confidence_level: Mapped[str] = mapped_column(String(16), default="medium")
    # How this memory was produced.
    memory_source: Mapped[str] = mapped_column(String(32), default="static_analysis")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    project: Mapped["Project"] = relationship(back_populates="memory")

    # ── helpers ──────────────────────────────────────────────────────────────

    def modules_list(self) -> list[str]:
        return [m for m in self.modules.splitlines() if m.strip()]

    def entities_list(self) -> list[str]:
        return [e for e in self.domain_entities.splitlines() if e.strip()]

    def patterns_list(self) -> list[str]:
        import json as _json
        if not self.discovered_patterns:
            return []
        try:
            return _json.loads(self.discovered_patterns)
        except Exception:
            return []

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "summary": self.summary,
            "architecture_type": self.architecture_type,
            "modules": self.modules_list(),
            "domain_entities": self.entities_list(),
            "discovered_patterns": self.patterns_list(),
            "confidence_level": self.confidence_level,
            "memory_source": self.memory_source,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ── Memory Proposals (pending developer approval) ─────────────────────────────

class MemoryProposal(Base):
    """AI-proposed memory update awaiting human approval.
    The AI calls suggest_memory_update() → row inserted here.
    Developer approves/rejects via /memory/approve-update.
    """
    __tablename__ = "memory_proposals"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    category: Mapped[str] = mapped_column(String(64))   # module|domain_entity|architectural_decision|rule
    content: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending|approved|rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    project: Mapped["Project"] = relationship(back_populates="proposals")


# ── Architectural Rules / ADRs ────────────────────────────────────────────────

class ArchRule(Base):
    """A named architectural rule or Architecture Decision Record (ADR)."""
    __tablename__ = "arch_rules"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(256))          # "ADR-001" or "no-repo-in-controller"
    description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(16), default="error")  # error|warning|info
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    project: Mapped["Project"] = relationship(back_populates="rules")
    __table_args__ = (UniqueConstraint("project_id", "name"),)


# ── Chat Sessions & Messages ─────────────────────────────────────────────────

class ChatSession(Base):
    """A persistent chat session tied to a project, optionally using an agent persona."""
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    agent_id: Mapped[str | None] = mapped_column(ForeignKey("agent_personas.id"), nullable=True)
    title: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", order_by="ChatMessage.created_at"
    )


class ChatMessage(Base):
    """A single message within a chat session."""
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(ForeignKey("chat_sessions.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)  # "user" | "assistant"
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    session: Mapped["ChatSession"] = relationship(back_populates="messages")


# ── Agent Personas ────────────────────────────────────────────────────────────

class AgentPersona(Base):
    """A custom agent persona with an extra system prompt fragment, scoped to a project."""
    __tablename__ = "agent_personas"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str | None] = mapped_column(String(512), nullable=True)
    system_prompt_extra: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# ── Change Tracking ───────────────────────────────────────────────────────────

class ChatFileChange(Base):
    """A file written to disk as a result of an accepted AI proposal in a chat session."""
    __tablename__ = "chat_file_changes"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    # "create" | "update" | "delete"
    action: Mapped[str] = mapped_column(String(16), nullable=False, default="update")
    summary: Mapped[str] = mapped_column(Text, default="")
    completed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_chat_file_changes_session_id", "session_id"),)


class ChatTodo(Base):
    """A TODO item extracted from an AI response in a chat session."""
    __tablename__ = "chat_todos"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (Index("ix_chat_todos_session_id", "session_id"),)


# ── Parser Discrepancies ───────────────────────────────────────────────────────

class ParserDiscrepancy(Base):
    """Records differences between the regex and tree-sitter Java parsers per file.

    Populated when USE_TREE_SITTER_JAVA=True and a project_id is supplied to
    _parse_java_file().  Used for observability and to evaluate tree-sitter
    accuracy before promoting it to the sole parser.

    No ForeignKey to projects — project_id is a plain string so that rows can be
    written even when the project row is not yet committed.
    """
    __tablename__ = "parser_discrepancies"

    id: Mapped[str] = mapped_column(String(128), primary_key=True, default=_uuid)
    # Not a FK — keeps writes independent of project lifecycle
    project_id: Mapped[str] = mapped_column(String(128), nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    # JSON arrays, capped at 50 entries each
    regex_classes: Mapped[str] = mapped_column(Text, default="[]")
    ts_classes: Mapped[str] = mapped_column(Text, default="[]")
    regex_count: Mapped[int] = mapped_column(Integer, default=0)
    ts_count: Mapped[int] = mapped_column(Integer, default=0)
    # "high" | "medium" | "low"
    confidence: Mapped[str] = mapped_column(String(16), nullable=False)
    # "tree-sitter" | "regex"
    parser_used: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_parser_discrepancies_project_id", "project_id"),
        Index("ix_parser_discrepancies_file_path", "file_path"),
    )
