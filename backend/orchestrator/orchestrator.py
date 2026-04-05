import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from fastapi import HTTPException
from llm.factory import get_provider
from billing import budget as _budget
from orchestrator.skills import apply_skill
from config import settings

logger = logging.getLogger(__name__)

# Tools that require deep code analysis — use Sonnet for these rounds
_CODE_ANALYSIS_TOOLS = frozenset({
    "query_code_graph", "search_files", "search_docs", "read_file",
    "propose_file_edit", "analyze_impact",
})


def _select_model(provider_name: str, tool_names_called: list[str], base_model: str) -> str:
    """Route to cheaper model for intent steps, heavier model for code analysis."""
    if provider_name.lower() != "anthropic":
        return base_model  # Only route for Anthropic
    if any(t in _CODE_ANALYSIS_TOOLS for t in tool_names_called):
        return "claude-sonnet-4-6"
    return "claude-haiku-4-5-20251001"


def _system_param(provider_name: str, static_text: str, dynamic_text: str = "") -> str | list:
    """Build system parameter with Anthropic prompt caching.

    Only static_text is wrapped with cache_control — it must never contain
    per-request values (project_id, agent type, persona) so the cache key
    is stable across all calls with the same base prompt.
    dynamic_text is appended as a separate non-cached block.
    """
    if provider_name.lower() == "anthropic":
        blocks: list[dict] = [{"type": "text", "text": static_text, "cache_control": {"type": "ephemeral"}}]
        if dynamic_text:
            blocks.append({"type": "text", "text": dynamic_text})
        return blocks
    return static_text + ("\n\n" + dynamic_text if dynamic_text else "")


# Session-level tool result cache with TTL.
# Key structure: _session_tool_cache[session_id][(tool_name, json_input)] = (result, timestamp)
_session_tool_cache: dict[str, dict[tuple[str, str], tuple[str, float]]] = {}
_TOOL_CACHE_TTL = 300  # 5 minutes


def clear_tool_cache() -> None:
    """Invalidate all cached tool results.

    Must be called after any project rescan — graph and file content cached
    before the scan would return stale data for up to _TOOL_CACHE_TTL seconds.
    """
    count = len(_session_tool_cache)
    _session_tool_cache.clear()
    logger.info("Tool cache cleared after rescan (%d session(s) invalidated)", count)

SYSTEM_PROMPT = """You are CodeLM — an AI Software Architect embedded inside a developer's IDE.

You operate inside a structured system with the following components:
- Project Memory: persistent knowledge about this project's architecture, modules, rules, and domain entities
- Code Graph: a live graph of class/module relationships, dependencies, and layer classifications
- File Index: vector-indexed files for semantic search

Your responsibilities:
1. Use available tools to retrieve project knowledge before answering
2. Respect architectural rules stored in Project Memory
3. Enforce DRY: always check the Code Graph before suggesting new components
4. Propose memory updates when you detect new stable architectural knowledge
5. Analyze change impact before suggesting code modifications

Your behavior:
- Think like a senior software architect, not just a code generator
- Every suggestion is a proposal — you never make automatic changes
- Be concise and direct. Cite file paths and class names when relevant.
- If project has not been scanned yet, guide the user to run a project scan first.
- Never use hedging language. Forbidden words and phrases: "presumably", "probably", "likely", "seems to be", "might be", "предположительно", "вероятно", "похоже", or any equivalent. If the data says X, state X as fact. If the data does not say X, state clearly what IS known and what IS NOT known yet.
- When query_code_graph returns status "empty_graph": the graph has 0 nodes. Tell the user the graph is empty and they should run /full-scan. Do not speculate about why.
- When query_code_graph returns status "no_results" or "no_exact_match": the graph is populated but nothing matched exactly. The response includes a sample of graph nodes — use those to answer. Do NOT tell the user to rescan.
- When asked to implement or fix code: always use read_file first to read the current content, then propose changes with propose_file_edit. Never paste raw code blocks as final output for code changes — always use the tool.
- Available scan modes users can type: /full-scan, /auto-scan <hint>, /package-scan. Mention these when guiding users to scan.

RESPONSE STYLE (mandatory):
- Answer the specific question asked. Do not volunteer architecture explanations the user didn't request.
- If you found the data: present it directly. No preamble like "based on static analysis" or confidence disclaimers unless the data is genuinely uncertain.
- If you could NOT find the data: say so in one sentence, explain what you tried, and suggest a concrete next step (e.g., "I couldn't find that class in the graph. Try running /full-scan to re-index, or give me the file path and I'll read it directly.").
- Never pad a response with tangentially related information to compensate for missing data. A short, honest answer is better than a long padded one.
- When listing classes, entities, or files: just list them. Don't add percentage breakdowns, import statistics, or layer diagrams unless the user asked for analysis.
- When the user asks "how many X": give the number and the names. Not a textbook explanation of what X is.
- Do not use phrases like "confidence level for this data: medium" or "from static analysis" — these are internal implementation details.

When explaining a module or feature: lead with what user problem it solves, then the technical structure as supporting detail. Only do this when the user is explicitly asking for an explanation — not on every response.

EFFICIENCY RULES (critical — you are rate-limited):
- Never call the same tool with the same or equivalent query twice. If you already have a result, use it.
- Maximum 2 search_files calls per response turn. Pick the most specific query.
- Maximum 1 search_docs call per response turn.
- Maximum 2 query_code_graph calls per response turn.
- Maximum 3 read_file calls per response turn — only read files you will actually propose changes to.
- Maximum 3 search_text calls per response turn.
- Do not call get_project_memory more than once per conversation.
- Aim to answer in 3 tool-call rounds or fewer. If you have enough information, stop calling tools and respond.

GROUNDING RULES (anti-hallucination — mandatory):
- Use ONLY class names, method names, file paths, and architecture that appear in tool results or user-provided context. Never invent them.
- If a class, file, or pattern is NOT present in the retrieved context — say explicitly: "This was not found in the scanned code."
- Do NOT assume framework conventions unless that convention is visible in the actual code.
- Do NOT say "typically", "usually", "in most projects", "the standard approach is". You are working with THIS project only.
- If the request cannot be completed with available context, state exactly what is missing and which tool would retrieve it.
- When reporting violations or architecture issues: always cite the exact file path, class name, and line number from tool results. If you don't have line numbers, say so.
- A correct "I don't have enough data" is better than a plausible-sounding fabrication.

Response formatting (output is rendered in a terminal/CLI — not a browser):
- Prefer section headers over large markdown tables
- Keep paragraphs short (1-3 sentences)
- Use bullet lists for structured data
- Avoid dense markdown tables
- Never produce large compact markdown blocks that are hard to read in terminals
- Separate logical sections with clear headers and blank lines between them"""

# Tools the AI can call (defined as Anthropic tool schemas)
TOOLS = [
    {
        "name": "get_project_memory",
        "description": "Load the project summary: architecture type, modules, rules, domain entities, discovered structural patterns, and key decisions. Always call this first for any architecture-related question.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "The project identifier"}
            },
            "required": ["project_id"]
        }
    },
    {
        "name": "query_code_graph",
        "description": "Query the code knowledge graph to find classes, modules, services, and their relationships. Use this to check what already exists before suggesting new components (DRY enforcement).",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "query": {"type": "string", "description": "Natural language query, e.g. 'invoice', 'user authentication', 'payment service'"},
                "depth": {"type": "integer", "description": "How many relationship hops to expand (default: 2)", "default": 2}
            },
            "required": ["project_id", "query"]
        }
    },
    {
        "name": "search_files",
        "description": "Semantic search across the project's file index. Use when you need to find relevant code files by meaning, not just by name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "query": {"type": "string", "description": "Semantic search query"},
                "limit": {"type": "integer", "description": "Maximum files to return (default: 10)", "default": 10}
            },
            "required": ["project_id", "query"]
        }
    },
    {
        "name": "suggest_memory_update",
        "description": "Propose a new piece of knowledge to be added to Project Memory. Only call this for STABLE architectural knowledge: new modules, domain entities, architectural decisions, or important rules. Do NOT call for temporary code or debugging notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "category": {
                    "type": "string",
                    "enum": ["module", "domain_entity", "architectural_decision", "rule"],
                    "description": "What kind of knowledge this is"
                },
                "content": {"type": "string", "description": "The knowledge to store. For domain_entity, use format 'ClassName (Role)' e.g. 'Integration (Entity)' or 'PaymentService (Service)'."},
                "reason": {"type": "string", "description": "Why this qualifies as stable architectural knowledge"}
            },
            "required": ["project_id", "category", "content", "reason"]
        }
    },
    {
        "name": "check_architecture_rules",
        "description": "Validate a proposed code change or design against the project's known architectural rules. Returns violations and suggestions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "description": {"type": "string", "description": "Description of the proposed change or design"}
            },
            "required": ["project_id", "description"]
        }
    },
    {
        "name": "read_file",
        "description": "Read the full contents of a source file before proposing edits. Always call this first when you need to modify existing code.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file"}
            },
            "required": ["file_path"]
        }
    },
    {
        "name": "search_docs",
        "description": "Search project documentation (README files, .md docs, PDFs) by meaning. Use this when explaining a feature, module, or business concept to find relevant documentation context. Always call this before explaining what a module or feature does to users.",
        "input_schema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "query": {"type": "string", "description": "What you are looking for in the docs, e.g. 'invoice payment flow', 'authentication design', 'user onboarding'"},
                "limit": {"type": "integer", "description": "Maximum documents to return (default: 5)", "default": 5}
            },
            "required": ["project_id", "query"]
        }
    },
    {
        "name": "propose_file_edit",
        "description": "Propose a code change to a specific file. Architecture is validated automatically before every call — you will receive objections if rules are violated. If you receive an architect_review response, revise your proposal or ask the user if the violation is critical.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file to edit"},
                "description": {"type": "string", "description": "What is being changed and why"},
                "original_snippet": {"type": "string", "description": "The exact original code to replace (empty string for new insertions)"},
                "new_snippet": {"type": "string", "description": "The replacement code"},
                "arch_validated": {"type": "boolean", "description": "Set to true after reviewing architect_review feedback and confirming no violations (or after user approved a critical one)"}
            },
            "required": ["file_path", "description", "new_snippet"]
        }
    },
    {
        "name": "search_text",
        "description": "Fast exact text search across all project files using ripgrep. Use this to find specific references, imports, usages of a class/function/variable name, or any literal string pattern. Returns matching lines with file paths and line numbers. Faster and more precise than semantic search for exact matches.",
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Text pattern to search for (literal string or regex)"
                },
                "file_glob": {
                    "type": "string",
                    "description": "Optional file pattern filter, e.g. '*.java', '*.ts'. Omit to search all files."
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of matching lines to return. Default 30.",
                    "default": 30
                }
            },
            "required": ["pattern"]
        }
    }
]


