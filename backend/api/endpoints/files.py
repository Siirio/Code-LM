"""File write endpoint — applies AI-proposed edits after user approval in the IDE."""
import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from scanner.project_scanner import _resolve_path

logger = logging.getLogger(__name__)
router = APIRouter()


class ApplyEditRequest(BaseModel):
    file_path: str
    description: str = ""
    original_snippet: str = ""
    new_snippet: str


@router.post("/apply-edit")
async def apply_edit(request: ApplyEditRequest):
    """Write an AI-proposed edit to disk after the user accepted it in the IDE."""
    file_path = _resolve_path(request.file_path)
    try:
        if not os.path.exists(file_path) and not request.original_snippet:
            # New file — create parent dirs and write
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(request.new_snippet)
            logger.info("apply_edit: created new file %s", file_path)
            return {"status": "created", "file_path": file_path}

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            current = f.read()

        if request.original_snippet:
            if request.original_snippet not in current:
                raise HTTPException(
                    status_code=409,
                    detail=f"Original snippet not found in {file_path} — file may have changed since the scan.",
                )
            updated = current.replace(request.original_snippet, request.new_snippet, 1)
        else:
            # Append mode — no original to replace
            updated = current.rstrip() + "\n\n" + request.new_snippet + "\n"

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(updated)

        logger.info("apply_edit: wrote %s (%d chars)", file_path, len(updated))
        return {"status": "ok", "file_path": file_path}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("apply_edit: failed for %s", file_path)
        raise HTTPException(status_code=500, detail=str(exc))
