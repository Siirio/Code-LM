"""Project scanner: walks a codebase, parses source files (Python via ast,
TypeScript/JavaScript via regex), writes class/function nodes to Neo4j,
and saves a project memory summary to PostgreSQL.
"""
import asyncio
import ast
import json
import logging
import os
import re
from pathlib import Path

from config import settings
from storage.neo4j_client import neo4j_client
from storage import memory_service
from storage.qdrant_client import qdrant_client, COLLECTION_FILES, COLLECTION_DOCS
from scanner.import_resolver import resolve_and_write_edges
from scanner.validator import validate_parsed_file
from scanner.role_inference import (
    RoleResult,
    infer_role_heuristic,
    refine_role_with_graph,
    build_imported_by_index,
    extract_annotations_and_superclass,
)
import embedding as _emb

DOC_EXTENSIONS: set[str] = {".md", ".pdf"}
EMBEDDING_DIM = _emb.EMBEDDING_DIM


def _generate_embedding(class_names: list[str], file_path: str, layer: str, methods: list[str]) -> list[float]:
    """Generate a 384-dim embedding via the ONNX runtime engine."""
    text = f"{' '.join(class_names)} {file_path} {layer} {' '.join(methods)}"
    return _emb.embed(text)


async def _ensure_embedding_model() -> None:
    """Pre-warm the ONNX embedding model (non-blocking thread pool load)."""
    await _emb.ensure_model()

logger = logging.getLogger(__name__)

SOURCE_EXTENSIONS: set[str] = {".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java", ".go"}

# Skipped only at the ROOT level of the scanned project (depth == 1 from root_path).
# These are common output/tooling dirs that should not be indexed, but the same
# name deeper in the tree (e.g. a "dist" module inside a monorepo) is fine.
ROOT_SKIP_DIRS: set[str] = {
    "node_modules", "venv", ".venv", "env", ".env", "virtualenv",
    ".git", "__pycache__", "dist", "target", "build", "out",
    ".gradle", ".mvn", ".idea", ".vscode", ".claude", "worktrees",
    "migrations",
}

# Always skipped at every depth — these are universally noise and never contain
# user source code regardless of where they appear in the tree.
ALWAYS_SKIP_DIRS: set[str] = {"node_modules", "__pycache__", ".git", "venv", ".venv", "worktrees"}

_LAYER_RULES: list[tuple[tuple[str, ...], str]] = [
    (("Controller", "Router", "Handler", "Resolver"), "Controller"),
    (("Service",),                                     "Service"),
    (("Repository", "Repo", "Store"),                  "Repository"),
    (("Model", "Entity", "Schema"),                    "Entity"),
    (("DTO", "Dto", "Request", "Response"),            "DTO"),
]


def _classify_layer(name: str) -> str:
    for suffixes, layer in _LAYER_RULES:
        for suffix in suffixes:
            if name.endswith(suffix):
                return layer
    return "Util"


def _resolve_path(root_path: str) -> str:
    """Convert Windows drive paths to WSL mount paths — only when running on Linux.
    e.g. C:/Users/foo  →  /mnt/c/Users/foo   (Linux/WSL only)
         D:\\Projects   →  /mnt/d/Projects    (Linux/WSL only)
    On Windows/macOS paths are returned unchanged.
    """
    import sys, re
    if sys.platform != "linux":
        return root_path  # on Windows the path is already valid
    match = re.match(r'^([A-Za-z])[:/\\]+(.*)', root_path)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).replace('\\', '/')
        return f"/mnt/{drive}/{rest}"
    return root_path


