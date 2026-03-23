"""Tree-sitter based Java parser.

Provides _parse_java_treesitter(file_path) which extracts:
  - class / interface / enum / record names
  - method names
  - package declaration

Imports are intentionally NOT extracted here — the regex parser handles imports
and those results are always merged into the final output.

The parser and language objects are module-level singletons (lazy-initialised on
first call) so the ~100 ms tree-sitter initialisation only happens once per
process.

All errors propagate to the caller; _parse_java_file catches them and falls
back to regex.
"""
import logging
import os

logger = logging.getLogger(__name__)

# Lazy-loaded singletons — None until first call to _parse_java_treesitter().
_ts_parser = None
_ts_language = None


def _get_parser_and_language():
    """Return (Parser, Language), initialising once on first call.

    Raises ImportError / RuntimeError if tree-sitter or tree-sitter-java is
    not installed — callers must handle these and fall back to regex.
    """
    global _ts_parser, _ts_language
    if _ts_parser is not None:
        return _ts_parser, _ts_language

    from tree_sitter import Language, Parser  # noqa: PLC0415
    import tree_sitter_java as _ts_java       # noqa: PLC0415

    # tree-sitter-java exposes either language() or LANGUAGE depending on version.
    try:
        _ts_language = Language(_ts_java.language())
    except Exception:
        _ts_language = Language(_ts_java.LANGUAGE)

    _ts_parser = Parser()
    _ts_parser.set_language(_ts_language)

    logger.info("Java tree-sitter parser initialised (tree_sitter_java loaded)")
    return _ts_parser, _ts_language


# ── Queries (compiled once alongside the singletons) ─────────────────────────

_TYPE_QUERY_SRC = """
(class_declaration       name: (identifier) @name)
(interface_declaration   name: (identifier) @name)
(enum_declaration        name: (identifier) @name)
(record_declaration      name: (identifier) @name)
"""

_METHOD_QUERY_SRC = """
(method_declaration name: (identifier) @name)
"""

# Package can be a plain identifier ("package foo;") or a dotted path
# ("package com.example.service;").  We capture the entire first child of
# package_declaration regardless of its concrete node type.
_PACKAGE_QUERY_SRC = """
(package_declaration _ @name)
"""

_ts_type_query = None
_ts_method_query = None
_ts_package_query = None


def _get_queries():
    global _ts_type_query, _ts_method_query, _ts_package_query
    if _ts_type_query is not None:
        return _ts_type_query, _ts_method_query, _ts_package_query

    _, language = _get_parser_and_language()
    _ts_type_query = language.query(_TYPE_QUERY_SRC)
    _ts_method_query = language.query(_METHOD_QUERY_SRC)
    _ts_package_query = language.query(_PACKAGE_QUERY_SRC)
    return _ts_type_query, _ts_method_query, _ts_package_query


# ── Public API ────────────────────────────────────────────────────────────────

def _parse_java_treesitter(file_path: str) -> dict:
    """Parse a Java source file with tree-sitter.

    Returns:
        dict with keys: classes (list[str]), functions (list[str]), package (str)
        imports key is intentionally absent — caller merges regex imports.

    Raises:
        Any exception from tree-sitter (ImportError, OSError, RuntimeError, …).
        Callers MUST wrap in try/except and fall back to regex on any error.
    """
    parser, _ = _get_parser_and_language()
    type_query, method_query, package_query = _get_queries()

    if not os.path.isfile(file_path):
        raise FileNotFoundError(f"tree-sitter: file not found: {file_path}")

    with open(file_path, "r", encoding="utf-8", errors="replace") as fh:
        source = fh.read()

    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)

    classes: list[str] = []
    functions: list[str] = []
    package: str = ""

    # tree-sitter 0.22.x: captures() returns list[(Node, capture_name_str)]
    for node, _ in type_query.captures(tree.root_node):
        name = node.text.decode("utf-8", errors="replace").strip()
        if name:
            classes.append(name)

    for node, _ in method_query.captures(tree.root_node):
        name = node.text.decode("utf-8", errors="replace").strip()
        if name:
            functions.append(name)

    pkg_captures = package_query.captures(tree.root_node)
    if pkg_captures:
        # The first capture is the package name node (identifier or scoped_identifier).
        pkg_node = pkg_captures[0][0]
        package = pkg_node.text.decode("utf-8", errors="replace").strip()

    return {"classes": classes, "functions": functions, "package": package}
