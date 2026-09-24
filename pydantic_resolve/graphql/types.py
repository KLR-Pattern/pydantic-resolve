"""
Data structures for GraphQL functionality.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TypedDict


@dataclass(eq=False)  # We'll implement custom __eq__ and __hash__
class FieldSelection:
    """Represents a selected field in GraphQL query.

    ``sub_fields`` is keyed by *response key* — the alias when present,
    otherwise the field name (GraphQL response-key semantics). ``name``
    keeps the original field name so executors resolve methods/fields by
    their real identity while responses are keyed the way the client asked.
    """

    sub_fields: Optional[dict[str, 'FieldSelection']] = None
    arguments: Optional[dict[str, Any]] = None
    name: Optional[str] = None
    alias: Optional[str] = None

    def __hash__(self):
        """Make FieldSelection hashable for caching.

        Note: arguments are intentionally excluded from hash
        as they don't affect the model structure.
        """
        sub_fields_hash = None
        if self.sub_fields:
            sub_fields_hash = tuple(
                (name, hash(sel))
                for name, sel in sorted(self.sub_fields.items())
            )
        return hash(sub_fields_hash)

    def __eq__(self, other):
        if not isinstance(other, FieldSelection):
            return False
        return (
            self.sub_fields == other.sub_fields
            and self.name == other.name
            and self.alias == other.alias
        )


@dataclass
class ParsedQuery:
    """Represents a parsed GraphQL query."""
    field_tree: dict[str, FieldSelection]
    variables: dict[str, Any] = field(default_factory=dict)
    operation_name: Optional[str] = None


# === GraphQL Introspection Types ===

class GraphQLTypeRef(TypedDict, total=False):
    """Type reference in GraphQL introspection (can be nested with ofType)."""
    kind: str  # "OBJECT", "LIST", "NON_NULL", "SCALAR", "ENUM"
    name: Optional[str]
    ofType: Optional[GraphQLTypeRef]


class GraphQLArgument(TypedDict, total=False):
    """Argument info in GraphQL introspection."""
    name: str
    description: Optional[str]
    type: GraphQLTypeRef
    defaultValue: Optional[str]


class GraphQLField(TypedDict, total=False):
    """Field info in GraphQL introspection."""
    name: str
    description: Optional[str]
    args: list[GraphQLArgument]
    type: GraphQLTypeRef
    isDeprecated: bool
    deprecationReason: Optional[str]


class GraphQLEnumValue(TypedDict, total=False):
    """Enum value in GraphQL introspection."""
    name: str
    description: Optional[str]
    isDeprecated: bool
    deprecationReason: Optional[str]


class GraphQLType(TypedDict, total=False):
    """Full type info in GraphQL introspection."""
    kind: str  # "OBJECT", "INPUT_OBJECT", "SCALAR", "ENUM", "LIST", "NON_NULL"
    name: Optional[str]
    description: Optional[str]
    fields: Optional[list[GraphQLField]]
    inputFields: Optional[list[GraphQLField]]
    enumValues: Optional[list[GraphQLEnumValue]]
    interfaces: Optional[list[str]]
    possibleTypes: Optional[list[GraphQLType]]


class IntrospectionData(TypedDict, total=False):
    """Root introspection data structure."""
    queryType: Optional[dict[str, str]]
    mutationType: Optional[dict[str, str]]
    subscriptionType: Optional[None]
    types: list[GraphQLType]
    directives: list[dict[str, Any]]
