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
    annotation_is_nullable,
    describe_literal_values,
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
        # Mixed value types cannot map to one GraphQL scalar — the error
        # must point at the Enum alternative (members may mix value types).
        with pytest.raises(ValueError, match="share one Python type") as exc_info:
            map_scalar_type(Literal["fast", 1])
        assert "Use an Enum instead" in str(exc_info.value)

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

    def test_annotation_is_nullable_covers_both_layers(self):
        # The single predicate NON_NULL decisions must ask (#320): Optional
        # wrapper and Literal None member, either one alone suffices.
        assert annotation_is_nullable(Optional[str]) is True
        assert annotation_is_nullable(str | None) is True
        assert annotation_is_nullable(Literal["a", None]) is True
        assert annotation_is_nullable(Optional[Literal["a"]]) is True
        assert annotation_is_nullable(Literal["a"]) is False
        assert annotation_is_nullable(str) is False
        assert annotation_is_nullable(int | str) is False
        # Element nullability inside a list is the inner type's concern —
        # the list annotation itself is not nullable.
        assert annotation_is_nullable(List[Literal["a", None]]) is False

    def test_describe_literal_values(self):
        assert describe_literal_values(None, Literal["open", "closed"]) == (
            "Allowed values: open, closed"
        )
        assert describe_literal_values("Task status.", Literal["open"]) == (
            "Task status. Allowed values: open"
        )
        assert describe_literal_values("Plain.", str) == "Plain."

    @pytest.mark.parametrize(
        ("annotation", "expected"),
        [
            (Literal[True, False], "Allowed values: true, false"),
            (Literal["open", None], "Allowed values: open (or null)"),
            (Optional[Literal["fast"]], "Allowed values: fast (or null)"),
            (Literal["fast"] | None, "Allowed values: fast (or null)"),
            (Optional[Literal[True, None]], "Allowed values: true (or null)"),
            (List[Literal[False, None]], "Allowed values: false (or null)"),
            (Optional[List[Literal[1, 2]]], "Allowed values: 1, 2 (or null)"),
            (Literal["True", "False"], "Allowed values: True, False"),
            (Literal[0, 1], "Allowed values: 0, 1"),
        ],
    )
    def test_description_uses_graphql_literals_and_preserves_nullability(
        self, annotation, expected
    ):
        assert describe_literal_values(None, annotation) == expected
        assert describe_literal_values("Choose a value.", annotation) == (
            f"Choose a value. {expected}"
        )

    @pytest.mark.parametrize("annotation", [str, Optional[str], Literal[None], int | str])
    def test_no_literal_constraint_preserves_description(self, annotation):
        assert describe_literal_values("Existing.", annotation) == "Existing."
        assert describe_literal_values(None, annotation) is None


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

    def test_none_member_output_field_is_nullable(self):
        # #320: output SDL matches introspection — nullable annotations
        # (Literal[..., None] included) no longer force NON_NULL.
        assert "note: String" in self.sdl
        assert "note: String!" not in self.sdl

    def test_optional_output_field_is_nullable(self):
        # #320 option A: plain Optional[T] output fields used to render as
        # T! by convention; they now match introspection (nullable).
        assert "memo: String" in self.sdl
        assert "memo: String!" not in self.sdl

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
        assert fields["flag"]["description"] == "Allowed values: true, false"
        assert fields["note"]["description"] == "Allowed values: draft (or null)"

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
