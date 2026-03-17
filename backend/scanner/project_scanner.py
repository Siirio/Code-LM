"""Project scanner: walks a codebase, parses source files (Python via ast,
TypeScript/JavaScript via regex), writes class/function nodes to Neo4j,
and saves a project memory summary to PostgreSQL.
"""
import ast
import logging
import os
import re
from pathlib import Path

from storage.neo4j_client import neo4j_client
from storage import memory_service
from storage.qdrant_client import qdrant_client, COLLECTION_FILES, EMBEDDING_DIM
from scanner.import_resolver import resolve_and_write_edges

logger = logging.getLogger(__name__)

SOURCE_EXTENSIONS: set[str] = {".py", ".ts", ".tsx", ".js", ".jsx", ".kt", ".java", ".go"}
SKIP_DIRS: set[str] = {"node_modules", "venv", ".venv", ".git", "__pycache__", "build", "dist", "target",
                       ".claude", "memory", "worktrees"}

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
    """Convert Windows drive paths to WSL mount paths when running under WSL.
    e.g. C:/Users/foo  →  /mnt/c/Users/foo
         D:\\Projects   →  /mnt/d/Projects
    Non-Windows paths are returned unchanged.
    """
    import re
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

    # Prevent the scanner from indexing its own backend directory.
    # __file__ is  .../backend/scanner/project_scanner.py
    # backend_dir  is  .../backend
    backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # If backend_dir lives inside the scanned project, collect its absolute
    # path so we can prune it during the walk.
    backend_dir_to_skip: str | None = (
        backend_dir if backend_dir.startswith(root_path) else None
    )

    found: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root_path):
        # Skip well-known noise directories (by basename).
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        # Skip the backend directory itself when the project root is a parent.
        if backend_dir_to_skip:
            dirnames[:] = [
                d for d in dirnames
                if os.path.abspath(os.path.join(dirpath, d)) != backend_dir_to_skip
            ]
        for fname in filenames:
            if Path(fname).suffix in SOURCE_EXTENSIONS:
                found.append(os.path.join(dirpath, fname))
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


def _parse_java_file(file_path: str) -> dict:
    """Extract classes, interfaces, enums, records, and public methods from a Java file
    using regex patterns.  Returns the same shape as _parse_python_file.

    Handles:
      - class / abstract class / final class
      - interface
      - enum
      - record (Java 16+)
      - public/protected/private methods of the form:
            [annotations] [modifiers] ReturnType methodName(
    Imports are collected from 'import com.example.Foo;' style lines.
    """
    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
        source = f.read()

    # Strip line comments (// ...) and block comments (/* ... */) so that words
    # inside comment text don't produce false-positive matches.
    source_no_comments = re.sub(r'//[^\n]*', '', source)
    source_no_comments = re.sub(r'/\*.*?\*/', '', source_no_comments, flags=re.DOTALL)

    classes: list[str] = []
    functions: list[str] = []
    imports: list[str] = []

    # Type declarations: class, interface, enum, record
    # Matches optional modifiers (public, abstract, final, static) before the keyword.
    for m in re.finditer(
        r'(?:(?:public|protected|private|abstract|final|static)\s+)*'
        r'(?:class|interface|enum|record)\s+([A-Za-z_$][A-Za-z0-9_$]*)',
        source_no_comments,
    ):
        classes.append(m.group(1))

    # Method declarations: capture the method name just before the opening parenthesis.
    # Requires at least one visibility or modifier keyword so that bare variable
    # declarations (int clampedDays = ...) and annotation-only lines are not matched.
    # Matches lines like:
    #   public DashboardResponse getDashboard(
    #   private Merchant requireMerchant(
    #   public static void main(
    #   @Override public List<Foo> getProfitByProduct(
    _JAVA_KEYWORDS = {
        "if", "for", "while", "switch", "catch", "return", "new",
        "throw", "assert", "else", "do", "try", "finally", "synchronized",
        "instanceof", "import", "package", "class", "interface", "enum", "record",
    }
    for m in re.finditer(
        r'(?:@\w+\s+)*'                          # optional leading annotations
        r'(?:public|protected|private|static|final|abstract|synchronized|native|default|override)'
        r'(?:\s+(?:public|protected|private|static|final|abstract|synchronized|native|default|override))*'
        r'\s+[\w<>\[\],\s]+?\s+'                 # return type (non-greedy)
        r'([a-z_$][A-Za-z0-9_$]*)\s*\(',        # method name starts with lowercase
        source_no_comments,
    ):
        name = m.group(1)
        if name not in _JAVA_KEYWORDS and name not in functions:
            functions.append(name)

    # Import statements: import com.example.foo.Bar;
    for m in re.finditer(r'^\s*import\s+(?:static\s+)?([\w.]+)\s*;', source, re.MULTILINE):
        # Store the top-level package prefix (e.g. 'com.example.foo') for grouping
        imports.append(m.group(1))

    return {"classes": classes, "functions": functions, "imports": imports}


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


