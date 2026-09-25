"""Tests for scalar ``Literal`` handling on the graphql/ main path (#316).

Before the fix, every Literal fell through ``map_scalar_type``'s lenient
string-matching fallback and became ``String`` — int/bool literals got a
wrong type, and ``Literal[..., None]`` was forced NON_NULL. The compose
path already validated Literals (``_literal_info``, backported in #313);
these tests pin the same rules on the entity-first path, where the shared
helpers now live (``graphql/type_mapping.py``).
"""

from enum import Enum
from typing import List, Literal, Optional

import pytest

from pydantic_resolve import config_global_resolver
from pydantic_resolve.graphql import GraphQLHandler, SchemaBuilder
from pydantic_resolve.graphql.type_mapping import (
    describe_literal_values,
    literal_allowed_values,
    literal_is_nullable,
    map_python_to_graphql,
    map_scalar_type,
)
from tests.graphql.fixtures.literal_entities import BaseEntity


class Level(Enum):
    HIGH = "high"


class TestLiteralScalarMapping:
    """map_scalar_type maps a Literal to the scalar shared by its values."""

    def test_int_literal_maps_to_int(self):
        assert map_scalar_type(Literal[1, 2]) == "Int"

    def test_bool_literal_maps_to_boolean(self):
        assert map_scalar_type(Literal[True, False]) == "Boolean"

    def test_str_literal_maps_to_string(self):
        assert map_scalar_type(Literal["open", "closed"]) == "String"

    def test_float_literal_maps_to_float(self):
        assert map_scalar_type(Literal[1.5, 2.5]) == "Float"


class TestLiteralFullTypeMapping:
    """map_python_to_graphql handles nullability and wrappers."""

    def test_scalar_literals(self):
        assert map_python_to_graphql(Literal[1, 2]) == "Int!"
        assert map_python_to_graphql(Literal[True, False]) == "Boolean!"
        assert map_python_to_graphql(Literal["open", "closed"]) == "String!"

    def test_none_member_is_nullable(self):
        # The regression from #316: Literal['open', None] was forced NON_NULL.
        assert map_python_to_graphql(Literal["open", None]) == "String"

    def test_optional_literal_is_nullable(self):
        assert map_python_to_graphql(Optional[Literal["open", "closed"]]) == "String"

    def test_list_literal(self):
        assert map_python_to_graphql(List[Literal[1, 2]]) == "[Int!]!"


class TestLiteralValidation:
    """Invalid Literals raise loudly instead of mapping to String."""

    def test_mixed_type_literal_rejected(self):
        with pytest.raises(ValueError, match="share one Python type"):
            map_scalar_type(Literal["fast", 1])

    def test_all_none_literal_rejected(self):
        with pytest.raises(ValueError, match="non-None value"):
            map_scalar_type(Literal[None])

    def test_enum_member_literal_rejected_with_guidance(self):
        with pytest.raises(ValueError, match="enum class directly"):
            map_scalar_type(Literal[Level.HIGH])


class TestLiteralHelpers:
    def test_literal_is_nullable(self):
        assert literal_is_nullable(Literal["a", None]) is True
        assert literal_is_nullable(Literal["a"]) is False
        assert literal_is_nullable(Optional[Literal["a"]]) is False
        assert literal_is_nullable(str) is False

    def test_literal_allowed_values_unwraps_wrappers(self):
        assert literal_allowed_values(Literal["open", "closed"]) == ("open", "closed")
        assert literal_allowed_values(Optional[Literal["open", None]]) == ("open",)
        assert literal_allowed_values(List[Literal[1, 2]]) == (1, 2)
        assert literal_allowed_values(str) is None

    def test_describe_literal_values(self):
        assert describe_literal_values(None, Literal["open", "closed"]) == (
            "Allowed values: open, closed"
        )
        assert describe_literal_values("Task status.", Literal["open"]) == (
            "Task status. Allowed values: open"
        )
        assert describe_literal_values("Plain.", str) == "Plain."


