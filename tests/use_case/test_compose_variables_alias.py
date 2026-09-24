"""Feature tests for compose GraphQL variables and method-level aliases.

Ported from nexusx: variables support (6.3.0) and alias support with
three-state mutation feedback (#140 / specs/023).
"""

from __future__ import annotations

import asyncio

import pytest

from pydantic_resolve import mutation, query
from pydantic_resolve.use_case.business import UseCaseService
from pydantic_resolve.use_case.compose import ComposeError
from pydantic_resolve.use_case.manager import UseCaseAppConfig, UseCaseManager


class TaskService(UseCaseService):
    @query
    async def get_task(cls, task_id: int) -> dict:
        if task_id == 99:
            raise ValueError("db exploded")
        return {"id": task_id}

    @mutation
    async def create_task(cls, title: str, fail: bool = False) -> dict:
        if fail:
            raise ValueError("write rejected")
        return {"id": 1, "title": title}


def _app() -> "object":
    return UseCaseManager(
        [UseCaseAppConfig(name="p", services=[TaskService], description="x")]
    ).get_app("p")


class TestVariables:
    def test_variable_resolves_in_argument(self):
        result = asyncio.run(
            _app().compose(
                "query ($id: Int!) { TaskService { get_task(task_id: $id) } }",
                variables={"id": 5},
            )
        )
        assert result == {
            "data": {"TaskService": {"get_task": {"id": 5}}},
            "errors": [],
        }

    def test_string_variable_with_quotes_and_newlines(self):
        # The #1 agent parse-error source — variables sidestep all escaping.
        title = 'He said "hi" \\ done'
        result = asyncio.run(
            _app().compose(
                'query ($t: String!) { TaskService { create_task(title: $t) { id } } }',
                variables={"t": title},
            )
        )
        assert result["errors"] == []
        assert result["data"]["TaskService"]["create_task"]["id"] == 1

    def test_declared_but_missing_variable_fails_fast(self):
        with pytest.raises(ComposeError, match="declares variables"):
            asyncio.run(
                _app().compose(
                    "query ($id: Int!) { TaskService { get_task(task_id: $id) } }"
                )
            )

    def test_declared_default_is_not_auto_applied(self):
        with pytest.raises(ComposeError, match="default values are never auto-applied"):
            asyncio.run(
                _app().compose(
                    'query ($id: Int! = 1) { TaskService { get_task(task_id: $id) } }'
                )
            )

    def test_variable_in_input_object(self):
        result = asyncio.run(
            _app().compose(
                "query ($f: Boolean!) { TaskService { create_task(title: \"x\", fail: $f) { id } } }",
                variables={"f": False},
            )
        )
        assert result["errors"] == []
        assert result["data"]["TaskService"]["create_task"]["id"] == 1


class TestMethodLevelAliases:
    def test_aliased_queries_execute_independently(self):
        result = asyncio.run(
            _app().compose(
                "{ TaskService { a: get_task(task_id: 1) b: get_task(task_id: 2) } }"
            )
        )
        assert result == {
            "data": {"TaskService": {"a": {"id": 1}, "b": {"id": 2}}},
            "errors": [],
        }

    def test_failing_aliased_query_nulls_only_its_key(self):
        result = asyncio.run(
            _app().compose(
                "{ TaskService { a: get_task(task_id: 1) bad: get_task(task_id: 99) } }"
            )
        )
        assert result["data"]["TaskService"]["a"] == {"id": 1}
        assert result["data"]["TaskService"]["bad"] is None
        error = result["errors"][0]
        assert error["path"] == ["TaskService", "bad"]
        assert error["extensions"]["code"] == "QUERY_FAILED"
        assert error["extensions"]["service_method"] == "TaskService.get_task"

    def test_duplicate_response_key_rejected(self):
        with pytest.raises(ComposeError, match="Duplicate response key 'get_task'"):
            asyncio.run(
                _app().compose(
                    "{ TaskService { get_task(task_id: 1) get_task(task_id: 2) } }"
                )
            )

    def test_alias_name_collision_rejected(self):
        with pytest.raises(ComposeError, match="Duplicate response key 'get_task'"):
            asyncio.run(
                _app().compose(
                    "{ TaskService { get_task(task_id: 1) get_task(task_id: 2) } }"
                )
            )

    def test_nested_alias_rejected(self):
        with pytest.raises(ComposeError, match="nested level"):
            asyncio.run(
                _app().compose(
                    "{ TaskService { get_task(task_id: 1) { n: id } } }"
                )
            )

    def test_service_level_alias_keys_response(self):
        result = asyncio.run(
            _app().compose(
                "{ t1: TaskService { get_task(task_id: 1) } t2: TaskService { get_task(task_id: 2) } }"
            )
        )
        assert result["errors"] == []
        assert result["data"]["t1"]["get_task"] == {"id": 1}
        assert result["data"]["t2"]["get_task"] == {"id": 2}


class TestMutationThreeStateFeedback:
    def test_success_failure_skip(self):
        """成功保留 / 失败独立报错 / 后续跳过标注（specs/023 US3）。"""
        result = asyncio.run(
            _app().compose(
                '{ TaskService { '
                't1: create_task(title: "ok") '
                't2: create_task(title: "x", fail: true) '
                't3: create_task(title: "never") } }'
            )
        )
        data = result["data"]["TaskService"]
        assert data["t1"] == {"id": 1, "title": "ok"}  # 已成功保留
        assert data["t2"] is None  # 失败独立报错
        assert data["t3"] is None  # 跳过标注
        codes = [(e["path"][1], e["extensions"]["code"]) for e in result["errors"]]
        assert codes == [
            ("t2", "MUTATION_FAILED"),
            ("t3", "SKIPPED_PRIOR_FAILURE"),
        ]

    def test_query_unaffected_by_mutation_failure(self):
        """mutation 失败不回滚已成功的 query 结果（queries 先并发跑完）。"""
        result = asyncio.run(
            _app().compose(
                "{ TaskService { q: get_task(task_id: 1) "
                'm: create_task(title: "x", fail: true) } }'
            )
        )
        assert result["data"]["TaskService"]["q"] == {"id": 1}
        assert result["data"]["TaskService"]["m"] is None
        assert result["errors"][0]["extensions"]["code"] == "MUTATION_FAILED"
