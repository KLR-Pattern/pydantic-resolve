"""
GraphQL query parser using graphql-core.
"""

from typing import Any, Optional
from graphql import parse as parse_graphql
from graphql.language.ast import (
    ArgumentNode,
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    InlineFragmentNode,
    OperationDefinitionNode,
    OperationType,
    SelectionSetNode,
)

from pydantic_resolve.graphql.types import FieldSelection, ParsedQuery
from pydantic_resolve.graphql.exceptions import QueryParseError


class QueryParser:
    """Parse GraphQL queries and extract field selection trees"""

    def parse(self, query: str, variables: Optional[dict] = None) -> ParsedQuery:
        """
        Parse GraphQL query string

        Args:
            query: GraphQL query string
            variables: Optional variables dict — when provided, ``$var``
                references in arguments resolve to their values; a reference
                to an unset variable raises. Pass string arguments this way
                instead of inline literals (quotes/backslashes/newlines in
                inline strings are the #1 parse-error source).

        Returns:
            ParsedQuery object containing parsed query information. The
            field tree is keyed by *response key* (alias when present,
            otherwise field name); each :class:`FieldSelection` keeps the
            original field name in ``.name``.

        Raises:
            QueryParseError: When query parsing fails, a response key
                collides (:class:`ResponseKeyConflictError`), or a variable
                reference has no value.
        """
        try:
            document = parse_graphql(query)
        except Exception as e:
            raise QueryParseError(f"GraphQL syntax error: {e}")

        # Extract operation definition (Query)
        operation = self._extract_operation(document)
        if not operation:
            raise QueryParseError("No query operation found")

        # Extract fragment definitions for spread expansion
        fragments = self._extract_fragments(document)

        # Extract all root query fields (supports multiple, including fragments)
        root_fields = self._extract_root_fields(operation, fragments)
        if not root_fields:
            raise QueryParseError("Query is empty")

        # Build field selection trees for all root fields
        field_tree = {}
        for root_field in root_fields:
            response_key = (
                root_field.alias.value if root_field.alias else root_field.name.value
            )
            parsed_field = self._build_field_tree(root_field, fragments, variables)
            self._register_field(field_tree, response_key, parsed_field)

        return ParsedQuery(
            field_tree=field_tree,
            variables=dict(variables) if variables else {},
            operation_name=None
        )

    def _extract_operation(self, document: DocumentNode) -> OperationDefinitionNode:
        """Extract operation definition (supports Query and Mutation)"""
        for definition in document.definitions:
            if isinstance(definition, OperationDefinitionNode):
                # Support Query and Mutation operations
                if definition.operation in (OperationType.QUERY, OperationType.MUTATION):
                    return definition
        return None

    def _extract_fragments(
        self,
        document: DocumentNode,
    ) -> dict[str, FragmentDefinitionNode]:
        """Extract named fragment definitions from document."""
        fragments: dict[str, FragmentDefinitionNode] = {}
        for definition in document.definitions:
            if isinstance(definition, FragmentDefinitionNode):
                fragments[definition.name.value] = definition
        return fragments

    def _extract_root_fields(
        self,
        operation: OperationDefinitionNode,
        fragments: Optional[dict[str, FragmentDefinitionNode]] = None,
    ) -> list[FieldNode]:
        """Extract all root query fields"""
        selection_set = operation.selection_set
        if not selection_set or not selection_set.selections:
            raise QueryParseError("Query is empty")

        return self._extract_fields_from_selection_set(selection_set, fragments or {})

    def _extract_fields_from_selection_set(
        self,
        selection_set: SelectionSetNode,
        fragments: dict[str, FragmentDefinitionNode],
    ) -> list[FieldNode]:
        """Flatten FieldNodes from selection set, expanding fragments recursively."""
        fields: list[FieldNode] = []

        for selection in selection_set.selections:
            if isinstance(selection, FieldNode):
                fields.append(selection)
            elif isinstance(selection, FragmentSpreadNode):
                fragment_name = selection.name.value
                fragment = fragments.get(fragment_name)
                if fragment is None:
                    raise QueryParseError(f"Unknown fragment: {fragment_name}")
                fields.extend(self._extract_fields_from_selection_set(fragment.selection_set, fragments))
            elif isinstance(selection, InlineFragmentNode):
                fields.extend(self._extract_fields_from_selection_set(selection.selection_set, fragments))

        return fields

    def _build_field_tree(
        self,
        field_node: FieldNode,
        fragments: Optional[dict[str, FragmentDefinitionNode]] = None,
        variables: Optional[dict] = None,
    ) -> FieldSelection:
        """Recursively build field selection tree.

        Aliases are captured (``FieldSelection.alias``/``.name``) and the
        parent's ``sub_fields`` dict is keyed by response key. Whether
        aliases are *supported* at a given level is an executor decision,
        not a parser one — see :func:`find_nested_alias` and
        :func:`reject_all_aliases` for the shared gates.
        """
        fragments = fragments or {}

        alias = field_node.alias.value if field_node.alias else None
        name = field_node.name.value

        # Extract arguments
        arguments = self._extract_arguments(field_node, variables)

        # Recursively process nested fields
        sub_fields = None
        if field_node.selection_set:
            sub_fields = {}
            for selection in field_node.selection_set.selections:
                if isinstance(selection, FieldNode):
                    response_key = (
                        selection.alias.value
                        if selection.alias
                        else selection.name.value
                    )
                    parsed_field = self._build_field_tree(
                        selection, fragments, variables
                    )
                    self._register_field(sub_fields, response_key, parsed_field)
                elif isinstance(selection, FragmentSpreadNode):
                    fragment_name = selection.name.value
                    fragment = fragments.get(fragment_name)
                    if fragment is None:
                        raise QueryParseError(f"Unknown fragment: {fragment_name}")

                    for fragment_field in self._extract_fields_from_selection_set(fragment.selection_set, fragments):
                        response_key = (
                            fragment_field.alias.value
                            if fragment_field.alias
                            else fragment_field.name.value
                        )
                        parsed_field = self._build_field_tree(
                            fragment_field, fragments, variables
                        )
                        self._register_field(sub_fields, response_key, parsed_field)
                elif isinstance(selection, InlineFragmentNode):
                    for inline_field in self._extract_fields_from_selection_set(selection.selection_set, fragments):
                        response_key = (
                            inline_field.alias.value
                            if inline_field.alias
                            else inline_field.name.value
                        )
                        parsed_field = self._build_field_tree(
                            inline_field, fragments, variables
                        )
                        self._register_field(sub_fields, response_key, parsed_field)

        return FieldSelection(
            sub_fields=sub_fields,
            arguments=arguments,
            name=name,
            alias=alias,
        )

    def _extract_arguments(
        self, field_node: FieldNode, variables: Optional[dict] = None
    ) -> dict[str, Any]:
        """Extract field arguments (``$var`` references resolve from ``variables``)."""
        arguments = {}
        if field_node.arguments:
            for arg in field_node.arguments:
                if isinstance(arg, ArgumentNode):
                    # Get argument value
                    value = self._get_argument_value(arg.value, variables)
                    arguments[arg.name.value] = value
        return arguments

    def _get_argument_value(self, value_node, variables: Optional[dict] = None) -> Any:
        """Get argument value from GraphQL AST node"""
        # kind is string in GraphQL AST, not enum
        kind = getattr(value_node, 'kind', '')

        # IntValue
        if kind == 'int_value':
            return int(value_node.value)
        # FloatValue
        elif kind == 'float_value':
            return float(value_node.value)
        # StringValue
        elif kind == 'string_value':
            return value_node.value
        # BooleanValue
        elif kind == 'boolean_value':
            return value_node.value
        # Variable — resolve from the provided variables dict
        elif kind == 'variable':
            variable_name = getattr(getattr(value_node, 'name', None), 'value', '<unknown>')
            if variables is None or variable_name not in variables:
                raise QueryParseError(
                    f"Variable '${variable_name}' is used in the query but no "
                    "value was provided. Pass values via the 'variables' "
                    "argument (recommended for any string containing quotes, "
                    "backslashes or newlines)."
                )
            return variables[variable_name]
        # ObjectValue (object literal)
        elif kind == 'object_value' and hasattr(value_node, 'fields'):
            obj = {}
            for field in value_node.fields:
                field_name = field.name.value
                obj[field_name] = self._get_argument_value(field.value, variables)
            return obj
        # ListValue
        elif hasattr(value_node, 'values'):
            return [self._get_argument_value(v, variables) for v in value_node.values]
        # Other types, try to get value attribute
        elif hasattr(value_node, 'value'):
            return value_node.value
        else:
            return None

    def _register_field(
        self,
        target: dict[str, FieldSelection],
        name: str,
        selection: FieldSelection,
    ) -> None:
        """Insert ``selection`` into ``target`` under ``name``.

        Raises ResponseKeyConflictError on duplicate — each response key may
        appear at most once within its parent selection (field merging is
        not supported). In compose semantics every (service, method, args)
        tuple is a real method invocation, so merging duplicate keys would
        silently clobber arguments and lose calls. Aliases are the way to
        invoke one method multiple times with different arguments.
        """
        if name in target:
            raise ResponseKeyConflictError(
                f"Duplicate response key '{name}' — each response key may "
                "appear at most once within its parent selection. Use an "
                "alias to select the same field multiple times."
            )
        target[name] = selection


