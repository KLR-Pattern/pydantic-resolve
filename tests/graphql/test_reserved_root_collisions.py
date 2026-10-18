"""Data-only entities must not shadow generated operation root types."""

import pytest
from pydantic import BaseModel

from pydantic_resolve import Entity, ErDiagram, mutation, query
from pydantic_resolve.graphql import GraphQLHandler, SchemaBuilder
from pydantic_resolve.graphql.schema_errors import ReservedEntityError


class RootSource(BaseModel):
    id: int

    @query
    async def list_items(cls) -> list['RootSource']:
        return []

    @mutation
    async def clear(cls) -> bool:
        return True


@pytest.mark.parametrize('name', ['Query', 'Mutation'])
@pytest.mark.parametrize('entrypoint', ['handler', 'sdl'])
@pytest.mark.parametrize('reverse', [False, True])
def test_data_only_entity_cannot_shadow_operation_root(name, entrypoint, reverse):
    data_only = type(name, (BaseModel,), {'__annotations__': {'id': int}})
    configs = [Entity(kls=RootSource), Entity(kls=data_only)]
    if reverse:
        configs.reverse()
    diagram = ErDiagram(entities=configs)

    with pytest.raises(ReservedEntityError, match=name):
        if entrypoint == 'handler':
            GraphQLHandler(diagram)
        else:
            SchemaBuilder(diagram).build_schema()


def test_unrelated_data_only_entity_keeps_normal_schema():
    class RootData(BaseModel):
        id: int

    diagram = ErDiagram(entities=[Entity(kls=RootSource), Entity(kls=RootData)])
    schema = SchemaBuilder(diagram).build_schema()
    assert schema.count('type Query {') == 1
    assert schema.count('type Mutation {') == 1
    assert 'type RootData {' in schema
    handler = GraphQLHandler(diagram)
    assert list(handler.query_map) == ['RootSource']
    assert list(handler.mutation_map) == ['RootSource']


@pytest.mark.parametrize('name', ['Query', 'Mutation'])
def test_data_only_name_without_generated_root_is_unchanged(name):
    data_only = type(name, (BaseModel,), {'__annotations__': {'id': int}})
    diagram = ErDiagram(entities=[Entity(kls=data_only)])
    assert f'type {name} {{' in SchemaBuilder(diagram).build_schema()
    handler = GraphQLHandler(diagram)
    assert handler.query_map == {}
    assert handler.mutation_map == {}