async def _execute_tool(tool_name: str, tool_input: dict, project_id: str) -> str:
    """Execute a tool call and return the result as a string.

    project_id is passed explicitly from the orchestrator rather than being
    read from tool_input.  The LLM may hallucinate or omit the project_id
    field, so we always use the authoritative value from the chat request.
    """
    from storage.memory_service import load_memory, create_proposal, list_rules

    import logging
    logger = logging.getLogger(__name__)
    logger.info("_execute_tool: tool=%s project_id=%s", tool_name, project_id)

    if tool_name == "get_project_memory":
        logger.info("get_project_memory called for project_id=%s", project_id)
        mem = await load_memory(project_id)
        logger.info("load_memory result: %s", mem)
        if mem:
            rules = await list_rules(project_id)
            return json.dumps({"status": "ok", "memory": mem, "rules": rules})
        return json.dumps({
            "status": "not_indexed",
            "message": "Project not yet scanned. Run Tools → Scan Project with CodeLM.",
            "project_id": project_id,
        })

    elif tool_name == "query_code_graph":
        from storage.neo4j_client import neo4j_client
        query_text = tool_input.get("query", "")
        depth = tool_input.get("depth", 2)

        if not neo4j_client.is_connected:
            return json.dumps({
                "status": "unavailable",
                "nodes": [],
                "message": "Neo4j not connected. Ensure Neo4j is running and the backend has started correctly.",
            })

        # Module-specific query shortcuts
        _q_lower = query_text.lower().strip()

        # "module:<name>" or "show module order" → load all classes in that module
        _module_name: str | None = None
        import re as _re_graph
        _mod_match = _re_graph.match(r'^module[:\s]+(\w+)$', _q_lower)
        if _mod_match:
            _module_name = _mod_match.group(1)
        elif _q_lower.startswith("show module "):
            _module_name = _q_lower[len("show module "):]
        elif _q_lower.startswith("list module "):
            _module_name = _q_lower[len("list module "):]

        if _module_name:
            mod_rows = await neo4j_client.query(
                """
                MATCH (c:Class {module: $module, project_id: $project_id})
                RETURN c.name AS name, c.declared_role AS declared_role,
                       coalesce(c.declared_role, c.layer) AS role,
                       c.layer AS layer, c.file_path AS file_path
                ORDER BY c.layer, c.name
                """,
                {"project_id": project_id, "module": _module_name},
            )
            dep_rows = await neo4j_client.query(
                """
                MATCH (m:Module {name: $module, project_id: $project_id})-[d:DEPENDS_ON]->(other:Module)
                RETURN other.name AS depends_on, d.weight AS import_count
                ORDER BY d.weight DESC
                """,
                {"project_id": project_id, "module": _module_name},
            )
            return json.dumps({
                "status": "ok",
                "query": query_text,
                "module": _module_name,
                "nodes": mod_rows,
                "cross_module_deps": dep_rows,
                "message": f"Module '{_module_name}': {len(mod_rows)} classes.",
            })

        # "modules" or "list modules" → return all Module nodes
        if _q_lower in ("modules", "list modules", "all modules", "module overview"):
            mod_overview = await neo4j_client.query(
                """
                MATCH (m:Module {project_id: $project_id})
                RETURN m.name AS name, m.class_count AS class_count,
                       m.roles_summary AS roles_summary, m.detection_method AS detection_method
                ORDER BY m.class_count DESC
                """,
                {"project_id": project_id},
            )
            return json.dumps({
                "status": "ok",
                "query": query_text,
                "nodes": mod_overview,
                "message": f"Found {len(mod_overview)} modules.",
            })

        # When the query is empty or generic, return a layer/type breakdown overview.
        # IMPORTANT: put results in "nodes" so the LLM does not misread an empty
        # "nodes" list as "graph is empty" — each row is a {type, layer, count} summary.
        if not query_text or query_text.lower() in ("all", "overview", "*"):
            summary_cypher = """
                MATCH (n) WHERE n.project_id = $project_id
                RETURN labels(n)[0] AS type, n.layer AS layer, count(n) AS count
                ORDER BY count DESC
            """
            layer_summary = await neo4j_client.query(summary_cypher, {"project_id": project_id})
            total = sum(r["count"] for r in layer_summary)
            return json.dumps({
                "status": "ok",
                "layer_summary": layer_summary,
                # Expose summary rows as "nodes" so the LLM sees non-zero content
                "nodes": layer_summary,
                "relationships": [],
                "message": f"Project has {total} indexed nodes across {len(layer_summary)} type/layer groups.",
            })

        # Find nodes whose name, file path, or layer contains the query term (case-insensitive).
        # Layer matching lets the AI search by role ("controller", "service", "repository")
        # and find nodes even when neither the class name nor file path contains the term.
        cypher = """
            MATCH (n) WHERE n.project_id = $project_id
            AND (toLower(n.name) CONTAINS toLower($query)
              OR toLower(n.file_path) CONTAINS toLower($query)
              OR toLower(n.layer) CONTAINS toLower($query))
            RETURN n.name AS name, labels(n)[0] AS type, n.layer AS layer, n.file_path AS file_path
            LIMIT 20
        """
        nodes = await neo4j_client.query(cypher, {"project_id": project_id, "query": query_text})

        # Expand IMPORTS relationships for matched nodes (and their neighbours)
        relationships = []
        if depth > 0:
            dep_cypher = """
                MATCH (a)-[r:IMPORTS]->(b)
                WHERE a.project_id = $project_id
                  AND (toLower(a.name) CONTAINS toLower($query)
                    OR toLower(b.name) CONTAINS toLower($query)
                    OR toLower(a.layer) CONTAINS toLower($query)
                    OR toLower(b.layer) CONTAINS toLower($query))
                RETURN a.name AS from, a.layer AS from_layer, b.name AS to, b.layer AS to_layer
                LIMIT 30
            """
            relationships = await neo4j_client.query(dep_cypher, {"project_id": project_id, "query": query_text})

        # Compress node records to only essential fields to reduce token usage
        _KEEP_FIELDS = {"name", "file_path", "layer", "package", "declared_role"}
        nodes = [{k: v for k, v in n.items() if k in _KEEP_FIELDS and v} for n in nodes]

        if not nodes:
            # Distinguish "graph is empty" from "query matched nothing".
            count_result = await neo4j_client.query(
                "MATCH (n) WHERE n.project_id = $project_id RETURN count(n) AS total",
                {"project_id": project_id},
            )
            total_graph_nodes = count_result[0]["total"] if count_result else 0
            if total_graph_nodes == 0:
                return json.dumps({
                    "status": "empty_graph",
                    "query": query_text,
                    "nodes": [],
                    "relationships": [],
                    "total_graph_nodes": 0,
                    "message": "Graph is empty for this project. Run /full-scan to index it.",
                })
            # Check if the query term matches a known module name — if so,
            # return that module's classes instead of a random sample.
            module_match = await neo4j_client.query(
                "MATCH (m:Module {project_id: $project_id}) "
                "WHERE toLower(m.name) CONTAINS toLower($query) "
                "RETURN m.name AS name LIMIT 1",
                {"project_id": project_id, "query": query_text},
            )
            if module_match:
                matched_mod = module_match[0]["name"]
                mod_nodes = await neo4j_client.query(
                    "MATCH (c:Class {module: $module, project_id: $project_id}) "
                    "RETURN c.name AS name, coalesce(c.declared_role, c.layer) AS role, "
                    "c.layer AS layer, c.file_path AS file_path "
                    "ORDER BY c.layer, c.name",
                    {"project_id": project_id, "module": matched_mod},
                )
                return json.dumps({
                    "status": "module_match",
                    "query": query_text,
                    "module": matched_mod,
                    "nodes": mod_nodes,
                    "message": f"Query matched module '{matched_mod}' ({len(mod_nodes)} classes).",
                })

            # Fallback: return a sample of nodes so the AI always has something
            # concrete to work with instead of an empty result.
            sample_nodes = await neo4j_client.query(
                "MATCH (n) WHERE n.project_id = $project_id "
                "RETURN n.name AS name, n.declared_role AS declared_role, "
                "n.layer AS layer, n.file_path AS file_path "
                "LIMIT 30",
                {"project_id": project_id},
            )
            sample_nodes = [
                {k: v for k, v in n.items() if k in _KEEP_FIELDS and v}
                for n in sample_nodes
            ]
            return json.dumps({
                "status": "no_exact_match",
                "query": query_text,
                "nodes": sample_nodes,
                "relationships": [],
                "total_graph_nodes": total_graph_nodes,
                "message": (
                    f"No nodes matched '{query_text}' exactly. "
                    f"Showing a sample of {len(sample_nodes)} nodes from the graph "
                    f"(total: {total_graph_nodes}). "
                    f"Try a different term (partial name, file name, or layer/role name) "
                    f"to find specific classes."
                ),
                "query_executed": "fallback sample — no exact match found",
            })

        return json.dumps({
            "status": "ok",
            "query": query_text,
            "nodes": nodes,
            "relationships": relationships,
            "message": f"Found {len(nodes)} nodes matching '{query_text}'.",
        })

    elif tool_name == "search_files":
        from storage.qdrant_client import qdrant_client, COLLECTION_FILES
        from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchText

        query_text = tool_input.get("query", "")
        limit = int(tool_input.get("limit", 10))

        if not qdrant_client.is_connected:
            return json.dumps({
                "status": "unavailable",
                "files": [],
                "message": "Qdrant not connected. Ensure Qdrant is running and the backend has started correctly.",
            })

        # Check whether this project has any indexed files at all by scrolling
        # for a single point with a project_id match.  If zero points exist the
        # scan either hasn't run yet or failed silently — return a clear message
        # so the LLM does not mislead the user.
        try:
            probe, _ = await qdrant_client.client.scroll(
                collection_name=COLLECTION_FILES,
                scroll_filter=Filter(
                    must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
                ),
                limit=1,
                with_payload=False,
                with_vectors=False,
            )
        except Exception:
            logger.warning("search_files: Qdrant scroll probe failed for project_id=%s", project_id, exc_info=True)
            probe = []

        if not probe:
            return json.dumps({
                "status": "empty_index",
                "files": [],
                "message": "File index is empty for this project. Re-scan to populate it.",
            })

        # Build a payload filter: project_id must match AND at least one of the
        # text fields (file_path, classes list, functions list) must contain the
        # query term.  MatchText performs case-insensitive substring matching on
        # string and keyword fields; for array fields Qdrant checks each element.
        # We use a should (OR) across the three fields inside a must block with
        # project_id so that project scoping is always enforced.
        try:
            text_conditions = []
            if query_text:
                text_conditions = [
                    FieldCondition(key="file_path", match=MatchText(text=query_text)),
                    FieldCondition(key="classes", match=MatchText(text=query_text)),
                    FieldCondition(key="functions", match=MatchText(text=query_text)),
                    FieldCondition(key="package", match=MatchText(text=query_text)),
                ]

            if text_conditions:
                search_filter = Filter(
                    must=[
                        FieldCondition(key="project_id", match=MatchValue(value=project_id)),
                    ],
                    should=text_conditions,
                )
            else:
                # Empty query — return all files for this project up to limit
                search_filter = Filter(
                    must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
                )

            results, _ = await qdrant_client.client.scroll(
                collection_name=COLLECTION_FILES,
                scroll_filter=search_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            logger.warning("search_files: Qdrant scroll failed for project_id=%s query=%s",
                           project_id, query_text, exc_info=True)
            return json.dumps({
                "status": "error",
                "files": [],
                "message": "Qdrant query failed. See server logs for details.",
            })

        files = []
        for point in results:
            p = point.payload or {}
            entry = {
                "file_path": p.get("file_path", ""),
                "language": p.get("language", ""),
                "layer": p.get("layer", ""),
                "classes": p.get("classes", []),
                "functions": p.get("functions", []),
            }
            if p.get("package"):
                entry["package"] = p["package"]
            files.append(entry)

        return json.dumps({
            "status": "ok",
            "query": query_text,
            "files": files,
            "message": f"Found {len(files)} file(s) matching '{query_text}'.",
        })

    elif tool_name == "search_docs":
        from storage.qdrant_client import qdrant_client, COLLECTION_DOCS
        from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchText

        query_text = tool_input.get("query", "")
        limit = int(tool_input.get("limit", 5))

        if not qdrant_client.is_connected:
            return json.dumps({
                "status": "unavailable",
                "docs": [],
                "message": "Qdrant not connected.",
            })

        try:
            probe, _ = await qdrant_client.client.scroll(
                collection_name=COLLECTION_DOCS,
                scroll_filter=Filter(
                    must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))]
                ),
                limit=1,
                with_payload=False,
                with_vectors=False,
            )
        except Exception:
            probe = []

        if not probe:
            return json.dumps({
                "status": "no_docs",
                "docs": [],
                "message": "No documentation indexed for this project. Run /full-scan to index .md and PDF files.",
            })

        try:
            search_filter = Filter(
                must=[FieldCondition(key="project_id", match=MatchValue(value=project_id))],
                should=[FieldCondition(key="content", match=MatchText(text=query_text))] if query_text else [],
            )
            results, _ = await qdrant_client.client.scroll(
                collection_name=COLLECTION_DOCS,
                scroll_filter=search_filter,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            logger.warning("search_docs: Qdrant scroll failed", exc_info=True)
            return json.dumps({"status": "error", "docs": [], "message": "Doc search failed."})

        docs = []
        for point in results:
            p = point.payload or {}
            # Return a snippet of the content to avoid flooding the context window
            content = p.get("content", "")
            snippet = content[:2000] + ("…" if len(content) > 2000 else "")
            docs.append({
                "file_path": p.get("file_path", ""),
                "title": p.get("title", ""),
                "snippet": snippet,
            })

        return json.dumps({
            "status": "ok",
            "query": query_text,
            "docs": docs,
            "message": f"Found {len(docs)} document(s) matching '{query_text}'.",
        })

    elif tool_name == "suggest_memory_update":
        proposal = await create_proposal(
            project_id=project_id,
            category=tool_input.get("category", "architectural_decision"),
            content=tool_input.get("content", ""),
            reason=tool_input.get("reason", ""),
        )
        return json.dumps({
            "status": "proposal_queued",
            "message": "Memory update proposal saved. Review it at GET /api/v1/memory/{project_id}/proposals",
            "proposal_id": proposal["id"],
        })

    elif tool_name == "check_architecture_rules":
        rules = await list_rules(project_id)
        if not rules:
            return json.dumps({
                "status": "no_rules",
                "violations": [],
                "message": "No architectural rules defined yet for this project.",
            })
        return json.dumps({
            "status": "ok",
            "rules": rules,
            "violations": [],  # TODO: graph-based violation detection in Phase 2
            "message": f"{len(rules)} rule(s) loaded. Graph-based violation detection coming in Phase 2.",
        })

    elif tool_name == "read_file":
        import os
        from scanner.project_scanner import _resolve_path
        file_path = _resolve_path(tool_input.get("file_path", ""))
        if not file_path or not os.path.isfile(file_path):
            return json.dumps({"status": "error", "message": f"File not found: {file_path}"})
        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            line_count = content.count("\n") + 1
            truncated = False
            if len(content) > READ_FILE_MAX_CHARS:
                content = content[:READ_FILE_MAX_CHARS]
                truncated = True
            visible_lines = content.count("\n") + 1
            return json.dumps({
                "status": "ok",
                "file_path": file_path,
                "content": content,
                "lines": line_count,
                "visible_lines": visible_lines,
                "truncated": truncated,
                "note": (
                    f"FILE TRUNCATED: showing lines 1-{visible_lines} of {line_count} total. "
                    f"DO NOT call propose_file_edit for any code that is not in the visible portion above. "
                    f"If the target code is beyond line {visible_lines}, use search_files to locate the specific method, "
                    f"then call read_file again — the tool will show the same range, so ask the user to use /full-scan "
                    f"or narrow your search to the specific class or function name."
                ) if truncated else "",
            })
        except Exception as exc:
            return json.dumps({"status": "error", "message": str(exc)})

    elif tool_name == "propose_file_edit":
        # ── Internal Architect validation ─────────────────────────────────────
        # Before accepting any edit proposal, silently validate against architecture
        # rules. Skipped if Coder already confirmed validation (arch_validated=True).
        already_validated = tool_input.get("arch_validated", False)
        description = tool_input.get("description", "")

        if not already_validated and description:
            arch_result_str = await _execute_tool(
                "check_architecture_rules",
                {"project_id": project_id, "description": description},
                project_id,
            )
            try:
                arch_result = json.loads(arch_result_str)
                rules = arch_result.get("rules", [])
                if rules:
                    # Determine if any rule mentions critical layer boundaries
                    desc_lower = description.lower()
                    is_critical = any(
                        kw in desc_lower
                        for kw in ["controller", "repository", "direct call", "bypass", "cross-module"]
                    )
                    return json.dumps({
                        "status": "architect_review",
                        "architecture_rules": rules,
                        "critical": is_critical,
                        "proposal": {
                            "file_path": tool_input.get("file_path", ""),
                            "description": description,
                            "original_snippet": tool_input.get("original_snippet", ""),
                            "new_snippet": tool_input.get("new_snippet", ""),
                        },
                        "instruction": (
                            "CRITICAL layer violation risk detected. You MUST ask the user before proceeding. "
                            "Explain the violation and ask for explicit approval."
                            if is_critical else
                            "Review the architecture_rules above against your proposed change. "
                            "If no violations: call propose_file_edit again with arch_validated=true. "
                            "If violations exist: revise your proposal first, then call propose_file_edit again."
                        ),
                    })
            except Exception:
                pass  # arch check failed — proceed with proposal

        return json.dumps({
            "status": "proposal_ready",
            "file_path": tool_input.get("file_path", ""),
            "description": description or tool_input.get("description", ""),
            "original_snippet": tool_input.get("original_snippet", ""),
            "new_snippet": tool_input.get("new_snippet", ""),
        })

    elif tool_name == "search_text":
        import shutil
        import subprocess
        from scanner.project_scanner import _resolve_path, ROOT_SKIP_DIRS, ALWAYS_SKIP_DIRS
        from storage.memory_service import get_project

        pattern = tool_input.get("pattern", "")
        file_glob = tool_input.get("file_glob", "")
        max_results = int(tool_input.get("max_results", 30))

        if not pattern:
            return json.dumps({"status": "error", "message": "pattern is required"})

        rg_path = shutil.which("rg")
        if not rg_path:
            return json.dumps({
                "status": "unavailable",
                "results": [],
                "message": "ripgrep (rg) is not installed. Install with: apt-get install ripgrep",
            })

        project_info = await get_project(project_id)
        if not project_info or not project_info.get("root_path"):
            return json.dumps({
                "status": "error",
                "results": [],
                "message": "Project root path not found. Run a project scan first.",
            })

        project_root = _resolve_path(project_info["root_path"])

        cmd = [rg_path, "--json", f"--max-count={max_results}"]
        skip_dirs = ALWAYS_SKIP_DIRS | ROOT_SKIP_DIRS
        for skip in sorted(skip_dirs):
            cmd.extend(["--glob", f"!{skip}"])
        if file_glob:
            cmd.extend(["--glob", file_glob])
        cmd.append(pattern)
        cmd.append(project_root)

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return json.dumps({"status": "error", "results": [], "message": "Search timed out after 30 seconds"})
        except Exception as exc:
            return json.dumps({"status": "error", "results": [], "message": str(exc)})

        results: list[dict] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except Exception:
                continue
            if entry.get("type") == "match":
                data = entry.get("data", {})
                fp = data.get("path", {}).get("text", "")
                ln = data.get("line_number", 0)
                content = data.get("lines", {}).get("text", "").rstrip()
                if fp and content:
                    results.append({"file": fp, "line": ln, "content": content})
            if len(results) >= max_results:
                break

        return json.dumps({
            "status": "ok",
            "pattern": pattern,
            "results": results,
            "message": f"Found {len(results)} matching line(s) for '{pattern}'.",
        })

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


# ── Sub-agent system prompt extensions ────────────────────────────────────────

# ── Context pre-loading helpers ───────────────────────────────────────────────

_CODE_GEN_WORDS: frozenset[str] = frozenset({
    "add", "create", "implement", "write", "build", "generate", "new", "make",
})
_CODE_TYPE_WORDS: frozenset[str] = frozenset({
    "controller", "service", "endpoint", "test", "repository",
})
_DEBUG_WORDS: frozenset[str] = frozenset({
    "fix", "bug", "error", "broken", "failing", "crash", "exception", "traceback",
    "issue", "wrong", "incorrect",
})
_LAYER_FROM_TYPE_WORD: dict[str, str] = {
    "controller": "Controller",
    "service":    "Service",
    "repository": "Repository",
    "endpoint":   "Controller",
    "test":       "Util",
}


def _parse_class_registry(domain_entities: list[str]) -> dict[str, str]:
    """Parse domain_entities into {class_name: module_or_group}.

    Handles:
    - Module format:  "module:order — OrderController(controller), OrderService(service)"
    - Role format:    "controllers: OrderController, ProductController"
    - Legacy format:  "OrderController (Controller)"
    """
    import re as _re
    class_to_module: dict[str, str] = {}
    for line in domain_entities:
        line = line.strip()
        if not line:
            continue
        # Module-grouped format: "module:order — ClassName(role), ..."
        if line.startswith("module:"):
            mod_part, _, entries_str = line.partition(" — ")
            mod_name = mod_part[len("module:"):].strip()
            for entry in entries_str.split(","):
                entry = entry.strip()
                m = _re.match(r"^(\w+)\(", entry)
                if m:
                    class_to_module[m.group(1)] = mod_name
        elif ":" in line:
            # Role-grouped: "controllers: OrderController, ProductController"
            group, _, names_str = line.partition(":")
            group = group.strip()
            for name in names_str.split(","):
                name = name.strip()
                if name:
                    class_to_module[name] = group
        elif "(" in line and ")" in line:
            # Legacy: "OrderController (Controller)"
            m = _re.match(r"^(\w+)\s+\((\w+)\)$", line)
            if m:
                class_to_module[m.group(1)] = m.group(2).lower() + "s"
    return class_to_module


async def _build_preload_context(
    message: str,
    memory_json: str,
    project_id: str,
    agent: str = "main",
) -> str | None:
    """Build a deterministic pre-load context block to inject before first LLM call.

    Returns a formatted string or None if nothing useful was found.
    Runs only Neo4j + memory reads — no file reads, no AI calls.
    """
    from storage.neo4j_client import neo4j_client as _neo4j

    try:
        memory_data = json.loads(memory_json)
    except Exception:
        return None

    mem = memory_data.get("memory")
    if not mem:
        return None

    domain_entities: list[str] = mem.get("domain_entities", [])
    class_to_group = _parse_class_registry(domain_entities)

    if not class_to_group and not mem.get("summary"):
        return None

    message_lower = message.lower()
    words = set(message_lower.split())

    # Intent detection
    is_code_gen = bool(words & _CODE_GEN_WORDS) and bool(words & _CODE_TYPE_WORDS)
    is_debug = bool(words & _DEBUG_WORDS)

    # Keyword-triggered: find class names mentioned in the message (cap at 5)
    matched_classes: list[str] = []
    for cls_name in class_to_group:
        if cls_name.lower() in message_lower:
            matched_classes.append(cls_name)
        if len(matched_classes) >= 5:
            break

    # Also match module names directly from the message
    # e.g. "order feature" → load the "order" module
    all_module_names: set[str] = set()
    for line in domain_entities:
        if line.startswith("module:"):
            mod_part = line.split(" — ")[0]
            all_module_names.add(mod_part[len("module:"):].strip())

    matched_modules: list[str] = []
    for mod_name in all_module_names:
        if mod_name.startswith("_"):
            continue
        if mod_name in message_lower or any(
            word.startswith(mod_name) or mod_name.startswith(word)
            for word in words if len(word) > 3
        ):
            matched_modules.append(mod_name)

    # Find module via matched class name
    for cls_name in matched_classes:
        mod = class_to_group.get(cls_name, "")
        if mod and not mod.startswith("_") and mod not in matched_modules:
            matched_modules.append(mod)

    # Build graph context if Neo4j available
    graph_sections: list[str] = []
    loaded_class_count = 0
    MAX_PRELOAD_CLASSES = 20

    if _neo4j.is_connected and matched_modules:
        # Load entire module(s) — primary first, up to cap
        for mod_name in matched_modules[:2]:
            if loaded_class_count >= MAX_PRELOAD_CLASSES:
                break
            try:
                limit = MAX_PRELOAD_CLASSES - loaded_class_count
                rows = await _neo4j.query(
                    """
                    MATCH (c:Class {project_id: $project_id, module: $module})
                    RETURN c.name AS name, c.file_path AS file_path,
                           coalesce(c.declared_role, c.layer) AS role
                    ORDER BY c.layer, c.name
                    LIMIT $limit
                    """,
                    {"project_id": project_id, "module": mod_name, "limit": limit},
                )
                if rows:
                    section = [f"Module: {mod_name} ({len(rows)} classes)"]
                    for r in rows:
                        section.append(f"- {r['name']} ({r.get('role','?')}) — {r.get('file_path','?')}")
                    loaded_class_count += len(rows)

                    # Cross-module dependencies
                    dep_rows = await _neo4j.query(
                        """
                        MATCH (m:Module {name: $module, project_id: $project_id})-[d:DEPENDS_ON]->(other:Module)
                        RETURN other.name AS dep, d.weight AS weight
                        ORDER BY d.weight DESC LIMIT 5
                        """,
                        {"project_id": project_id, "module": mod_name},
                    )
                    if dep_rows:
                        dep_str = ", ".join(
                            f"{r['dep']}({r['weight']} imports)" for r in dep_rows
                        )
                        section.append(f"Cross-module dependencies: {dep_str}")

                    graph_sections.append("\n".join(section))
            except Exception:
                pass

    elif _neo4j.is_connected and matched_classes and not matched_modules:
        # Fallback: individual class neighborhoods (no module data in graph yet)
        for cls_name in matched_classes[:5]:
            try:
                rows = await _neo4j.query(
                    """
                    MATCH (c:Class {name: $name, project_id: $project_id})
                    OPTIONAL MATCH (c)-[:IMPORTS]->(dep:Class {project_id: $project_id})
                    OPTIONAL MATCH (imp:Class {project_id: $project_id})-[:IMPORTS]->(c)
                    RETURN c.name AS name, c.file_path AS file_path, c.layer AS layer,
                           collect(DISTINCT dep.name)[..5] AS imports,
                           collect(DISTINCT imp.name)[..5] AS imported_by
                    LIMIT 1
                    """,
                    {"name": cls_name, "project_id": project_id},
                )
                if rows:
                    r = rows[0]
                    role = class_to_group.get(cls_name, r.get("layer", "?"))
                    parts = [f"- {cls_name} ({role}) — {r.get('file_path', '?')}"]
                    imp_list = [x for x in (r.get("imports") or []) if x]
                    imp_by = [x for x in (r.get("imported_by") or []) if x]
                    if imp_list:
                        parts.append(f"  Imports: {', '.join(imp_list)}")
                    if imp_by:
                        parts.append(f"  Imported by: {', '.join(imp_by)}")
                    graph_sections.append("\n".join(parts))
            except Exception:
                pass

    elif _neo4j.is_connected and is_code_gen and not matched_classes and not matched_modules:
        # Load existing classes of target type as a reference pattern
        target_layer = next(
            (_LAYER_FROM_TYPE_WORD[w] for w in words if w in _LAYER_FROM_TYPE_WORD),
            None,
        )
        if target_layer:
            try:
                existing = await _neo4j.query(
                    "MATCH (n:Class) WHERE n.project_id = $project_id AND n.layer = $layer "
                    "RETURN n.name AS name, n.file_path AS file_path LIMIT 8",
                    {"project_id": project_id, "layer": target_layer},
                )
                if existing:
                    section = [f"Existing {target_layer} classes (as reference pattern):"]
                    for r in existing:
                        section.append(f"  - {r['name']} — {r.get('file_path', '?')}")
                    graph_sections.append("\n".join(section))
            except Exception:
                pass

    if not graph_sections and not is_debug:
        return None

    lines = ["[Pre-loaded Context]"]
    summary = mem.get("summary", "")
    if summary:
        first_sentence = summary.split(".")[0] + "." if "." in summary else summary
        lines.append(first_sentence)

    if graph_sections:
        lines.append("\nRelevant to your request:")
        lines.extend(graph_sections)
    elif is_debug:
        module_counts: dict[str, int] = {}
        for grp in class_to_group.values():
            module_counts[grp] = module_counts.get(grp, 0) + 1
        if module_counts:
            counts_str = ", ".join(f"{v} in {k}" for k, v in sorted(module_counts.items()))
            lines.append(f"\nModule/group summary: {counts_str}")

    lines.append("[End Pre-loaded Context]")
    return "\n".join(lines)


_DEBUGGER_PROMPT = """
You are acting as the Debugger Agent for this request.
Focus exclusively on: identifying root causes, tracing error paths, explaining why something breaks.
Read the relevant files first with read_file before diagnosing.
Be precise — cite file paths and line content. Do not speculate.
You always have full project context pre-loaded. Never ask the user to describe the project,
re-explain what modules exist, or repeat information already in the project memory.
Use the pre-loaded memory and read files directly — the context is already here.
"""

_CODER_PROMPT = """
You are acting as the Coder Agent for this request.
You handle ALL coding tasks: implementing features, fixing bugs, explaining code, and refactoring.

DISCOVERY RULES (read as many files as the task requires — there is no penalty for thorough reading):
1. Start with query_code_graph to locate relevant classes.
2. If query_code_graph returns no match or status "no_exact_match", IMMEDIATELY call search_text with a filename glob (e.g. "*Merchant*", "*.java") — do NOT ask the user where the file is.
3. Read every file that is relevant to the task. For cross-cutting changes (e.g. RBAC across all controllers) read ALL affected controllers before writing a single line.
4. You may batch multiple read_file calls in the same turn to reduce round trips.

IMPLEMENTATION RULES:
- propose changes with propose_file_edit — never paste raw code blocks as final output.
- Follow the project's existing naming conventions and layer rules.
- Architecture validation runs automatically before every propose_file_edit — you will receive objections if violations are found. Revise accordingly.
- Complete the full implementation. Do not stop mid-task because you have read many files — reading is free, stopping early is not acceptable.
"""

_INTENT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("debugger", ["bug", "error", "fix", "broken", "crash", "fail", "exception", "traceback", "issue", "not working", "wrong", "incorrect"]),
    ("coder",    ["implement", "add", "create", "write", "generate", "build", "make", "new method", "new class", "new endpoint", "scaffold",
                  "explain", "what is", "what does", "how does", "describe", "show me", "walk me through", "refactor"]),
]

