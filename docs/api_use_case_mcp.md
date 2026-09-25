---
description: API reference for create_use_case_graphql_mcp_server — expose UseCase business operations to AI agents through MCP, using the same 4-layer progressive disclosure as the ERD-driven MCP server.
---

# UseCase MCP API

[中文版](./api_use_case_mcp.zh.md)

## create_use_case_graphql_mcp_server

```python
from pydantic_resolve.use_case import create_use_case_graphql_mcp_server

mcp = create_use_case_graphql_mcp_server(
    apps: list[UseCaseAppConfig],
    name: str = "Pydantic-Resolve UseCase GraphQL API",
) -> "FastMCP"
```

Creates an MCP server that exposes `UseCaseService` methods to AI agents via a 4-layer progressive disclosure pattern using GraphQL-string style for the data layer.

| Parameter | Type | Description |
|-----------|------|-------------|
| `apps` | `list[UseCaseAppConfig]` | Application configurations |
| `name` | `str` | MCP server name (default: `"Pydantic-Resolve UseCase GraphQL API"`) |

Returns a configured `FastMCP` server instance.

```python
mcp = create_use_case_graphql_mcp_server(
    apps=[UseCaseAppConfig(name="project", services=[TaskService])]
)
mcp.run(transport="streamable-http", port=8080)
```

## UseCaseAppConfig

```python
from pydantic_resolve.use_case import UseCaseAppConfig

UseCaseAppConfig(
    name: str,
    services: list[type[UseCaseService]],
    description: str | None = None,
    enable_mutation: bool = True,
    context_extractor: Callable | None = None,
)
```

| Parameter | Type | Description |
|-----------|------|-------------|
| `name` | `str` | Application name (required) |
| `services` | `list[type[UseCaseService]]` | List of UseCaseService subclasses (required) |
| `description` | `str \| None` | Application description for AI agents |
| `enable_mutation` | `bool` | Whether mutation methods are visible in MCP (default: `True`) |
| `context_extractor` | `Callable \| None` | Callback to extract request-scoped context |

### context_extractor

Optional callback that extracts request-scoped context (e.g. user identity from Authorization header) from the MCP HTTP request. The extracted dict is merged into method kwargs for parameters annotated with `FromContext`.

Signature: `(Context) -> dict | Awaitable[dict]`, supports both sync and async.

```python
from fastmcp.server.context import Context
from fastmcp.server.dependencies import get_http_headers

def extract_user_context(ctx: Context) -> dict:
    headers = get_http_headers(include={"authorization"})
    auth = headers.get("authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:]
        return {"user_id": int(token)}
    return {}

apps = [
    UseCaseAppConfig(
        name="project",
        services=[TaskService],
        context_extractor=extract_user_context,
    ),
]
```

Data flow:

```
HTTP Request (Authorization: Bearer <token>)
  → FastMCP Context
    → context_extractor(ctx) → {"user_id": 1}
      → method invocation merges context into kwargs
        → TaskService.get_my_tasks(user_id=1)
```

**Important:** `get_http_headers()` excludes `authorization`, `content-type`, and other sensitive headers by default. You must pass `include={"authorization"}` to receive the Authorization header. When MCP runs via stdio transport (no HTTP request), `get_http_headers()` returns an empty dict.

## UseCaseService

```python
from pydantic_resolve.use_case import UseCaseService
from pydantic_resolve import query, mutation

class MyService(UseCaseService):
    """Service description (used by AI agents)."""

    @query
    async def my_method(cls, param1: int) -> MyDTO:
        """Method description (used by AI agents)."""
        ...
```

Base class for business service definitions. The `BusinessMeta` metaclass automatically discovers methods decorated with `@query` or `@mutation` and stores them for introspection.

**Conventions:**

- Methods must be decorated with `@query` or `@mutation` (from `pydantic_resolve`)
- Methods must be `async`
- Private methods (prefixed with `_`) and `get_tag_name` are excluded from discovery
- Docstrings on the class and methods become descriptions visible to AI agents
- Return type annotations are used for SDL type generation

### get_tag_name

```python
@classmethod
def get_tag_name(cls) -> str
```

Returns the class name by default. Override to customize the OpenAPI tag name when using with FastAPI:

```python
class TaskService(UseCaseService):
    @classmethod
    def get_tag_name(cls):
        return "Tasks"

# Usage in FastAPI
@app.get("/tasks", tags=[TaskService.get_tag_name()])
```

## FromContext

```python
from typing import Annotated
from pydantic_resolve.use_case import FromContext

user_id: Annotated[int, FromContext()]
```

Marker annotation for method parameters that should receive values from `context_extractor` rather than from the GraphQL query. This keeps the method signature identical for both FastAPI (parameter passed directly) and MCP (injected from context).

```python
class TaskService(UseCaseService):
    @query
    async def get_my_tasks(
        cls,
        user_id: Annotated[int, FromContext()],
    ) -> list[TaskSummary]:
        ...
```

- If the context key is present, it is injected into the method call
- If the context key is missing and the parameter has no default, an error is returned
- If the context key is missing and the parameter has a default, the default is used
- `FromContext` parameters cannot be supplied via GraphQL query arguments

## Progressive Disclosure Tools

The MCP server registers four tools, organized as a discovery funnel from cheap (broad) to expensive (precise):

