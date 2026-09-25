"""Tests for GraphQLHandler execution behavior."""

from typing import ClassVar, List

import pytest
from pydantic import BaseModel

from pydantic_resolve import base_entity, config_global_resolver, mutation, query
from pydantic_resolve.graphql import GraphQLHandler


class TestHandlerExecutionBehavior:
    """Behavior tests for query/mutation execution paths."""

    @pytest.mark.asyncio
    async def test_query_method_receives_real_cls(self):
        """@query methods that use cls should receive the entity class, not None."""
        Base = base_entity()

        class UserEntity(BaseModel, Base):
            __relationships__ = []
            id: int
            label: str
            LABEL_PREFIX: ClassVar[str] = "U"

            @query
            async def get_all(cls) -> List['UserEntity']:
                return [UserEntity(id=1, label=f"{cls.LABEL_PREFIX}-1")]

        diagram = Base.get_diagram()
        config_global_resolver(diagram)
        handler = GraphQLHandler(diagram)

        result = await handler.execute("{ UserEntity { get_all { id label } } }")

        assert result["errors"] is None
        assert result["data"]["UserEntity"]["get_all"][0]["label"] == "U-1"

    @pytest.mark.asyncio
    async def test_query_not_misclassified_as_mutation_by_substring(self):
        """A query containing mutation name text should still execute as query."""
        Base = base_entity()

        class UserEntity(BaseModel, Base):
            __relationships__ = []
            id: int
            note: str

            @query
            async def get_by_note(cls, note: str) -> List['UserEntity']:
                return [UserEntity(id=1, note=note)]

            @mutation
            async def update_user(cls, id: int) -> bool:
                return True

        diagram = Base.get_diagram()
        config_global_resolver(diagram)
        handler = GraphQLHandler(diagram)

        # Includes mutation field name text in argument value.
        result = await handler.execute(
            '{ UserEntity { get_by_note(note: "userEntityUpdateUser") { id note } } }'
        )

        assert result["errors"] is None
        assert result["data"]["UserEntity"]["get_by_note"][0]["note"] == "userEntityUpdateUser"

    @pytest.mark.asyncio
    async def test_staticmethod_query_with_arguments_works(self):
        """@query + @staticmethod methods should execute correctly with arguments."""
        from tests.graphql.fixtures.entities import BaseEntity

        diagram = BaseEntity.get_diagram()
        config_global_resolver(diagram)
        handler = GraphQLHandler(diagram)

        result = await handler.execute("{ UserEntity { get_by_id(id: 1) { id name email } } }")

        assert result["errors"] is None
        assert result["data"]["UserEntity"]["get_by_id"]["id"] == 1
        assert result["data"]["UserEntity"]["get_by_id"]["name"] == "Alice"

    @pytest.mark.asyncio
    async def test_query_with_variable_returns_parse_error(self):
        """Using GraphQL variables should return a clear parse error."""
        from tests.graphql.fixtures.entities import BaseEntity

        diagram = BaseEntity.get_diagram()
        config_global_resolver(diagram)
        handler = GraphQLHandler(diagram)

        result = await handler.execute(
            """
            query GetUser($id: Int!) {
                UserEntity { get_by_id(id: $id) { id name email } }
            }
            """
        )

        assert result["data"] is None
        assert result["errors"] is not None
        assert result["errors"][0]["extensions"]["code"] == "GRAPHQL_PARSE_ERROR"
        assert "no value was provided" in result["errors"][0]["message"]
