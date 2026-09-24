"""Tests for UseCase compose — GraphQL-style multi-method composition.

The MCP-tool surface (``list_apps`` → ``describe_compose_schema`` →
``describe_compose_method`` → ``compose_query`` via the FastMCP server
factory) lives in ``test_compose_mcp.py``. This file covers
``UseCaseResources.compose`` — the Python API used by the MCP tool, the
HTTP GraphiQL demo, and direct callers.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Annotated, Optional

import pytest
from pydantic import BaseModel

from pydantic_resolve import Resolver, query, mutation
from pydantic_resolve.use_case.business import UseCaseService
from pydantic_resolve.use_case.compose import ComposeError
from pydantic_resolve.use_case.context import FromContext
from pydantic_resolve.use_case.manager import UseCaseAppConfig, UseCaseManager


# ──────────────────────────────────────────────────
# DTOs
# ──────────────────────────────────────────────────


class OwnerDTO(BaseModel):
    id: int
    name: str


class TaskDTO(BaseModel):
    id: int
    title: str
    owner_id: int
    owner: Optional[OwnerDTO] = None

    def resolve_owner(self):
        return OwnerDTO(id=self.owner_id, name=f"User{self.owner_id}")


class SprintDTO(BaseModel):
    id: int
    name: str
    task_count: int = 0


class UuidDTO(BaseModel):
    id: uuid.UUID


# ──────────────────────────────────────────────────
# Services
# ──────────────────────────────────────────────────


class SprintService(UseCaseService):
    """Sprint service."""

    @query
    async def list_sprints(cls) -> list[SprintDTO]:
        """List all sprints."""
        return [
            SprintDTO(id=1, name="Sprint A"),
            SprintDTO(id=2, name="Sprint B"),
        ]

    @query
    async def get_sprint(cls, sprint_id: int) -> Optional[SprintDTO]:
        """Get a sprint by ID."""
        if sprint_id == 1:
            return SprintDTO(id=1, name="Sprint A")
        return None


class TaskService(UseCaseService):
    """Task service."""

    @query
    async def list_tasks(cls) -> list[TaskDTO]:
        """List all tasks."""
        return [
            TaskDTO(id=10, title="Task 10", owner_id=1),
            TaskDTO(id=11, title="Task 11", owner_id=2),
        ]

    @query
    async def get_task(
        cls, task_id: int, include_owner: bool = True
    ) -> Optional[TaskDTO]:
        """Get a task by ID."""
        dto = TaskDTO(id=task_id, title=f"Task {task_id}", owner_id=1)
        # Method self-resolves so resolve_owner fires — compose does not
        # run Resolver on returned DTOs.
        resolved = await Resolver().resolve([dto])
        return resolved[0]

    @mutation
    async def create_task(cls, title: str) -> TaskDTO:
        """Create a task."""
        return TaskDTO(id=99, title=title, owner_id=1)


class ContextService(UseCaseService):
    """Service demonstrating FromContext param."""

    @query
    async def get_my_tasks(
        cls, user_id: Annotated[int, FromContext()]
    ) -> list[TaskDTO]:
        """Return tasks for the calling user."""
        return [TaskDTO(id=task_id, title=f"Task of {user_id}", owner_id=user_id) for task_id in (1, 2)]


class UuidService(UseCaseService):
    @query
    async def get_one(cls) -> UuidDTO:
        return UuidDTO(id=uuid.UUID("12345678-1234-5678-1234-567812345678"))

    @query
    async def get_payload(cls) -> dict:
        return {
            "id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
            "nested": {
                "item_id": uuid.UUID("87654321-4321-8765-4321-876543218765")
            },
        }


# Module-level log so SeqService methods can record their completion order
# across calls within a single compose query. Cleared per-test via fixture.
_seq_log: list[str] = []


class SeqService(UseCaseService):
    """Service for verifying execution ordering of queries vs mutations."""

    @query
    async def slow_query(cls) -> str:
        await asyncio.sleep(0.02)
        _seq_log.append("slow_query")
        return "slow_query"

    @query
    async def fast_query(cls) -> str:
        _seq_log.append("fast_query")
        return "fast_query"

    @mutation
    async def slow_mutation(cls) -> str:
        await asyncio.sleep(0.02)
        _seq_log.append("slow_mutation")
        return "slow_mutation"

    @mutation
    async def fast_mutation(cls) -> str:
        _seq_log.append("fast_mutation")
        return "fast_mutation"


# ──────────────────────────────────────────────────
# Manager / app fixtures
# ──────────────────────────────────────────────────


def _make_manager(
    *,
    enable_mutation: bool = True,
    context_extractor=None,
    with_seq: bool = False,
    with_uuid: bool = False,
) -> UseCaseManager:
    services = [SprintService, TaskService]
    if with_uuid:
        services.append(UuidService)
    if with_seq:
        services.append(SeqService)
    return UseCaseManager(
        apps=[
            UseCaseAppConfig(
                name="project",
                description="project app",
                services=services,
                enable_mutation=enable_mutation,
                context_extractor=context_extractor,
            ),
        ],
    )


def _make_context_manager() -> UseCaseManager:
    return UseCaseManager(
        apps=[
            UseCaseAppConfig(
                name="project",
                description="project app",
                services=[ContextService],
                context_extractor=lambda ctx: {"user_id": ctx.get("user_id", 0)},
            ),
        ],
    )


async def _compose(app, query: str, **kwargs):
    """Call ``app.compose`` and unwrap the ``{data, errors}`` contract.

    Behavior tests want the data dict; unexpected field errors fail loudly
    here instead of leaking None into assertions. Contract-specific tests
    call ``app.compose`` directly.
    """
    result = await app.compose(query, **kwargs)
    if result["errors"]:
        raise AssertionError(f"unexpected field errors: {result['errors']}")
    return result["data"]


# ──────────────────────────────────────────────────
# Happy path
# ──────────────────────────────────────────────────


class TestComposeHappyPath:
    @pytest.mark.asyncio
    async def test_single_service_single_method(self):
        app = _make_manager().get_app("project")
        result = await _compose(app, 
            "{ SprintService { list_sprints { id name } } }",
        )
        assert list(result.keys()) == ["SprintService"]
        assert list(result["SprintService"].keys()) == ["list_sprints"]
        sprints = result["SprintService"]["list_sprints"]
        assert len(sprints) == 2
        assert sprints[0] == {"id": 1, "name": "Sprint A"}

    @pytest.mark.asyncio
    async def test_single_service_multiple_methods(self):
        app = _make_manager().get_app("project")
        result = await _compose(app, 
            "{ SprintService { list_sprints { id } get_sprint(sprint_id: 1) { name } } }",
        )
        svc = result["SprintService"]
        assert len(svc["list_sprints"]) == 2
        assert svc["get_sprint"] == {"name": "Sprint A"}

    @pytest.mark.asyncio
    async def test_multiple_services_parallel(self):
        app = _make_manager().get_app("project")
        result = await _compose(app, 
            """
            {
              SprintService { list_sprints { id } }
              TaskService { list_tasks { id title } }
            }
            """,
        )
        assert set(result.keys()) == {"SprintService", "TaskService"}
        assert len(result["TaskService"]["list_tasks"]) == 2

    @pytest.mark.asyncio
    async def test_method_with_argument_int_coercion(self):
        app = _make_manager().get_app("project")
        result = await _compose(app, 
            "{ TaskService { get_task(task_id: 42) { id title } } }",
        )
        assert result["TaskService"]["get_task"] == {"id": 42, "title": "Task 42"}

    @pytest.mark.asyncio
    async def test_optional_argument_with_default_omitted(self):
        app = _make_manager().get_app("project")
        # include_owner has default=True; omit it.
        result = await _compose(app, 
            "{ TaskService { get_task(task_id: 1) { id } } }",
        )
        assert result["TaskService"]["get_task"]["id"] == 1

    @pytest.mark.asyncio
    async def test_list_return_with_projection(self):
        app = _make_manager().get_app("project")
        result = await _compose(app, 
            "{ SprintService { list_sprints { id name } } }",
        )
        sprints = result["SprintService"]["list_sprints"]
        assert all(set(s.keys()) == {"id", "name"} for s in sprints)

    @pytest.mark.asyncio
    async def test_optional_dto_returning_none(self):
        app = _make_manager().get_app("project")
        result = await _compose(app, 
            "{ SprintService { get_sprint(sprint_id: 999) { name } } }",
        )
        assert result["SprintService"]["get_sprint"] is None

    @pytest.mark.asyncio
    async def test_uuid_field_serializes_to_json_string(self):
        app = _make_manager(with_uuid=True).get_app("project")
        result = await _compose(app, 
            "{ UuidService { get_one { id } } }",
        )
        assert result["UuidService"]["get_one"] == {
            "id": "12345678-1234-5678-1234-567812345678"
        }

    @pytest.mark.asyncio
    async def test_dict_payload_recursively_serializes_uuid_values(self):
        app = _make_manager(with_uuid=True).get_app("project")
        result = await _compose(app, 
            "{ UuidService { get_payload } }",
        )
        assert result["UuidService"]["get_payload"] == {
            "id": "12345678-1234-5678-1234-567812345678",
            "nested": {
                "item_id": "87654321-4321-8765-4321-876543218765"
            },
        }

    @pytest.mark.asyncio
    async def test_self_resolved_method_result_is_projected(self):
        """DTO.resolve_owner fires inside the method (self-resolve); compose just projects."""
        app = _make_manager().get_app("project")
        result = await _compose(app, 
            "{ TaskService { get_task(task_id: 1) { id owner { id name } } } }",
        )
        task = result["TaskService"]["get_task"]
        assert task["owner"] == {"id": 1, "name": "User1"}

    @pytest.mark.asyncio
    async def test_mutation_allowed_when_enabled(self):
        app = _make_manager(enable_mutation=True).get_app("project")
        result = await _compose(app, 
            '{ TaskService { create_task(title: "New") { id title } } }',
        )
        assert result["TaskService"]["create_task"]["title"] == "New"

    @pytest.mark.asyncio
    async def test_from_context_param_injection(self):
        app = _make_context_manager().get_app("project")
        result = await _compose(app, 
            "{ ContextService { get_my_tasks { id title } } }",
            context={"user_id": 7},
        )
        tasks = result["ContextService"]["get_my_tasks"]
        assert all("Task of 7" in t["title"] for t in tasks)

    @pytest.mark.asyncio
    async def test_from_context_wrong_type_returns_field_error(self):
        """FromContext value goes through the same pydantic validation as
        query args — wrong type surfaces as a per-field error (the method
        is nulled), not deep inside the method body."""
        app = _make_context_manager().get_app("project")
        result = await app.compose(
            "{ ContextService { get_my_tasks { id title } } }",
            context={"user_id": "not-an-int"},
        )
        assert result["data"]["ContextService"]["get_my_tasks"] is None
        error = result["errors"][0]
        assert "user_id" in error["message"]
        assert error["extensions"]["code"] == "QUERY_FAILED"


# ──────────────────────────────────────────────────
# Validation errors
# ──────────────────────────────────────────────────


class TestComposeValidation:
    @pytest.mark.asyncio
    async def test_empty_query(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="Query is empty"):
            await _compose(app, "")

    @pytest.mark.asyncio
    async def test_syntax_error(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="GraphQL syntax error"):
            await _compose(app, "{ SprintService ")

    @pytest.mark.asyncio
    async def test_method_level_alias_supported(self):
        """方法级别名受支持：按别名独立调用，响应按 response key 返回。"""
        app = _make_manager().get_app("project")
        result = await app.compose(
            "{ SprintService { a: get_sprint(sprint_id: 1) { name } b: get_sprint(sprint_id: 2) { name } } }",
        )
        assert result["errors"] == []
        assert result["data"]["SprintService"]["a"] == {"name": "Sprint A"}
        # get_sprint(sprint_id: 2) returns None by design — the aliased
        # invocation is an independent call with its own result.
        assert result["data"]["SprintService"]["b"] is None

    @pytest.mark.asyncio
    async def test_nested_alias_rejected(self):
        """嵌套（DTO 层）别名不支持，validation 阶段拒绝。"""
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="nested level"):
            await app.compose(
                "{ SprintService { get_sprint(sprint_id: 1) { n: name } } }",
            )

    @pytest.mark.asyncio
    async def test_unknown_service(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="Service 'NoSuchService'"):
            await _compose(app, 
                "{ NoSuchService { anything { id } } }",
            )

    @pytest.mark.asyncio
    async def test_unknown_method(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="Method 'no_such_method'"):
            await _compose(app, 
                "{ SprintService { no_such_method { id } } }",
            )

    @pytest.mark.asyncio
    async def test_missing_required_argument(self):
        app = _make_manager().get_app("project")
        result = await app.compose(
            "{ SprintService { get_sprint { name } } }",
        )
        assert result["data"]["SprintService"]["get_sprint"] is None
        error = result["errors"][0]
        assert "Missing required argument 'sprint_id'" in error["message"]
        assert error["extensions"]["code"] == "QUERY_FAILED"

    @pytest.mark.asyncio
    async def test_unexpected_argument(self):
        app = _make_manager().get_app("project")
        result = await app.compose(
            "{ SprintService { get_sprint(sprint_id: 1, foobar: 2) { name } } }",
        )
        assert result["data"]["SprintService"]["get_sprint"] is None
        assert "Unexpected argument 'foobar'" in result["errors"][0]["message"]

    @pytest.mark.asyncio
    async def test_argument_type_coercion_failure(self):
        app = _make_manager().get_app("project")
        result = await app.compose(
            '{ SprintService { get_sprint(sprint_id: "not-an-int") { name } } }',
        )
        assert result["data"]["SprintService"]["get_sprint"] is None
        assert "Failed to coerce argument 'sprint_id'" in result["errors"][0]["message"]

    @pytest.mark.asyncio
    async def test_mutation_when_disabled(self):
        app = _make_manager(enable_mutation=False).get_app("project")
        with pytest.raises(ComposeError, match="mutations are disabled"):
            await _compose(app, 
                '{ TaskService { create_task(title: "x") { id } } }',
            )

    @pytest.mark.asyncio
    async def test_dto_method_requires_selection(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="requires field selection"):
            await _compose(app, 
                "{ SprintService { get_sprint(sprint_id: 1) } }",
            )

    @pytest.mark.asyncio
    async def test_dto_leaf_arguments_rejected(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="Arguments are not allowed on DTO field"):
            await _compose(app, 
                "{ TaskService { get_task(task_id: 1) { owner(limit: 5) { id } } } }",
            )

    @pytest.mark.asyncio
    async def test_service_level_arguments_rejected(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="not allowed on Service"):
            await _compose(app, 
                "{ SprintService(limit: 5) { list_sprints { id } } }",
            )

    @pytest.mark.asyncio
    async def test_unknown_field_in_selection(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="Unknown field"):
            await _compose(app, 
                "{ SprintService { list_sprints { nonexistent } } }",
            )

    @pytest.mark.asyncio
    async def test_unknown_field_error_lists_available_fields(self):
        # The error must include the candidate field names so LLMs can
        # recover without a separate schema discovery call.
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError, match="Available fields:") as exc_info:
            await _compose(app, 
                "{ SprintService { list_sprints { nonexistent } } }",
            )
        assert "id" in str(exc_info.value)
        assert "name" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_sub_selection_on_scalar_rejected(self):
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError):
            await _compose(app, 
                "{ SprintService { list_sprints { id { foo } } } }",
            )


# ──────────────────────────────────────────────────
# Security: FromContext params cannot be overridden via query args
# ──────────────────────────────────────────────────


class TestComposeFromContextSecurity:
    """Regression for privilege-escalation via argument override.

    Before the fix, a query like ``{ CtxService { whoami(user_id: 999) } }``
    would let the client impersonate any user by overwriting the
    server-injected ``user_id`` FromContext param.
    """

    @pytest.mark.asyncio
    async def test_from_context_param_in_query_args_is_rejected(self):
        app = _make_context_manager().get_app("project")
        result = await app.compose(
            "{ ContextService { get_my_tasks(user_id: 999) { id title } } }",
        )
        assert result["data"]["ContextService"]["get_my_tasks"] is None
        error = result["errors"][0]
        assert "server-injected" in error["message"]
        assert error["extensions"]["code"] == "QUERY_FAILED"

    @pytest.mark.asyncio
    async def test_from_context_param_still_works_without_query_arg(self):
        # Sanity: the legitimate path (no query arg, value comes from context)
        # must still work after the fix.
        app = _make_context_manager().get_app("project")
        # context_extractor returns {"user_id": ctx.get("user_id", 0)},
        # so passing user_id=7 in context yields tasks for user 7.
        result = await _compose(app, 
            "{ ContextService { get_my_tasks { id title } } }",
            context={"user_id": 7},
        )
        tasks = result["ContextService"]["get_my_tasks"]
        assert all("Task of 7" in t["title"] for t in tasks)


# ──────────────────────────────────────────────────
# Execution ordering: mutations serial, queries parallel
# ──────────────────────────────────────────────────


class TestComposeExecutionOrdering:
    """Regression for concurrent mutation execution.

    Before the fix, ``asyncio.gather`` ran all plans concurrently, so
    mutations could complete out of declaration order. GraphQL spec
    requires mutations to execute serially.
    """

    def setup_method(self):
        _seq_log.clear()

    @pytest.mark.asyncio
    async def test_mutations_execute_in_declaration_order(self):
        # slow_mutation is declared first but sleeps; if gather ran them
        # concurrently, fast_mutation would complete first and the log
        # would be ["fast_mutation", "slow_mutation"]. Serial execution
        # preserves declaration order: ["slow_mutation", "fast_mutation"].
        app = _make_manager(with_seq=True).get_app("project")
        await _compose(app, 
            "{ SeqService { slow_mutation fast_mutation } }",
        )
        assert _seq_log == ["slow_mutation", "fast_mutation"]

    @pytest.mark.asyncio
    async def test_queries_run_concurrently(self):
        # If queries were serial, slow_query (declared first, sleeps 20ms)
        # would always complete before fast_query. Concurrent execution
        # lets fast_query win — log = ["fast_query", "slow_query"].
        app = _make_manager(with_seq=True).get_app("project")
        await _compose(app, 
            "{ SeqService { slow_query fast_query } }",
        )
        assert _seq_log == ["fast_query", "slow_query"]

    @pytest.mark.asyncio
    async def test_single_mutation_works(self):
        # Sanity: a lone mutation executes and returns a value.
        app = _make_manager(with_seq=True).get_app("project")
        result = await _compose(app, 
            "{ SeqService { fast_mutation } }",
        )
        assert result["SeqService"]["fast_mutation"] == "fast_mutation"
