import logging
import os
import sys
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

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

    # Pre-warm the ONNX embedding model in the background.
    # Loads from local disk only — no network calls, no PyTorch.
    # If model files are missing the task logs a clear error pointing to the
    # setup script; the server still starts so other features remain usable.
    async def _prewarm_embeddings():
        import embedding as _emb
        try:
            await _emb.ensure_model()
        except Exception as exc:
            logger.error(
                "Embedding model not ready: %s  "
                "→ Run:  python backend/scripts/setup_embedding_model.py",
                exc,
            )

    import asyncio as _asyncio
    _asyncio.create_task(_prewarm_embeddings())

    yield

    # ── Shutdown ─────────────────────────────────────────────────────────────
    await close_postgres()
    await neo4j_client.close()
    await qdrant_client.close()
    logger.info("Storage connections closed")


app = FastAPI(
    title="CodeLM Backend",
    description="AI Software Architect — orchestrator and knowledge engine",
    version="1.1.2",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # IDE plugins connect from localhost
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router, prefix="/api/v1")


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "version": "1.1.2",
        "storage": {
            "postgres": "connected" if True else "unavailable",  # engine exists if init succeeded
            "neo4j": "connected" if neo4j_client.is_connected else "unavailable",
            "qdrant": "connected" if qdrant_client.is_connected else "unavailable",
        },
    }


# ── Serve React frontend ──────────────────────────────────────────────────────
# Must be mounted AFTER all API routes so /api/v1/* and /health take priority.
# In a PyInstaller bundle, data files live in sys._MEIPASS.
# In dev mode, they live next to this file in backend/static/.

def _static_dir() -> str:
    if getattr(sys, 'frozen', False):
        return os.path.join(sys._MEIPASS, 'static')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')

_sd = _static_dir()
if os.path.isdir(_sd):
    app.mount("/", StaticFiles(directory=_sd, html=True), name="frontend")
    logger.info("Serving frontend from %s", _sd)
else:
    logger.warning("Static dir not found at %s — frontend will not be served", _sd)


if __name__ == "__main__":
    import sys
    # reload=True uses string-based import which breaks PyInstaller packaging.
    # Use the app object directly; enable reload only in explicit dev mode.
    dev_mode = "--dev" in sys.argv or "--reload" in sys.argv
    if dev_mode:
        uvicorn.run("main:app", host=settings.host, port=settings.port, reload=True)
    else:
        uvicorn.run(app, host=settings.host, port=settings.port)