| Tool | Layer | Description |
|------|-------|-------------|
| `list_apps` | 1 | Cheap app discovery — names + service counts |
| `describe_compose_schema` | 2 | Per-app service + method listing (no args / types / DTO fields) |
| `describe_compose_method` | 3 | Per-method detail: args, return type, and an SDL string with the full type tree |
| `compose_query` | 4 | Execute a GraphQL data query against the compose surface |

The funnel intentionally delays loading detailed type information until the agent has selected a specific method. Schema discovery is via Layers 2 + 3 — `compose_query` rejects GraphQL introspection (`__schema`, `__type`, `__typename`) and points back to `describe_compose_schema`.

### list_apps

```python
list_apps() -> dict
```

Returns metadata for every configured application.

```python
{
  "success": True,
  "data": [
    {"name": "project", "description": "...", "services_count": 3}
  ],
  "hint": "Use describe_compose_schema(app_name='project') ..."
}
```

### describe_compose_schema

```python
describe_compose_schema(app_name: str) -> dict
```

Lists services and methods for an app. Compact: names + kinds + descriptions only. Mutations are filtered out when the app has `enable_mutation=False`.

```python
{
  "success": True,
  "data": {
    "services": {
      "TaskService": {
        "description": "Task management service.",
        "methods": [
          {"name": "list_tasks", "kind": "query", "description": "Get all tasks."},
          {"name": "get_task", "kind": "query", "description": "Get a task by ID."}
        ]
      }
    }
  },
  "hint": "Use describe_compose_method(app_name='project', service_name='TaskService', method_name='get_task') ..."
}
```

### describe_compose_method

```python
describe_compose_method(
    app_name: str,
    service_name: str,
    method_name: str,
) -> dict
```

Returns detailed info for a single method: args (with types + defaults), return type, and an `sdl` string. The `sdl` shows the method signature as a comment header followed by full type definitions for the return DTO and every nested DTO reachable through its fields. Use this as the source of truth for field names — top-level and nested alike — before composing a query.

```python
{
  "success": True,
  "data": {
    "name": "get_task",
    "kind": "query",
    "description": "Get a task by ID.",
    "args": [{"name": "task_id", "type": "int"}],
    "returns": "TaskSummary",
    "sdl": "# TaskService.get_task(task_id: Int): TaskSummary\n\ntype TaskSummary {\n  id: Int!\n  title: String!\n  owner: UserSummary\n}\n\ntype UserSummary {\n  id: Int!\n  name: String!\n}"
  },
  "hint": "Use compose_query(app_name='project', query='{ TaskService { get_task(task_id: 1) { title } } }') ..."
}
```

### compose_query

```python
compose_query(
    app_name: str,
    query: str,
    variables: dict[str, Any] | None = None,
) -> dict
```

Executes a GraphQL data query against the compose surface. Fixed 3-level hierarchy: `Query → Service → Method → DTO field selection`. Useful for fetching related data across services in one round trip.

**Rules:**

- Method-level aliases are supported (GraphQL `alias: field` syntax): each aliased invocation is an independent call and the response is keyed by the alias. Nested (DTO-level) aliases are not supported. Each response key must be unique within its parent — use aliases to invoke one method multiple times with different arguments.
- Pass string arguments via `variables` (declared as `query ($name: Type!) ...`) — never inline them as GraphQL literals if they might contain quotes, backslashes or newlines; `variables` sidesteps all escaping. Every declared variable must be provided explicitly (declared defaults are not applied).
- Service / method names must match the schema. Use `describe_compose_schema` to discover valid names.
- Method arguments go in parentheses on the method field: `get_sprint(sprint_id: 1)`.
- Parameters marked `FromContext` cannot be set from query arguments — they are server-injected.
- DTO field selection under each method projects into that method's return DTO. Nested DTOs require sub-selection; on a wrong sub-field the error response lists the available fields for that DTO.
- Mutations require the app to have `enable_mutation=True`.
- Introspection queries (`__schema` / `__type` / `__typename`) are rejected — use `describe_compose_schema` instead.

**Execution semantics:**

- `@query` methods run concurrently.
- `@mutation` methods run serially in declaration order.
- The relative ordering between queries and mutations within a single `compose_query` call is NOT guaranteed. For create-then-read semantics, issue them as separate calls.
- Validation failures (unknown fields, duplicate response keys, missing variables, ...) are detected before any method executes and fail the whole call. Execution failures are isolated per field: the failed response key is `null` and the call still returns `success=True` with partial data plus an `errors` list (`extensions.code`: `QUERY_FAILED` / `MUTATION_FAILED` / `SKIPPED_PRIOR_FAILURE` / `PROJECTION_FAILED`).
- Mutation feedback is three-state: a succeeded call keeps its result, a failed call nulls only its own key with `MUTATION_FAILED`, and every later mutation in the same call is skipped with `SKIPPED_PRIOR_FAILURE`. Already-executed writes are never erased from the response.

**Response shape** mirrors the request: each Service becomes a key whose value is a dict of response key (alias when present, otherwise method name) → result.

```python
compose_query(
    app_name="project",
    query='''
    {
      SprintService {
        list_sprints { id name }
        get_sprint(sprint_id: 1) { name }
      }
      TaskService {
        get_task(task_id: 1) { title owner_id }
      }
    }
    ''',
)
```

On failure: `success=False`, `error`, `error_type` (one of `validation_error`, `type_not_found`, `operation_not_found`, `query_execution_error`, `mutation_execution_error`, `app_not_found`, `internal_error`).
