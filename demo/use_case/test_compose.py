"""Demo tests for compose_query against real SprintService / TaskService.

Run with verbose output to see the JSON response shapes::

    uv run pytest demo/use_case/test_compose.py -v -s

These tests double as human verification: print() output shows the actual
response payload for each scenario.
"""

from __future__ import annotations

import json

import pytest

from demo.use_case.database import init_db

from pydantic_resolve.use_case.manager import UseCaseAppConfig, UseCaseManager


@pytest.fixture(autouse=True)
async def setup_db():
    """Initialize the in-memory database before each test."""
    await init_db()


def _make_manager() -> UseCaseManager:
    # Import here so DB init runs first.
    from demo.use_case.services import SprintService, TaskService, UserService

    return UseCaseManager(
        apps=[
            UseCaseAppConfig(
                name="sprint",
                description="Sprint management",
                services=[UserService, TaskService, SprintService],
            ),
        ],
    )


def _print(label: str, payload: object) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(payload, indent=2, default=str))


# ──────────────────────────────────────────────────
# Scenario 1: multiple services in parallel
# ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_multi_service_parallel():
    app = _make_manager().get_app("sprint")
    result = await app.compose(
        """
        {
          SprintService { list_sprints { id name task_count } }
          TaskService { list_tasks { id title owner_detail { name } } }
        }
        """,
    )
    result = result["data"]
    _print("Multi-service parallel", result)
    assert "SprintService" in result
    assert "TaskService" in result
    assert result["SprintService"]["list_sprints"][0]["task_count"] >= 0
    assert result["TaskService"]["list_tasks"][0]["owner_detail"]["name"]


# ──────────────────────────────────────────────────
# Scenario 2: one service, multiple methods
# ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_single_service_multiple_methods():
    app = _make_manager().get_app("sprint")
    result = await app.compose(
        """
        {
          SprintService {
            list_sprints { id name }
            get_sprint(sprint_id: 1) { id name task_count }
          }
        }
        """,
    )
    result = result["data"]
    _print("Single service, multiple methods", result)
    svc = result["SprintService"]
    assert len(svc["list_sprints"]) >= 1
    assert svc["get_sprint"]["id"] == 1


# ──────────────────────────────────────────────────
# Scenario 3: selection projection on AutoLoad field
# ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_autoload_owner_detail_through_selection():
    """TaskSummary.owner_detail is AutoLoad — Resolver must fire and the
    selection must project it out."""
    app = _make_manager().get_app("sprint")
    result = await app.compose(
        """
        {
          TaskService {
            get_tasks_by_sprint(sprint_id: 1) {
              id title
              owner_detail { id name }
            }
          }
        }
        """,
    )
    result = result["data"]
    _print("AutoLoad projection", result)
    tasks = result["TaskService"]["get_tasks_by_sprint"]
    assert len(tasks) >= 1
    for t in tasks:
        assert set(t.keys()) == {"id", "title", "owner_detail"}
        assert t["owner_detail"]["name"]


# ──────────────────────────────────────────────────
# Scenario 4: end-to-end via MCP tool
# ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_query_tool_end_to_end():
    from demo.use_case.services import SprintService, TaskService, UserService
    from pydantic_resolve.use_case.mcp_server import (
        create_use_case_graphql_mcp_server,
    )

    mcp = create_use_case_graphql_mcp_server(
        apps=[
            UseCaseAppConfig(
                name="sprint",
                description="Sprint management",
                services=[UserService, TaskService, SprintService],
            ),
        ],
        name="Compose Demo API",
    )
    result = await mcp.call_tool(
        "compose_query",
        {
            "app_name": "sprint",
            "query": """
                {
                  TaskService {
                    list_tasks { id title owner_detail { name } }
                  }
                }
            """,
        },
    )
    data = json.loads(result.content[0].text)
    _print("MCP tool response envelope", data)
    assert data["success"] is True
    assert data["data"]["TaskService"]["list_tasks"][0]["owner_detail"]["name"]


# ──────────────────────────────────────────────────
# Scenario 5: error path
# ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compose_unknown_service_error():
    app = _make_manager().get_app("sprint")
    with pytest.raises(Exception, match="NoSuchService"):
        await app.compose(
            "{ NoSuchService { anything { id } } }",
        )