async def _write_class_to_neo4j(class_name: str, file_path: str, project_id: str, layer: str) -> None:
    cypher = (
        "MERGE (c:Class {name: $name, project_id: $project_id}) "
        "SET c.file_path = $file_path, c.layer = $layer"
    )
    await neo4j_client.execute(cypher, {"name": class_name, "project_id": project_id,
                                        "file_path": file_path, "layer": layer})


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


def _detect_entities(all_classes: list[str]) -> list[str]:
    entity_suffixes = ("Model", "Entity", "Schema", "DTO", "Dto")
    return sorted({name for name in all_classes if any(name.endswith(s) for s in entity_suffixes)})


def _find_build_files(root_path: str, filename: str, max_depth: int = 2) -> list[str]:
    """Walk up to max_depth directory levels under root_path and collect all
    occurrences of filename, sorted shallowest-first (root wins).

    Returns a list of absolute paths, never raises.
    """
    found: list[tuple[int, str]] = []
    root = Path(root_path)
    try:
        for dirpath, dirnames, filenames in os.walk(root_path):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
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

    neo4j_available = neo4j_client.is_connected
    if not neo4j_available:
        logger.warning("Scan [%s]: Neo4j not connected — skipping graph writes", project_id)

    qdrant_available = qdrant_client.is_connected
    if not qdrant_available:
        logger.warning("Scan [%s]: Qdrant not connected — skipping file index writes", project_id)

    all_classes, all_functions = [], []
    # Accumulate per-file parsed data for Qdrant indexing
    parsed_files: list[dict] = []

    for i, fpath in enumerate(source_files):
        ext = Path(fpath).suffix
        if ext not in (".py",) and ext not in _TS_JS_EXTENSIONS and ext not in _JAVA_EXTENSIONS:
            continue
        if i % 50 == 0:
            logger.info("Scan [%s]: parsing file %d/%d", project_id, i + 1, len(source_files))
        try:
            if ext == ".py":
                parsed = _parse_python_file(fpath)
            elif ext in _JAVA_EXTENSIONS:
                parsed = _parse_java_file(fpath)
            else:
                parsed = _parse_ts_js_file(fpath)
        except SyntaxError:
            logger.warning("Scan [%s]: syntax error in %s — skipping", project_id, fpath)
            continue
        except Exception:
            logger.warning("Scan [%s]: failed to parse %s — skipping", project_id, fpath, exc_info=True)
            continue

        all_classes.extend(parsed["classes"])
        all_functions.extend(parsed["functions"])

        # Determine layer for this file.
        # Priority: (1) path-based hint, (2) content-based hint from parser
        # (layer_hints field, only populated by _parse_ts_js_file),
        # (3) class-name suffix rule, (4) default "Util".
        layer_hints: list[str] = parsed.get("layer_hints", [])
        path_layer = _classify_layer_from_path(fpath)
        if path_layer:
            layer = path_layer
        elif layer_hints:
            layer = layer_hints[0]
        elif parsed["classes"]:
            layer = _classify_layer(parsed["classes"][0])
        else:
            layer = "Util"

        parsed_files.append({
            "file_path": fpath,
            "language": Path(fpath).suffix.lstrip("."),
            "layer": layer,
            "classes": parsed["classes"],
            "functions": parsed["functions"],
            "imports": parsed.get("imports", []),
            "imports_detailed": parsed.get("imports_detailed", []),
        })

        if neo4j_available:
            for class_name in parsed["classes"]:
                try:
                    cls_layer = _classify_layer_from_path(fpath) or _classify_layer(class_name)
                    await _write_class_to_neo4j(class_name, fpath, project_id, cls_layer)
                except Exception:
                    logger.warning("Scan [%s]: Neo4j write failed for %s", project_id, class_name, exc_info=True)
            for func_name in parsed["functions"]:
                try:
                    await _write_function_to_neo4j(func_name, fpath, project_id)
                except Exception:
                    logger.warning("Scan [%s]: Neo4j write failed for %s", project_id, func_name, exc_info=True)

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
    # Index each parsed file into the project_files collection so that
    # search_files can do payload-based filtering even before real embeddings
    # are available.  A zero vector of dimension 384 is used as a placeholder;
    # this will be replaced with real embeddings when an embedding model is
    # configured (see CodeAiPlan.md Phase 2).
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

            points.append(PointStruct(
                id=point_id,
                vector=[0.0] * EMBEDDING_DIM,
                payload={
                    "project_id": project_id,
                    "file_path": pf["file_path"],
                    "language": pf["language"],
                    "layer": pf["layer"],
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
    entities = _detect_entities(all_classes)

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

    # ── Auto-save architecture rules ─────────────────────────────────────────
    # Always save AT LEAST one rule regardless of how many layers were found.
    # Rules are chosen by specificity: full layered > MVC > frontend > script.
    rules_saved = 0

    async def _try_add_rule(name: str, description: str, severity: str = "error") -> None:
        nonlocal rules_saved
        try:
            await memory_service.add_rule(
                project_id=project_id,
                name=name,
                description=description,
                severity=severity,
            )
            logger.info("Scan [%s]: saved auto-detected rule: %s", project_id, name)
            rules_saved += 1
        except Exception:
            logger.warning(
                "Scan [%s]: failed to save rule '%s'", project_id, name, exc_info=True
            )

    if has_controller and has_service and has_repository:
        # Full layered architecture: Controller → Service → Repository
        await _try_add_rule(
            "No direct Controller→Repository calls",
            "Controllers should not call Repositories directly; "
            "all data access must go through the Service layer.",
        )
        await _try_add_rule(
            "Business logic in Service layer",
            "Business logic belongs in Service layer, not Controllers. "
            "Controllers should only handle request/response translation.",
        )
    elif has_controller and has_service:
        # MVC / two-tier layered
        await _try_add_rule(
            "Business logic in Service layer",
            "Business logic belongs in Service layer, not Controllers. "
            "Controllers should only handle request/response translation.",
        )
    elif has_view and not has_controller and not has_service:
        # Pure frontend / React / Vue / Svelte project
        framework_name = stack.get("framework", "the framework")
        await _try_add_rule(
            "Components should not contain business logic",
            f"Keep {framework_name} components focused on rendering. "
            "Extract business logic and side effects into custom hooks, "
            "services, or state management modules.",
            severity="warning",
        )
    elif has_controller and not has_service:
        # Controller-only — thin routing layer or script-style web app
        await _try_add_rule(
            "Keep functions under 50 lines",
            "Route handlers are growing large. Extract logic into dedicated "
            "service functions to improve testability and readability.",
            severity="warning",
        )

    # Fallback: if no rules were saved yet (single-file project, pure scripts,
    # utilities, or any other project with no clear layering), save generic rules.
    if rules_saved == 0:
        # Determine if this looks like a frontend project by framework name
        fw = stack.get("framework", "unknown")
        if fw in ("React", "Vue.js", "Angular", "Svelte", "Remix", "Next.js"):
            await _try_add_rule(
                "Components should not contain business logic",
                f"Keep {fw} components focused on rendering. "
                "Extract business logic and side effects into custom hooks or services.",
                severity="warning",
            )
        else:
            # Generic script / utility / unknown project
            await _try_add_rule(
                "Follow single-responsibility principle",
                "Each module, class, and function should have exactly one reason "
                "to change. Keep functions under 50 lines and avoid global state.",
                severity="warning",
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
    summary_text = (
        f"Language: {stack['language']}. Framework: {stack['framework']}. "
        f"Build tool: {stack['build_tool']}. "
        f"{len(source_files)} source files across {len(modules)} modules. "
        f"{len(all_classes)} classes, {len(all_functions)} functions. "
        f"Architecture pattern: {arch_pattern}. "
        + (f"Layers detected: {layer_summary}. " if layer_summary else "")
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

    result = {"files_found": len(source_files), "classes_found": len(all_classes),
              "functions_found": len(all_functions), "modules": modules}
    logger.info("Scan [%s]: completed — %s", project_id, result)
    return result
