import logging
import platform
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from orchestrator.orchestrator import clear_tool_cache
from scanner.project_scanner import scan_project
from storage.memory_service import get_or_create_project, list_sessions, list_personas, reset_project_knowledge

logger = logging.getLogger(__name__)

router = APIRouter()


def _normalize_path(path: str) -> str:
    """Translate Windows paths to Linux paths when the backend runs on Linux (Docker).

    C:\\Users\\foo\\project  →  /mnt/c/Users/foo/project
    C:/Users/foo/project    →  /mnt/c/Users/foo/project

    No-op on native Windows or when path is already Linux-style.
    """
    if platform.system() == "Windows":
        return path
    m = re.match(r'^([A-Za-z]):[/\\](.*)$', path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace('\\', '/')
        return f"/mnt/{drive}/{rest}"
    return path


class ScanRequest(BaseModel):
    project_id: str
    root_path: str
    branch: str = "main"
    scan_mode: str = "full"       # "full" | "folder" | "smart"
    folder_path: str | None = None  # required when scan_mode == "folder"
    entry_point: str | None = None  # required when scan_mode == "smart"


class ScanResponse(BaseModel):
    project_id: str
    status: str
    files_found: int = 0
    classes_found: int = 0
    functions_found: int = 0
    docs_indexed: int = 0
    modules: list[str] = []
    message: str = ""


@router.post("/scan", response_model=ScanResponse)
async def scan_project_endpoint(request: ScanRequest):
    root_path = _normalize_path(request.root_path)
    try:
        result = await scan_project(
            project_id=request.project_id,
            root_path=root_path,
            scan_mode=request.scan_mode,
            folder_path=request.folder_path,
            entry_point=request.entry_point,
        )
        # Graph and file content cached before this scan are now stale
        clear_tool_cache()
        return ScanResponse(
            project_id=request.project_id,
            status="completed",
            files_found=result["files_found"],
            classes_found=result["classes_found"],
            functions_found=result["functions_found"],
            docs_indexed=result.get("docs_indexed", 0),
            modules=result["modules"],
        )
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail=f"root_path does not exist: {root_path}")
    except Exception:
        logger.exception("Scan failed for project %s", request.project_id)
        raise HTTPException(status_code=500, detail="Scan failed — see server logs")


@router.delete("/{project_id}/knowledge")
async def delete_project_knowledge(project_id: str):
    """Wipe all stored knowledge for a project so a clean full scan can run.

    Deletes:
    - All Neo4j nodes/relationships for this project (skipped if Neo4j unavailable)
    - All Qdrant vectors for this project (skipped if Qdrant unavailable)
    - ProjectMemory and ArchRules rows in PostgreSQL
    - Resets project.indexed = False
    """
    try:
        from storage.neo4j_client import neo4j_client
        from storage.qdrant_client import qdrant_client, COLLECTION_FILES, COLLECTION_FUNCTIONS, COLLECTION_DOCS

        # 1. Neo4j — only when connected
        if neo4j_client.is_connected:
            try:
                await neo4j_client.execute(
                    "MATCH (n {project_id: $pid}) DETACH DELETE n",
                    {"pid": project_id},
                )
            except Exception:
                logger.warning("Neo4j delete failed for project %s — continuing", project_id, exc_info=True)

        # 2. Qdrant — only when connected
        if qdrant_client.is_connected:
            from qdrant_client.models import Filter, FieldCondition, MatchValue
            pf = Filter(must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))])
            for collection in (COLLECTION_FILES, COLLECTION_FUNCTIONS, COLLECTION_DOCS):
                try:
                    await qdrant_client.client.delete(collection_name=collection, points_selector=pf)
                except Exception:
                    pass  # collection may not exist yet

        # 3. PostgreSQL — reset memory + counters
        await reset_project_knowledge(project_id)

        # 4. Clear in-memory tool cache
        clear_tool_cache()

        return {"project_id": project_id, "status": "cleared"}
    except Exception:
        logger.exception("Failed to clear knowledge for project %s", project_id)
        raise HTTPException(status_code=500, detail="Failed to clear project knowledge — see server logs")


@router.get("/{project_id}/sessions")
async def project_sessions(project_id: str):
    try:
        return await list_sessions(project_id)
    except Exception:
        return []


@router.get("/{project_id}/agents")
async def project_agents(project_id: str):
    try:
        return await list_personas(project_id)
    except Exception:
        return []


@router.get("/{project_id}/status")
async def project_status(project_id: str):
    try:
        project = await get_or_create_project(project_id)
    except Exception as exc:
        # Return a valid 200 response instead of 503 so IDE clients can still
        # proceed.  A 503 would be treated as an exception by the plugin and
        # surface a confusing "Could not check index status" error to the user.
        logger.warning("DB unavailable while fetching status for project %s: %s", project_id, exc)
        return {
            "project_id": project_id,
            "status": "not_indexed",
            "indexed": False,
            "files_indexed": 0,
            "last_scanned_at": None,
        }
    return {
        "project_id": project["id"],
        "status": "indexed" if project["indexed"] else "not_indexed",
        "name": project["name"],
        "indexed": project["indexed"],
        "files_indexed": project["files_indexed"],
        "last_scanned_at": project["last_scanned_at"],
    }
