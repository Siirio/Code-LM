"""Validation layer for parsed source entities.

Every extracted class/function name passes through validate_parsed_file()
BEFORE it is written to Neo4j or Qdrant.  Suspicious or clearly wrong
extractions are silently dropped with a WARNING log so the pipeline never
crashes but also never stores garbage.

Rules applied per entity:
  1. Name must be non-empty and non-whitespace.
  2. Name must be a plausible identifier (only word chars + $, starts with
     a letter or underscore/dollar — Java/Kotlin/Go/TS naming rules).
  3. The raw name string must actually appear in the file content so that
     regex false-positives (matches inside strings or unusual constructs)
     are caught.  We read the file once and cache the content.
  4. Duplicate names within the same file are deduplicated (keep first).
  5. File must exist on disk — guard against path resolution bugs.

Returns a sanitised copy of the parsed dict.  The original is never mutated.
"""

import logging
import os
import re

logger = logging.getLogger(__name__)

# Identifier must start with letter/underscore/dollar, contain only word chars
# or dollar signs.  Max 256 chars to reject obviously malformed regex captures.
_IDENT_RE = re.compile(r'^[A-Za-z_$][A-Za-z0-9_$]{0,255}$')


def validate_parsed_file(parsed: dict, file_path: str) -> dict:
    """Return a sanitised copy of *parsed* with invalid entities removed.

    Args:
        parsed:    output from any _parse_*_file function
                   (keys: classes, functions, imports, package, …)
        file_path: absolute path to the source file

    Returns:
        New dict with the same keys; classes/functions/imports may be shorter.
        Counters for rejected items are included under '_validation_rejected'.
    """
    rejected = {"classes": 0, "functions": 0}

    # ── Guard: file must exist ────────────────────────────────────────────────
    if not os.path.isfile(file_path):
        logger.warning("Validator: file does not exist on disk: %s — skipping all entities", file_path)
        return {**parsed, "classes": [], "functions": [], "_validation_rejected": rejected}

    # Read raw source once for presence checks.
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
            raw_source = fh.read()
    except OSError as exc:
        logger.warning("Validator: cannot read %s: %s — skipping all entities", file_path, exc)
        return {**parsed, "classes": [], "functions": [], "_validation_rejected": rejected}

    def _accept_name(name: str, kind: str) -> bool:
        """Return True if *name* passes all validation rules."""
        # Rule 1: non-empty
        if not name or not name.strip():
            return False
        # Rule 2: valid identifier pattern
        if not _IDENT_RE.match(name):
            logger.warning(
                "Validator: rejected %s '%s' in %s — invalid identifier",
                kind, name, file_path,
            )
            return False
        # Rule 3: name must appear literally in the file
        if name not in raw_source:
            logger.warning(
                "Validator: rejected %s '%s' in %s — name not found in source",
                kind, name, file_path,
            )
            return False
        return True

    # ── Validate classes ──────────────────────────────────────────────────────
    seen_classes: set[str] = set()
    clean_classes: list[str] = []
    for cls in parsed.get("classes", []):
        if cls in seen_classes:
            continue  # deduplicate silently
        if _accept_name(cls, "class"):
            clean_classes.append(cls)
            seen_classes.add(cls)
        else:
            rejected["classes"] += 1

    # ── Validate functions ────────────────────────────────────────────────────
    seen_funcs: set[str] = set()
    clean_functions: list[str] = []
    for fn in parsed.get("functions", []):
        if fn in seen_funcs:
            continue
        if _accept_name(fn, "function"):
            clean_functions.append(fn)
            seen_funcs.add(fn)
        else:
            rejected["functions"] += 1

    if rejected["classes"] or rejected["functions"]:
        logger.info(
            "Validator [%s]: rejected classes=%d functions=%d",
            os.path.basename(file_path), rejected["classes"], rejected["functions"],
        )

    return {
        **parsed,
        "classes": clean_classes,
        "functions": clean_functions,
        "_validation_rejected": rejected,
    }
