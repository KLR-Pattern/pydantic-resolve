"""One-shot entity iterables retain operation discovery and validation."""

import pytest
from pydantic import BaseModel

from pydantic_resolve import Entity, ErDiagram, mutation, query
from pydantic_resolve.graphql import SchemaBuilder
from pydantic_resolve.graphql.schema_errors import (
    GroupTypeCollisionError,
    ReservedEntityError,
    ReservedMethodFieldError,
    scan_grouped_methods,
)


class ScanEntity(BaseModel):
    id: int

    @query
    async def all(cls) -> list['ScanEntity']:
        return []

    @mutation
    async def clear(cls) -> bool:
        return True


@pytest.mark.parametrize('suffix', ['Query', 'Mutation'])
@pytest.mark.parametrize('factory', [list, tuple, iter, lambda items: (x for x in items)])
def test_operations_are_preserved_for_iterables(suffix, factory):
    configs = [Entity(kls=ScanEntity)]
    builder = SchemaBuilder(ErDiagram(entities=configs))
    extract = getattr(builder, f'_extract_{suffix.lower()}_methods')

    result = scan_grouped_methods(factory(configs), extract, suffix)

    assert list(result) == ['ScanEntity']
    assert [method['name'] for method in result['ScanEntity']] == [
        'all' if suffix == 'Query' else 'clear'
    ]


@pytest.mark.parametrize('suffix', ['Query', 'Mutation'])
def test_iterator_still_checks_group_name_collisions(suffix):
    collision = type(f'ScanEntity{suffix}', (BaseModel,), {'__annotations__': {'id': int}})
    configs = [Entity(kls=ScanEntity), Entity(kls=collision)]
    builder = SchemaBuilder(ErDiagram(entities=configs))
    extract = getattr(builder, f'_extract_{suffix.lower()}_methods')

    with pytest.raises(GroupTypeCollisionError):
        scan_grouped_methods(iter(configs), extract, suffix)


@pytest.mark.parametrize('error, class_name, field_name', [
    (ReservedEntityError, 'Query', 'all'),
    (ReservedMethodFieldError, 'ScanEntity', '__hidden'),
])
def test_iterator_does_not_skip_reserved_name_errors(error, class_name, field_name):
    cls = type(class_name, (BaseModel,), {'__annotations__': {'id': int}})
    with pytest.raises(error):
        scan_grouped_methods(
            iter([Entity(kls=cls)]),
            lambda _cls: [{'name': field_name}],
            'Query',
        )
