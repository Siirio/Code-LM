import asyncio
import json
import logging
import time
from collections.abc import AsyncGenerator
from fastapi import HTTPException
from llm.factory import get_provider

logger = logging.getLogger(__name__)

# Session-level tool result cache with TTL.
# Key structure: _session_tool_cache[session_id][(tool_name, json_input)] = (result, timestamp)
_session_tool_cache: dict[str, dict[tuple[str, str], tuple[str, float]]] = {}
_TOOL_CACHE_TTL = 300  # 5 minutes

SYSTEM_PROMPT = """You are EngramAI — an AI Software Architect embedded inside a developer's IDE.

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
- Be concise but precise. Cite file paths and class names when relevant.
- If project has not been scanned yet, guide the user to run a project scan first.
- Never use hedging language. Forbidden words and phrases: "presumably", "probably", "likely", "seems to be", "might be", "предположительно", "вероятно", "похоже", or any equivalent. If the data says X, state X as fact. If the data does not say X, state clearly what IS known and what IS NOT known yet. Distinguish explicitly between: (a) data was retrieved and is definitive, (b) data was not retrieved — name exactly which tool returned empty or nothing and state what that means.
- When query_code_graph returns 0 nodes, say exactly: "Code graph is empty for this project — a scan with the latest version is needed." Do not say the project has not been scanned. Do not speculate about why. Say the graph is empty and a rescan will fix it.
- When asked to implement or fix code: always use read_file first to read the current content, then propose changes with propose_file_edit. Never paste raw code blocks as final output for code changes — always use the tool.
- Available scan modes users can type: /full-scan, /auto-scan <hint>, /package-scan. Mention these when guiding users to scan.
- EFFICIENCY RULES (critical — you are rate-limited):
  * Never call the same tool with the same or equivalent query twice. If you already have a result, use it.
  * Maximum 2 search_files calls per response turn. Pick the most specific query.
  * Maximum 2 query_code_graph calls per response turn.
  * Maximum 3 read_file calls per response turn — only read files you will actually propose changes to.
  * Do not call get_project_memory more than once per conversation.
  * Aim to answer in 3 tool-call rounds or fewer. If you have enough information, stop calling tools and respond.

GROUNDING RULES (anti-hallucination — mandatory):
- Use ONLY class names, method names, file paths, and architecture that appear in tool results or user-provided context. Never invent them.
- If a class, file, or pattern is NOT present in the retrieved context — say explicitly: "This was not found in the scanned code."
- Do NOT assume framework conventions (e.g. "in Spring Boot you usually...") unless that convention is visible in the actual code.
- Do NOT say "typically", "usually", "in most projects", "the standard approach is". You are working with THIS project only.
- If the request cannot be completed with available context, state exactly what is missing and which tool would retrieve it.
- When reporting violations or architecture issues: always cite the exact file path, class name, and line number from tool results. If you don't have line numbers, say so.
- A correct "I don't have enough data" is better than a plausible-sounding fabrication.

Response formatting (output is rendered in a terminal/CLI — not a browser):
- Prefer section headers over large markdown tables
- Keep paragraphs short (1-3 sentences)
- Use bullet lists for structured data
- Avoid dense markdown tables
- When explaining a project, use this section order: Project Overview, Current Limitations, Architecture/Structure, Key Statistics, Recommended Next Steps, What the User Can Ask Next
- Never produce large compact markdown blocks that are hard to read in terminals
- Separate logical sections with clear headers and blank lines between them
- Keep explanations concise and structured"""

