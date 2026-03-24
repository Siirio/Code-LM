import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from fastapi import HTTPException
from llm.factory import get_provider
from billing import budget as _budget
from orchestrator.skills import apply_skill

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
    """Parse the grouped domain_entities format into {class_name: group_name}.

    Handles both new grouped format ("controllers: A, B") and legacy
    per-entry format ("A (Controller)").
    """
    class_to_group: dict[str, str] = {}
    for line in domain_entities:
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            # New grouped format: "controllers: OrderController, ProductController"
            group, _, names_str = line.partition(":")
            group = group.strip()
            for name in names_str.split(","):
                name = name.strip()
                if name:
                    class_to_group[name] = group
        elif "(" in line and ")" in line:
            # Legacy format: "OrderController (Controller)"
            import re as _re
            m = _re.match(r"^(\w+)\s+\((\w+)\)$", line)
            if m:
                class_to_group[m.group(1)] = m.group(2).lower() + "s"
    return class_to_group


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

    # Build graph context if Neo4j available
    graph_lines: list[str] = []
    if _neo4j.is_connected and matched_classes:
        for cls_name in matched_classes:
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
                    graph_lines.append("\n".join(parts))
            except Exception:
                pass

    elif _neo4j.is_connected and is_code_gen and not matched_classes:
        # Load existing classes of the target type as a reference
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
                    graph_lines.append(f"Existing {target_layer} classes:")
                    for r in existing:
                        graph_lines.append(f"  - {r['name']} — {r.get('file_path', '?')}")
            except Exception:
                pass

    # Only inject when graph lines or debug mode provides useful context
    if not graph_lines and not is_debug:
        return None

    lines = ["[Pre-loaded Context]"]
    summary = mem.get("summary", "")
    if summary:
        # Extract first sentence of summary as project headline
        first_sentence = summary.split(".")[0] + "." if "." in summary else summary
        lines.append(first_sentence)

    if graph_lines:
        lines.append("\nRelevant to your request:")
        lines.extend(graph_lines)
    elif is_debug:
        # For debug requests without matched classes, add class registry summary
        group_counts = {}
        for grp in class_to_group.values():
            group_counts[grp] = group_counts.get(grp, 0) + 1
        if group_counts:
            counts_str = ", ".join(f"{v} {k}" for k, v in sorted(group_counts.items()))
            lines.append(f"\nClass registry: {counts_str}")

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
For implementation: (1) read the relevant files first, (2) check the code graph for existing components, (3) propose changes with propose_file_edit — never paste raw code blocks as final output.
For explanation: read the relevant file(s), then explain clearly citing actual file paths and function names.
Follow the project's existing naming conventions and layer rules.
Architecture validation runs automatically before every propose_file_edit — you will receive objections if violations are found. Revise accordingly.
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


