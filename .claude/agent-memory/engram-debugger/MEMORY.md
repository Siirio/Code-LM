# CodeLM Debugger Memory Index

## Project
- [Backend Architecture](project_backend_arch.md) — Key file locations, data flow, and fragile areas in the Python backend

## Bugs Fixed
- [Chat 500 / Empty Response Bug](bug_chat_500_empty_response.md) — Root cause and fix for POST /api/v1/chat returning 500 with "Backend returned an empty response"
- [load_memory Returns None After Scan](bug_load_memory_returns_none.md) — Three root causes documented: (1) models not imported before create_all, (2) flush missing before to_dict, (3) _execute_tool reading project_id from LLM tool_input instead of the authoritative chat() parameter
- [Post-Scan "Not Indexed" / "Language Unknown"](bug_post_scan_not_indexed.md) — Three root causes: (1) Neo4j execute() not consuming result → silent write no-op, (2) _detect_stack returns "unknown" language → LLM misinterprets as unscanned, (3) save_memory failures had no explicit log trace

## Features Added
- [Qdrant Dimension Guard + Import-Based Stack Detection + Auto Arch Rules](feat_scan_improvements.md) — Three scan improvements: (1) ensure_collections() now detects and fixes dim mismatch, (2) _detect_stack falls back to imports when config files are missing/in subdirs, (3) scan_project() auto-saves architecture rules to PostgreSQL based on detected layers
