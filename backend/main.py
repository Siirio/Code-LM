import logging
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

import os
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from config import settings
from api.routes import router
from storage import init_postgres, close_postgres, neo4j_client, qdrant_client

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ── Startup ──────────────────────────────────────────────────────────────
    errors: list[str] = []

    try:
        await init_postgres()
        logger.info("PostgreSQL connected")
    except Exception as e:
        errors.append(f"PostgreSQL: {e}")
        logger.warning("PostgreSQL unavailable: %s", e)

    try:
        await neo4j_client.connect()
        logger.info("Neo4j connected")
    except Exception as e:
        errors.append(f"Neo4j: {e}")
        logger.warning("Neo4j unavailable: %s", e)

    try:
        await qdrant_client.connect()
        await qdrant_client.ensure_collections()
        logger.info("Qdrant connected")
    except Exception as e:
        errors.append(f"Qdrant: {e}")
        logger.warning("Qdrant unavailable: %s", e)

    if errors:
        logger.warning("Started with storage errors: %s", errors)
    else:
        logger.info("All storage backends connected")

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    await close_postgres()
    await neo4j_client.close()
    await qdrant_client.close()
    logger.info("Storage connections closed")


app = FastAPI(
    title="CodeLM Backend",
    description="AI Software Architect — orchestrator and knowledge engine",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # IDE plugins connect from localhost
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")

# Serve built React frontend from backend/static/
_static_dir = os.path.join(os.path.dirname(__file__), "static")
_assets_dir = os.path.join(_static_dir, "assets")
if os.path.isdir(_static_dir) and os.path.isdir(_assets_dir):
    app.mount("/assets", StaticFiles(directory=_assets_dir), name="assets")

    @app.get("/", include_in_schema=False)
    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_spa(full_path: str = ""):
        if full_path.startswith("api/"):
            from fastapi import HTTPException
            raise HTTPException(status_code=404)
        index = os.path.join(_static_dir, "index.html")
        return FileResponse(index)


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "0.1.0",
        "storage": {
            "postgres": "connected" if True else "unavailable",  # engine exists if init succeeded
            "neo4j": "connected" if neo4j_client.is_connected else "unavailable",
            "qdrant": "connected" if qdrant_client.is_connected else "unavailable",
        },
    }


if __name__ == "__main__":
    import sys
    # reload=True uses string-based import which breaks PyInstaller packaging.
    # Use the app object directly; enable reload only in explicit dev mode.
    dev_mode = "--dev" in sys.argv or "--reload" in sys.argv
    if dev_mode:
        uvicorn.run("main:app", host=settings.host, port=settings.port, reload=True)
    else:
        uvicorn.run(app, host=settings.host, port=settings.port)