# coder wins over debugger on ties — explicit creation intent beats ambiguous bug terms
_AGENT_PRIORITY: dict[str, int] = {
    "coder": 2,
    "debugger": 1,
}


def _detect_agent(message: str) -> str:
    """Return coder, debugger, or main based on keyword scoring + priority tiebreak."""
    lower = message.lower()
    scores: dict[str, int] = {
        agent: sum(1 for kw in keywords if kw in lower)
        for agent, keywords in _INTENT_KEYWORDS
    }
    active = {agent: score for agent, score in scores.items() if score > 0}
    if not active:
        return "main"
    return max(active, key=lambda k: (active[k], _AGENT_PRIORITY.get(k, 0)))


def _agent_prompt_extension(agent: str) -> str:
    if agent == "debugger":
        return _DEBUGGER_PROMPT
    if agent == "coder":
        return _CODER_PROMPT
    return ""


# Short messages or continuation openers that suggest the user is extending the
# previous task rather than starting a new one.
_CONTINUATION_OPENERS = (
    "also", "and then", "next", "now", "continue", "additionally",
    "furthermore", "what about", "can you also", "then", "after that",
    "ok now", "okay now", "now also",
)


def _is_continuation(message: str) -> bool:
    """Return True if message looks like an extension of the previous task."""
    lower = message.lower().strip()
    if any(lower.startswith(op) for op in _CONTINUATION_OPENERS):
        return True
    # Very short messages (≤6 words) are likely follow-ups, not new tasks
    if len(lower.split()) <= 6:
        return True
    return False


