from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class GraphQueryRequest(BaseModel):
    project_id: str
    query: str  # natural language — e.g. "invoice", "user authentication"
    depth: int = 2  # how many hops to expand from matched nodes


class GraphNode(BaseModel):
    id: str
    label: str  # Class, Function, Module, etc.
    name: str
    layer: str  # Controller, Service, Repository, Entity, DTO
    file_path: str


class GraphQueryResponse(BaseModel):
    nodes: list[GraphNode]
    edges: list[dict]


@router.post("/query", response_model=GraphQueryResponse)
async def query_graph(request: GraphQueryRequest):
    # TODO: translate to Cypher and query Neo4j
    return GraphQueryResponse(nodes=[], edges=[])


@router.get("/{project_id}/health-score")
async def architecture_health_score(project_id: str):
    # TODO: compute from graph metrics
    return {
        "project_id": project_id,
        "score": None,
        "message": "Graph not yet indexed.",
    }
