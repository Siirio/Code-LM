"""Qdrant connection for vector / semantic search.

Stores: embeddings of files, functions, classes, and comments.
Used by the Vector Agent for semantic file retrieval (Layer 4 of the context pipeline).
"""
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import Distance, VectorParams

from config import settings

# Collection names used across the system
COLLECTION_FILES = "project_files"
COLLECTION_FUNCTIONS = "project_functions"  # NOT IMPLEMENTED — collection is created but nothing writes to it
COLLECTION_DOCS = "project_docs"  # .md and PDF files indexed as full text

# Embedding dimension — matches sentence-transformers all-MiniLM-L6-v2 output (384).
# Changing this requires dropping and recreating Qdrant collections.
EMBEDDING_DIM = 384


class QdrantClientWrapper:
    def __init__(self) -> None:
        self._client: AsyncQdrantClient | None = None

    async def connect(self) -> None:
        kwargs: dict = {"host": settings.qdrant_host, "port": settings.qdrant_port}
        if settings.qdrant_api_key:
            kwargs["api_key"] = settings.qdrant_api_key
        self._client = AsyncQdrantClient(**kwargs)
        # Verify connectivity
        await self._client.get_collections()

    async def close(self) -> None:
        if self._client:
            await self._client.close()
            self._client = None

    async def ensure_collections(self) -> None:
        """Create default collections if they don't exist yet.

        Also checks the vector dimension of any existing collection.  If it
        differs from EMBEDDING_DIM (e.g. the collection was created with
        dim=1536 but the scanner now writes 384-dim vectors) the stale
        collection is deleted and recreated with the correct dimension.
        Qdrant does not allow in-place dimension changes, so a delete+recreate
        is the only safe path.
        """
        if self._client is None:
            raise RuntimeError("Qdrant not connected — call connect() first")
        existing = {c.name for c in (await self._client.get_collections()).collections}
        for name in (COLLECTION_FILES, COLLECTION_FUNCTIONS, COLLECTION_DOCS):
            if name in existing:
                # Verify the stored dimension matches our current EMBEDDING_DIM.
                info = await self._client.get_collection(collection_name=name)
                # The vectors_config is a dict keyed by vector name for named
                # vectors, or a single VectorParams object for unnamed vectors.
                # Handle both shapes.
                stored_dim: int | None = None
                vc = info.config.params.vectors
                if hasattr(vc, "size"):
                    # Unnamed vector — VectorParams object directly
                    stored_dim = vc.size
                elif isinstance(vc, dict):
                    # Named vectors — take the first entry
                    first = next(iter(vc.values()), None)
                    if first is not None and hasattr(first, "size"):
                        stored_dim = first.size

                if stored_dim is not None and stored_dim != EMBEDDING_DIM:
                    import logging as _logging
                    _logging.getLogger(__name__).warning(
                        "Qdrant collection '%s' has dim=%d but EMBEDDING_DIM=%d — "
                        "deleting and recreating with correct dimension",
                        name, stored_dim, EMBEDDING_DIM,
                    )
                    await self._client.delete_collection(collection_name=name)
                    await self._client.create_collection(
                        collection_name=name,
                        vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
                    )
                # Dimension matches — nothing to do.
            else:
                await self._client.create_collection(
                    collection_name=name,
                    vectors_config=VectorParams(size=EMBEDDING_DIM, distance=Distance.COSINE),
                )

    @property
    def client(self) -> AsyncQdrantClient:
        if self._client is None:
            raise RuntimeError("Qdrant not connected — call connect() first")
        return self._client

    @property
    def is_connected(self) -> bool:
        return self._client is not None


qdrant_client = QdrantClientWrapper()
