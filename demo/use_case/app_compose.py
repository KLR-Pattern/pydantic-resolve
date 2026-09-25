"""FastAPI app exposing the compose surface as an HTTP endpoint with GraphiQL.

This is the UseCase counterpart of ``demo/graphql/app.py``: instead of an
ErDiagram-driven GraphQL handler, it serves the fixed 3-level compose query
hierarchy (``Query → Service → Method → DTO fields``). Introspection
queries (``__schema`` / ``__type`` / ``__typename``) are dispatched
explicitly to :func:`compose_introspect`; data queries go through
``app.compose``.

Run::

    uv run uvicorn demo.use_case.app_compose:app --reload --port 8008

Then open http://localhost:8008/graphql in a browser. GraphiQL will fire
its canonical introspection query on load; once the schema populates the
left-hand Explorer panel will show every service, method, and DTO.

Port 8008 avoids collisions with the other demos:
- 8000: ``demo/graphql/app.py`` (Entity-based GraphQL)
- 8007: ``demo/use_case/app.py`` (UseCase FastAPI REST-style)
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

from pydantic_resolve.graphql import get_graphiql_html
from pydantic_resolve.use_case.introspection import (
    compose_introspect,
    is_introspection_query,
)
from pydantic_resolve.use_case.manager import UseCaseManager
from pydantic_resolve.use_case.manager import UseCaseAppConfig

from demo.use_case.database import init_db
from demo.use_case.services import SprintService, TaskService, UserService


# Single shared app — built once at import time so the schema is stable
# across requests and the in-memory database seeded by ``init_db`` persists.
_manager = UseCaseManager(
    apps=[
        UseCaseAppConfig(
            name="sprint",
            description="Sprint management (Users, Tasks, Sprints)",
            services=[UserService, TaskService, SprintService],
            enable_mutation=True,
        ),
    ]
)
_APP = _manager.get_app("sprint")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Pydantic-Resolve Compose GraphiQL Demo",
    description=(
        "HTTP + GraphiQL frontend for the compose surface. "
        "POST /graphql with a GraphQL query shaped as "
        "{ Service { method(args) { dtoField } } }."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class GraphQLRequest(BaseModel):
    query: str
    operationName: str | None = None
    variables: dict[str, Any] | None = None


_GRAPHIQL_HTML = get_graphiql_html(endpoint="/graphql", title="Compose GraphiQL")


router = APIRouter()


@router.get("/graphql", response_class=HTMLResponse)
async def graphiql() -> str:
    """Serve the GraphiQL IDE. It will POST to /graphql on execute."""
    return _GRAPHIQL_HTML


@router.post("/graphql")
async def graphql_endpoint(req: GraphQLRequest, request: Request) -> JSONResponse:
    """Run a compose query (data) or introspection query, dispatched explicitly."""
    auth_header = request.headers.get("Authorization", "")
    context: dict[str, Any] | None = None
    if auth_header.startswith("Bearer "):
        # Placeholder: in production decode the JWT and build a real context.
        context = {"user_id": 1}

    try:
        if is_introspection_query(req.query):
            # Both branches return the GraphQL {data, errors} envelope
            # directly — no re-wrapping.
            data = compose_introspect(_APP, req.query)
        else:
            # Field-level failures surface in `errors` with partial data.
            data = await _APP.compose(
                req.query, context=context, variables=req.variables
            )
    except Exception as e:
        # ComposeError and any other exception — return as GraphQL-style
        # {errors: [...]} envelope so GraphiQL surfaces them in the UI.
        return JSONResponse({"data": None, "errors": [{"message": str(e)}]})

    return JSONResponse(data)


app.include_router(router)


@app.get("/")
async def root() -> dict[str, Any]:
    """Landing page: link to the playground and show a sample query."""
    return {
        "message": "Pydantic-Resolve Compose GraphiQL Demo",
        "endpoints": {
            "playground": "/graphql (GET — GraphiQL IDE)",
            "query": "/graphql (POST — compose query)",
            "docs": "/docs",
        },
        "services": list(_APP.services.keys()),
        "example_query": (
            "{ SprintService { list_sprints { id name task_count } "
            "get_sprint(sprint_id: 1) { id name } } "
            "TaskService { list_tasks { id title owner_detail { name } } } }"
        ),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8008)
