"""Per-language module detection — deterministic static analysis.

Assigns each source file to a named feature module based on language conventions:
  - Java/Kotlin : package structure (first non-layer segment after base package)
  - TypeScript  : @Module() declarations (NestJS) or top-level directory
  - JavaScript  : top-level directory under src/
  - Python      : Django apps (apps.py) or top-level directory
  - Go          : directory name (package)
  - Fallback    : top-level directory name

Returns dict[file_path, module_name].

Special module names:
  _shared      — config, utilities, shared base classes
  _entrypoint  — Go cmd/ entry points
"""
from __future__ import annotations

import os
import re
from pathlib import Path


# Package/directory segments that represent architectural layers, NOT feature modules.
# Skipped when walking Java/Kotlin package paths to find the feature module name.
_LAYER_SEGMENTS: frozenset[str] = frozenset({
    "controller", "controllers",
    "service", "services",
    "repository", "repositories", "repo", "repos",
    "entity", "entities", "domain",
    "dto", "dtos", "request", "response",
    "model", "models",
    "config", "configuration",
    "exception", "exceptions", "error", "errors",
    "mapper", "mappers",
    "util", "utils", "utility", "utilities",
    "common", "shared", "lib", "library",
    "helper", "helpers",
    "middleware",
    "interceptor", "interceptors",
    "filter", "filters",
    "guard", "guards",
    "decorator", "decorators",
    "pipe", "pipes",
    "event", "events",
    "listener", "listeners",
    "scheduler", "schedulers",
    "constant", "constants",
    "interface", "interfaces",
    "type", "types",
    "enum", "enums",
    "annotation", "annotations",
    "aspect", "aspects",
})

# Top-level directories that are never feature modules regardless of language.
_SKIP_MODULE_DIRS: frozenset[str] = frozenset({
    "utils", "helpers", "common", "shared", "lib", "types", "interfaces",
    "constants", "config", "middleware", "assets", "styles", "components",
    "test", "tests", "__tests__", "spec", "migrations", "scripts", "tools",
    "hooks", "store", "redux", "context", "theme", "i18n", "locales",
})

# Go-specific skip dirs
_GO_SKIP_DIRS: frozenset[str] = frozenset({"vendor", "testdata"})


def detect_modules(
    root_path: str,
    parsed_files: list[dict],
    stack: dict,
) -> dict[str, str]:
    """Return {file_path: module_name} for every file in parsed_files.

    Dispatches to the best per-language strategy.  All unresolved files
    fall back to directory-based grouping and ultimately to '_shared'.
    """
    language = (stack.get("language") or "").lower()
    framework = (stack.get("framework") or "").lower()

    if language in ("java", "kotlin", "java/kotlin"):
        return _detect_java_modules(root_path, parsed_files)

    if language in ("typescript", "javascript"):
        if "nestjs" in framework:
            result = _detect_nestjs_modules(root_path, parsed_files)
            if result:
                return result
        return _detect_directory_modules(root_path, parsed_files)

    if language == "python":
        django_result = _detect_django_modules(root_path, parsed_files)
        if django_result:
            return django_result
        return _detect_directory_modules(root_path, parsed_files)

    if language == "go":
        return _detect_go_modules(root_path, parsed_files)

    return _detect_directory_modules(root_path, parsed_files)


# ── Java / Kotlin ─────────────────────────────────────────────────────────────

def _compute_base_package(parsed_files: list[dict]) -> str:
    """Longest common package prefix across all Java/Kotlin files."""
    packages = [
        pf["package"].split(".")
        for pf in parsed_files
        if pf.get("package") and pf.get("language") in ("java", "kt")
    ]
    if not packages:
        return ""
    base = packages[0]
    for pkg in packages[1:]:
        common: list[str] = []
        for s1, s2 in zip(base, pkg):
            if s1 == s2:
                common.append(s1)
            else:
                break
        base = common
    return ".".join(base)


def _package_to_module(package: str, base_package: str) -> str:
    """Strip base package, then return first non-layer segment."""
    if not package:
        return "_shared"
    remainder = package
    if base_package and package.startswith(base_package):
        remainder = package[len(base_package):].lstrip(".")
    if not remainder:
        return "_shared"
    for segment in remainder.split("."):
        seg_lower = segment.lower()
        if seg_lower and seg_lower not in _LAYER_SEGMENTS:
            return seg_lower
    return "_shared"


def _detect_java_modules(root_path: str, parsed_files: list[dict]) -> dict[str, str]:
    java_kt = [pf for pf in parsed_files if pf.get("language") in ("java", "kt")]
    base_pkg = _compute_base_package(java_kt)
    result: dict[str, str] = {}
    for pf in parsed_files:
        if pf.get("language") not in ("java", "kt"):
            result[pf["file_path"]] = _dir_module(root_path, pf["file_path"])
        else:
            result[pf["file_path"]] = _package_to_module(pf.get("package", ""), base_pkg)
    return result


