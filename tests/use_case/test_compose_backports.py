"""Regression coverage for fixes backported from nexusx.

- enum wire-name coercion (nexusx 6.3.1): the schema renders enum members
  by name while Pydantic validates by value — both wire conventions must
  work and errors must name the member names.
- multi-operation documents (nexusx #142): compose has no operationName
  channel; multiple operations must be rejected loudly instead of
  silently executing only the first one.
- scalar Literal annotations (nexusx #151): map to the shared scalar with
  the allowed values surfaced in descriptions; reject mixed-type and
  enum-member literals at build time.
"""

from __future__ import annotations

import asyncio
from enum import Enum
from typing import Literal

import pytest
from pydantic import BaseModel

from pydantic_resolve import query
from pydantic_resolve.use_case.business import UseCaseService
from pydantic_resolve.use_case.compose import ComposeError
from pydantic_resolve.use_case.manager import UseCaseAppConfig, UseCaseManager


class Level(str, Enum):
    HIGH = "high"


class Priority(Enum):
    URGENT = 1


class EnumService(UseCaseService):
    @query
    async def by_level(cls, level: Level) -> str:
        return f"{level.name}={level.value}"

    @query
    async def by_priority(cls, priority: Priority) -> str:
        return f"{priority.name}={priority.value}"


def _app(services):
    return UseCaseAppConfig(name="t", services=services, description="x")


class TestEnumWireNameCoercion:
    def test_member_name_wire_value(self):
        res = UseCaseManager([_app([EnumService])]).get_app("t")
        result = asyncio.run(res.compose("{ EnumService { by_level(level: HIGH) } }"))
        assert result == {"EnumService": {"by_level": "HIGH=high"}}

    def test_member_value_wire_value(self):
        res = UseCaseManager([_app([EnumService])]).get_app("t")
        result = asyncio.run(res.compose('{ EnumService { by_level(level: "high") } }'))
        assert result == {"EnumService": {"by_level": "HIGH=high"}}

    def test_int_valued_enum_member_name(self):
        res = UseCaseManager([_app([EnumService])]).get_app("t")
        result = asyncio.run(
            res.compose("{ EnumService { by_priority(priority: URGENT) } }")
        )
        assert result == {"EnumService": {"by_priority": "URGENT=1"}}

    def test_invalid_name_error_lists_member_names(self):
        res = UseCaseManager([_app([EnumService])]).get_app("t")
        with pytest.raises(ComposeError, match="HIGH"):
            asyncio.run(res.compose("{ EnumService { by_level(level: NOPE) } }"))


class TestMultiOperationDocument:
    def test_multiple_operations_rejected(self):
        class S(UseCaseService):
            @query
            async def m1(cls) -> str:
                return "first"

            @query
            async def m2(cls) -> str:
                return "second"

        res = UseCaseManager([_app([S])]).get_app("t")
        with pytest.raises(ComposeError, match="multiple operations"):
            asyncio.run(res.compose("{ S { m1 } } query Other { S { m2 } }"))

    def test_single_operation_unaffected(self):
        class S(UseCaseService):
            @query
            async def m1(cls) -> str:
                return "first"

        res = UseCaseManager([_app([S])]).get_app("t")
        result = asyncio.run(res.compose("{ S { m1 } }"))
        assert result == {"S": {"m1": "first"}}


class LiteralTaskDTO(BaseModel):
    status: Literal["open", "closed"] = "open"


class LiteralService(UseCaseService):
    @query
    async def echo_status(cls, status: Literal["open", "closed"]) -> str:
        return status

    @query
    async def by_task(cls, task: LiteralTaskDTO) -> str:
        return task.status


class TestScalarLiteralSupport:
    def test_argument_maps_to_scalar_with_allowed_values(self):
        res = UseCaseManager([_app([LiteralService])]).get_app("t")
        svc = next(
            t
            for t in res.compose_schema.values()
            if getattr(t, "name", None) == "LiteralServiceQuery"
        )
        method = next(f for f in svc.fields.values() if f.name == "echo_status")
        arg = next(a for a in method.args if a.name == "status")
        assert arg.graphql_type_name == "String"
        assert arg.description == "Allowed values: open, closed"

    def test_dto_field_description_lists_allowed_values(self):
        res = UseCaseManager([_app([LiteralService])]).get_app("t")
        dto = next(
            t
            for t in res.compose_schema.values()
            if getattr(t, "name", None) == "LiteralTaskDTO"
        )
        field = dto.fields["status"]
        assert field.description == "Allowed values: open, closed"

    @pytest.mark.parametrize(
        ("status", "ok"), [("open", True), ("pending", False)]
    )
    def test_runtime_constraint_enforced(self, status, ok):
        res = UseCaseManager([_app([LiteralService])]).get_app("t")
        if ok:
            result = asyncio.run(
                res.compose(f'{{ LiteralService {{ echo_status(status: "{status}") }} }}')
            )
            assert result == {"LiteralService": {"echo_status": status}}
        else:
            with pytest.raises(ComposeError):
                asyncio.run(
                    res.compose(
                        f'{{ LiteralService {{ echo_status(status: "{status}") }} }}'
                    )
                )

    def test_mixed_type_literal_rejected_at_build(self):
        class BadService(UseCaseService):
            @query
            async def pick(cls, mode: Literal["fast", 1]) -> str:
                return "x"

        with pytest.raises(ValueError, match="share one Python type"):
            UseCaseManager([_app([BadService])]).get_app("t")

    def test_enum_member_literal_rejected_with_guidance(self):
        class BadService(UseCaseService):
            @query
            async def pick(cls, level: Literal[Level.HIGH]) -> str:
                return "x"

        with pytest.raises(ValueError, match="enum class directly"):
            UseCaseManager([_app([BadService])]).get_app("t")
