import os
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve the .env path relative to this file's location so the backend can be
# started from any working directory (e.g. the repo root, a Docker WORKDIR, or
# a VS Code task runner) without the relative "../.env" silently missing.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


def _env_int(key: str, fallback: int) -> int:
    """Read an integer env var, falling back to a default."""
    val = os.environ.get(key)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            pass
    return fallback


def _env_str(key: str, fallback: str) -> str:
    return os.environ.get(key) or fallback


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # Server
    host: str = "127.0.0.1"
    port: int = 8765

    # ── LLM API keys (set in .env on the server — users never see these) ──────
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    llm_provider: str = "anthropic"  # default provider
    llm_model: str = ""              # empty = use provider default

    # ── PostgreSQL ─────────────────────────────────────────────────────────────
    # CODELM_POSTGRES_PORT is injected by Electron at runtime with the
    # dynamically allocated port. Falls back to .env / hardcoded default.
    postgres_host: str = "localhost"
    postgres_port: int = _env_int("CODELM_POSTGRES_PORT", 54320)
    postgres_db: str = "codelm"
    postgres_user: str = "codelm"
    postgres_password: str = "codelm"

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Neo4j ──────────────────────────────────────────────────────────────────
    # CODELM_NEO4J_URI is injected by Electron at runtime.
    neo4j_uri: str = _env_str("CODELM_NEO4J_URI", "bolt://localhost:54321")
    neo4j_user: str = "neo4j"
    neo4j_password: str = "codelm123"

    # ── Qdrant ─────────────────────────────────────────────────────────────────
    # CODELM_QDRANT_PORT is injected by Electron at runtime.
    qdrant_host: str = "localhost"
    qdrant_port: int = _env_int("CODELM_QDRANT_PORT", 54323)
    qdrant_api_key: str = ""  # empty = no auth (local instance)


settings = Settings()