def _walk_source_files(root_path: str) -> list[str]:
    root_path = _resolve_path(root_path)
    if not os.path.isdir(root_path):
        raise FileNotFoundError(f"root_path does not exist: {root_path}")

    logger.info("Scan: walking root_path=%s", root_path)

    # Prevent the scanner from indexing its own backend directory.
    # __file__ is  .../backend/scanner/project_scanner.py
    # backend_dir  is  .../backend
    backend_dir = os.path.abspath(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    # Use commonpath to check if backend_dir is actually inside root_path —
    # avoids the startswith string-prefix bug where "/mnt/c/proj-backend"
    # would incorrectly match root_path="/mnt/c/proj".
    try:
        backend_dir_to_skip: str | None = (
            backend_dir
            if os.path.commonpath([backend_dir, root_path]) == root_path
            else None
        )
    except ValueError:
        # commonpath raises ValueError on Windows when paths are on different drives
        backend_dir_to_skip = None

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        is_root = os.path.abspath(dirpath) == os.path.abspath(root_path)

        pruned: list[str] = []
        for d in dirnames:
            abs_d = os.path.abspath(os.path.join(dirpath, d))

            # Always skip universally-noisy dirs at any depth.
            if d in ALWAYS_SKIP_DIRS:
                logger.info("Scan: skipping dir=%s reason=always_skip", abs_d)
                continue

            # At root level only, also skip build-output and tooling dirs.
            if is_root and d in ROOT_SKIP_DIRS:
                logger.info("Scan: skipping dir=%s reason=root_skip_dirs", abs_d)
                continue

            # Skip the CodeLM backend dir when it lives inside the scanned project.
            if backend_dir_to_skip and abs_d == backend_dir_to_skip:
                logger.info("Scan: skipping dir=%s reason=codelm_backend", abs_d)
                continue

            pruned.append(d)

        dirnames[:] = pruned

        for fname in filenames:
            if Path(fname).suffix in SOURCE_EXTENSIONS:
                found.append(os.path.join(dirpath, fname))

    if not found:
        logger.warning("Scan: 0 source files found under '%s' — verify the path is correct", root_path)
    elif len(found) < 5:
        logger.warning("Scan: only %d source files found under '%s' — "
                       "this seems low; verify root_path is the project root", len(found), root_path)
    return found


def _parse_python_file(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()
    tree = ast.parse(source, filename=file_path)
    classes, functions, imports = [], [], []
    imports_detailed: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            classes.append(node.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
                imports_detailed.append({
                    "raw": f"import {alias.name}",
                    "module": alias.name,
                    "names": [],
                })
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
                names = [alias.name for alias in node.names]
                names_str = ", ".join(names)
                imports_detailed.append({
                    "raw": f"from {node.module} import {names_str}",
                    "module": node.module,
                    "names": names,
                })
    return {"classes": classes, "functions": functions, "imports": imports,
            "imports_detailed": imports_detailed}


_TS_JS_EXTENSIONS: set[str] = {".ts", ".tsx", ".js", ".jsx"}
_JAVA_EXTENSIONS: set[str] = {".java"}

# Printed once per process when tree-sitter Java is enabled so the developer
# knows a full scan is needed for a consistent graph.
_TS_CONSISTENCY_WARNED: bool = False

_JAVA_KEYWORDS = {
    "if", "for", "while", "switch", "catch", "return", "new",
    "throw", "assert", "else", "do", "try", "finally", "synchronized",
    "instanceof", "import", "package", "class", "interface", "enum", "record",
}


# ── Tree-sitter validation ────────────────────────────────────────────────────

def _validate_java_parse_result(result: dict, source_content: str) -> bool:
    """Return True if *result* contains a usable class list.

    Rules (per spec):
      1. classes must be a list
      2. every entry must be a non-empty string
      3. every name must be shorter than 200 characters
      4. every name must appear literally in the source content
      5. no duplicates within the list
    """
    classes = result.get("classes")
    if not isinstance(classes, list):
        return False
    seen: set[str] = set()
    for cls in classes:
        if not isinstance(cls, str) or not cls.strip():
            return False
        if len(cls) >= 200:
            return False
        if cls not in source_content:
            return False
        if cls in seen:
            return False
        seen.add(cls)
    return True


# ── Discrepancy persistence (async, fire-and-forget) ──────────────────────────

async def _write_parser_discrepancy(
    project_id: str,
    file_path: str,
    regex_classes: list[str],
    ts_classes: list[str],
    regex_count: int,
    ts_count: int,
    confidence: str,
    parser_used: str,
) -> None:
    """Persist one parser comparison row, replacing any prior row for the same file."""
    try:
        from sqlalchemy import delete as _sa_delete
        from storage.postgres import get_pg_session
        from storage.models import ParserDiscrepancy
        async with get_pg_session() as session:
            # Deduplicate: one row per (project_id, file_path)
            await session.execute(
                _sa_delete(ParserDiscrepancy).where(
                    ParserDiscrepancy.project_id == project_id,
                    ParserDiscrepancy.file_path == file_path,
                )
            )
            session.add(ParserDiscrepancy(
                project_id=project_id,
                file_path=file_path,
                regex_classes=json.dumps(regex_classes[:50]),
                ts_classes=json.dumps(ts_classes[:50]),
                regex_count=regex_count,
                ts_count=ts_count,
                confidence=confidence,
                parser_used=parser_used,
            ))
    except Exception as exc:
        logger.error(
            "[Java Parse] discrepancy DB write failed for %s: %s",
            os.path.basename(file_path), exc,
        )


def _schedule_discrepancy_write(
    project_id: str,
    file_path: str,
    regex_classes: list[str],
    ts_classes: list[str],
    regex_count: int,
    ts_count: int,
    confidence: str,
    parser_used: str,
) -> None:
    """Schedule an async discrepancy write without blocking the sync parser."""
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(_write_parser_discrepancy(
            project_id, file_path,
            regex_classes, ts_classes,
            regex_count, ts_count,
            confidence, parser_used,
        ))
    except RuntimeError:
        # No running event loop — skip DB write (pure-sync context)
        pass


# ── Java parsers ──────────────────────────────────────────────────────────────

def _parse_java_file(file_path: str, project_id: str | None = None) -> dict:
    """Extract classes, interfaces, enums, records, and public methods from a Java file.

    Step 1 always runs the regex parser.
    Step 2 (when USE_TREE_SITTER_JAVA=True) runs the tree-sitter parser,
    validates both results, picks the better one, and logs the comparison.

    project_id is optional:
      - When None (all existing call sites): DB write is skipped, comparison
        still runs and is logged.
      - When provided: discrepancy row is written to parser_discrepancies.

    Handles:
      - class / abstract class / final class
      - interface / enum / record (Java 16+)
      - public/protected/private methods
      - import statements
    """
    # ── STEP 1: Regex parser (always runs) ───────────────────────────────────
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()

    # Strip line/block comments to avoid false-positive matches inside comment text.
    source_no_comments = re.sub(r'//[^\n]*', '', source)
    source_no_comments = re.sub(r'/\*.*?\*/', '', source_no_comments, flags=re.DOTALL)

    classes: list[str] = []
    functions: list[str] = []
    imports: list[str] = []
    package: str = ""

    # Package declaration: package com.example.service;
    pkg_match = re.search(r'^\s*package\s+([\w.]+)\s*;', source, re.MULTILINE)
    if pkg_match:
        package = pkg_match.group(1)

    # Type declarations: class, interface, enum, record
    for m in re.finditer(
        r'(?:(?:public|protected|private|abstract|final|static)\s+)*'
        r'(?:class|interface|enum|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)',
        source_no_comments,
    ):
        classes.append(m.group(1))

    # Method declarations
    for m in re.finditer(
        r'(?:@\w+\s+)*'
        r'(?:public|protected|private|static|final|abstract|synchronized|native|default|override)'
        r'(?:\s+(?:public|protected|private|static|final|abstract|synchronized|native|default|override))*'
        r'\s+[\w<>\[\],\s]+?\s+'
        r'([a-z_$][A-Za-z0-9_$]*)\s*\(',
        source_no_comments,
    ):
        name = m.group(1)
        if name not in _JAVA_KEYWORDS and name not in functions:
            functions.append(name)

    # Import statements: import com.example.foo.Bar;
    for m in re.finditer(r'^\s*import\s+(?:static\s+)?([\w.]+)\s*;', source, re.MULTILINE):
        imports.append(m.group(1))

    regex_result = {"classes": classes, "functions": functions, "imports": imports, "package": package}

    # ── STEP 2: Feature-flag gate ─────────────────────────────────────────────
    if not settings.use_tree_sitter_java:
        return regex_result

    # Log the graph-consistency notice once per process start.
    global _TS_CONSISTENCY_WARNED
    if not _TS_CONSISTENCY_WARNED:
        logger.warning(
            "Tree-sitter enabled. For consistency, run a full scan to rebuild graph."
        )
        _TS_CONSISTENCY_WARNED = True

    # ── STEP 3: Tree-sitter parse ─────────────────────────────────────────────
    try:
        from scanner.java_treesitter import _parse_java_treesitter
        ts_result = _parse_java_treesitter(file_path)
    except Exception as exc:
        logger.error(
            "[Java Parse] tree-sitter failed for %s: %s — falling back to regex",
            os.path.basename(file_path), exc,
        )
        return regex_result

    # ── STEP 4: Validate both results ─────────────────────────────────────────
    regex_valid = _validate_java_parse_result(regex_result, source)
    ts_valid = _validate_java_parse_result(ts_result, source)

    regex_count = len(regex_result.get("classes", []))
    ts_count = len(ts_result.get("classes", []))

    # ── STEP 5: Decision logic ────────────────────────────────────────────────
    if not ts_valid and not regex_valid:
        parser_used = "regex"
        confidence = "low"
        chosen = regex_result
    elif not ts_valid:
        parser_used = "regex"
        confidence = "low"
        chosen = regex_result
    elif not regex_valid:
        # Regex produced garbage; trust tree-sitter
        if ts_count > 0:
            confidence = "medium"
        else:
            confidence = "low"
        parser_used = "tree-sitter"
        chosen = ts_result
    elif ts_count >= regex_count:
        parser_used = "tree-sitter"
        confidence = "high"
        chosen = ts_result
    elif ts_count > 0:
        parser_used = "tree-sitter"
        confidence = "medium"
        chosen = ts_result
    else:
        # ts_count == 0 and regex_count > 0
        parser_used = "regex"
        confidence = "low"
        chosen = regex_result

    # ── STEP 6: Log ──────────────────────────────────────────────────────────
    logger.info(
        "[Java Parse] file=%s | ts=%d regex=%d | using=%s | confidence=%s",
        os.path.basename(file_path), ts_count, regex_count, parser_used, confidence,
    )

    # ── STEP 7: DB write (fire-and-forget, only when project_id is known) ────
    if project_id is not None:
        _schedule_discrepancy_write(
            project_id=project_id,
            file_path=file_path,
            regex_classes=regex_result.get("classes", []),
            ts_classes=ts_result.get("classes", []),
            regex_count=regex_count,
            ts_count=ts_count,
            confidence=confidence,
            parser_used=parser_used,
        )

    # ── STEP 8: Return — always merge regex imports (tree-sitter skips them) ─
    if parser_used == "tree-sitter":
        return {
            **chosen,
            "imports": regex_result.get("imports", []),
            "package": chosen.get("package") or regex_result.get("package", ""),
        }
    return regex_result


def _parse_kotlin_file(file_path: str) -> dict:
    """Extract classes, functions, and imports from a Kotlin file.
    Kotlin syntax is close enough to Java for regex-based extraction to work:
    class/object/interface declarations and fun declarations map directly.
    Reuses the Java parser internals with Kotlin-specific additions.
    """
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()

    source_no_comments = re.sub(r'//[^\n]*', '', source)
    source_no_comments = re.sub(r'/\*.*?\*/', '', source_no_comments, flags=re.DOTALL)

    classes: list[str] = []
    functions: list[str] = []
    imports: list[str] = []
    package: str = ""

    pkg_match = re.search(r'^\s*package\s+([\w.]+)', source, re.MULTILINE)
    if pkg_match:
        package = pkg_match.group(1)

    # class / data class / sealed class / abstract class / object / interface / enum class
    for m in re.finditer(
        r'(?:(?:public|private|protected|internal|abstract|sealed|data|open|final|inner|companion)\s+)*'
        r'(?:class|object|interface|enum\s+class)\s+([A-Za-z_$][A-Za-z0-9_$]*)',
        source_no_comments,
    ):
        classes.append(m.group(1))

    # fun declarations
    _KT_KEYWORDS = {"if", "for", "while", "when", "catch", "finally"}
    for m in re.finditer(
        r'(?:(?:public|private|protected|internal|override|suspend|inline|operator|infix|tailrec)\s+)*'
        r'fun\s+(?:<[^>]*>\s*)?([a-zA-Z_$][A-Za-z0-9_$]*)\s*\(',
        source_no_comments,
    ):
        name = m.group(1)
        if name not in _KT_KEYWORDS and name not in functions:
            functions.append(name)

    # import statements
    for m in re.finditer(r'^\s*import\s+([\w.]+)', source, re.MULTILINE):
        imports.append(m.group(1))

    return {"classes": classes, "functions": functions, "imports": imports, "package": package}


def _parse_go_file(file_path: str) -> dict:
    """Extract package name, func declarations, and imports from a Go file
    using regex.  Returns the same shape as the other parsers.
    """
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()

    source_no_comments = re.sub(r'//[^\n]*', '', source)
    source_no_comments = re.sub(r'/\*.*?\*/', '', source_no_comments, flags=re.DOTALL)

    classes: list[str] = []   # Go has no classes; type declarations go here
    functions: list[str] = []
    imports: list[str] = []
    package: str = ""

    pkg_match = re.search(r'^\s*package\s+(\w+)', source, re.MULTILINE)
    if pkg_match:
        package = pkg_match.group(1)

    # type declarations: structs and interfaces (Go's equivalent of classes)
    for m in re.finditer(r'\btype\s+([A-Z][A-Za-z0-9_]*)\s+(?:struct|interface)\b', source_no_comments):
        classes.append(m.group(1))

    # func and method declarations
    # func Name(      — top-level function
    # func (r Recv) Name(  — method on a receiver
    for m in re.finditer(
        r'\bfunc\s+(?:\([^)]*\)\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*\(',
        source_no_comments,
    ):
        name = m.group(1)
        if name not in functions:
            functions.append(name)

    # import blocks:  import "pkg"  or  import ( "pkg"\n "pkg2" )
    # Single import
    for m in re.finditer(r'\bimport\s+"([\w./\-]+)"', source):
        imports.append(m.group(1))
    # Grouped import block
    block_match = re.search(r'\bimport\s*\(([^)]*)\)', source, re.DOTALL)
    if block_match:
        for m in re.finditer(r'"([\w./\-]+)"', block_match.group(1)):
            pkg = m.group(1)
            if pkg not in imports:
                imports.append(pkg)

    return {"classes": classes, "functions": functions, "imports": imports, "package": package}


def _classify_layer_from_path(file_path: str) -> str | None:
    """Return a layer name if the file path contains a known component directory.

    Checks path segments for JS/TS conventions (components, pages, hooks,
    routes, store/redux, context) in addition to class-name suffixes.
    Returns None when no known pattern is matched so the caller can fall
    through to _classify_layer().
    """
    lowered = file_path.replace("\\", "/").lower()
    # Split into path segments for exact-segment matching (avoids false
    # positives like "components" matching inside "decomponents").
    parts = set(lowered.replace("\\", "/").split("/"))

    # View / Component layer
    if "/components/" in lowered or "/pages/" in lowered:
        return "View"
    if "component" in parts or "components" in parts or "pages" in parts:
        return "View"

    # Controller layer — route files
    if "/routes/" in lowered or "/route/" in lowered:
        return "Controller"
    if "routes" in parts or "route" in parts or "controllers" in parts:
        return "Controller"

    # Service layer — state management and context
    if "/store/" in lowered or "/redux/" in lowered or "/context/" in lowered:
        return "Service"
    if "store" in parts or "redux" in parts or "context" in parts:
        return "Service"

    # Util layer — hooks directory
    if "/hooks/" in lowered or "/hook/" in lowered:
        return "Util"
    if "hooks" in parts or "hook" in parts:
        return "Util"

    return None


def _parse_ts_js_file(file_path: str) -> dict:
    """Extract classes, functions, and imports from a TypeScript/JavaScript file
    using regex patterns.  Returns the same shape as _parse_python_file.

    Also detects:
    - React components (JSX, export default function, React.Component extends)
    - Express routes (app.get/post, router.get/post) → tagged as Controller
    - React hooks usage (useState, useEffect) → tagged as View
    """
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()

    classes: list[str] = []
    functions: list[str] = []
    imports: list[str] = []
    # Extra layer hints collected from source content (used by scan_project)
    layer_hints: list[str] = []

    # Classes: class Foo, export class Foo, abstract class Foo, export default class Foo
    for m in re.finditer(
        r'(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+([A-Za-z_$][A-Za-z0-9_$]*)',
        source,
    ):
        classes.append(m.group(1))

    # Named functions: function foo(), async function foo(), export function foo()
    for m in re.finditer(
        r'(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([A-Za-z_$][A-Za-z0-9_$]*)',
        source,
    ):
        functions.append(m.group(1))

    # Arrow function assignments: const foo = (...) => or const foo = async (...) =>
    for m in re.finditer(
        r'(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][A-Za-z0-9_$]*)\s*(?::\s*[^=]+?)?\s*=>',
        source,
    ):
        functions.append(m.group(1))

    # Arrow / function-expression assignments where the arrow may be on the next line:
    # const foo = async (   or   const foo = (
    for m in re.finditer(
        r'(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*(?:async\s+)?\(',
        source,
    ):
        name = m.group(1)
        if name not in functions:
            functions.append(name)

    # ES module imports: import { X } from 'y', import X from 'y'
    imports_detailed: list[dict] = []
    for m in re.finditer(r'''import\s+(.*?)\s+from\s+['"]([^'"]+)['"]''', source):
        clause = m.group(1).strip()
        module_path = m.group(2)
        imports.append(module_path)
        # Extract named imports: { A, B } or default import name
        names: list[str] = []
        named_match = re.findall(r'\{([^}]+)\}', clause)
        if named_match:
            for group in named_match:
                for name in group.split(","):
                    name = name.strip().split(" as ")[0].strip()
                    if name:
                        names.append(name)
        elif not clause.startswith("{") and not clause.startswith("*"):
            # Default import: import Foo from '...'
            default_name = clause.split(",")[0].strip()
            if default_name and default_name != "type":
                names.append(default_name)
        imports_detailed.append({
            "raw": m.group(0),
            "module": module_path,
            "names": names,
        })

    # CommonJS require: require('y')
    for m in re.finditer(r'''require\(\s*['"]([^'"]+)['"]\s*\)''', source):
        imports.append(m.group(1))
        imports_detailed.append({
            "raw": m.group(0),
            "module": m.group(1),
            "names": [],
        })

    # --- React component detection ---
    # JSX presence: any <Tag or </Tag pattern strongly implies a React component
    has_jsx = bool(re.search(r'<[A-Z][A-Za-z0-9]*[\s/>]|</[A-Za-z]', source))
    # React.Component base class
    has_react_class = bool(re.search(r'extends\s+(?:React\.)?(?:Component|PureComponent)', source))
    # export default function ComponentName (capital letter = component convention)
    has_default_func_component = bool(
        re.search(r'export\s+default\s+(?:function|class)\s+[A-Z]', source)
    )
    if has_jsx or has_react_class or has_default_func_component:
        layer_hints.append("View")

    # --- Express route detection ---
    # app.get/post/put/delete/patch or router.get/post/...
    has_express_routes = bool(
        re.search(
            r'(?:app|router)\s*\.\s*(?:get|post|put|delete|patch|use)\s*\(',
            source,
        )
    )
    if has_express_routes:
        layer_hints.append("Controller")

    # --- React hook usage detection ---
    has_hooks = bool(re.search(r'\b(?:useState|useEffect|useCallback|useMemo|useRef)\s*\(', source))
    if has_hooks and "View" not in layer_hints:
        layer_hints.append("View")

    return {
        "classes": classes,
        "functions": functions,
        "imports": imports,
        "imports_detailed": imports_detailed,
        "layer_hints": layer_hints,
    }


async def _write_class_to_neo4j(
    class_name: str,
    file_path: str,
    project_id: str,
    layer: str,
    package: str = "",
    role_confidence: float = 0.0,
    role_source: str = "default",
    annotations: list[str] | None = None,
    superclass: str | None = None,
    declared_role: str | None = None,
) -> None:
    # Preserve AI-inferred role/confidence if this node was previously tagged by AI.
    # All other properties are overwritten on every scan to reflect the current code.
    cypher = (
        "MERGE (c:Class {name: $name, project_id: $project_id}) "
        "SET c.file_path = $file_path, c.layer = $layer, c.package = $package, "
        "c.annotations = $annotations, c.superclass = $superclass, "
        "c.declared_role = $declared_role, "
        "c.inferred_role = CASE WHEN c.role_source = 'ai' THEN c.inferred_role ELSE $layer END, "
        "c.role_confidence = CASE WHEN c.role_source = 'ai' THEN c.role_confidence ELSE $role_confidence END, "
        "c.role_source = CASE WHEN c.role_source = 'ai' THEN 'ai' ELSE $role_source END"
    )
    await neo4j_client.execute(cypher, {
        "name": class_name,
        "project_id": project_id,
        "file_path": file_path,
        "layer": layer,
        "package": package,
        "annotations": annotations or [],
        "superclass": superclass or "",
        "declared_role": declared_role or "",
        "role_confidence": role_confidence,
        "role_source": role_source,
    })


async def _write_function_to_neo4j(func_name: str, file_path: str, project_id: str) -> None:
    cypher = "MERGE (f:Function {name: $name, file_path: $file_path, project_id: $project_id})"
    await neo4j_client.execute(cypher, {"name": func_name, "file_path": file_path, "project_id": project_id})


def _detect_modules(root_path: str, source_files: list[str]) -> list[str]:
    root = Path(root_path)
    modules: set[str] = set()
    for fpath in source_files:
        try:
            rel = Path(fpath).relative_to(root)
            if len(rel.parts) > 1:
                modules.add(rel.parts[0])
        except ValueError:
            pass
    return sorted(modules)


_ROLE_TO_GROUP: dict[str, str] = {
    "Controller": "controllers",
    "Service":    "services",
    "Repository": "repositories",
    "Entity":     "entities",
    "DTO":        "dtos",
}


def _build_class_registry(parsed_files: list[dict]) -> list[str]:
    """Return ALL classes grouped by role in a structured, parseable format.

    Output: one string per non-empty group, e.g.
      "controllers: OrderController, ProductController"
      "services: OrderService, ProductService"
      "unclassified: SomeHelper"

    When joined with '\\n' this gives the LLM an immediately readable class roster.
    Classes with roles View/Util/Component/default go to 'unclassified'.
    """
    groups: dict[str, list[str]] = {
        "controllers": [],
        "services": [],
        "repositories": [],
        "entities": [],
        "dtos": [],
        "unclassified": [],
    }
    seen: set[str] = set()
    for pf in parsed_files:
        role = pf.get("layer", "Util") or "Util"
        group = _ROLE_TO_GROUP.get(role, "unclassified")
        for cls in pf["classes"]:
            if cls not in seen:
                seen.add(cls)
                groups[group].append(cls)

    result: list[str] = []
    for group_name, classes in groups.items():
        if classes:
            result.append(f"{group_name}: {', '.join(sorted(classes))}")
    return result


def _find_build_files(root_path: str, filename: str, max_depth: int = 2) -> list[str]:
    """Walk up to max_depth directory levels under root_path and collect all
    occurrences of filename, sorted shallowest-first (root wins).

    Returns a list of absolute paths, never raises.
    """
    found: list[tuple[int, str]] = []
    root = Path(root_path)
    try:
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in ALWAYS_SKIP_DIRS]
            try:
                depth = len(Path(dirpath).relative_to(root).parts)
            except ValueError:
                continue
            if depth > max_depth:
                # Prune walk: don't descend deeper
                dirnames.clear()
                continue
            if filename in filenames:
                found.append((depth, os.path.join(dirpath, filename)))
    except OSError as exc:
        logger.warning("_find_build_files: OS error walking %s — %s", root_path, exc)
    # Sort by depth so the shallowest file (closest to root) is first
    found.sort(key=lambda t: t[0])
    return [path for _, path in found]


def _detect_stack(
    root_path: str,
    all_imports: list[str] | None = None,
    parsed_files: list[dict] | None = None,
) -> dict:
    """Detect tech stack by reading well-known project config files.

    Checks for Maven, Gradle, package.json, go.mod, requirements.txt, and
    pyproject.toml.  Returns a dict with keys: language, framework,
    build_tool, dependencies, and (when multiple languages coexist in a
    monorepo) secondary_languages.

    Monorepo / mixed-language handling:
    - All candidate build files are collected first, across all types and up
      to 2 directory levels deep.
    - Each candidate is scored by:
        1. depth (root=0 wins over subdirectory=1 or deeper=2)
        2. language-file priority weight:
              requirements.txt / pyproject.toml  → weight 10 (Python first)
              pom.xml                            → weight 8
              build.gradle.kts / build.gradle    → weight 6
              package.json                       → weight 4
              go.mod                             → weight 4
    - The candidate with the lowest (depth, -weight) score wins the primary
      language slot.
    - When build files of DIFFERENT language types are found, the dominant
      language is verified by counting source files per language extension
      in parsed_files (if provided).  The language with the most source files
      becomes stack["language"]; the others go into stack["secondary_languages"].

    Every file read is wrapped in try/except so malformed XML/JSON never
    crashes detection.

    If framework remains "unknown" after config-file parsing (e.g. the config
    file exists in a subdirectory rather than at root_path), a second pass
    over all_imports is performed to infer the framework from import names
    collected during AST/regex parsing of source files.
    """
    stack = {
        "language": "unknown",
        "framework": "unknown",
        "build_tool": "unknown",
        "dependencies": [],
        "secondary_languages": [],
    }

    # ── Collect all candidate build files with (depth, weight, filename, path) ──
    # weight: higher = more important / higher-priority language signal
    _BUILD_FILE_WEIGHTS: dict[str, int] = {
        "requirements.txt": 10,
        "pyproject.toml":   10,
        "pom.xml":           8,
        "build.gradle.kts":  6,
        "build.gradle":      6,
        "package.json":      4,
        "go.mod":            4,
    }

    candidates: list[tuple[int, int, str, str]] = []  # (depth, weight, filename, abs_path)
    for fname, weight in _BUILD_FILE_WEIGHTS.items():
        for fpath in _find_build_files(root_path, fname, max_depth=2):
            root = Path(root_path)
            try:
                depth = len(Path(fpath).parent.relative_to(root).parts)
            except ValueError:
                depth = 99
            candidates.append((depth, weight, fname, fpath))

    if not candidates:
        # No build files at all — fall through to import-based detection below
        pass
    else:
        # Sort: shallowest depth first, then highest weight first
        candidates.sort(key=lambda t: (t[0], -t[1]))

        # Identify which language types are present and at what depth+weight
        # so we can detect mixed-language monorepos.
        _FILENAME_TO_LANG: dict[str, str] = {
            "requirements.txt": "Python",
            "pyproject.toml":   "Python",
            "pom.xml":          "Java",
            "build.gradle.kts": "Kotlin",
            "build.gradle":     "Java/Kotlin",
            "package.json":     "JavaScript/TypeScript",
            "go.mod":           "Go",
        }
        # Deduplicate: track the best (lowest score) candidate per language type
        lang_best: dict[str, tuple[int, int, str, str]] = {}
        for cand in candidates:
            depth, weight, fname, fpath = cand
            lang = _FILENAME_TO_LANG.get(fname, "unknown")
            if lang not in lang_best:
                lang_best[lang] = cand  # candidates already sorted best-first

        distinct_langs = list(lang_best.keys())

        # Choose primary language candidate: best score overall (already at
        # candidates[0] after sorting).
        primary_cand = candidates[0]
        primary_lang = _FILENAME_TO_LANG.get(primary_cand[2], "unknown")
        secondary_langs = [lg for lg in distinct_langs if lg != primary_lang]

        # If multiple distinct language types exist, use source file counts to
        # pick the true dominant language (monorepo with, e.g., Python backend
        # + Kotlin plugin).
        if secondary_langs and parsed_files:
            ext_to_lang = {
                ".py":  "Python",
                ".ts":  "JavaScript/TypeScript",
                ".tsx": "JavaScript/TypeScript",
                ".js":  "JavaScript/TypeScript",
                ".jsx": "JavaScript/TypeScript",
                ".kt":  "Kotlin",
                ".java": "Java",
                ".go":  "Go",
            }
            lang_file_counts: dict[str, int] = {}
            for pf in parsed_files:
                ext = "." + pf.get("language", "")
                mapped = ext_to_lang.get(ext)
                if mapped:
                    lang_file_counts[mapped] = lang_file_counts.get(mapped, 0) + 1

            if lang_file_counts:
                dominant_by_files = max(lang_file_counts, key=lambda k: lang_file_counts[k])
                # Remap "JavaScript/TypeScript" to the actual build-file signal
                # (TypeScript vs JavaScript) once we know it's dominant.
                # We'll resolve this properly when parsing the package.json below.
                primary_lang = dominant_by_files
                secondary_langs = [lg for lg in distinct_langs if lg != dominant_by_files]
                logger.info(
                    "_detect_stack: monorepo detected — source file counts=%s, dominant=%s, secondary=%s",
                    lang_file_counts, dominant_by_files, secondary_langs,
                )
                # Re-select the best candidate for the dominant language
                for cand in candidates:
                    if _FILENAME_TO_LANG.get(cand[2], "unknown") == dominant_by_files:
                        primary_cand = cand
                        break
                    # "JavaScript/TypeScript" counted above; package.json maps to it
                    if dominant_by_files == "JavaScript/TypeScript" and cand[2] == "package.json":
                        primary_cand = cand
                        break

        stack["secondary_languages"] = secondary_langs

        # ── Parse the winning build file ─────────────────────────────────────
        _, _, primary_fname, primary_fpath = primary_cand

        if primary_fname == "pom.xml":
            try:
                content = open(primary_fpath, encoding="utf-8", errors="replace").read()
                stack["build_tool"] = "Maven"
                stack["language"] = "Java"
                if "spring-boot" in content:
                    stack["framework"] = "Spring Boot"
                elif "quarkus" in content:
                    stack["framework"] = "Quarkus"
                elif "micronaut" in content:
                    stack["framework"] = "Micronaut"
                stack["dependencies"] = re.findall(r'<artifactId>([^<]+)</artifactId>', content)[:20]
            except OSError as exc:
                logger.warning("_detect_stack: could not read pom.xml at %s — %s", primary_fpath, exc)
            except Exception as exc:
                logger.warning("_detect_stack: error parsing pom.xml at %s — %s", primary_fpath, exc)

        elif primary_fname in ("build.gradle.kts", "build.gradle"):
            try:
                content = open(primary_fpath, encoding="utf-8", errors="replace").read()
                stack["build_tool"] = "Gradle"
                stack["language"] = "Kotlin" if primary_fname.endswith(".kts") else "Java/Kotlin"
                if "spring-boot" in content:
                    stack["framework"] = "Spring Boot"
            except OSError as exc:
                logger.warning("_detect_stack: could not read %s — %s", primary_fpath, exc)
            except Exception as exc:
                logger.warning("_detect_stack: error parsing %s — %s", primary_fpath, exc)

        elif primary_fname == "package.json":
            try:
                import json as _json
                with open(primary_fpath, encoding="utf-8", errors="replace") as fh:
                    data = _json.load(fh)
                deps = {**data.get("dependencies", {}), **data.get("devDependencies", {})}
                stack["build_tool"] = "npm/yarn"
                stack["language"] = "TypeScript" if "typescript" in deps else "JavaScript"

                if "vite" in deps:
                    stack["build_tool"] = "Vite"
                elif "webpack" in deps or "webpack-cli" in deps:
                    stack["build_tool"] = "webpack"

                if "next" in deps:
                    stack["framework"] = "Next.js"
                elif "@nestjs/core" in deps:
                    stack["framework"] = "NestJS"
                elif "@remix-run/node" in deps or "@remix-run/react" in deps:
                    stack["framework"] = "Remix"
                elif "express" in deps:
                    stack["framework"] = "Express"
                elif "react" in deps:
                    stack["framework"] = "React"
                elif "vue" in deps or "@vue/core" in deps:
                    stack["framework"] = "Vue.js"
                elif "@angular/core" in deps:
                    stack["framework"] = "Angular"
                elif "svelte" in deps or "@sveltejs/kit" in deps:
                    stack["framework"] = "Svelte"

                stack["dependencies"] = list(deps.keys())[:20]
            except Exception as exc:
                logger.warning("_detect_stack: error parsing package.json at %s — %s", primary_fpath, exc)

        elif primary_fname == "go.mod":
            try:
                content = open(primary_fpath, encoding="utf-8", errors="replace").read()
                stack["language"] = "Go"
                stack["build_tool"] = "Go modules"
                if "gin-gonic" in content:
                    stack["framework"] = "Gin"
                elif "echo" in content:
                    stack["framework"] = "Echo"
            except OSError as exc:
                logger.warning("_detect_stack: could not read go.mod at %s — %s", primary_fpath, exc)
            except Exception as exc:
                logger.warning("_detect_stack: error parsing go.mod at %s — %s", primary_fpath, exc)

        elif primary_fname in ("requirements.txt", "pyproject.toml"):
            try:
                content = open(primary_fpath, encoding="utf-8", errors="replace").read()
                stack["language"] = "Python"
                stack["build_tool"] = "pip" if primary_fname == "requirements.txt" else "pyproject"
                if "fastapi" in content.lower():
                    stack["framework"] = "FastAPI"
                elif "django" in content.lower():
                    stack["framework"] = "Django"
                elif "flask" in content.lower():
                    stack["framework"] = "Flask"
            except OSError as exc:
                logger.warning("_detect_stack: could not read %s — %s", primary_fpath, exc)
            except Exception as exc:
                logger.warning("_detect_stack: error parsing %s — %s", primary_fpath, exc)

    # ── Import-based framework fallback ─────────────────────────────────────
    # If framework is still unknown after config-file parsing, infer it from
    # the imports collected during source parsing.  This handles projects where
    # no config file is at/near root or where requirements.txt doesn't list the
    # framework explicitly.
    if stack["framework"] == "unknown" and all_imports:
        imports_lower = {imp.lower() for imp in all_imports}
        # Check each module prefix (e.g. "fastapi.routing" → "fastapi")
        flat_prefixes = {imp.split(".")[0].split("/")[-1] for imp in imports_lower}
        if "fastapi" in flat_prefixes:
            stack["framework"] = "FastAPI"
            if stack["language"] == "unknown":
                stack["language"] = "Python"
        elif "django" in flat_prefixes:
            stack["framework"] = "Django"
            if stack["language"] == "unknown":
                stack["language"] = "Python"
        elif "flask" in flat_prefixes:
            stack["framework"] = "Flask"
            if stack["language"] == "unknown":
                stack["language"] = "Python"
        elif any(p in flat_prefixes for p in ("springframework", "spring")):
            stack["framework"] = "Spring Boot"
            if stack["language"] == "unknown":
                stack["language"] = "Java"
        elif "express" in flat_prefixes:
            stack["framework"] = "Express"
            if stack["language"] == "unknown":
                stack["language"] = "JavaScript"
        elif "react" in flat_prefixes:
            stack["framework"] = "React"
            if stack["language"] == "unknown":
                stack["language"] = "JavaScript"
        elif "vue" in flat_prefixes:
            stack["framework"] = "Vue.js"
            if stack["language"] == "unknown":
                stack["language"] = "JavaScript"

    return stack


def _infer_language_from_extensions(source_files: list[str]) -> str:
    """Return the dominant language by counting source file extensions.

    Used as a last-resort fallback when no config file (pom.xml, package.json,
    etc.) is present and _detect_stack returns language='unknown'.
    """
    counts: dict[str, int] = {}
    ext_to_lang = {
        ".py": "Python",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".kt": "Kotlin",
        ".java": "Java",
        ".go": "Go",
    }
    for fpath in source_files:
        ext = Path(fpath).suffix
        lang = ext_to_lang.get(ext)
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    if not counts:
        return "unknown"
    return max(counts, key=lambda k: counts[k])


def _read_doc_text(file_path: str) -> str | None:
    """Extract plain text from a documentation file.

    .md  — read as UTF-8 text directly.
    .pdf — attempt PyMuPDF (fitz), then pdfplumber.  Returns None if no
           PDF library is available or extraction fails.
    """
    ext = Path(file_path).suffix.lower()
    if ext == ".md":
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError as e:
            logger.warning("Doc read failed for %s: %s", file_path, e)
            return None

    if ext == ".pdf":
        # Try PyMuPDF first (faster, more reliable)
        try:
            import fitz  # PyMuPDF
            doc = fitz.open(file_path)
            pages = [page.get_text() for page in doc]
            doc.close()
            text = "\n".join(pages).strip()
            return text if text else None
        except ImportError:
            pass
        except Exception as e:
            logger.warning("PyMuPDF extraction failed for %s: %s", file_path, e)

        # Fall back to pdfplumber
        try:
            import pdfplumber
            with pdfplumber.open(file_path) as pdf:
                pages = [p.extract_text() or "" for p in pdf.pages]
            text = "\n".join(pages).strip()
            return text if text else None
        except ImportError:
            logger.debug("No PDF library (fitz / pdfplumber) — skipping %s", file_path)
        except Exception as e:
            logger.warning("pdfplumber extraction failed for %s: %s", file_path, e)

    return None


async def _index_docs(
    project_id: str,
    root_path: str,
    scan_mode: str,
) -> int:
    """Walk the project tree for .md and .pdf files, extract text, and upsert
    into the COLLECTION_DOCS Qdrant collection.

    Returns the number of documents successfully indexed.
    """
    import hashlib
    from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue

    if not qdrant_client.is_connected:
        return 0

    # Collect doc files using the same skip logic as source files
    doc_files: list[str] = []
    abs_root = os.path.abspath(root_path)
    for dirpath, dirnames, filenames in os.walk(root_path):
        is_root = os.path.abspath(dirpath) == abs_root
        pruned: list[str] = []
        for d in dirnames:
            if d in ALWAYS_SKIP_DIRS:
                continue
            if is_root and d in ROOT_SKIP_DIRS:
                continue
            pruned.append(d)
        dirnames[:] = pruned
        for fname in filenames:
            if Path(fname).suffix.lower() in DOC_EXTENSIONS:
                doc_files.append(os.path.join(dirpath, fname))

    if not doc_files:
        return 0

    # On full scan wipe existing docs so stale files don't persist
    if scan_mode == "full":
        try:
            await qdrant_client.client.delete(
                collection_name=COLLECTION_DOCS,
                points_selector=Filter(
                    must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
                ),
            )
        except Exception:
            logger.warning("Scan [%s]: Qdrant doc pre-delete failed — continuing", project_id, exc_info=True)

    indexed = 0
    points: list[PointStruct] = []
    BATCH_SIZE = 50

    for fpath in doc_files:
        text = _read_doc_text(fpath)
        if not text:
            continue

        title = Path(fpath).name
        # Truncate to 8000 chars for embedding — full text stored in payload
        embed_text = f"{title} {text[:8000]}"
        embedding = _generate_embedding(
            class_names=[title],
            file_path=fpath,
            layer="docs",
            methods=[],
        )
        raw_id = hashlib.md5(f"{project_id}:doc:{fpath}".encode()).hexdigest()
        point_id = int(raw_id[:16], 16)

        points.append(PointStruct(
            id=point_id,
            vector=embedding,
            payload={
                "project_id": project_id,
                "file_path": fpath,
                "title": title,
                "type": "docs",
                "content": text[:32000],  # store up to 32k chars in payload
            },
        ))
        indexed += 1

        if len(points) >= BATCH_SIZE:
            try:
                await qdrant_client.client.upsert(collection_name=COLLECTION_DOCS, points=points)
            except Exception:
                logger.warning("Scan [%s]: doc batch upsert failed", project_id, exc_info=True)
            points = []

    if points:
        try:
            await qdrant_client.client.upsert(collection_name=COLLECTION_DOCS, points=points)
        except Exception:
            logger.warning("Scan [%s]: doc final upsert failed", project_id, exc_info=True)

    logger.info("Scan [%s]: indexed %d doc files into %s", project_id, indexed, COLLECTION_DOCS)
    return indexed


async def _discover_patterns(project_id: str, parsed_files: list[dict]) -> list[str]:
    """Analyze the Neo4j import graph and class naming patterns discovered
    during scanning.  Returns a list of human-readable pattern strings that
    describe ACTUAL structural behaviour observed in THIS codebase.

    These are NOT hardcoded rules — they are measurements derived from the graph.
    Example outputs:
      "7/8 classes named *Controller import at least one *Service class (88%)"
      "0/7 classes named *Repository are imported by any *Controller class"
      "Module 'auth' has 12 incoming IMPORTS edges, all from 'api' module"
    """
    if not neo4j_client.is_connected:
        return []

    patterns: list[str] = []

    # ── Pattern 1: Layer cross-call rates ─────────────────────────────────────
    # For each source layer S and target layer T, count how many nodes in S
    # have at least one IMPORTS edge pointing to a node in T.
    try:
        layer_cypher = """
            MATCH (a:Class)-[:IMPORTS]->(b:Class)
            WHERE a.project_id = $project_id AND b.project_id = $project_id
                  AND a.layer IS NOT NULL AND b.layer IS NOT NULL
            RETURN a.layer AS from_layer, b.layer AS to_layer, count(DISTINCT a) AS callers
        """
        edges = await neo4j_client.query(layer_cypher, {"project_id": project_id})

        # Count total nodes per layer
        count_cypher = """
            MATCH (n:Class) WHERE n.project_id = $project_id AND n.layer IS NOT NULL
            RETURN n.layer AS layer, count(n) AS total
        """
        counts_raw = await neo4j_client.query(count_cypher, {"project_id": project_id})
        layer_totals: dict[str, int] = {r["layer"]: r["total"] for r in counts_raw}

        for row in edges:
            fl, tl, callers = row["from_layer"], row["to_layer"], row["callers"]
            total = layer_totals.get(fl, 0)
            if total == 0:
                continue
            pct = round(callers / total * 100)
            patterns.append(
                f"{callers}/{total} {fl} classes ({pct}%) import at least one {tl} class"
            )
    except Exception as e:
        logger.warning("Pattern discovery: layer edge query failed: %s", e)

    # ── Pattern 2: Isolated layers (no incoming edges from any other layer) ───
    try:
        isolated_cypher = """
            MATCH (n:Class) WHERE n.project_id = $project_id AND n.layer IS NOT NULL
            AND NOT EXISTS {
                MATCH (other:Class)-[:IMPORTS]->(n)
                WHERE other.project_id = $project_id AND other.layer <> n.layer
            }
            RETURN n.layer AS layer, count(n) AS isolated
        """
        isolated_rows = await neo4j_client.query(isolated_cypher, {"project_id": project_id})
        for row in isolated_rows:
            lyr, iso = row["layer"], row["isolated"]
            total = layer_totals.get(lyr, 0) if 'layer_totals' in dir() else 0
            if iso > 0 and total > 0 and iso == total:
                patterns.append(
                    f"{lyr} layer ({iso} classes) has no incoming cross-layer IMPORTS edges"
                )
    except Exception as e:
        logger.warning("Pattern discovery: isolation query failed: %s", e)

    # ── Pattern 3: Module-level fan-in / fan-out ──────────────────────────────
    try:
        # Use package name if available, otherwise derive from file path top segment
        module_edges_cypher = """
            MATCH (a:Class)-[:IMPORTS]->(b:Class)
            WHERE a.project_id = $project_id AND b.project_id = $project_id
            WITH
                CASE WHEN a.package IS NOT NULL AND a.package <> ''
                     THEN split(a.package, '.')[0]
                     ELSE null END AS from_mod,
                CASE WHEN b.package IS NOT NULL AND b.package <> ''
                     THEN split(b.package, '.')[0]
                     ELSE null END AS to_mod,
                count(*) AS edge_count
            WHERE from_mod IS NOT NULL AND to_mod IS NOT NULL AND from_mod <> to_mod
            RETURN from_mod, to_mod, edge_count
            ORDER BY edge_count DESC
            LIMIT 10
        """
        mod_edges = await neo4j_client.query(module_edges_cypher, {"project_id": project_id})
        for row in mod_edges:
            patterns.append(
                f"Module '{row['from_mod']}' has {row['edge_count']} import edge(s) into module '{row['to_mod']}'"
            )
    except Exception as e:
        logger.warning("Pattern discovery: module edge query failed: %s", e)

    # ── Pattern 4: Classes with no outgoing imports (leaf nodes) ──────────────
    try:
        leaf_cypher = """
            MATCH (n:Class) WHERE n.project_id = $project_id AND n.layer IS NOT NULL
            AND NOT EXISTS { MATCH (n)-[:IMPORTS]->(:Class {project_id: $project_id}) }
            RETURN n.layer AS layer, count(n) AS leaves
        """
        leaf_rows = await neo4j_client.query(leaf_cypher, {"project_id": project_id})
        for row in leaf_rows:
            if row["leaves"] > 0:
                patterns.append(
                    f"{row['leaves']} {row['layer']} class(es) have no outgoing imports (leaf nodes)"
                )
    except Exception as e:
        logger.warning("Pattern discovery: leaf node query failed: %s", e)

    logger.info("Scan [%s]: discovered %d structural patterns", project_id, len(patterns))
    return patterns


async def scan_project(
    project_id: str,
    root_path: str,
    scan_mode: str = "full",
    folder_path: str | None = None,
    entry_point: str | None = None,
) -> dict:
    """Scan a project directory, parse source files, write to Neo4j/Qdrant,
    and persist a memory summary + architecture rules to PostgreSQL.

    scan_mode options:
      "full"   — scan every source file under root_path (default)
      "folder" — scan only files under folder_path
      "smart"  — start from entry_point class/file, expand dependency graph
    """
    try:
        return await _scan_project_impl(project_id, root_path, scan_mode, folder_path, entry_point)
    except Exception:
        logger.exception(
            "Scan [%s]: unhandled exception — scan aborted", project_id
        )
        raise


def _smart_scan_files(root_path: str, entry_point: str, max_depth: int = 4) -> list[str]:
    """Starting from files that contain entry_point (class or filename),
    expand the dependency graph by following imports up to max_depth levels.
    Returns list of relevant file paths.
    """
    all_files = _walk_source_files(root_path)

    # Index files by class names and by filename stem for fast lookup
    file_by_class: dict[str, str] = {}
    file_by_stem: dict[str, str] = {}
    for fpath in all_files:
        stem = Path(fpath).stem.lower()
        file_by_stem[stem] = fpath

    # Parse classes from each file (lightweight pass)
    for fpath in all_files:
        ext = Path(fpath).suffix
        try:
            if ext == ".py":
                parsed = _parse_python_file(fpath)
            elif ext in _JAVA_EXTENSIONS:
                parsed = _parse_java_file(fpath)
            elif ext == ".kt":
                parsed = _parse_kotlin_file(fpath)
            elif ext == ".go":
                parsed = _parse_go_file(fpath)
            elif ext in _TS_JS_EXTENSIONS:
                parsed = _parse_ts_js_file(fpath)
            else:
                continue
            for cls in parsed.get("classes", []):
                file_by_class[cls.lower()] = fpath
        except Exception:
            continue

    # Find seed file(s) matching entry_point
    entry_lower = entry_point.lower()
    seed_files: set[str] = set()
    if entry_lower in file_by_class:
        seed_files.add(file_by_class[entry_lower])
    if entry_lower in file_by_stem:
        seed_files.add(file_by_stem[entry_lower])
    # Also partial match on class name
    for cls_name, fpath in file_by_class.items():
        if entry_lower in cls_name:
            seed_files.add(fpath)

    if not seed_files:
        logger.warning("Smart scan: no files found matching entry_point '%s' — falling back to full scan", entry_point)
        return all_files

    # BFS expansion following imports
    visited: set[str] = set()
    frontier = list(seed_files)
    depth = 0
    while frontier and depth < max_depth:
        next_frontier: list[str] = []
        for fpath in frontier:
            if fpath in visited:
                continue
            visited.add(fpath)
            ext = Path(fpath).suffix
            try:
                if ext == ".py":
                    parsed = _parse_python_file(fpath)
                elif ext in _JAVA_EXTENSIONS:
                    parsed = _parse_java_file(fpath)
                elif ext == ".kt":
                    parsed = _parse_kotlin_file(fpath)
                elif ext == ".go":
                    parsed = _parse_go_file(fpath)
                elif ext in _TS_JS_EXTENSIONS:
                    parsed = _parse_ts_js_file(fpath)
                else:
                    continue
            except Exception:
                continue
            for imp in parsed.get("imports", []):
                # Match import module/class name against known files
                imp_parts = imp.replace(".", "/").split("/")
                for part in imp_parts:
                    part_lower = part.lower()
                    if part_lower in file_by_class and file_by_class[part_lower] not in visited:
                        next_frontier.append(file_by_class[part_lower])
                    if part_lower in file_by_stem and file_by_stem[part_lower] not in visited:
                        next_frontier.append(file_by_stem[part_lower])
        frontier = next_frontier
        depth += 1

    logger.info("Smart scan: entry_point='%s', found %d relevant files (depth=%d)", entry_point, len(visited), depth)
    return list(visited)


async def _scan_project_impl(
    project_id: str,
    root_path: str,
    scan_mode: str = "full",
    folder_path: str | None = None,
    entry_point: str | None = None,
) -> dict:
    root_path = _resolve_path(root_path)
    await memory_service.get_or_create_project(project_id=project_id, name=project_id, root_path=root_path)

    # On a full scan wipe previously auto-generated rules so outdated ones don't linger.
    # Rules added manually by the developer are deleted too — full scan is a fresh index.
    if scan_mode == "full":
        try:
            deleted = await memory_service.delete_all_rules(project_id)
            if deleted:
                logger.info("Scan [%s]: cleared %d stale rules before full rescan", project_id, deleted)
        except Exception:
            logger.warning("Scan [%s]: failed to clear stale rules — continuing", project_id, exc_info=True)

    if scan_mode == "folder" and folder_path:
        scan_root = _resolve_path(folder_path) if folder_path else root_path
        source_files = _walk_source_files(scan_root)
        logger.info("Scan [%s]: folder mode — scanning %s", project_id, scan_root)
    elif scan_mode == "smart" and entry_point:
        source_files = _smart_scan_files(root_path, entry_point)
        logger.info("Scan [%s]: smart mode — entry_point=%s", project_id, entry_point)
    else:
        source_files = _walk_source_files(root_path)
    logger.info("Scan [%s]: found %d source files in %s", project_id, len(source_files), root_path)

    # ── Ghost node cleanup (folder / smart scans only) ────────────────────────
    # Full scans wipe and rebuild the entire graph, so no cleanup needed there.
    # For partial scans, delete all existing Neo4j nodes owned by the files
    # we are about to rescan — this removes stale class/function nodes and their
    # edges (DETACH DELETE) before we write fresh ones.
    if scan_mode in ("folder", "smart") and neo4j_client.is_connected and source_files:
        try:
            ghost_deleted = await neo4j_client.query(
                """
                MATCH (n)
                WHERE n.project_id = $project_id
                  AND n.file_path IN $file_paths
                WITH n, count(n) AS cnt
                DETACH DELETE n
                RETURN cnt
                """,
                {"project_id": project_id, "file_paths": list(source_files)},
            )
            # The RETURN after DETACH DELETE returns one row per deleted node.
            n_deleted = len(ghost_deleted)
            logger.info(
                "Scan [%s]: ghost cleanup — deleted %d stale nodes for %d rescanned files",
                project_id, n_deleted, len(source_files),
            )
        except Exception:
            logger.warning(
                "Scan [%s]: ghost node cleanup failed — continuing without cleanup",
                project_id, exc_info=True,
            )

    neo4j_available = neo4j_client.is_connected
    if not neo4j_available:
        logger.warning("Scan [%s]: Neo4j not connected — skipping graph writes", project_id)

    qdrant_available = qdrant_client.is_connected
    if not qdrant_available:
        logger.warning("Scan [%s]: Qdrant not connected — skipping file index writes", project_id)

    # Load embedding model now (non-blocking, up to 120 s timeout).
    # Must happen before the Qdrant indexing loop that calls _generate_embedding.
    if qdrant_available:
        await _ensure_embedding_model()

    all_classes, all_functions = [], []
    # Accumulate per-file parsed data for Qdrant indexing
    parsed_files: list[dict] = []
    total_nodes_created = 0
    total_rejected = 0

    for i, fpath in enumerate(source_files):
        ext = Path(fpath).suffix
        if i % 50 == 0:
            logger.info("Scan [%s]: parsing file %d/%d", project_id, i + 1, len(source_files))
            # Yield the event loop every 50 files so the server stays responsive
            # during large scans (file parsing is synchronous/CPU-bound).
            await asyncio.sleep(0)
        try:
            if ext == ".py":
                parsed = _parse_python_file(fpath)
            elif ext in _JAVA_EXTENSIONS:
                parsed = _parse_java_file(fpath)
            elif ext == ".kt":
                parsed = _parse_kotlin_file(fpath)
            elif ext == ".go":
                parsed = _parse_go_file(fpath)
            elif ext in _TS_JS_EXTENSIONS:
                parsed = _parse_ts_js_file(fpath)
            else:
                continue
        except SyntaxError:
            logger.warning("Scan [%s]: syntax error in %s — skipping", project_id, fpath)
            continue
        except Exception:
            logger.warning("Scan [%s]: failed to parse %s — skipping", project_id, fpath, exc_info=True)
            continue

        # ── Validation layer ─────────────────────────────────────────────────
        # Strips invalid/suspicious extractions before anything reaches Neo4j.
        parsed = validate_parsed_file(parsed, fpath)
        rej = parsed.get("_validation_rejected", {})
        total_rejected += rej.get("classes", 0) + rej.get("functions", 0)

        all_classes.extend(parsed["classes"])
        all_functions.extend(parsed["functions"])

        # ── Role inference (Tier 1 — heuristic, per-file) ────────────────────
        # Uses annotations, path, class-name suffix, content signals.
        # Tier 2 (import-graph refinement) runs after all files are parsed.
        layer_hints: list[str] = parsed.get("layer_hints", [])
        role_result: RoleResult = infer_role_heuristic(
            file_path=fpath,
            classes=parsed["classes"],
            layer_hints=layer_hints,
            imports=parsed.get("imports", []),
        )
        layer = role_result.role

        pkg = parsed.get("package", "")
        parsed_files.append({
            "file_path": fpath,
            "language": Path(fpath).suffix.lstrip("."),
            "layer": layer,
            "package": pkg,
            "classes": parsed["classes"],
            "functions": parsed["functions"],
            "imports": parsed.get("imports", []),
            "imports_detailed": parsed.get("imports_detailed", []),
            "_role_result": role_result,
        })

        # Neo4j write deferred to post-graph pass (after Tier 2 refinement).
        for func_name in parsed["functions"]:
            if neo4j_available:
                try:
                    await _write_function_to_neo4j(func_name, fpath, project_id)
                except Exception:
                    logger.warning("Scan [%s]: Neo4j write failed for %s", project_id, func_name, exc_info=True)

    logger.info(
        "Scan [%s]: parse complete — files=%d nodes_created=%d rejected_entities=%d",
        project_id, len(source_files), total_nodes_created, total_rejected,
    )

    # ── Role inference Tier 2: import-graph refinement ────────────────────────
    # Build an in-memory imported_by index, then refine any non-annotation results.
    if parsed_files:
        imported_by_index = build_imported_by_index(parsed_files)
        upgraded = 0
        for pf in parsed_files:
            t1: RoleResult = pf["_role_result"]
            if t1.source == "annotation":
                # Tier 1 was definitive; no refinement needed.
                final_result = t1
            else:
                consumed_by = imported_by_index.get(pf["file_path"], [])
                final_result = refine_role_with_graph(t1, pf["file_path"], consumed_by)
                if final_result.role != t1.role or final_result.confidence > t1.confidence + 0.01:
                    upgraded += 1
            pf["_role_result"] = final_result
            pf["layer"] = final_result.role  # keep layer in sync

        if upgraded:
            logger.info(
                "Scan [%s]: Tier 2 graph refinement upgraded %d file(s)",
                project_id, upgraded,
            )

    # ── Deferred Neo4j class writes (uses final Tier 1+2 role) ───────────────
    if neo4j_available and parsed_files:
        for pf in parsed_files:
            final_result = pf.get("_role_result")
            layer = pf["layer"]
            pkg = pf.get("package", "")
            fpath = pf["file_path"]
            ext = Path(fpath).suffix
            # Extract raw annotations/superclass once per file (first 8 KB read)
            file_annotations, file_superclass, file_declared_role = \
                extract_annotations_and_superclass(fpath, ext)
            for class_name in pf["classes"]:
                try:
                    await _write_class_to_neo4j(
                        class_name, fpath, project_id, layer, pkg,
                        role_confidence=final_result.confidence if final_result else 0.0,
                        role_source=final_result.source if final_result else "default",
                        annotations=file_annotations,
                        superclass=file_superclass,
                        declared_role=file_declared_role,
                    )
                    total_nodes_created += 1
                except Exception:
                    logger.warning(
                        "Scan [%s]: Neo4j write failed for %s", project_id, class_name, exc_info=True
                    )

    # --- IMPORTS / CONTAINS edges ---
    if neo4j_available and parsed_files:
        try:
            await resolve_and_write_edges(project_id, root_path, parsed_files, neo4j_client)
        except Exception:
            logger.warning(
                "Scan [%s]: resolve_and_write_edges failed — continuing",
                project_id, exc_info=True,
            )

    # --- Qdrant file index ---
    # Index each parsed file into the project_files collection with real
    # embeddings generated by sentence-transformers (all-MiniLM-L6-v2, 384 dims).
    if qdrant_available and parsed_files:
        from qdrant_client.models import PointStruct
        import hashlib

        BATCH_SIZE = 100

        # Full scan: wipe the entire project index first so deleted/moved files
        # don't linger.  Partial scans (folder/smart) do additive upserts only —
        # wiping would erase previously scanned packages.
        if scan_mode == "full":
            try:
                from qdrant_client.models import Filter, FieldCondition, MatchValue
                await qdrant_client.client.delete(
                    collection_name=COLLECTION_FILES,
                    points_selector=Filter(
                        must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
                    ),
                )
            except Exception:
                logger.warning("Scan [%s]: Qdrant pre-delete failed — continuing with upsert", project_id, exc_info=True)

        points: list[PointStruct] = []
        for pf in parsed_files:
            # Derive a stable UUID-shaped integer id from project+path so that
            # re-scans are idempotent (same file → same point id → upsert
            # overwrites rather than duplicates).
            raw_id = hashlib.md5(f"{project_id}:{pf['file_path']}".encode()).hexdigest()
            point_id = int(raw_id[:16], 16)  # 64-bit integer from first 16 hex chars

            embedding = _generate_embedding(
                class_names=pf["classes"],
                file_path=pf["file_path"],
                layer=pf["layer"],
                methods=pf["functions"],
            )

            points.append(PointStruct(
                id=point_id,
                vector=embedding,
                payload={
                    "project_id": project_id,
                    "file_path": pf["file_path"],
                    "language": pf["language"],
                    "layer": pf["layer"],
                    "package": pf.get("package", ""),
                    "classes": pf["classes"],
                    "functions": pf["functions"],
                },
            ))

            # Flush in batches to avoid oversized payloads
            if len(points) >= BATCH_SIZE:
                try:
                    await qdrant_client.client.upsert(
                        collection_name=COLLECTION_FILES,
                        points=points,
                    )
                except Exception:
                    logger.warning("Scan [%s]: Qdrant batch upsert failed", project_id, exc_info=True)
                points = []

        # Flush remaining points
        if points:
            try:
                await qdrant_client.client.upsert(
                    collection_name=COLLECTION_FILES,
                    points=points,
                )
            except Exception:
                logger.warning("Scan [%s]: Qdrant final upsert failed", project_id, exc_info=True)

        logger.info("Scan [%s]: indexed %d files into Qdrant project_files", project_id, len(parsed_files))

    modules = _detect_modules(root_path, source_files)
    entities = _build_class_registry(parsed_files)

    # Count classes per role (used for summary_text below)
    class_role_counts: dict[str, int] = {}
    for pf in parsed_files:
        role = pf.get("layer", "Util") or "Util"
        class_role_counts[role] = class_role_counts.get(role, 0) + len(pf["classes"])

    # Collect all imports across every parsed file for the import-based
    # framework fallback in _detect_stack.
    all_imports: list[str] = []
    for pf in parsed_files:
        all_imports.extend(pf.get("imports", []))

    layer_names = {_classify_layer(c) for c in all_classes}
    if "Controller" in layer_names and "Service" in layer_names:
        arch_type = "layered"
    elif "Controller" in layer_names:
        arch_type = "mvc"
    else:
        arch_type = "unknown"

    stack = _detect_stack(root_path, all_imports=all_imports, parsed_files=parsed_files)

    # If config-file detection could not identify the language, fall back to
    # counting source file extensions so the summary never reads "unknown".
    if stack["language"] == "unknown":
        stack["language"] = _infer_language_from_extensions(source_files)

    # ── Architecture pattern detection (CodeAiPlan.md Section 9) ─────────────
    # Count how many source files belong to each architectural layer so we can
    # detect dominant patterns and automatically save rules to PostgreSQL.
    layer_file_counts: dict[str, int] = {}
    for pf in parsed_files:
        lyr = pf["layer"]
        layer_file_counts[lyr] = layer_file_counts.get(lyr, 0) + 1

    has_controller = "Controller" in layer_file_counts
    has_service = "Service" in layer_file_counts
    has_repository = "Repository" in layer_file_counts
    has_view = "View" in layer_file_counts or "Component" in layer_file_counts

    logger.info(
        "Scan [%s]: layer distribution — %s",
        project_id,
        ", ".join(f"{k}={v}" for k, v in sorted(layer_file_counts.items())),
    )

    if arch_type == "unknown" and stack["language"] != "unknown":
        arch_type = stack["language"].lower()

    logger.info(
        "Scan [%s]: stack=%s arch=%s classes=%d functions=%d",
        project_id, stack, arch_type, len(all_classes), len(all_functions),
    )

    # Build a layer summary string, e.g. "Controller=3, Service=5, Repository=2"
    layer_summary = ", ".join(
        f"{k}={v}" for k, v in sorted(layer_file_counts.items()) if k != "Util"
    )
    arch_pattern = "unknown"
    if has_controller and has_service and has_repository:
        arch_pattern = "Controller-Service-Repository (layered)"
    elif has_controller and has_service:
        arch_pattern = "Controller-Service (MVC)"
    elif has_controller:
        arch_pattern = "Controller-only (MVC)"
    elif has_view:
        arch_pattern = f"Frontend ({stack.get('framework', 'unknown')})"

    secondary_langs = stack.get("secondary_languages", [])
    secondary_lang_note = (
        f" Secondary languages also present: {', '.join(secondary_langs)}."
        if secondary_langs
        else ""
    )

    # Build per-role class count summary, e.g. "7 controllers, 12 services, 3 unclassified"
    _DISPLAY_ORDER = [
        ("Controller", "controllers"),
        ("Service",    "services"),
        ("Repository", "repositories"),
        ("Entity",     "entities"),
        ("DTO",        "DTOs"),
    ]
    class_count_parts: list[str] = []
    named_roles = {r for r, _ in _DISPLAY_ORDER}
    for role_key, role_label in _DISPLAY_ORDER:
        cnt = class_role_counts.get(role_key, 0)
        if cnt:
            class_count_parts.append(f"{cnt} {role_label}")
    unclassified_cnt = sum(v for k, v in class_role_counts.items() if k not in named_roles)
    if unclassified_cnt:
        class_count_parts.append(f"{unclassified_cnt} unclassified")

    class_count_str = (
        (", ".join(class_count_parts) + ". Roles from annotations — unclassified classes can be identified by AI during chat.")
        if class_count_parts
        else f"{len(all_classes)} classes."
    )

    summary_text = (
        f"{len(source_files)} source files. "
        f"{class_count_str} "
        f"Language: {stack['language']}. Framework: {stack['framework']}. "
        f"Build tool: {stack['build_tool']}. "
        f"Architecture pattern: {arch_pattern}."
        + secondary_lang_note
    )
    try:
        await memory_service.save_memory(
            project_id=project_id,
            summary=summary_text,
            architecture_type=arch_type,
            modules=modules,
            domain_entities=entities,
        )
        await memory_service.mark_project_scanned(project_id, files_count=len(source_files))
    except Exception:
        # Log explicitly so the cause of "not scanned" state is always visible
        # in server logs, then re-raise so the endpoint correctly returns 500.
        logger.exception(
            "Scan [%s]: FAILED to persist memory/project-status to PostgreSQL — "
            "project will appear as not-indexed to the LLM",
            project_id,
        )
        raise

    # ── Post-scan: index documentation files ─────────────────────────────────
    try:
        docs_indexed = await _index_docs(project_id, root_path, scan_mode)
    except Exception:
        logger.warning("Scan [%s]: doc indexing failed — continuing", project_id, exc_info=True)
        docs_indexed = 0

    # ── Post-scan: pattern discovery (full scan only) ─────────────────────────
    if scan_mode == "full" and neo4j_available:
        try:
            import asyncio as _asyncio
            patterns = await _asyncio.wait_for(_discover_patterns(project_id, parsed_files), timeout=30.0)
            if patterns:
                await memory_service.save_discovered_patterns(project_id, patterns)
        except _asyncio.TimeoutError:
            logger.warning("Scan [%s]: pattern discovery timed out after 30 s — skipping", project_id)
        except Exception:
            logger.warning("Scan [%s]: pattern discovery failed — continuing", project_id, exc_info=True)

    result = {"files_found": len(source_files), "classes_found": len(all_classes),
              "functions_found": len(all_functions), "modules": modules,
              "docs_indexed": docs_indexed}
    logger.info("Scan [%s]: completed — %s", project_id, result)
    return result
