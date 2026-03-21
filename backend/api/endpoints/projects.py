import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from orchestrator.orchestrator import clear_tool_cache
from scanner.project_scanner import scan_project
from storage.memory_service import get_or_create_project, list_sessions, list_personas

logger = logging.getLogger(__name__)

router = APIRouter()


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
    modules: list[str] = []
    message: str = ""


@router.post("/scan", response_model=ScanResponse)
async def scan_project_endpoint(request: ScanRequest):
    try:
        result = await scan_project(
            project_id=request.project_id,
            root_path=request.root_path,
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
            modules=result["modules"],
        )
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail=f"root_path does not exist: {request.root_path}")
    except Exception:
        logger.exception("Scan failed for project %s", request.project_id)
        raise HTTPException(status_code=500, detail="Scan failed — see server logs")


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