# ============================================================================
# Alias gates (shared by executors — the parser itself accepts aliases)
# ============================================================================


class ResponseKeyConflictError(QueryParseError):
    """Duplicate response key at one selection level.

    Raised for alias repeats, alias/field-name collisions, and plain
    duplicate fields — field merging is not supported. Subclasses
    ``QueryParseError`` so existing ``except QueryParseError`` callers keep
    working; callers that want the specific type catch this class.
    """


def find_nested_alias(sel: FieldSelection) -> tuple[str, str] | None:
    """First alias strictly BELOW ``sel``, as ``(dotted_path, field_name)``.

    Nested-field aliases are out of scope on every execution path (DTO
    projection walks by field name, so a nested alias would silently
    mis-project). Paths that only support method-level aliases detect and
    reject through this single walk.

    ``dotted_path`` is the response-key path from ``sel`` down to the
    aliased node (e.g. ``"owner.reviews"``); ``field_name`` is the ORIGINAL
    field name of the aliased node, so error messages can render
    ``'reviews' aliased to 'r'`` instead of the bare alias key.
    """

    def _walk(selection: FieldSelection) -> tuple[str, str] | None:
        for key, child in (selection.sub_fields or {}).items():
            if child.alias is not None:
                return key, child.name or key
            deeper = _walk(child)
            if deeper is not None:
                return f"{key}.{deeper[0]}", deeper[1]
        return None

    return _walk(sel)