# ── TypeScript / JavaScript (NestJS) ─────────────────────────────────────────

def _detect_nestjs_modules(root_path: str, parsed_files: list[dict]) -> dict[str, str]:
    """Parse @Module({ controllers, providers, ... }) declarations.

    Returns an empty dict if no @Module decorators are found (signals caller
    to fall back to directory-based detection).
    """
    ts_js_langs = {"ts", "tsx", "js", "jsx"}
    class_to_module: dict[str, str] = {}

    for pf in parsed_files:
        if pf.get("language") not in ts_js_langs:
            continue
        fpath = pf["file_path"]
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError:
            continue
        if not re.search(r'@Module\s*\(', src):
            continue

        # Derive module name from the *Module class name
        module_classes = [c for c in pf.get("classes", []) if c.lower().endswith("module")]
        if not module_classes:
            continue
        mod_cls = module_classes[0]
        mod_name = mod_cls[:-6].lower()  # strip "Module" suffix

        # Parse the @Module({...}) body for listed class names
        mod_match = re.search(r'@Module\s*\(\s*\{(.*?)\}\s*\)', src, re.DOTALL)
        if not mod_match:
            continue
        mod_body = mod_match.group(1)
        for arr_match in re.finditer(r'\[(.*?)\]', mod_body, re.DOTALL):
            for id_match in re.finditer(r'\b([A-Z][A-Za-z0-9_]*)\b', arr_match.group(1)):
                class_to_module[id_match.group(1)] = mod_name
        class_to_module[mod_cls] = mod_name

    if not class_to_module:
        return {}

    result: dict[str, str] = {}
    for pf in parsed_files:
        assigned: str | None = None
        for cls in pf.get("classes", []):
            if cls in class_to_module:
                assigned = class_to_module[cls]
                break
        result[pf["file_path"]] = assigned or _dir_module(root_path, pf["file_path"])
    return result


# ── Directory-based (TS/JS non-NestJS, Python non-Django, fallback) ──────────

def _dir_module(root_path: str, file_path: str) -> str:
    """Return the top-level feature directory name under root or src/."""
    root = Path(root_path)
    fpath = Path(file_path)
    try:
        rel = fpath.relative_to(root)
    except ValueError:
        return "_shared"

    parts = rel.parts
    if len(parts) <= 1:
        return "_shared"

    # Unwrap src/ wrapper if present
    top = parts[0]
    if top == "src" and len(parts) > 2:
        top = parts[1]
    elif top == "src":
        return "_shared"

    if top.lower() in _SKIP_MODULE_DIRS or top.startswith("."):
        return "_shared"
    return top.lower()


def _detect_directory_modules(root_path: str, parsed_files: list[dict]) -> dict[str, str]:
    return {pf["file_path"]: _dir_module(root_path, pf["file_path"]) for pf in parsed_files}


# ── Python / Django ───────────────────────────────────────────────────────────

def _detect_django_modules(root_path: str, parsed_files: list[dict]) -> dict[str, str]:
    """Detect Django apps by locating apps.py files.

    Returns empty dict when no apps.py files are found.
    """
    app_dirs: list[str] = []
    for dirpath, _, filenames in os.walk(root_path):
        if any(d in dirpath for d in (".git", "__pycache__", "venv", ".venv")):
            continue
        if "apps.py" in filenames:
            app_dirs.append(os.path.abspath(dirpath))

    if not app_dirs:
        return {}

    result: dict[str, str] = {}
    for pf in parsed_files:
        fabs = os.path.abspath(pf["file_path"])
        assigned = "_shared"
        for app_dir in app_dirs:
            if fabs.startswith(app_dir + os.sep) or fabs == app_dir:
                assigned = Path(app_dir).name.lower()
                break
        result[pf["file_path"]] = assigned
    return result


# ── Go ────────────────────────────────────────────────────────────────────────

def _detect_go_modules(root_path: str, parsed_files: list[dict]) -> dict[str, str]:
    """Go module = directory name.  cmd/ → _entrypoint.  internal/* → sub-dir."""
    root = Path(root_path)
    result: dict[str, str] = {}
    for pf in parsed_files:
        fpath = Path(pf["file_path"])
        try:
            rel = fpath.relative_to(root)
        except ValueError:
            result[pf["file_path"]] = "_shared"
            continue

        parts = rel.parts
        if len(parts) <= 1:
            result[pf["file_path"]] = "_shared"
            continue

        top = parts[0].lower()
        if top == "cmd":
            result[pf["file_path"]] = "_entrypoint"
        elif top == "internal":
            result[pf["file_path"]] = parts[1].lower() if len(parts) > 2 else "_shared"
        elif top in _GO_SKIP_DIRS:
            result[pf["file_path"]] = "_shared"
        else:
            result[pf["file_path"]] = top
    return result
