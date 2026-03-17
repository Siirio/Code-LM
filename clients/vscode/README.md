# EngramAI — VSCode Extension (future)

This folder will contain the VSCode extension client.
It will call the same Python backend HTTP API as the IntelliJ plugin.

Backend base URL: http://127.0.0.1:8765/api/v1

Endpoints used:
- POST /chat
- POST /projects/scan
- GET  /memory/{project_id}
- POST /graph/query
- GET  /graph/{project_id}/health-score
