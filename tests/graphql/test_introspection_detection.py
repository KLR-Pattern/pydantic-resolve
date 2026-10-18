"""Introspection routing must inspect fields, not user-supplied text."""

import pytest
from pydantic import BaseModel

from pydantic_resolve import Entity, ErDiagram, query
from pydantic_resolve.graphql import GraphQLHandler
from pydantic_resolve.use_case.introspection import is_introspection_query


class EchoEntity(BaseModel):
    text: str

    @query
    async def echo(cls, text: str) -> 'EchoEntity':
        return EchoEntity(text=text)


@pytest.fixture
def handler():
    return GraphQLHandler(ErDiagram(entities=[Entity(kls=EchoEntity)]))


@pytest.mark.parametrize('document', [
    '{ __schema { types { name } } }',
    '{ schema: __schema { types { name } } }',
    '{ __type(name: "EchoEntity") { name } }',
    '{ __typename }',
    'query { ...Info } fragment Info on Query { __schema { types { name } } }',
])
def test_detects_introspection_fields(handler, document):
    assert is_introspection_query(document)
    assert handler.introspection.is_introspection_query(document)


@pytest.mark.parametrize('document', [
    '{ EchoEntity { echo(text: "__schema") { text } } }',
    '{ EchoEntity { echo(text: "__type") { text } } }',
    '{ EchoEntity { echo(text: "__typename") { text } } }',
    '{ EchoEntity { echo(text: """__schema __type __typename""") { text } } }',
    '# __schema\n{ EchoEntity { echo(text: "hello") { text } } }',
    'query __schema { EchoEntity { echo(text: "hello") { text } } }',
    '{ __schema: EchoEntity { echo(text: "hello") { text } } }',
    'query($x__type: String) { EchoEntity { echo(text: $x__type) { text } } }',
    '{ __typewriter }',
    '',
    '{ __schema',
])
def test_does_not_route_non_introspection_fields(handler, document):
    assert not is_introspection_query(document)
    assert not handler.introspection.is_introspection_query(document)


@pytest.mark.asyncio
@pytest.mark.parametrize('text', ['__schema', '__type', '__typename'])
async def test_keyword_argument_reaches_business_method(handler, text):
    result = await handler.execute(
        '{ EchoEntity { echo(text: "' + text + '") { text } } }'
    )
    assert result == {
        'data': {'EchoEntity': {'echo': {'text': text}}},
        'errors': None,
    }