def _select_agent(message: str, last_agent: str | None) -> str:
    """Return the agent for this turn, respecting session continuity.

    If the message looks like a continuation and we already have an active
    non-main agent, stick with that agent instead of re-detecting.
    """
    if last_agent and last_agent != "main" and _is_continuation(message):
        return last_agent
    return _detect_agent(message)


async def _summarize_history(
    history_messages: list[dict],
    api_key: str,
    provider_name: str = "anthropic",
) -> list[dict]:
    """Summarize older history messages into a compact summary when history exceeds 10 messages.

    Takes the older messages (all but the last 10), calls a fast/cheap model to produce
    a 1-2 paragraph summary, then returns a condensed message list:
    [summary_user_msg, ack_assistant_msg] + last_10_messages.
    """
    if len(history_messages) <= 10:
        return history_messages

    older = history_messages[:-10]
    recent = history_messages[-10:]

    # Format older messages into a readable transcript for summarization
    transcript_lines = []
    for msg in older:
        role_label = "User" if msg["role"] == "user" else "Assistant"
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        transcript_lines.append(f"{role_label}: {content}")
    transcript = "\n".join(transcript_lines)

    summarize_prompt = (
        "Summarize the following conversation into 1-2 concise paragraphs. "
        "Focus on key decisions, topics discussed, and any important context. "
        "Be factual and brief.\n\n"
        f"{transcript}"
    )

    try:
        if provider_name.lower() == "deepseek":
            from openai import OpenAI
            from llm.deepseek_provider import DEEPSEEK_BASE_URL
            client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
            resp = client.chat.completions.create(
                model="deepseek-chat",
                max_tokens=300,
                messages=[{"role": "user", "content": summarize_prompt}],
            )
            summary_text = resp.choices[0].message.content or ""
        else:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": summarize_prompt}],
            )
            summary_text = resp.content[0].text
    except Exception:
        logger.warning("History summarization failed — falling back to truncation", exc_info=True)
        return recent

    condensed = [
        {"role": "user", "content": f"Summary of earlier conversation: {summary_text}"},
        {"role": "assistant", "content": "Understood."},
    ]
    condensed.extend(recent)
    return condensed