def nested_alias_message(
    dotted: str, field_name: str, *, method_level_ok: bool = True
) -> str:
    """Error text for a nested-field alias, shared by every reject site.

    ``method_level_ok`` tailors the remedy to the calling path: compose
    supports method-level aliases (the default), while the entity-first
    executor rejects aliases everywhere.
    """
    alias_key = dotted.rsplit(".", 1)[-1]
    message = (
        "Field aliases are not supported at nested level "
        f"('{field_name}' aliased to '{alias_key}')"
    )
    if method_level_ok:
        return message + "; only method-level aliases are supported"
    return message + ". Use the original field name."


def reject_all_aliases(field_tree: dict[str, FieldSelection]) -> None:
    """Reject any alias at any level — the entity-first gate.

    The entity-first executor projects DTOs by field name, so aliases are
    not supported anywhere on that path (this preserves the parser's
    pre-alias behavior; the compose path supports method-level aliases).
    """
    for key, sel in field_tree.items():
        if sel.alias is not None:
            raise QueryParseError(
                f"Field aliases are not supported: '{sel.alias}' on "
                f"'{sel.name}'. Use the original field name."
            )
        nested = find_nested_alias(sel)
        if nested is not None:
            raise QueryParseError(
                nested_alias_message(nested[0], nested[1], method_level_ok=False)
            )