async def _summarize_history(history_messages: list[dict], api_key: str) -> list[dict]:
    """Summarize older history messages into a compact summary when history exceeds 10 messages.

    Takes the older messages (all but the last 10), calls Claude Haiku to produce
    a 1-2 paragraph summary, then returns a condensed message list:
    [summary_user_msg, ack_assistant_msg] + last_10_messages.
    """
    if len(history_messages) <= 10:
        return history_messages

    import anthropic
    older = history_messages[:-10]
    recent = history_messages[-10:]

    # Format older messages into a readable transcript for summarization
    transcript_lines = []
    for msg in older:
        role_label = "User" if msg["role"] == "user" else "Assistant"
        content = msg["content"] if isinstance(msg["content"], str) else str(msg["content"])
        transcript_lines.append(f"{role_label}: {content}")
    transcript = "\n".join(transcript_lines)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        summary_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{
                "role": "user",
                "content": (
                    "Summarize the following conversation into 1-2 concise paragraphs. "
                    "Focus on key decisions, topics discussed, and any important context. "
                    "Be factual and brief.\n\n"
                    f"{transcript}"
                ),
            }],
        )
        summary_text = summary_response.content[0].text
    except Exception:
        logger.warning("History summarization failed — falling back to truncation", exc_info=True)
        # Fallback: just use the last 10 messages without summary
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
    }
    return defaults.get(provider_name.lower().strip(), "claude-sonnet-4-6")


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
        messages = await _summarize_history(history_msgs, api_key)
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
            # Append assistant turn to history
            if hasattr(response.raw, "content"):
                # Convert Pydantic content blocks to plain dicts so they survive
                # re-serialisation in subsequent API calls without pydantic-core errors.
                content = [
                    block.model_dump() if hasattr(block, "model_dump") else block
                    for block in response.raw.content
                ]
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append({"role": "assistant", "content": response.reply or ""})

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

    import anthropic

    from storage.memory_service import (
        get_messages, add_message as save_message, get_persona,
    )

    if provider_name != "anthropic":
        yield _sse({"chunk": "Streaming is currently only supported with the Anthropic provider."})
        yield _sse({"done": True})
        return

    resolved_model = model or _default_model(provider_name)

    client = anthropic.Anthropic(api_key=api_key)

    # Detect sub-agent and emit it as first SSE event
    detected_agent = _detect_agent(message)
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

    # Sub-agents run with fresh context (no history) — main agent uses session history
    messages: list[dict] = []
    if session_id and detected_agent == "main":
        history = await get_messages(session_id)
        history_msgs = [{"role": msg["role"], "content": msg["content"]} for msg in history]
        messages = await _summarize_history(history_msgs, api_key)
        await save_message(session_id, "user", message)
    elif session_id:
        # Sub-agents: fresh context, but still persist messages for continuity
        await save_message(session_id, "user", message)

    messages.append({"role": "user", "content": message})

    tool_round = 0
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

    messages.append({"role": "user", "content": user_tool_result_content})

    while True:
        # Reduce max_tokens when balance is exhausted to encourage a quick finish
        max_tokens = (
            _budget.FINISH_QUICKLY_MAX_TOKENS
            if _budget.should_finish_quickly(remaining_budget)
            else 4096
        )

        # Open a streaming request to Anthropic
        with client.messages.stream(
            model=current_model,
            max_tokens=max_tokens,
            system=system_param,
            tools=skill_tools,
            messages=messages,
            extra_headers={"anthropic-beta": "token-efficient-tools-2025-02-19"},
        ) as stream:
            # Accumulate the full response so we can append it to history
            collected_text = ""
            collected_content_blocks: list[dict] = []
            stop_reason = "end_turn"
            tool_calls = []

            for event in stream:
                if event.type == "content_block_start":
                    block = event.content_block
                    if block.type == "text":
                        collected_content_blocks.append(
                            {"type": "text", "text": ""}
                        )
                    elif block.type == "tool_use":
                        collected_content_blocks.append(
                            {"type": "tool_use", "id": block.id, "name": block.name, "input": ""}
                        )
                        yield _sse({"tool": block.name, "status": "running"})

                elif event.type == "content_block_delta":
                    delta = event.delta
                    if delta.type == "text_delta":
                        collected_text += delta.text
                        # Update last text block
                        if collected_content_blocks and collected_content_blocks[-1]["type"] == "text":
                            collected_content_blocks[-1]["text"] += delta.text
                        yield _sse({"chunk": delta.text})
                    elif delta.type == "input_json_delta":
                        # Accumulate partial JSON for tool input
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
                # Stream was interrupted (client disconnect, timeout, API error)
                # before usage was finalised.  Budget NOT decremented — conservative:
                # better to under-charge than to silently lose tracking.
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

        # Hard limit: stop tool loop to prevent runaway API usage
        tool_round += 1
        _max_rounds = TOOL_ROUND_LIMITS.get(detected_agent, TOOL_ROUND_LIMITS["default"])
        if tool_round > _max_rounds:
            logger.warning(
                "chat_stream [%s]: reached tool round limit (%d) for agent=%s — forcing end_turn",
                project_id, _max_rounds, detected_agent,
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

            # Emit file_edit SSE so the IDE can show an accept/reject dialog
            if tc["name"] == "propose_file_edit":
                try:
                    edit_data = json.loads(result)
                    if edit_data.get("status") == "proposal_ready":
                        yield _sse({"file_edit": edit_data})
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

        messages.append({"role": "user", "content": tool_results})

        # Smart model routing: pick model for next round based on tools called this round
        last_tool_names = [tc["name"] for tc in tool_calls]
        current_model = _select_model(provider_name, last_tool_names, resolved_model)

        tool_calls = []
        # Continue the while loop to stream the next LLM turn


TOOL_ROUND_LIMITS: dict[str, int] = {
    "coder":    12,   # code generation needs more rounds
    "debugger": 10,   # bug tracing can go deep
    "architect": 6,   # internal validator, should be quick
    "default":   8,   # general chat
}
# Maximum characters returned by read_file before truncation (prevents context bloat)
READ_FILE_MAX_CHARS = 12_000


def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Event line."""
    return f"data: {json.dumps(data)}\n\n"