def _default_model(provider_name: str) -> str:
    """Return the default model name for a given LLM provider."""
    defaults = {
        "anthropic": "claude-sonnet-4-6",
        "deepseek": "deepseek-chat",
    }
    return defaults.get(provider_name.lower().strip(), "claude-sonnet-4-6")


async def gather_context_via_hypothesis(project_id: str, user_request: str) -> str:
    """Gather context using hypothesis-driven exploration."""
    if not settings.hypothesis_mode_enabled:
        return ""

    try:
        from orchestrator.hypothesis_engine import run_hypothesis_engine
        logger.info("Running hypothesis engine for request: %s", user_request)
        context = await run_hypothesis_engine(project_id, user_request)

        # Format context for LLM
        lines = []
        lines.append("=== HYPOTHESIS ENGINE CONTEXT ===")
        lines.append(f"User request: {context['user_request']}")
        if context.get('discovered_goal'):
            lines.append(f"Discovered goal: {context['discovered_goal']}")
        lines.append(f"Confidence: {context['confidence']:.2f}")
        lines.append("")

        if context['facts']:
            lines.append("Confirmed facts:")
            for fact in context['facts']:
                lines.append(f"  • {fact['text']} (from {fact['source']})")
            lines.append("")

        if context['hypotheses']:
            lines.append("Active hypotheses:")
            for hyp in context['hypotheses']:
                lines.append(f"  • {hyp['text']} (confidence: {hyp['confidence']:.2f})")
            lines.append("")

        if context['visited_nodes']:
            lines.append(f"Explored nodes: {', '.join(context['visited_nodes'])}")
            lines.append(f"Steps taken: {context['steps_taken']}")

        return "\n".join(lines)
    except Exception as e:
        logger.error("Hypothesis engine failed: %s", e, exc_info=True)
        return f"[Hypothesis engine error: {e}]"


