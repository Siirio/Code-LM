---
name: query_code_graph empty nodes bug
description: Root cause of query_code_graph returning "0 nodes" despite Neo4j having data — overview path returned nodes:[] which the LLM interpreted as empty
type: project
---

The overview path of `query_code_graph` in `orchestrator/orchestrator.py` put summary data only in `layer_summary` and returned `"nodes": []`. The system prompt rule says: "When query_code_graph returns 0 nodes, say 'Code graph is empty'." The LLM saw `nodes: []` and triggered that rule even though the graph had 314 nodes.

**Why:** The Cypher queries and neo4j_client both worked correctly. The bug was purely in the JSON response shape — the LLM decision heuristic checks `nodes`, not `layer_summary`.

**How to apply:** If the LLM ever reports "code graph is empty" again, check whether `_execute_tool("query_code_graph", ...)` returns `"nodes": []`. The fix is to always put real data in the `nodes` key. For overview queries, `nodes` should be populated with the summary rows (type/layer/count), not left as an empty list.
