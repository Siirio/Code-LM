"""Import resolver: resolves import statements from parsed source files into
structured edges and writes IMPORTS/CONTAINS relationships to Neo4j.

Each language has a dedicated pure-function resolver that converts parsed
import data into a universal edge format.  The Neo4j writer batches all
edges for performance.

No imports from ``orchestrator`` or ``storage`` — only ``os``, ``logging``,
and the Neo4j client type hint.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from storage.neo4j_client import Neo4jClient

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Universal edge dict shape (documented here for reference):
#
#   {
#       "from_file": str,       # absolute path of the importing file
#       "from_name": str,       # class or module name doing the importing
#       "to_file": str | None,  # absolute path of the imported file, or None
#       "to_name": str,         # class or module name being imported
#       "import_raw": str,      # original import string as written in source
#       "language": str,        # "python" | "java" | "typescript" | "go"
#       "is_external": bool,    # True for third-party / stdlib
#   }
# ---------------------------------------------------------------------------

BATCH_SIZE = 50


# ═══════════════════════════════════════════════════════════════════════════
# Resolver 1 — Python
# ═══════════════════════════════════════════════════════════════════════════

def resolve_python(parsed_file: dict, project_root: str) -> list[dict]:
    """Resolve Python imports to universal edge dicts.

    Uses ``imports_detailed`` when available (list of dicts with ``raw``,
    ``module``, ``names`` keys).  Falls back to the plain ``imports`` list
    (module paths only) for backward compatibility.
    """
    edges: list[dict] = []
    file_path: str = parsed_file["file_path"]
    from_name = parsed_file["classes"][0] if parsed_file.get("classes") else _stem(file_path)

    detailed = parsed_file.get("imports_detailed")
    if detailed:
        for imp in detailed:
            raw: str = imp.get("raw", imp.get("module", ""))
            module_path: str = imp.get("module", "")
            names: list[str] = imp.get("names", [])

            resolved_file = _resolve_python_module(module_path, project_root)
            is_external = resolved_file is None

            if names:
                for name in names:
                    edges.append({
                        "from_file": file_path,
                        "from_name": from_name,
                        "to_file": resolved_file,
                        "to_name": name,
                        "import_raw": raw,
                        "language": "python",
                        "is_external": is_external,
                    })
            else:
                to_name = module_path.rsplit(".", 1)[-1] if module_path else "unknown"
                edges.append({
                    "from_file": file_path,
                    "from_name": from_name,
                    "to_file": resolved_file,
                    "to_name": to_name,
                    "import_raw": raw,
                    "language": "python",
                    "is_external": is_external,
                })
    else:
        # Fallback: plain imports list (module paths only)
        for module_path in parsed_file.get("imports", []):
            resolved_file = _resolve_python_module(module_path, project_root)
            is_external = resolved_file is None
            to_name = module_path.rsplit(".", 1)[-1] if module_path else "unknown"
            edges.append({
                "from_file": file_path,
                "from_name": from_name,
                "to_file": resolved_file,
                "to_name": to_name,
                "import_raw": module_path,
                "language": "python",
                "is_external": is_external,
            })

    return edges


def _resolve_python_module(module_path: str, project_root: str) -> str | None:
    """Convert a dotted module path to an absolute file path, or None."""
    if not module_path:
        return None
    rel = module_path.replace(".", "/")
    for suffix in [".py", "/__init__.py"]:
        candidate = os.path.join(project_root, rel + suffix)
        if os.path.exists(candidate):
            return candidate
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Resolver 2 — Java
# ═══════════════════════════════════════════════════════════════════════════

_JAVA_EXTERNAL_PREFIXES = (
    "java.", "javax.", "org.springframework.", "com.fasterxml.",
    "io.", "org.apache.", "lombok.",
)

# Cache: project_root -> list of discovered Java/Kotlin source roots.
# Populated lazily on first resolve_java() call for a given project_root so
# that the filesystem walk runs once per scan rather than once per file.
_java_source_roots: dict[str, list[str]] = {}


def resolve_java(parsed_file: dict, project_root: str) -> list[dict]:
    """Resolve Java imports to universal edge dicts."""
    edges: list[dict] = []
    file_path: str = parsed_file["file_path"]
    from_name = parsed_file["classes"][0] if parsed_file.get("classes") else _stem(file_path)

    source_roots = _find_java_source_roots(project_root)

    for raw_import in parsed_file.get("imports", []):
        imp = raw_import.rstrip(";").strip()
        if not imp:
            continue

        # Check external prefixes first
        if any(imp.startswith(prefix) for prefix in _JAVA_EXTERNAL_PREFIXES):
            to_name = imp.rsplit(".", 1)[-1]
            edges.append({
                "from_file": file_path,
                "from_name": from_name,
                "to_file": None,
                "to_name": to_name,
                "import_raw": raw_import,
                "language": "java",
                "is_external": True,
            })
            continue

        # Last segment is the class name
        to_name = imp.rsplit(".", 1)[-1]
        rel_path = imp.replace(".", "/") + ".java"

        resolved_file = None
        for src_root in source_roots:
            candidate = os.path.join(src_root, rel_path)
            if os.path.exists(candidate):
                resolved_file = candidate
                break

        edges.append({
            "from_file": file_path,
            "from_name": from_name,
            "to_file": resolved_file,
            "to_name": to_name,
            "import_raw": raw_import,
            "language": "java",
            "is_external": resolved_file is None,
        })

    return edges


def _find_java_source_roots(project_root: str) -> list[str]:
    """Return all Java/Kotlin source root directories for a project.

    Handles both single-module and multi-module Maven/Gradle layouts by
    walking up to depth 2 from project_root and collecting every directory
    that matches ``*/src/main/java`` or ``*/src/main/kotlin``.

    The result is cached in ``_java_source_roots`` so the filesystem walk
    runs once per project_root per process lifetime (i.e., once per scan).
    """
    if project_root in _java_source_roots:
        return _java_source_roots[project_root]

    _SRC_SUFFIXES = (
        os.path.join("src", "main", "java"),
        os.path.join("src", "main", "kotlin"),
    )

    found: list[str] = []

    # Depth 0 — project_root itself (single-module layout)
    # Depth 1 — direct children       (module-a/src/main/java, …)
    # Depth 2 — grandchildren         (group/module-a/src/main/java, …)
    search_bases = [project_root]
    try:
        for entry in os.scandir(project_root):
            if entry.is_dir(follow_symlinks=False):
                search_bases.append(entry.path)
                # depth 2
                try:
                    for sub in os.scandir(entry.path):
                        if sub.is_dir(follow_symlinks=False):
                            search_bases.append(sub.path)
                except OSError:
                    pass
    except OSError:
        pass

    for base in search_bases:
        for suffix in _SRC_SUFFIXES:
            candidate = os.path.join(base, suffix)
            if os.path.isdir(candidate) and candidate not in found:
                found.append(candidate)

    # Fallback chain for projects without a standard Maven layout: keep the
    # old behaviour so single-module non-Maven repos still resolve correctly.
    if not found:
        src = os.path.join(project_root, "src")
        if os.path.isdir(src):
            found.append(src)
        found.append(project_root)

    _java_source_roots[project_root] = found
    logger.debug(
        "Java source roots for %s: %s", project_root, found
    )
    return found


# ═══════════════════════════════════════════════════════════════════════════
# Resolver 3 — TypeScript / JavaScript
# ═══════════════════════════════════════════════════════════════════════════

_TS_JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".d.ts")
_TS_JS_INDEX_SUFFIXES = ("/index.ts", "/index.tsx", "/index.js")


def resolve_typescript(parsed_file: dict, project_root: str) -> list[dict]:
    """Resolve TypeScript/JavaScript imports to universal edge dicts.

    Uses ``imports_detailed`` when available for richer ``to_name``
    extraction.  Falls back to the plain ``imports`` list.
    """
    edges: list[dict] = []
    file_path: str = parsed_file["file_path"]
    from_name = parsed_file["classes"][0] if parsed_file.get("classes") else _stem(file_path)
    file_dir = os.path.dirname(file_path)

    detailed = parsed_file.get("imports_detailed")
    items: list[tuple[str, str, list[str]]] = []  # (raw, module_path, names)

    if detailed:
        for imp in detailed:
            items.append((
                imp.get("raw", imp.get("module", "")),
                imp.get("module", ""),
                imp.get("names", []),
            ))
    else:
        for module_path in parsed_file.get("imports", []):
            items.append((module_path, module_path, []))

    for raw, module_path, names in items:
        if not module_path:
            continue

        # Non-relative imports are external
        if not module_path.startswith("./") and not module_path.startswith("../"):
            to_name = names[0] if names else module_path.rsplit("/", 1)[-1]
            edges.append({
                "from_file": file_path,
                "from_name": from_name,
                "to_file": None,
                "to_name": to_name,
                "import_raw": raw,
                "language": "typescript",
                "is_external": True,
            })
            continue

        # Resolve relative import
        base = os.path.normpath(os.path.join(file_dir, module_path))
        resolved_file = _resolve_ts_path(base)
        to_name = names[0] if names else _stem(module_path)

        edges.append({
            "from_file": file_path,
            "from_name": from_name,
            "to_file": resolved_file,
            "to_name": to_name,
            "import_raw": raw,
            "language": "typescript",
            "is_external": resolved_file is None,
        })

    return edges


def _resolve_ts_path(base_path: str) -> str | None:
    """Try common TS/JS extensions and index files to resolve a path."""
    # If the path already has a valid extension and exists, use it
    if os.path.exists(base_path) and os.path.isfile(base_path):
        return base_path
    # Try extensions
    for ext in _TS_JS_EXTENSIONS:
        candidate = base_path + ext
        if os.path.exists(candidate):
            return candidate
    # Try index files (base_path is a directory)
    for suffix in _TS_JS_INDEX_SUFFIXES:
        candidate = base_path + suffix
        if os.path.exists(candidate):
            return candidate
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Resolver 4 — Go
# ═══════════════════════════════════════════════════════════════════════════

def resolve_go(parsed_file: dict, project_root: str) -> list[dict]:
    """Resolve Go imports to universal edge dicts."""
    edges: list[dict] = []
    file_path: str = parsed_file["file_path"]
    from_name = parsed_file["classes"][0] if parsed_file.get("classes") else _stem(file_path)

    go_module = _read_go_module(project_root)

    for raw_import in parsed_file.get("imports", []):
        imp = raw_import.strip().strip('"')
        if not imp:
            continue

        to_name = imp.rsplit("/", 1)[-1]

        # stdlib: single-word imports like "fmt", "os", "io"
        if "/" not in imp:
            edges.append({
                "from_file": file_path,
                "from_name": from_name,
                "to_file": None,
                "to_name": to_name,
                "import_raw": raw_import,
                "language": "go",
                "is_external": True,
            })
            continue

        # Internal import: starts with module name
        if go_module and imp.startswith(go_module):
            rel = imp[len(go_module):].lstrip("/")
            candidate_dir = os.path.join(project_root, rel)
            resolved_file = None
            if os.path.isdir(candidate_dir):
                # Go packages are directories; pick the first .go file
                # as a representative target
                try:
                    for fname in sorted(os.listdir(candidate_dir)):
                        if fname.endswith(".go") and not fname.endswith("_test.go"):
                            resolved_file = os.path.join(candidate_dir, fname)
                            break
                except OSError:
                    pass

            edges.append({
                "from_file": file_path,
                "from_name": from_name,
                "to_file": resolved_file,
                "to_name": to_name,
                "import_raw": raw_import,
                "language": "go",
                "is_external": resolved_file is None,
            })
            continue

        # External dependency
        edges.append({
            "from_file": file_path,
            "from_name": from_name,
            "to_file": None,
            "to_name": to_name,
            "import_raw": raw_import,
            "language": "go",
            "is_external": True,
        })

    return edges


def _read_go_module(project_root: str) -> str | None:
    """Read the module name from go.mod at project_root."""
    gomod = os.path.join(project_root, "go.mod")
    if not os.path.exists(gomod):
        return None
    try:
        with open(gomod, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("module "):
                    return line.split(None, 1)[1].strip()
    except OSError:
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _stem(file_path: str) -> str:
    """Return the filename without extension."""
    return os.path.splitext(os.path.basename(file_path))[0]


# Extension-to-language mapping for resolver dispatch
_EXT_LANGUAGE = {
    "py": "python",
    "java": "java",
    "ts": "typescript",
    "tsx": "typescript",
    "js": "typescript",
    "jsx": "typescript",
    "go": "go",
}

_RESOLVERS = {
    "python": resolve_python,
    "java": resolve_java,
    "typescript": resolve_typescript,
    "go": resolve_go,
}


# ═══════════════════════════════════════════════════════════════════════════
# Calls resolver
# ═══════════════════════════════════════════════════════════════════════════

def resolve_calls(parsed_files: list[dict]) -> list[dict]:
    """Extract call edges from parsed files.

    Returns list of call edge dicts with keys:
        from_class: str          # caller class name
        to_function: str         # callee method name
        target_obj: str | None   # object the method is called on (if known)
        file_path: str           # file where call occurs
        line: int | None         # line number
    """
    call_edges: list[dict] = []

    for pf in parsed_files:
        file_path = pf["file_path"]
        calls = pf.get("calls", [])
        if not calls:
            continue

        # Get classes defined in this file
        classes = pf.get("classes", [])
        if not classes:
            # No class in file, skip calls (they need a caller class)
            continue

        for call in calls:
            caller = call.get("caller")
            callee = call.get("callee")
            target_obj = call.get("target_obj")
            line = call.get("line")

            if not caller or not callee:
                continue

            # Ensure caller is one of the classes in this file
            if caller not in classes:
                # Caller might be a class from another file; we can't verify.
                # Still create edge, but note that caller class node may not exist.
                pass

            call_edges.append({
                "from_class": caller,
                "to_function": callee,
                "target_obj": target_obj,
                "file_path": file_path,
                "line": line,
            })

    return call_edges


def resolve_extends(parsed_files: list[dict]) -> list[dict]:
    """Extract extends/implements edges from parsed files.

    Returns list of extends edge dicts with keys:
        from_class: str          # subclass class name
        to_class: str            # superclass name
        file_path: str           # file where subclass is defined
    """
    extends_edges: list[dict] = []

    for pf in parsed_files:
        file_path = pf["file_path"]
        classes = pf.get("classes", [])
        if not classes:
            continue

        # Get superclass from parsed file (stored as "_superclass")
        superclass = pf.get("_superclass")
        if not superclass:
            continue

        # For each class in file, assume first class is the one with the superclass?
        # In most languages, only the first class has explicit superclass.
        # We'll associate superclass with the first class in the file.
        subclass = classes[0]

        extends_edges.append({
            "from_class": subclass,
            "to_class": superclass,
            "file_path": file_path,
        })

    return extends_edges


# ═══════════════════════════════════════════════════════════════════════════
# Neo4j edge writer
# ═══════════════════════════════════════════════════════════════════════════

async def _write_imports_batch(
    neo4j: "Neo4jClient",
    project_id: str,
    edges: list[dict],
) -> int:
    """Write a batch of IMPORTS edges via UNWIND.  Returns count written."""
    # Filter to internal-only edges with a resolved target file
    internal = [
        e for e in edges
        if not e["is_external"] and e["to_file"] is not None
    ]
    if not internal:
        return 0

    written = 0
    for i in range(0, len(internal), BATCH_SIZE):
        batch = internal[i : i + BATCH_SIZE]
        params = {
            "project_id": project_id,
            "edges": [
                {
                    "from_name": e["from_name"],
                    "to_name": e["to_name"],
                    "from_file": e["from_file"],
                    "import_raw": e["import_raw"],
                }
                for e in batch
            ],
        }
        cypher = (
            "UNWIND $edges AS e "
            "MERGE (a:Class {name: e.from_name, project_id: $project_id}) "
            "MERGE (b:Class {name: e.to_name, project_id: $project_id}) "
            "MERGE (a)-[r:IMPORTS {file: e.from_file, import_raw: e.import_raw}]->(b)"
        )
        await neo4j.execute(cypher, params)
        written += len(batch)

    return written


async def _write_contains_batch(
    neo4j: "Neo4jClient",
    project_id: str,
    parsed_files: list[dict],
) -> int:
    """Write CONTAINS edges (Class->Function) via UNWIND.  Returns count written."""
    pairs: list[dict] = []
    for pf in parsed_files:
        classes = pf.get("classes", [])
        functions = pf.get("functions", [])
        file_path = pf["file_path"]
        if not classes or not functions:
            continue
        # Associate all functions with the first class in the file
        class_name = classes[0]
        for func_name in functions:
            pairs.append({
                "class_name": class_name,
                "func_name": func_name,
                "file_path": file_path,
            })

    if not pairs:
        return 0

    written = 0
    for i in range(0, len(pairs), BATCH_SIZE):
        batch = pairs[i : i + BATCH_SIZE]
        params = {
            "project_id": project_id,
            "pairs": batch,
        }
        cypher = (
            "UNWIND $pairs AS p "
            "MERGE (c:Class {name: p.class_name, project_id: $project_id}) "
            "MERGE (f:Function {name: p.func_name, file_path: p.file_path, project_id: $project_id}) "
            "MERGE (c)-[:CONTAINS]->(f)"
        )
        await neo4j.execute(cypher, params)
        written += len(batch)

    return written


async def _write_calls_batch(
    neo4j: "Neo4jClient",
    project_id: str,
    call_edges: list[dict],
) -> int:
    """Write CALLS edges (Class->Function) via UNWIND.  Returns count written."""
    if not call_edges:
        return 0

    written = 0
    for i in range(0, len(call_edges), BATCH_SIZE):
        batch = call_edges[i : i + BATCH_SIZE]
        params = {
            "project_id": project_id,
            "edges": [
                {
                    "from_class": e["from_class"],
                    "to_function": e["to_function"],
                    "target_obj": e.get("target_obj"),
                    "file_path": e["file_path"],
                    "line": e.get("line"),
                }
                for e in batch
            ],
        }
        cypher = (
            "UNWIND $edges AS e "
            "MERGE (c:Class {name: e.from_class, project_id: $project_id}) "
            "MERGE (f:Function {name: e.to_function, file_path: e.file_path, project_id: $project_id}) "
            "MERGE (c)-[r:CALLS {target_obj: e.target_obj, line: e.line}]->(f)"
        )
        await neo4j.execute(cypher, params)
        written += len(batch)

    return written


async def _write_extends_batch(
    neo4j: "Neo4jClient",
    project_id: str,
    extends_edges: list[dict],
) -> int:
    """Write EXTENDS edges (Class->Class) via UNWIND.  Returns count written."""
    if not extends_edges:
        return 0

    written = 0
    for i in range(0, len(extends_edges), BATCH_SIZE):
        batch = extends_edges[i : i + BATCH_SIZE]
        params = {
            "project_id": project_id,
            "edges": [
                {
                    "from_class": e["from_class"],
                    "to_class": e["to_class"],
                    "file_path": e["file_path"],
                }
                for e in batch
            ],
        }
        cypher = (
            "UNWIND $edges AS e "
            "MERGE (c:Class {name: e.from_class, project_id: $project_id}) "
            "MERGE (s:Class {name: e.to_class, project_id: $project_id}) "
            "MERGE (c)-[:EXTENDS {file: e.file_path}]->(s)"
        )
        await neo4j.execute(cypher, params)
        written += len(batch)

    return written


# ═══════════════════════════════════════════════════════════════════════════
# Public entry point — called from scan_project()
# ═══════════════════════════════════════════════════════════════════════════

async def resolve_and_write_edges(
    project_id: str,
    project_root: str,
    parsed_files: list[dict],
    neo4j: "Neo4jClient",
) -> tuple[int, int, int, int]:
    """Resolve all imports across parsed files and write edges to Neo4j.

    Returns (imports_written, contains_written, calls_written, extends_written).
    """
    all_edges: list[dict] = []

    for pf in parsed_files:
        ext = pf.get("language", "")  # e.g. "py", "ts", "java", "go"
        lang = _EXT_LANGUAGE.get(ext)
        if lang is None:
            continue
        resolver = _RESOLVERS.get(lang)
        if resolver is None:
            continue
        try:
            edges = resolver(pf, project_root)
            all_edges.extend(edges)
        except Exception:
            logger.warning(
                "Scan [%s]: import resolver failed for %s — skipping",
                project_id, pf.get("file_path", "?"), exc_info=True,
            )

    imports_written = 0
    contains_written = 0
    calls_written = 0
    extends_written = 0

    try:
        imports_written = await _write_imports_batch(neo4j, project_id, all_edges)
    except Exception:
        logger.warning(
            "Scan [%s]: failed to write IMPORTS edges to Neo4j",
            project_id, exc_info=True,
        )

    try:
        contains_written = await _write_contains_batch(neo4j, project_id, parsed_files)
    except Exception:
        logger.warning(
            "Scan [%s]: failed to write CONTAINS edges to Neo4j",
            project_id, exc_info=True,
        )

    # Resolve and write CALLS edges
    try:
        call_edges = resolve_calls(parsed_files)
        calls_written = await _write_calls_batch(neo4j, project_id, call_edges)
    except Exception:
        logger.warning(
            "Scan [%s]: failed to write CALLS edges to Neo4j",
            project_id, exc_info=True,
        )

    # Resolve and write EXTENDS edges
    try:
        extends_edges = resolve_extends(parsed_files)
        extends_written = await _write_extends_batch(neo4j, project_id, extends_edges)
    except Exception:
        logger.warning(
            "Scan [%s]: failed to write EXTENDS edges to Neo4j",
            project_id, exc_info=True,
        )

    logger.info(
        "Scan [%s]: wrote %d IMPORTS edges, %d CONTAINS edges, %d CALLS edges, %d EXTENDS edges",
        project_id, imports_written, contains_written, calls_written, extends_written,
    )

    return imports_written, contains_written, calls_written, extends_written