async def chat(
    project_id: str,
    message: str,
    conversation_id: str | None,
    session_id: str | None = None,
    agent_id: str | None = None,
    api_key: str = "",
    provider_name: str = "anthropic",
    model: str | None = None,
) -> dict:
    """
    Run the orchestrator: load context -> call LLM -> handle tool calls -> return reply.
    Provider and API key are supplied per-request from the caller (HTTP headers).
    Returns dict with: reply, conversation_id, memory_update_proposed, memory_update_proposal
    """
    if not api_key:
        raise HTTPException(status_code=401, detail="API key required")

    from storage.memory_service import (
        get_messages, add_message as save_message, get_persona,
    )

    resolved_model = model or _default_model(provider_name)

    provider = get_provider(
        provider_name=provider_name,
        model=resolved_model,
        api_key=api_key,
    )

    # Dynamic context: project_id + persona — kept separate from the cached
    # static SYSTEM_PROMPT so the cache key never changes between projects.
    dynamic_context = (
        f"\n\nCurrent project_id: {project_id}\n"
        "Always pass this exact project_id value when calling any tool."
    )

    # Add hypothesis engine context if enabled
    hypothesis_context = await gather_context_via_hypothesis(project_id, message)
    if hypothesis_context:
        dynamic_context += f"\n\n{hypothesis_context}"

    # Append agent persona system prompt if set
    if agent_id:
        persona = await get_persona(agent_id)
        if persona and persona.get("system_prompt_extra"):
            dynamic_context += f"\n\n{persona['system_prompt_extra']}"

    # Build conversation history from session, with summarization for long histories
    messages: list[dict] = []
    if session_id:
        history = await get_messages(session_id)
        history_msgs = [{"role": msg["role"], "content": msg["content"]} for msg in history]
        messages = await _summarize_history(history_msgs, api_key, provider_name)
        # Persist the new user message
        await save_message(session_id, "user", message)

    messages.append({"role": "user", "content": message})
    memory_update_proposed = False
    memory_update_proposal = None

    # Agentic loop: keep going until the LLM stops calling tools
    while True:
        response = provider.chat(messages=messages, tools=TOOLS, system=_system_param(provider_name, SYSTEM_PROMPT, dynamic_context))

        if response.stop_reason == "end_turn":
            reply = response.reply or "I processed your request but had no text response."
            break

        if response.stop_reason == "tool_use":
            # Append assistant turn to history (provider builds the correct format)
            messages.append(provider.assistant_message(response))

            # Execute all tool calls and collect results.
            # project_id is passed explicitly so the correct value is always used
            # even if the LLM supplies a wrong or missing project_id in tool_input.
            tool_results = []
            for tc in response.tool_calls:
                result = await _execute_tool(tc.name, tc.input, project_id)

                if tc.name == "suggest_memory_update":
                    memory_update_proposed = True
                    memory_update_proposal = tc.input

                # For get_project_memory results with Anthropic, mark as cacheable
                if tc.name == "get_project_memory" and provider_name.lower() == "anthropic":
                    tool_result_content = [{"type": "text", "text": result, "cache_control": {"type": "ephemeral"}}]
                else:
                    tool_result_content = result

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": tool_result_content,
                })

            messages.append({"role": "user", "content": tool_results})
            continue

        reply = response.reply or f"Stopped with reason: {response.stop_reason}"
        break

    # Persist assistant reply to session
    if session_id:
        await save_message(session_id, "assistant", reply)

    return {
        "reply": reply,
        "conversation_id": conversation_id or "session-1",
        "memory_update_proposed": memory_update_proposed,
        "memory_update_proposal": memory_update_proposal,
    }


