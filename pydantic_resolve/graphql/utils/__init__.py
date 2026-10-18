"""GraphQL utilities."""

from __future__ import annotations

from graphql import BREAK, GraphQLSyntaxError, Visitor, parse, visit


def group_type_name(entity_name: str, group_suffix: str) -> str:
    """Return the group OBJECT type name an entity's operations live on.

    Under the grouped layout each entity's ``@query`` / ``@mutation`` methods
    become the fields of a ``{Entity}Query`` / ``{Entity}Mutation`` type.
    Centralizing the formula keeps the SDL generator, the introspection
    generator, and the executor's error messages from drifting apart.

    Args:
        entity_name: The owning entity class name (also the root mount field).
        group_suffix: ``"Query"`` or ``"Mutation"``.

    Examples:
        >>> group_type_name("User", "Query")
        'UserQuery'
        >>> group_type_name("Post", "Mutation")
        'PostMutation'
    """
    return f"{entity_name}{group_suffix}"


_INTROSPECTION_FIELDS = frozenset({"__schema", "__type", "__typename"})


class _IntrospectionVisitor(Visitor):
    found = False

    def enter_field(self, node, *_args):
        if node.name.value in _INTROSPECTION_FIELDS:
            self.found = True
            return BREAK


def is_introspection_query(query: str) -> bool:
    """Detect introspection fields without matching strings, aliases or comments.

    Invalid documents return False so the normal query parser can report the
    syntax error instead of dispatching them to the introspection handler.
    """
    if not query or not any(name in query for name in _INTROSPECTION_FIELDS):
        return False
    try:
        document = parse(query)
    except GraphQLSyntaxError:
        return False
    visitor = _IntrospectionVisitor()
    visit(document, visitor)
    return visitor.found