# Tools the AI can call (defined as Anthropic tool schemas)
TOOLS = [
    {
        "name": "get_project_memory",
        "description": "Load the project summary: architecture type, modules, rules, domain entities, and key decisions. Always call this first for any architecture-related question.",
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
                "content": {"type": "string", "description": "The knowledge to store"},
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
        "name": "propose_file_edit",
        "description": "Propose a code change to a specific file. Use when asked to implement, fix, or modify code. The user will review and accept or reject the change in the IDE.",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the file to edit"},
                "description": {"type": "string", "description": "What is being changed and why"},
                "original_snippet": {"type": "string", "description": "The exact original code to replace (empty string for new insertions)"},
                "new_snippet": {"type": "string", "description": "The replacement code"}
            },
            "required": ["file_path", "description", "new_snippet"]
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
            "message": "Project not yet scanned. Run Tools → Scan Project with EngramAI.",
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

        # Find nodes whose name or file path contains the query term (case-insensitive)
        cypher = """
            MATCH (n) WHERE n.project_id = $project_id
            AND (toLower(n.name) CONTAINS toLower($query)
              OR toLower(n.file_path) CONTAINS toLower($query))
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
                    OR toLower(b.name) CONTAINS toLower($query))
                RETURN a.name AS from, a.layer AS from_layer, b.name AS to, b.layer AS to_layer
                LIMIT 30
            """
            relationships = await neo4j_client.query(dep_cypher, {"project_id": project_id, "query": query_text})

        # Compress node records to only essential fields to reduce token usage
        _KEEP_FIELDS = {"name", "file_path", "layer"}
        nodes = [{k: v for k, v in n.items() if k in _KEEP_FIELDS} for n in nodes]

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
            files.append({
                "file_path": p.get("file_path", ""),
                "language": p.get("language", ""),
                "layer": p.get("layer", ""),
                "classes": p.get("classes", []),
                "functions": p.get("functions", []),
            })

        return json.dumps({
            "status": "ok",
            "query": query_text,
            "files": files,
            "message": f"Found {len(files)} file(s) matching '{query_text}'.",
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
            return json.dumps({
                "status": "ok",
                "file_path": file_path,
                "content": content,
                "lines": line_count,
                "truncated": truncated,
                "note": f"File truncated to {READ_FILE_MAX_CHARS} chars — propose edits based on visible content." if truncated else "",
            })
        except Exception as exc:
            return json.dumps({"status": "error", "message": str(exc)})

    elif tool_name == "propose_file_edit":
        # Return the proposal data — the streaming layer emits this as a file_edit SSE event
        return json.dumps({
            "status": "proposal_ready",
            "file_path": tool_input.get("file_path", ""),
            "description": tool_input.get("description", ""),
            "original_snippet": tool_input.get("original_snippet", ""),
            "new_snippet": tool_input.get("new_snippet", ""),
        })

    return json.dumps({"error": f"Unknown tool: {tool_name}"})


# ── Sub-agent system prompt extensions ────────────────────────────────────────

_DEBUGGER_PROMPT = """
You are acting as the Debugger Agent for this request.
Focus exclusively on: identifying root causes, tracing error paths, explaining why something breaks.
Read the relevant files first with read_file before diagnosing.
Be precise — cite file paths and line content. Do not speculate.
"""

_CODEGEN_PROMPT = """
You are acting as the Code Generation Agent for this request.
Focus exclusively on: writing clean, correct, DRY code that fits the existing architecture.
Always: (1) read the relevant files first, (2) check the code graph for existing components, (3) propose changes with propose_file_edit — never paste raw code blocks as final output.
Follow the project's existing naming conventions and layer rules.
"""

_ARCHITECT_PROMPT = """
You are acting as the Architecture Agent for this request.
Focus exclusively on: design decisions, layer violations, DRY enforcement, dependency analysis, and architectural impact.
Use query_code_graph and get_project_memory extensively before answering.
Think in terms of modules, layers, and long-term maintainability.
"""

_INTENT_KEYWORDS: list[tuple[str, list[str]]] = [
    ("debugger",  ["bug", "error", "fix", "broken", "crash", "fail", "exception", "traceback", "issue", "not working", "wrong", "incorrect"]),
    ("codegen",   ["implement", "add", "create", "write", "generate", "build", "make", "new method", "new class", "new endpoint", "scaffold"]),
    ("architect", ["architecture", "design", "structure", "pattern", "refactor", "dependency", "layer", "module", "coupling", "solid", "ddd"]),
]


def _detect_agent(message: str) -> str:
    """Return the best-matching sub-agent name based on message keywords."""
    lower = message.lower()
    scores: dict[str, int] = {}
    for agent, keywords in _INTENT_KEYWORDS:
        scores[agent] = sum(1 for kw in keywords if kw in lower)
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "main"


def _agent_prompt_extension(agent: str) -> str:
    if agent == "debugger":
        return _DEBUGGER_PROMPT
    if agent == "codegen":
        return _CODEGEN_PROMPT
    if agent == "architect":
        return _ARCHITECT_PROMPT
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
        "anthropic": "claude-opus-4-6",
        "openai": "gpt-4o",
        "gemini": "gemini-1.5-flash",
    }
    return defaults.get(provider_name.lower().strip(), "claude-opus-4-6")


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

    # Inject the current project_id into the system prompt so the LLM always
    # supplies the correct value when making tool calls.  Without this the LLM
    # has no basis for the project_id field and either hallucinates one or omits
    # it, causing load_memory to query for the wrong (or non-existent) project.
    system_prompt_with_context = (
        SYSTEM_PROMPT
        + f"\n\nCurrent project_id: {project_id}\n"
        "Always pass this exact project_id value when calling any tool."
    )

    # Append agent persona system prompt if set
    if agent_id:
        persona = await get_persona(agent_id)
        if persona and persona.get("system_prompt_extra"):
            system_prompt_with_context += f"\n\n{persona['system_prompt_extra']}"

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
        response = provider.chat(messages=messages, tools=TOOLS, system=system_prompt_with_context)

        if response.stop_reason == "end_turn":
            reply = response.reply or "I processed your request but had no text response."
            break

        if response.stop_reason == "tool_use":
            # Append assistant turn to history
            # For Anthropic the raw response content preserves tool_use blocks;
            # for Gemini we reconstruct a simple text message
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

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": result,
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

    system_prompt_with_context = (
        SYSTEM_PROMPT
        + f"\n\nCurrent project_id: {project_id}\n"
        "Always pass this exact project_id value when calling any tool."
        + _agent_prompt_extension(detected_agent)
    )

    # Append agent persona system prompt if set
    if agent_id:
        persona = await get_persona(agent_id)
        if persona and persona.get("system_prompt_extra"):
            system_prompt_with_context += f"\n\n{persona['system_prompt_extra']}"

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
    # Session-level tool result cache with TTL — prevents duplicate API calls
    # across the entire session, not just within a single request.
    cache_session_key = session_id or conversation_id or "__anonymous__"
    if cache_session_key not in _session_tool_cache:
        _session_tool_cache[cache_session_key] = {}
    tool_cache = _session_tool_cache[cache_session_key]

    while True:
        # Open a streaming request to Anthropic
        with client.messages.stream(
            model=resolved_model,
            max_tokens=4096,
            system=system_prompt_with_context,
            tools=TOOLS,
            messages=messages,
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
            yield _sse({"done": True})
            return

        # Hard limit: stop tool loop to prevent runaway API usage
        tool_round += 1
        if tool_round > MAX_TOOL_ROUNDS:
            logger.warning(
                "chat_stream [%s]: reached MAX_TOOL_ROUNDS (%d) — forcing end_turn",
                project_id, MAX_TOOL_ROUNDS,
            )
            if session_id and collected_text:
                await save_message(session_id, "assistant", collected_text)
            yield _sse({"chunk": "\n\n⚠ Reached maximum tool-call depth — partial results shown above."})
            yield _sse({"done": True})
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

            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tc["id"],
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})
        tool_calls = []
        # Continue the while loop to stream the next LLM turn


MAX_TOOL_ROUNDS = 6
# Maximum characters returned by read_file before truncation (prevents context bloat)
READ_FILE_MAX_CHARS = 12_000


def _sse(data: dict) -> str:
    """Format a dict as a Server-Sent Event line."""
    return f"data: {json.dumps(data)}\n\n"
