"""File endpoints — tree, content read, and AI-proposed edit apply."""
import base64
import logging
import os

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from scanner.project_scanner import _resolve_path

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/tree")
async def get_file_tree(root: str):
    """Return directory tree for the given root path."""
    SKIP = {"node_modules", "venv", ".venv", ".git", "__pycache__", "build", "dist", "target", ".idea", ".gradle", "out"}

    def walk(path: str, depth: int = 0) -> dict | None:
        if depth > 6:
            return None
        name = os.path.basename(path)
        if name.startswith('.') and name not in {'.env'}:
            return None
        if os.path.isdir(path):
            if name in SKIP:
                return None
            children = []
            try:
                for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
                    child = walk(entry.path, depth + 1)
                    if child:
                        children.append(child)
            except PermissionError:
                pass
            return {"name": name, "path": path, "type": "dir", "children": children}
        else:
            return {"name": name, "path": path, "type": "file", "children": []}

    if not root or not os.path.isdir(root):
        raise HTTPException(status_code=400, detail="Invalid root path")

    tree = walk(root)
    return tree or {"name": os.path.basename(root), "path": root, "type": "dir", "children": []}


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


_IMAGE_EXTS = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp', '.ico', '.bmp'}
_IMAGE_MIME = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.gif': 'image/gif', '.svg': 'image/svg+xml', '.webp': 'image/webp',
    '.ico': 'image/x-icon', '.bmp': 'image/bmp',
}


@router.get("/content")
async def get_file_content(path: str):
    """Return file content for the IDE viewer. Handles text, images, and binary."""
    resolved = _resolve_path(path)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")

    ext = os.path.splitext(resolved)[1].lower()
    size = os.path.getsize(resolved)

    if ext in _IMAGE_EXTS:
        with open(resolved, 'rb') as f:
            data = f.read()
        return {
            "type": "image",
            "content": base64.b64encode(data).decode(),
            "mime": _IMAGE_MIME.get(ext, 'application/octet-stream'),
            "size": size,
            "ext": ext.lstrip('.'),
        }

    try:
        with open(resolved, 'r', encoding='utf-8', errors='replace') as f:
            content = f.read(500_000)  # cap at 500 KB
        return {"type": "text", "content": content, "size": size, "ext": ext.lstrip('.')}
    except Exception:
        return {"type": "binary", "content": "", "size": size, "ext": ext.lstrip('.')}


class SaveContentRequest(BaseModel):
    content: str


@router.put("/content")
async def save_file_content(path: str, body: SaveContentRequest):
    """Overwrite a text file (IDE auto-save)."""
    resolved = _resolve_path(path)
    if not os.path.isfile(resolved):
        raise HTTPException(status_code=404, detail="File not found")
    try:
        with open(resolved, 'w', encoding='utf-8') as f:
            f.write(body.content)
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
