"""Neo4j connection via the official async driver.

Stores: class/module relationships, dependencies, import graph,
        call graph, layer classifications (Controller/Service/Repository/etc.).
"""
from typing import Any

from neo4j import AsyncGraphDatabase, AsyncDriver

from config import settings


class Neo4jClient:
    def __init__(self) -> None:
        self._driver: AsyncDriver | None = None

    async def connect(self) -> None:
        self._driver = AsyncGraphDatabase.driver(
            settings.neo4j_uri,
            auth=(settings.neo4j_user, settings.neo4j_password),
        )
        await self._driver.verify_connectivity()

    async def close(self) -> None:
        if self._driver:
            await self._driver.close()
            self._driver = None

    async def query(self, cypher: str, params: dict[str, Any] | None = None) -> list[dict]:
        """Run a read query and return all records as plain dicts."""
        if self._driver is None:
            raise RuntimeError("Neo4j not connected — call connect() first")
        async with self._driver.session() as session:
            result = await session.run(cypher, params or {})
            return [record.data() async for record in result]

    async def execute(self, cypher: str, params: dict[str, Any] | None = None) -> None:
        """Run a write statement (no return value needed).

        session.run() is lazy — the driver does not guarantee the write has
        been sent to the server until the result is consumed.  Calling
        result.consume() forces the query to complete before the session
        closes, preventing silent no-ops on write queries (MERGE, CREATE, SET).
        """
        if self._driver is None:
            raise RuntimeError("Neo4j not connected — call connect() first")
        async with self._driver.session() as session:
            result = await session.run(cypher, params or {})
            await result.consume()

    @property
    def is_connected(self) -> bool:
        return self._driver is not None


neo4j_client = Neo4jClient()
