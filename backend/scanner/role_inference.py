"""Role inference engine for CodeLM scanner.

Three-tier approach:
  Tier 1 — Pure heuristics (no AI):
    1a. Framework annotations / decorators  →  declared_role, confidence 1.0
    1b. Folder path segments               →  confidence 0.85
    1c. Class-name suffix rules            →  confidence 0.75
    1d. Content signals (routes, hooks…)   →  confidence 0.70

  Tier 2 — In-memory import-graph heuristics (no AI):
    Uses the imported_by index built from parsed_files after the parse loop.
    Upgrades or downgrades confidence based on who consumes the file.

  Tier 3 — LLM-based (reserved, not yet implemented):
    Haiku  → medium-confidence / single-file ambiguity
    Sonnet → complex cross-file inference

Usage:
    result = infer_role_heuristic(file_path, classes, layer_hints, imports)
    result = refine_role_with_graph(result, file_path, imported_by_paths)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RoleResult:
    role: str           # "Service" | "Controller" | "Repository" | "Entity" | "DTO" | "View" | "Util"
    confidence: float   # 0.0–1.0
    source: str         # "annotation" | "path" | "name_suffix" | "content" | "import_graph" | "default"
    ambiguity: str      # "none" | "low" | "medium" | "high"
    reasoning: str      # human-readable explanation; useful for LLM context and debugging


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1a — Annotation / decorator detection
# ─────────────────────────────────────────────────────────────────────────────

# Maps annotation name (without @) → (role, confidence).
# Order matters: more specific entries should come first where names overlap.
_ANNOTATION_MAP: dict[str, tuple[str, float]] = {
    # Spring (Java / Kotlin)
    "RestController":       ("Controller",  1.0),
    "Controller":           ("Controller",  1.0),
    "Service":              ("Service",     1.0),
    "Repository":           ("Repository",  1.0),
    "Mapper":               ("Repository",  0.95),  # MyBatis / MapStruct
    "Entity":               ("Entity",      1.0),
    "Table":                ("Entity",      0.95),
    "Document":             ("Entity",      0.95),  # Spring Data MongoDB
    "Component":            ("Util",        0.90),
    "Configuration":        ("Util",        0.85),
    "ConfigurationProperties": ("DTO",      0.80),
    # NestJS (TypeScript)
    "Injectable":           ("Service",     1.0),
    "Module":               ("Util",        0.85),
    # TypeORM / MikroORM (TypeScript)
    "Entity":               ("Entity",      1.0),
    # FastAPI (Python — not annotations but decorator patterns, handled separately)
}

# Java/Kotlin: class-level annotations appear as @Foo or @Foo(...)
_JAVA_ANNOTATION_RE = re.compile(
    r"@(" + "|".join(re.escape(k) for k in _ANNOTATION_MAP) + r")\b",
)

# TypeScript/JavaScript: decorators appear as @Foo or @Foo(...)
_TS_DECORATOR_RE = re.compile(
    r"@(" + "|".join(re.escape(k) for k in _ANNOTATION_MAP) + r")\s*[\(\n]",
)

# Python FastAPI: detect APIRouter or app.get/post → Controller
_PYTHON_ROUTER_RE = re.compile(
    r"(?:APIRouter\s*\(|(?:app|router)\s*\.\s*(?:get|post|put|delete|patch|use)\s*\()"
)


def _detect_annotations(file_path: str, extension: str) -> tuple[str, float] | None:
    """Read the first 6 KB of *file_path* and look for known annotations/decorators.

    Returns (role, confidence) on first match, or None if nothing recognised.
    Only reads once; annotations are always near the top of a file.
    """
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            head = fh.read(6144)
    except OSError:
        return None

    ext = extension.lower()

    if ext in (".java", ".kt"):
        m = _JAVA_ANNOTATION_RE.search(head)
        if m:
            ann = m.group(1)
            return _ANNOTATION_MAP.get(ann)

    elif ext in (".ts", ".tsx", ".js", ".jsx"):
        m = _TS_DECORATOR_RE.search(head)
        if m:
            ann = m.group(1)
            return _ANNOTATION_MAP.get(ann)
        # NestJS @Controller() with a path string: @Controller('users')
        if re.search(r"@Controller\s*\(", head):
            return ("Controller", 1.0)
        if re.search(r"@Injectable\s*\(", head):
            return ("Service", 1.0)

    elif ext == ".py":
        if _PYTHON_ROUTER_RE.search(head):
            return ("Controller", 0.90)

    return None


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1b — Path-segment rules
# ─────────────────────────────────────────────────────────────────────────────

_PATH_RULES: list[tuple[tuple[str, ...], str, float]] = [
    # (path segments to check, role, confidence)
    (("/controllers/", "/controller/"),                  "Controller",  0.85),
    (("/routes/", "/route/", "/routers/", "/router/"),   "Controller",  0.85),
    (("/services/", "/service/"),                        "Service",     0.85),
    (("/repositories/", "/repository/", "/repos/"),      "Repository",  0.85),
    (("/entities/", "/entity/"),                         "Entity",      0.85),
    (("/models/", "/model/"),                            "Entity",      0.80),
    (("/dto/", "/dtos/", "/request/", "/response/"),     "DTO",         0.85),
    (("/components/", "/pages/", "/views/", "/screens/"),"View",        0.85),
    (("/hooks/", "/hook/"),                              "Util",        0.80),
    (("/utils/", "/util/", "/helpers/", "/shared/"),     "Util",        0.80),
    (("/store/", "/redux/", "/context/", "/state/"),     "Service",     0.80),
    (("/middleware/",),                                  "Util",        0.80),
]


def _classify_by_path(file_path: str) -> tuple[str, float] | None:
    """Return (role, confidence) if the file path contains a recognised segment."""
    lowered = file_path.replace("\\", "/").lower()
    for segments, role, confidence in _PATH_RULES:
        for seg in segments:
            if seg in lowered:
                return role, confidence
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1c — Class / file name suffix rules
# ─────────────────────────────────────────────────────────────────────────────

_NAME_SUFFIX_RULES: list[tuple[tuple[str, ...], str, float]] = [
    (("Controller", "Router", "Handler", "Resolver"), "Controller",  0.75),
    (("Service",),                                    "Service",     0.75),
    (("Repository", "Repo", "Store"),                 "Repository",  0.75),
    (("Entity", "Model"),                             "Entity",      0.70),
    (("Schema",),                                     "Entity",      0.65),
    (("DTO", "Dto", "Request", "Response"),           "DTO",         0.70),
    (("Mapper", "Converter"),                         "Util",        0.65),
    (("Config", "Configuration", "Settings"),         "Util",        0.70),
    (("Test", "Spec", "Mock", "Stub", "Fixture"),     "Util",        0.80),
]


def _classify_by_name(name: str) -> tuple[str, float] | None:
    """Return (role, confidence) if *name* ends with a recognised suffix."""
    for suffixes, role, confidence in _NAME_SUFFIX_RULES:
        for suffix in suffixes:
            if name.endswith(suffix):
                return role, confidence
    return None


def _stem_role(file_path: str) -> tuple[str, float] | None:
    """Apply suffix rules to the file stem (e.g. userService → Service)."""
    stem = Path(file_path).stem  # preserve case
    return _classify_by_name(stem)


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1d — Content / import signals
# ─────────────────────────────────────────────────────────────────────────────

# Import module patterns that hint at the role of the *importing* file.
# E.g. if a TS file imports from 'typeorm' it's likely an Entity or Repository.
_IMPORT_SIGNALS: list[tuple[set[str], str, float]] = [
    # DB / ORM imports → likely Repository or Service with direct DB access
    ({"typeorm", "sequelize", "mongoose", "prisma", "knex"}, "Repository", 0.65),
    # Testing imports
    ({"jest", "mocha", "pytest", "unittest", "vitest", "@testing-library"}, "Util", 0.75),
    # HTTP/routing imports
    ({"express", "fastapi", "flask", "django", "koa", "hapi"}, "Controller", 0.65),
]


def _classify_by_imports(imports: Sequence[str]) -> tuple[str, float] | None:
    """Return (role, confidence) based on what a file imports."""
    import_set = {imp.lower().split("/")[0].split(".")[0] for imp in imports}
    for signal_set, role, confidence in _IMPORT_SIGNALS:
        if import_set & signal_set:
            return role, confidence
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Tier 1 — Combined heuristic entry point
# ─────────────────────────────────────────────────────────────────────────────

def infer_role_heuristic(
    file_path: str,
    classes: Sequence[str],
    layer_hints: Sequence[str],
    imports: Sequence[str],
) -> RoleResult:
    """Return a RoleResult using only local (per-file) signals.

    Priority:
      1a. Framework annotation/decorator (confidence 0.85–1.0)
      1b. Folder path segment            (confidence 0.80–0.85)
      1c. Class / file-stem name suffix  (confidence 0.65–0.75)
      1d. Parser layer_hints             (confidence 0.70)
      1e. Import-signal hints            (confidence 0.65)
      fallback: "Util", confidence 0.40, ambiguity "high"
    """
    ext = Path(file_path).suffix

    # ── 1a: Annotation ────────────────────────────────────────────────────────
    ann = _detect_annotations(file_path, ext)
    if ann:
        role, conf = ann
        return RoleResult(
            role=role,
            confidence=conf,
            source="annotation",
            ambiguity="none",
            reasoning=f"Framework annotation/decorator declares role={role} (confidence={conf:.2f}).",
        )

    # ── 1b: Path ──────────────────────────────────────────────────────────────
    path_hit = _classify_by_path(file_path)
    if path_hit:
        role, conf = path_hit
        return RoleResult(
            role=role,
            confidence=conf,
            source="path",
            ambiguity="low",
            reasoning=f"File path segment matches {role!r} convention (confidence={conf:.2f}).",
        )

    # ── 1c: Class-name suffix (prefer first class, then file stem) ────────────
    name_hit: tuple[str, float] | None = None
    name_source = ""
    for cls in classes:
        hit = _classify_by_name(cls)
        if hit:
            name_hit = hit
            name_source = f"class name '{cls}'"
            break
    if not name_hit:
        hit = _stem_role(file_path)
        if hit:
            name_hit = hit
            name_source = f"file stem '{Path(file_path).stem}'"

    if name_hit:
        role, conf = name_hit
        ambiguity = "low" if conf >= 0.75 else "medium"
        return RoleResult(
            role=role,
            confidence=conf,
            source="name_suffix",
            ambiguity=ambiguity,
            reasoning=f"{name_source} suffix infers role={role!r} (confidence={conf:.2f}). "
                      "No annotation or path signal to confirm.",
        )

    # ── 1d: Parser layer_hints (React JSX, Express routes, etc.) ─────────────
    if layer_hints:
        hint_role = layer_hints[0]
        return RoleResult(
            role=hint_role,
            confidence=0.70,
            source="content",
            ambiguity="low",
            reasoning=f"Parser detected content pattern implying role={hint_role!r} "
                      "(confidence=0.70, no annotation or path match).",
        )

    # ── 1e: Import signal ─────────────────────────────────────────────────────
    imp_hit = _classify_by_imports(imports)
    if imp_hit:
        role, conf = imp_hit
        return RoleResult(
            role=role,
            confidence=conf,
            source="content",
            ambiguity="medium",
            reasoning=f"Import list hints at role={role!r} (confidence={conf:.2f}). "
                      "Ambiguous — imports alone are not definitive.",
        )

    # ── fallback ──────────────────────────────────────────────────────────────
    return RoleResult(
        role="Util",
        confidence=0.40,
        source="default",
        ambiguity="high",
        reasoning="No annotation, path, name, or content signal matched. Defaulting to Util.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Tier 2 — Import-graph refinement (in-memory, no Neo4j call needed)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_role_from_filename(path: str) -> str | None:
    """Return a rough role label from a file name (used for imported_by analysis)."""
    stem = Path(path).stem
    hit = _classify_by_name(stem)
    if hit:
        return hit[0]
    hit = _classify_by_path(path)
    if hit:
        return hit[0]
    return None


def refine_role_with_graph(
    result: RoleResult,
    file_path: str,
    imported_by_paths: Sequence[str],
) -> RoleResult:
    """Refine *result* using the set of files that import *file_path*.

    Rules applied:
      - 2+ controller files import this file → Service signal
      - Only test files import this file     → Util / test helper
      - No files import this file at all     → possible DTO / leaf / dead code

    Only upgrades confidence when evidence is strong; never downgrades below
    the Tier 1 result to avoid false demotion.
    """
    if not imported_by_paths:
        # Leaf node — not consumed by anything in-project.
        if result.ambiguity in ("medium", "high"):
            new_reasoning = (
                result.reasoning
                + " No in-project consumers found (leaf node) — possibly DTO, config, or dead code."
            )
            return RoleResult(
                role=result.role,
                confidence=max(result.confidence - 0.05, 0.30),
                source=result.source,
                ambiguity="medium" if result.ambiguity == "high" else result.ambiguity,
                reasoning=new_reasoning,
            )
        return result

    consumer_roles: list[str] = []
    for path in imported_by_paths:
        r = _extract_role_from_filename(path)
        if r:
            consumer_roles.append(r)

    controller_consumers = consumer_roles.count("Controller")
    test_consumers = sum(
        1 for p in imported_by_paths
        if any(t in Path(p).stem.lower() for t in ("test", "spec", "mock", "stub"))
    )
    total = len(imported_by_paths)
    non_test_consumers = total - test_consumers

    # ── Signal: consumed by multiple controllers ───────────────────────────────
    if controller_consumers >= 2:
        graph_confidence = min(0.80 + 0.04 * (controller_consumers - 2), 0.95)
        new_role = result.role if result.role == "Service" else "Service"
        new_conf = max(result.confidence, graph_confidence)
        new_source = result.source if result.source == "annotation" else "import_graph"
        reasoning = (
            result.reasoning
            + f" {controller_consumers} controller(s) import this file — "
            f"strong Service signal from import graph (confidence boosted to {new_conf:.2f})."
        )
        return RoleResult(
            role=new_role,
            confidence=new_conf,
            source=new_source,
            ambiguity="low" if new_conf >= 0.75 else "medium",
            reasoning=reasoning,
        )

    # ── Signal: one controller consumes it ────────────────────────────────────
    if controller_consumers == 1 and result.role in ("Service", "Util", "Repository"):
        new_conf = min(result.confidence + 0.08, 0.88)
        reasoning = (
            result.reasoning
            + f" 1 controller imports this file — moderate Service signal "
            f"(confidence boosted to {new_conf:.2f})."
        )
        return RoleResult(
            role="Service",
            confidence=new_conf,
            source="import_graph" if result.source != "annotation" else result.source,
            ambiguity="low",
            reasoning=reasoning,
        )

    # ── Signal: only test files consume it ────────────────────────────────────
    if non_test_consumers == 0 and test_consumers > 0:
        reasoning = (
            result.reasoning
            + f" Only test files import this ({test_consumers} test consumer(s)) — "
            "may be a test helper or fixture."
        )
        return RoleResult(
            role=result.role,
            confidence=result.confidence,
            source=result.source,
            ambiguity="low",
            reasoning=reasoning,
        )

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: build the imported_by index from parsed_files list
# ─────────────────────────────────────────────────────────────────────────────

def build_imported_by_index(parsed_files: list[dict]) -> dict[str, list[str]]:
    """Return a mapping of {file_path → [file_paths that import it]}.

    Uses the 'imports' list from each parsed file and the file stem as a
    lightweight matching key — the same heuristic used by the smart scanner.
    This is an approximation; exact resolution lives in import_resolver.py.
    """
    # Build a stem → file_path index for fast lookup
    stem_to_path: dict[str, str] = {}
    for pf in parsed_files:
        stem = Path(pf["file_path"]).stem.lower()
        stem_to_path[stem] = pf["file_path"]

    imported_by: dict[str, list[str]] = {pf["file_path"]: [] for pf in parsed_files}

    for pf in parsed_files:
        importer = pf["file_path"]
        for raw_imp in pf.get("imports", []):
            # Normalise: take last path segment and strip extension
            parts = raw_imp.replace("\\", "/").split("/")
            for part in parts:
                key = Path(part).stem.lower()
                if key in stem_to_path:
                    target = stem_to_path[key]
                    if target != importer and importer not in imported_by[target]:
                        imported_by[target].append(importer)

    return imported_by