class TestLiteralSDL:
    """Entity SDL carries correct scalar types and Allowed values docs."""

    def setup_method(self):
        self.er_diagram = BaseEntity.get_diagram()
        config_global_resolver(self.er_diagram)
        self.sdl = SchemaBuilder(self.er_diagram).build_schema()

    def test_int_literal_field_type(self):
        assert "priority: Int!" in self.sdl

    def test_bool_literal_field_type(self):
        assert "flag: Boolean!" in self.sdl

    def test_str_literal_field_type(self):
        assert "status: String!" in self.sdl

    def test_none_member_field_follows_optional_convention(self):
        # Output fields are NON_NULL by convention on this path (even
        # Optional[T] renders as T!); the None-member Literal matches the
        # Optional treatment. Nullability itself is covered by the input
        # side (below) and by map_python_to_graphql.
        assert "note: String!" in self.sdl

    def test_none_member_input_field_is_nullable(self):
        input_block = self.sdl.split("input TaskFilter")[1]
        assert "status: String!" in input_block  # no None member → required

    def test_optional_literal_input_field_is_nullable(self):
        block = self.sdl.split("input NullableFilter")[1].split("}")[0]
        assert "note: String" in block
        assert "note: String!" not in block

    def test_allowed_values_docstring_on_field(self):
        assert '"""Allowed values: open, closed"""' in self.sdl

    def test_input_field_type_and_allowed_values(self):
        assert "input TaskFilter" in self.sdl
        # Inside the input definition the Literal field is String with docs.
        input_block = self.sdl.split("input TaskFilter")[1]
        assert "status: String" in input_block
        assert '"""Allowed values: open, closed"""' in input_block


class TestLiteralIntrospection:
    """Introspection reports the shared scalar and the constraint."""

    def setup_method(self):
        self.er_diagram = BaseEntity.get_diagram()
        config_global_resolver(self.er_diagram)
        self.handler = GraphQLHandler(self.er_diagram)

    @pytest.mark.asyncio
    async def test_entity_field_types_and_descriptions(self):
        result = await self.handler.execute("""
            {
                __type(name: "TaskLiteEntity") {
                    fields {
                        name
                        description
                        type { name }
                    }
                }
            }
        """)
        fields = {
            f["name"]: f
            for f in result["data"]["__type"]["fields"]
        }
        assert fields["priority"]["type"]["name"] == "Int"
        assert fields["flag"]["type"]["name"] == "Boolean"
        assert fields["status"]["type"]["name"] == "String"
        assert fields["note"]["type"]["name"] == "String"
        assert fields["status"]["description"] == "Allowed values: open, closed"
        # None member is excluded from the listed values.
        assert fields["note"]["description"] == "Allowed values: draft"

    @pytest.mark.asyncio
    async def test_method_arg_type_and_description(self):
        result = await self.handler.execute("""
            {
                __type(name: "TaskLiteEntityQuery") {
                    fields {
                        name
                        args {
                            name
                            description
                            type { name }
                        }
                    }
                }
            }
        """)
        method = next(
            f for f in result["data"]["__type"]["fields"] if f["name"] == "by_status"
        )
        arg = next(a for a in method["args"] if a["name"] == "status")
        assert arg["type"]["name"] == "String"
        assert arg["description"] == "Allowed values: open, closed"

    @pytest.mark.asyncio
    async def test_input_field_type_and_description(self):
        result = await self.handler.execute("""
            {
                __type(name: "TaskFilter") {
                    inputFields {
                        name
                        description
                        type { name kind ofType { name } }
                    }
                }
            }
        """)
        field = next(
            f
            for f in result["data"]["__type"]["inputFields"]
            if f["name"] == "status"
        )
        assert field["type"]["name"] == "String"
        assert field["description"] == "Allowed values: open, closed"
