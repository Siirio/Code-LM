from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Resolve the .env path relative to this file's location so the backend can be
# started from any working directory (e.g. the repo root, a Docker WORKDIR, or
# a VS Code task runner) without the relative "../.env" silently missing.
_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(_ENV_FILE), extra="ignore")

    # Server
    host: str = "127.0.0.1"
    port: int = 8765

    # ── PostgreSQL ────────────────────────────────────────────────────────────
    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str = "engramai"
    postgres_user: str = "engramai"
    postgres_password: str = "engramai"

    @property
    def postgres_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    # ── Neo4j ─────────────────────────────────────────────────────────────────
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "engramai"

    # ── Qdrant ────────────────────────────────────────────────────────────────
    qdrant_host: str = "localhost"
    qdrant_port: int = 6333
    qdrant_api_key: str = ""  # empty = no auth (local instance)


settings = Settings()