async def chat_stream(
    project_id: str,
    message: str,
    conversation_id: str | None,
    session_id: str | None = None,
    agent_id: str | None = None,
    api_key: str = "",
    provider_name: str = "anthropic",
    model: str | None = None,
    budget_usd: float = 999.0,
) -> AsyncGenerator[str, None]:
    """Stream the orchestrator response as Server-Sent Events.

    Yields SSE-formatted lines:
      - data: {"chunk": "..."}           for text fragments
      - data: {"tool": "...", "status": "running"}  when a tool call starts
      - data: {"done": true}             when the response is complete

    The tool loop works identically to chat(): tool calls block until resolved,
    then streaming resumes for the next LLM turn.  This method currently
    requires the Anthropic provider because it uses the streaming API directly.
    """
    if not api_key:
        yield _sse({"chunk": "Error: API key required. Please set your API key in the settings."})
        yield _sse({"done": True})
        return

    from storage.memory_service import (
        get_messages, add_message as save_message, get_persona,
    )

    resolved_model = model or _default_model(provider_name)

    if provider_name.lower() == "deepseek":
        from openai import OpenAI as _OpenAI
        from llm.deepseek_provider import anthropic_tools_to_openai, anthropic_messages_to_openai, DEEPSEEK_BASE_URL
        _deepseek_client = _OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        _anthropic_client = None
    else:
        import anthropic as _anthropic
        _anthropic_client = _anthropic.Anthropic(api_key=api_key)
        _deepseek_client = None

    # Working memory — ephemeral per-session understanding store
    from orchestrator.working_memory import (
        get_state as _wm_get_state,
        update_from_file_read as _wm_file_read,
        update_from_response as _wm_response,
        update_task_contract as _wm_contract,
        detect_topic_shift as _wm_topic_shift,
        format_injection as _wm_inject,
        format_task_contract as _wm_contract_inject,
    )
    _wm_key = session_id or conversation_id or "__anon__"
    _wm_state = _wm_get_state(_wm_key)

    # Topic shift detection — suggest a new chat before spending tokens
    if _wm_topic_shift(_wm_key, message):
        yield _sse({
            "suggest_new_chat": True,
            "reason": (
                "This looks like a new topic unrelated to the current task. "
                "Starting a fresh chat gives the AI a clean context and better results. "
                "Continue here if this is a clarification."
            ),
        })

    # Set goal on first message; treat follow-up messages as refinements
    if not _wm_get_state(_wm_key).task_contract.goal:
        _wm_contract(_wm_key, goal=message)
    else:
        # Only record as refinement if it's substantive (not a one-word reply)
        if len(message.split()) >= 5:
            _wm_contract(_wm_key, refinement=message)

    # Detect sub-agent with stickiness — continuation messages reuse last agent
    detected_agent = _select_agent(message, _wm_state.last_agent)
    _wm_state.last_agent = detected_agent
    _wm_state.last_agent_task = message[:80]
    yield _sse({"agent": detected_agent, "status": "assigned"})

    # Filter tools and set graph depth for this skill — narrower context per agent type
    skill_tools = apply_skill(detected_agent, TOOLS)

    # Dynamic context: project_id + agent extension + persona — kept separate
    # from the cached static SYSTEM_PROMPT so the cache key is stable across
    # all projects and agent types, maximising cache hits.
    dynamic_context = (
        f"\n\nCurrent project_id: {project_id}\n"
        "Always pass this exact project_id value when calling any tool."
        + _agent_prompt_extension(detected_agent)
    )

    # Append agent persona system prompt if set
    if agent_id:
        persona = await get_persona(agent_id)
        if persona and persona.get("system_prompt_extra"):
            dynamic_context += f"\n\n{persona['system_prompt_extra']}"

    # Main agent: full session history (with summarization for long sessions)
    # Sub-agents (coder, debugger): fresh context BUT with Task Contract + working memory
    # injected so they know the goal, constraints, and what has been done so far.
    messages: list[dict] = []
    if session_id and detected_agent == "main":
        history = await get_messages(session_id)
        history_msgs = [{"role": msg["role"], "content": msg["content"]} for msg in history]
        messages = await _summarize_history(history_msgs, api_key, provider_name)
        await save_message(session_id, "user", message)
    elif session_id:
        # Sub-agents: include a compact session summary so they share intent with Main.
        # We do NOT pass full history (token cost) — just the task contract + summary.
        history = await get_messages(session_id)
        if len(history) >= 2:
            # Build a quick transcript of the last few turns for the sub-agent
            recent = history[-6:]  # last 3 user+assistant pairs
            compact_lines: list[str] = ["[Session context for this sub-agent]"]
            for msg in recent:
                role_label = "User" if msg["role"] == "user" else "Assistant"
                content = msg["content"] if isinstance(msg["content"], str) else ""
                if content:
                    compact_lines.append(f"{role_label}: {content[:300]}")
            compact_lines.append("[End session context]")
            compact_summary = "\n".join(compact_lines)
            messages.append({"role": "user", "content": compact_summary})
            messages.append({"role": "assistant", "content": "Understood. I have the session context."})
        await save_message(session_id, "user", message)

    messages.append({"role": "user", "content": message})

    # Phase-aware tool round tracking (Task 8)
    current_phase: TaskPhase = TaskPhase.PLANNING
    phase_rounds: dict[str, int] = {p.value: 0 for p in TaskPhase}
    # Smart model routing: start with Haiku, escalate to Sonnet for code analysis
    current_model = "claude-haiku-4-5-20251001" if provider_name.lower() == "anthropic" else resolved_model
    # Budget tracking — starts at caller-supplied value, decremented per turn
    remaining_budget = budget_usd

    # Session-level tool result cache with TTL — prevents duplicate API calls
    # across the entire session, not just within a single request.
    cache_session_key = session_id or conversation_id or "__anonymous__"
    if cache_session_key not in _session_tool_cache:
        _session_tool_cache[cache_session_key] = {}
    tool_cache = _session_tool_cache[cache_session_key]

    # Build system param: static SYSTEM_PROMPT cached, dynamic context uncached
    system_param = _system_param(provider_name, SYSTEM_PROMPT, dynamic_context)

    # ── Mandatory project memory pre-load ────────────────────────────────────
    # Always inject get_project_memory as the first synthetic tool exchange so
    # the LLM has full project context before generating any response.
    # This is unconditional — no skill or agent can skip it.
    _pre_id = "pre_memory_0"
    pre_memory_result = await _execute_tool(
        "get_project_memory", {"project_id": project_id}, project_id
    )
    pre_memory_content = (
        [{"type": "text", "text": pre_memory_result, "cache_control": {"type": "ephemeral"}}]
        if provider_name.lower() == "anthropic"
        else pre_memory_result
    )
    messages.append({
        "role": "assistant",
        "content": [{"type": "tool_use", "id": _pre_id, "name": "get_project_memory",
                     "input": {"project_id": project_id}}],
    })

    # ── Lightweight context pre-loading ──────────────────────────────────────
    # Build deterministic context (class neighborhoods, type listings) based on
    # what the user is asking about.  Runs BEFORE the first LLM call to reduce
    # the number of "exploration" tool rounds.  Falls back gracefully if Neo4j
    # is unavailable.
    preload_block: str | None = None
    try:
        preload_block = await _build_preload_context(
            message, pre_memory_result, project_id, detected_agent
        )
    except Exception:
        logger.debug("chat_stream [%s]: preload context failed — skipping", project_id, exc_info=True)

    # Build the user tool-result message; optionally append pre-loaded context
    # as a separate text block (cache_control: ephemeral for stable sessions).
    user_tool_result_content: list[dict] = [
        {"type": "tool_result", "tool_use_id": _pre_id, "content": pre_memory_content}
    ]
    if preload_block:
        user_tool_result_content.append({
            "type": "text",
            "text": preload_block,
            "cache_control": {"type": "ephemeral"},
        })

    # Inject working memory if anything was accumulated in prior turns this session
    _wm_block = _wm_inject(_wm_key)
    if _wm_block:
        user_tool_result_content.append({"type": "text", "text": _wm_block})

    # Inject Task Contract for sub-agents (coder, debugger) — gives them goal + intent
    # Main agent already has session history; sub-agents need this to avoid working blind.
    if detected_agent != "main":
        _tc_block = _wm_contract_inject(_wm_key)
        if _tc_block:
            user_tool_result_content.append({"type": "text", "text": _tc_block})

    messages.append({"role": "user", "content": user_tool_result_content})

    while True:
        # Reduce max_tokens when balance is exhausted to encourage a quick finish
        max_tokens = (
            _budget.FINISH_QUICKLY_MAX_TOKENS
            if _budget.should_finish_quickly(remaining_budget)
            else 4096
        )

        # Accumulate the full response so we can append it to history
        collected_text = ""
        collected_content_blocks: list[dict] = []
        stop_reason = "end_turn"
        tool_calls = []

        if provider_name.lower() == "deepseek":
            # ── DeepSeek streaming (OpenAI-compatible) ────────────────────────
            _sys_text = system_param if isinstance(system_param, str) else SYSTEM_PROMPT
            _oai_messages = [{"role": "system", "content": _sys_text}] + anthropic_messages_to_openai(messages)
            _oai_tools = anthropic_tools_to_openai(skill_tools)
            _ds_kwargs: dict = {
                "model": current_model,
                "max_tokens": max_tokens,
                "messages": _oai_messages,
                "stream": True,
                "stream_options": {"include_usage": True},
            }
            if _oai_tools:
                _ds_kwargs["tools"] = _oai_tools
            _partial_tcs: dict[int, dict] = {}
            _ds_usage = None

            for chunk in _deepseek_client.chat.completions.create(**_ds_kwargs):  # type: ignore[union-attr]
                if chunk.usage:
                    _ds_usage = chunk.usage
                    continue
                if not chunk.choices:
                    continue
                _ch_delta = chunk.choices[0].delta
                _finish = chunk.choices[0].finish_reason

                if _ch_delta.content:
                    collected_text += _ch_delta.content
                    if collected_content_blocks and collected_content_blocks[-1]["type"] == "text":
                        collected_content_blocks[-1]["text"] += _ch_delta.content
                    else:
                        collected_content_blocks.append({"type": "text", "text": _ch_delta.content})
                    yield _sse({"chunk": _ch_delta.content})

                if _ch_delta.tool_calls:
                    for _tc_d in _ch_delta.tool_calls:
                        _i = _tc_d.index
                        if _i not in _partial_tcs:
                            _partial_tcs[_i] = {"id": "", "name": "", "arguments": ""}
                        if _tc_d.id:
                            _partial_tcs[_i]["id"] = _tc_d.id
                        if _tc_d.function:
                            if _tc_d.function.name:
                                if not _partial_tcs[_i]["name"]:
                                    yield _sse({"tool": _tc_d.function.name, "status": "running"})
                                _partial_tcs[_i]["name"] += _tc_d.function.name
                            if _tc_d.function.arguments:
                                _partial_tcs[_i]["arguments"] += _tc_d.function.arguments

                if _finish == "tool_calls":
                    stop_reason = "tool_use"

            for _i in sorted(_partial_tcs.keys()):
                _tc = _partial_tcs[_i]
                collected_content_blocks.append({
                    "type": "tool_use",
                    "id": _tc["id"],
                    "name": _tc["name"],
                    "input": _tc["arguments"],  # raw JSON string; parsed below
                })

            if _ds_usage:
                try:
                    call_cost = _budget.cost_usd(
                        model=current_model,
                        input_tokens=_ds_usage.prompt_tokens,
                        output_tokens=_ds_usage.completion_tokens,
                    )
                    remaining_budget -= call_cost
                    yield _sse({"cost_usd": round(call_cost, 6), "balance_usd": round(remaining_budget, 6)})
                    logger.info(
                        "chat_stream [%s] turn cost: $%.6f | model=%s | in=%d out=%d | balance: $%.4f",
                        project_id, call_cost, current_model,
                        _ds_usage.prompt_tokens, _ds_usage.completion_tokens, remaining_budget,
                    )
                except Exception as exc:
                    logger.warning(
                        "chat_stream [%s]: DeepSeek usage tracking failed. model=%s reason=%s",
                        project_id, current_model, exc,
                    )

        else:
            # ── Anthropic streaming ───────────────────────────────────────────
            with _anthropic_client.messages.stream(  # type: ignore[union-attr]
                model=current_model,
                max_tokens=max_tokens,
                system=system_param,
                tools=skill_tools,
                messages=messages,
                extra_headers={"anthropic-beta": "token-efficient-tools-2025-02-19"},
            ) as stream:
                for event in stream:
                    if event.type == "content_block_start":
                        block = event.content_block
                        if block.type == "text":
                            collected_content_blocks.append({"type": "text", "text": ""})
                        elif block.type == "tool_use":
                            collected_content_blocks.append(
                                {"type": "tool_use", "id": block.id, "name": block.name, "input": ""}
                            )
                            yield _sse({"tool": block.name, "status": "running"})

                    elif event.type == "content_block_delta":
                        delta = event.delta
                        if delta.type == "text_delta":
                            collected_text += delta.text
                            if collected_content_blocks and collected_content_blocks[-1]["type"] == "text":
                                collected_content_blocks[-1]["text"] += delta.text
                            yield _sse({"chunk": delta.text})
                        elif delta.type == "input_json_delta":
                            if collected_content_blocks and collected_content_blocks[-1]["type"] == "tool_use":
                                collected_content_blocks[-1]["input"] += delta.partial_json

                    elif event.type == "message_delta":
                        stop_reason = getattr(event.delta, "stop_reason", "end_turn") or "end_turn"

                # ── Budget accounting (inside the `with` block while stream is still open)
                # IMPORTANT: this try/except must NEVER silently swallow failures.
                # If usage is missing we log a WARNING so the operator knows money
                # may have been spent without being tracked.  We don't crash the
                # stream — the user already received the response — but the event
                # is recorded so it can be investigated and reconciled.
                try:
                    final_msg = stream.get_final_message()
                    u = final_msg.usage
                    call_cost = _budget.cost_usd(
                        model=current_model,
                        input_tokens=u.input_tokens,
                        output_tokens=u.output_tokens,
                        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
                    )
                    remaining_budget -= call_cost
                    yield _sse({"cost_usd": round(call_cost, 6), "balance_usd": round(remaining_budget, 6)})
                    logger.info(
                        "chat_stream [%s] turn cost: $%.6f | model=%s | in=%d out=%d "
                        "cache_read=%d cache_write=%d | balance: $%.4f",
                        project_id, call_cost, current_model,
                        u.input_tokens, u.output_tokens,
                        getattr(u, "cache_read_input_tokens", 0) or 0,
                        getattr(u, "cache_creation_input_tokens", 0) or 0,
                        remaining_budget,
                    )
                except Exception as exc:
                    logger.warning(
                        "chat_stream [%s]: usage unavailable after turn — cost NOT tracked. "
                        "model=%s reason=%s",
                        project_id, current_model, exc,
                    )

        # Parse tool_use blocks from collected content
        for block in collected_content_blocks:
            if block["type"] == "tool_use":
                try:
                    parsed_input = json.loads(block["input"]) if isinstance(block["input"], str) else block["input"]
                except (json.JSONDecodeError, TypeError):
                    parsed_input = {}
                block["input"] = parsed_input
                tool_calls.append(block)

        # Update working memory with decisions extracted from this turn's response
        if collected_text:
            _wm_response(_wm_key, collected_text)

        if stop_reason == "end_turn" or not tool_calls:
            # Persist assistant reply to session
            if session_id and collected_text:
                await save_message(session_id, "assistant", collected_text)
                try:
                    from storage.memory_service import add_todos_from_text
                    todos = await add_todos_from_text(session_id, collected_text)
                    if todos:
                        yield _sse({"todos_added": len(todos)})
                except Exception:
                    pass
            yield _sse({"done": True})
            return

        # ── Phase-aware round limit (Task 8) ─────────────────────────────────
        phase_rounds[current_phase.value] += 1
        _phase_limit = PHASE_LIMITS[current_phase]
        if phase_rounds[current_phase.value] > _phase_limit:
            logger.warning(
                "chat_stream [%s]: phase=%s limit=%d reached for agent=%s — forcing end_turn",
                project_id, current_phase.value, _phase_limit, detected_agent,
            )
            if session_id and collected_text:
                await save_message(session_id, "assistant", collected_text)
                try:
                    from storage.memory_service import add_todos_from_text
                    await add_todos_from_text(session_id, collected_text)
                except Exception:
                    pass
            yield _sse({"chunk": "\n\n⚠ Reached maximum tool-call depth — partial results shown above."})
            yield _sse({"done": True})
            return

        # ── Budget gate: don't start another tool round if overdraft floor reached
        if not _budget.can_start_task(remaining_budget):
            logger.info("chat_stream [%s]: budget exhausted (%.4f) — stopping after current turn", project_id, remaining_budget)
            if session_id and collected_text:
                await save_message(session_id, "assistant", collected_text)
            yield _sse({"chunk": "\n\n⚠ Budget limit reached — task stopped to protect your balance."})
            yield _sse({"done": True, "budget_exhausted": True})
            return

        # Tool loop: append assistant message, execute tools, continue
        messages.append({"role": "assistant", "content": collected_content_blocks})

        tool_results = []
        for tc in tool_calls:
            # Deduplication cache with TTL — same tool + same input returns
            # cached result if it hasn't expired.
            cache_key = (tc["name"], json.dumps(tc["input"], sort_keys=True))
            now = time.time()
            cached = tool_cache.get(cache_key)
            if cached and (now - cached[1]) < _TOOL_CACHE_TTL:
                logger.info(
                    "chat_stream [%s]: cache hit for %s — skipping duplicate call",
                    project_id, tc["name"],
                )
                result = cached[0]
            else:
                result = await _execute_tool(tc["name"], tc["input"], project_id)
                tool_cache[cache_key] = (result, now)

            # Working memory: record file reads
            if tc["name"] == "read_file":
                _fp = tc["input"].get("file_path") or tc["input"].get("path", "")
                if _fp:
                    _wm_file_read(_wm_key, _fp)

            # Emit file_edit SSE so the IDE can show an accept/reject dialog
            if tc["name"] == "propose_file_edit":
                try:
                    edit_data = json.loads(result)
                    if edit_data.get("status") == "proposal_ready":
                        yield _sse({"file_edit": edit_data})
                        # Track changed file in task contract for cross-agent awareness
                        _changed_fp = tc["input"].get("file_path", "")
                        if _changed_fp:
                            _wm_contract(_wm_key, file_changed=_changed_fp)
                except Exception:
                    pass

            # For get_project_memory results with Anthropic, mark as cacheable
            if tc["name"] == "get_project_memory" and provider_name.lower() == "anthropic":
                tool_result_content = [{"type": "text", "text": result, "cache_control": {"type": "ephemeral"}}]
            else:
                tool_result_content = result

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": tool_result_content,
            })

        # ── Phase transitions ─────────────────────────────────────────────────
        tool_names_this_round = [tc["name"] for tc in tool_calls]
        if current_phase == TaskPhase.PLANNING and "propose_file_edit" in tool_names_this_round:
            current_phase = TaskPhase.EXECUTION
            logger.debug("chat_stream [%s]: phase PLANNING → EXECUTION", project_id)
        elif current_phase == TaskPhase.EXECUTION and "suggest_memory_update" in tool_names_this_round:
            current_phase = TaskPhase.FINALIZATION
            logger.debug("chat_stream [%s]: phase EXECUTION → FINALIZATION", project_id)

        # ── Soft phase warning (inject before next LLM call) ─────────────────
        user_round_content: list[dict] = list(tool_results)
        _phase_used = phase_rounds[current_phase.value]
        _phase_cap  = PHASE_LIMITS[current_phase]
        if _phase_used >= int(_phase_cap * 0.8):
            _warn_msg = (
                f"[System: You are approaching the {current_phase.value} phase limit "
                f"({_phase_used}/{_phase_cap} rounds used). "
                f"Summarize your findings and wrap up this phase.]"
            )
            user_round_content.append({"type": "text", "text": _warn_msg})

        messages.append({"role": "user", "content": user_round_content})

        # Smart model routing: pick model for next round based on tools called this round
        last_tool_names = tool_names_this_round
        current_model = _select_model(provider_name, last_tool_names, resolved_model)

        tool_calls = []
        # Continue the while loop to stream the next LLM turn


TOOL_ROUND_LIMITS: dict[str, int] = {
    "coder":    12,   # code generation needs more rounds
    "debugger": 10,   # bug tracing can go deep
    "architect": 6,   # internal validator, should be quick
    "default":   8,   # general chat
}

from enum import Enum

class TaskPhase(str, Enum):
    PLANNING     = "planning"
    EXECUTION    = "execution"
    FINALIZATION = "finalization"

# Per-phase round budgets.
# PLANNING is generous — discovery depth depends on task complexity and we
# never want the AI to stop exploring mid-task because it ran out of rounds.
# EXECUTION is very generous — multi-file changes can require many edit rounds.
# FINALIZATION is tight — just enough for a memory update and summary.
PHASE_LIMITS: dict[TaskPhase, int] = {
    TaskPhase.PLANNING:     20,
    TaskPhase.EXECUTION:    30,
    TaskPhase.FINALIZATION: 5,
}

# Maximum characters returned by read_file before truncation (prevents context bloat)
READ_FILE_MAX_CHARS = 12_000


def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Event line."""
    return f"data: {json.dumps(data)}\n\n"
