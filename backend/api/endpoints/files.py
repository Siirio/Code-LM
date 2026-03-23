"""File endpoints — tree, content read, and AI-proposed edit apply."""
import base64
import logging
import os
import shutil as _shutil

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from scanner.project_scanner import _resolve_path

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/tree")
async def get_file_tree(root: str):
    """Return directory tree for the given root path."""
    SKIP = {
        "node_modules", "venv", ".venv", ".git", "__pycache__",
        "build", "dist", "target", ".idea", ".gradle", "out",
        ".next", ".nuxt", "coverage", ".nyc_output", "tmp", ".tmp",
    }

    def walk(path: str, depth: int = 0) -> dict | None:
        if depth > 10:
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

    resolved_root = _resolve_path(root)
    if not resolved_root or not os.path.isdir(resolved_root):
        raise HTTPException(status_code=400, detail=f"Path not found: {root!r}")

    tree = walk(resolved_root)
    return tree or {"name": os.path.basename(root), "path": root, "type": "dir", "children": []}


class ApplyEditRequest(BaseModel):
    file_path: str
    description: str = ""
    original_snippet: str = ""
    new_snippet: str
    session_id: str | None = None


@router.post("/apply-edit")
async def apply_edit(request: ApplyEditRequest):
    """Write an AI-proposed edit to disk after the user accepted it in the IDE."""
    raw_path = request.file_path
    # file_path is declared here so the except block can always reference it even
    # if _resolve_path raises before the assignment completes.
    file_path = raw_path
    try:
        # ── Path diagnostics ─────────────────────────────────────────────────
        logger.info("apply_edit: received raw_path=%r", raw_path)

        if '\\' in raw_path:
            logger.warning(
                "apply_edit: raw_path contains backslashes — likely a Windows path "
                "sent to a WSL backend. _resolve_path will attempt conversion: %r",
                raw_path,
            )

        file_path = _resolve_path(raw_path)
        logger.info("apply_edit: resolved file_path=%r", file_path)

        parent_dir = os.path.dirname(file_path) or "."
        parent_exists = os.path.isdir(parent_dir)
        parent_writable = os.access(parent_dir, os.W_OK) if parent_exists else False
        file_exists = os.path.isfile(file_path)
        logger.info(
            "apply_edit: parent_dir=%r exists=%s writable=%s file_exists=%s",
            parent_dir, parent_exists, parent_writable, file_exists,
        )

        if parent_exists and not parent_writable:
            raise PermissionError(f"No write permission on directory: {parent_dir!r}")

        # ── New file ──────────────────────────────────────────────────────────
        if not file_exists and not request.original_snippet:
            os.makedirs(parent_dir, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(request.new_snippet)
            logger.info("apply_edit: created new file %s", file_path)
            if request.session_id:
                from storage.memory_service import add_file_change
                await add_file_change(
                    session_id=request.session_id,
                    file_path=file_path,
                    action="create",
                    summary=request.description,
                )
            return {"status": "created", "file_path": file_path}

        # ── Edit existing file ────────────────────────────────────────────────
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
            updated = current.rstrip() + "\n\n" + request.new_snippet + "\n"

        with open(file_path, "w", encoding="utf-8") as f:
            f.write(updated)

        logger.info("apply_edit: wrote %s (%d chars)", file_path, len(updated))
        if request.session_id:
            from storage.memory_service import add_file_change
            await add_file_change(
                session_id=request.session_id,
                file_path=file_path,
                action="update",
                summary=request.description,
            )
        return {"status": "ok", "file_path": file_path}

    except HTTPException:
        raise
    except Exception as exc:
        logger.exception(
            "apply_edit: FAILED — raw_path=%r resolved=%r error_type=%s error=%s",
            raw_path, file_path, type(exc).__name__, exc,
        )
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


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


@router.delete("")
async def delete_path(path: str):
    """Delete a file or directory from disk."""
    resolved = _resolve_path(path)
    if not os.path.exists(resolved):
        raise HTTPException(status_code=404, detail="Path not found")
    try:
        if os.path.isfile(resolved):
            os.remove(resolved)
        elif os.path.isdir(resolved):
            _shutil.rmtree(resolved)
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


class CreatePathRequest(BaseModel):
    path: str
    is_dir: bool = False
    content: str = ""


@router.post("/create")
async def create_path(request: CreatePathRequest):
    """Create a new file or directory."""
    resolved = _resolve_path(request.path)
    try:
        if request.is_dir:
            os.makedirs(resolved, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(resolved), exist_ok=True)
            with open(resolved, 'w', encoding='utf-8') as f:
                f.write(request.content)
        return {"status": "ok"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
