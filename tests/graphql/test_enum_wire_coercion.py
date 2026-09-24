"""Entity-first executor enum wire coercion (backported from nexusx).

GraphQL enum wire values are member *names*; the executor converts them to
enum members. The conversion must also accept member *values* and must fail
loudly on invalid values instead of letting a raw string flow into the
method body (the old blanket catch downgraded failures to a warning).
"""

from enum import Enum

import pytest

from pydantic_resolve.graphql.executor import EnumWireError, QueryExecutor


class Level(str, Enum):
    HIGH = "high"


class FakeEntity:
    @classmethod
    async def by_level(cls, level: Level) -> str:
        return level.name


def _convert(wire_value: str):
    executor = QueryExecutor.__new__(QueryExecutor)
    return executor._convert_arguments(FakeEntity.by_level, {"level": wire_value})


class TestEnumWireCoercion:
    def test_member_name_wire_value(self):
        assert _convert("HIGH")["level"] is Level.HIGH

    def test_member_value_wire_value(self):
        assert _convert("high")["level"] is Level.HIGH

    def test_invalid_value_raises_instead_of_passing_string_through(self):
        with pytest.raises(EnumWireError, match="HIGH"):
            _convert("NOPE")
