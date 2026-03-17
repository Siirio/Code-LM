---
name: Post-Scan "Not Indexed" / "Language Unknown" Bug
description: Three root causes behind Claude reporting "not scanned" or "language unknown" after a successful scan
type: project
---

Root causes identified and fixed on 2026-03-16:

**Bug 1 — Neo4j silent write failure** (`storage/neo4j_client.py`)
`execute()` called `session.run()` but discarded the `AsyncResult` object.
The neo4j async driver is lazy: the Cypher query is not guaranteed to reach
the server until the result is consumed.  Without `await result.consume()`,
MERGE statements could silently no-op, leaving the code graph empty after scan.
Fix: added `result = await session.run(...)` then `await result.consume()`.

**Bug 2 — Language "unknown" in summary triggers LLM misinterpretation**
(`scanner/project_scanner.py`)
`_detect_stack()` returns `language: "unknown"` when no config file (pom.xml,
package.json, go.mod, requirements.txt, pyproject.toml) is found.  The stored
summary then reads "Language: unknown" which Claude interprets as "project not
properly scanned" and tells the user so.
Fix: added `_infer_language_from_extensions(source_files)` that counts file
extensions and returns the dominant language as a fallback.  Called immediately
after `_detect_stack()` when language is still "unknown".

**Bug 3 — Silent PostgreSQL failure leaves project un-indexed with no log trace**
(`scanner/project_scanner.py`)
`save_memory()` and `mark_project_scanned()` had no surrounding try/except,
so any DB exception (FK violation, connection drop, constraint error) propagated
silently out of the scan, but because the exception was caught by the broad
endpoint handler and logged only as "Scan failed", the actual cause (postgres
write failure) was invisible.
Fix: wrapped both calls in an explicit try/except that calls `logger.exception()`
with a clear message ("FAILED to persist memory/project-status to PostgreSQL")
before re-raising, ensuring the root cause is always visible in server logs.

**Why:** The compound effect was that scan returned 200 with valid file counts
but the ProjectMemory row was absent, so load_memory returned None, so the
orchestrator returned status: not_indexed, so Claude told the user to re-scan.

**How to apply:** When debugging "project not scanned" after a successful scan,
check: (1) server logs for the explicit FAILED message, (2) neo4j node count
with a direct Cypher query, (3) postgres project_memory table directly.
