"""
Unified type mapping logic for GraphQL schema generation.

This module provides TypeMapper that converts Python types to GraphQL type information.
It supports multiple output formats (SDL string, Introspection dict) through a unified
internal representation.
"""

from dataclasses import dataclass
from typing import Any, ForwardRef, Optional, get_args, TYPE_CHECKING

from pydantic import BaseModel

from .type_registry import FieldInfo, ArgumentInfo
from pydantic_resolve.utils.class_util import safe_issubclass
from pydantic_resolve.utils.types import get_core_types, _is_optional, _is_list
from pydantic_resolve.graphql.type_mapping import (
    map_scalar_type,
    is_enum_type,
    literal_is_nullable,
)

if TYPE_CHECKING:
    from pydantic_resolve.utils.er_diagram import ErDiagram


@dataclass
class GraphQLTypeInfo:
    """
    Unified GraphQL type representation.

    This is the internal representation that can be converted to:
    - SDL string: "String!", "[User!]!", "User"
    - Introspection dict: {"kind": "LIST", "name": None, "ofType": {...}}
    """
    kind: str  # SCALAR, OBJECT, INPUT_OBJECT, LIST, NON_NULL
    name: Optional[str] = None
    of_type: Optional['GraphQLTypeInfo'] = None
    description: Optional[str] = None

    def to_sdl(self) -> str:
        """Convert to SDL type string."""
        if self.kind == "NON_NULL":
            inner = self.of_type.to_sdl() if self.of_type else "String"
            return f"{inner}!"
        elif self.kind == "LIST":
            inner = self.of_type.to_sdl() if self.of_type else "String"
            return f"[{inner}]"
        elif self.kind in ("SCALAR", "OBJECT", "INPUT_OBJECT", "ENUM"):
            return self.name or "String"
        else:
            return self.name or "String"

    def to_introspection(self) -> dict[str, Any]:
        """Convert to introspection format."""
        result = {
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "ofType": None
        }

        if self.of_type:
            result["ofType"] = self.of_type.to_introspection()

        return result

    @property
    def leaf_name(self) -> Optional[str]:
        """Walk the ``of_type`` chain (NON_NULL / LIST wrappers) to the
        named leaf type. Returns the leaf's ``name`` (e.g. ``"UserDTO"``,
        ``"Int"``), or ``None`` if the chain doesn't end at a named type.
        """
        node: GraphQLTypeInfo | None = self
        while node is not None and node.of_type is not None:
            node = node.of_type
        return node.name if node is not None else None


class TypeMapper:
    """
    Maps Python types to GraphQL type information.

    This is the unified type mapping logic used by both
    SDL and Introspection generators.
    """

    def __init__(self, er_diagram: Optional['ErDiagram'] = None):
        self._er_diagram = er_diagram

    def _get_entity_by_name(self, name: str):
        """Find entity class by name from ERD."""
        if self._er_diagram is None:
            return None
        for cfg in self._er_diagram.entities:
            if cfg.kls.__name__ == name:
                return cfg.kls
        return None

    def _resolve_type(self, core_type: Any) -> Any:
        """Resolve ForwardRef or string type names to actual entity classes."""
        if isinstance(core_type, ForwardRef):
            resolved = self._get_entity_by_name(core_type.__forward_arg__)
            return resolved if resolved else core_type
        if isinstance(core_type, str):
            resolved = self._get_entity_by_name(core_type)
            return resolved if resolved else core_type
        return core_type

    def collect_referenced_types(
        self,
        annotation: Any,
        *,
        include_enums: bool = True,
    ) -> dict[str, type]:
        """Walk ``annotation`` and return every BaseModel / Enum reachable.

        Includes types reachable transitively through BaseModel fields
        (e.g. ``UserDTO.owner: OwnerDTO`` pulls in ``OwnerDTO`` and
        everything ``OwnerDTO`` references). Cycles (self-referencing
        DTOs) terminate naturally because the result dict is keyed by
        class name.

        Args:
            annotation: Any Python type annotation — bare class,
                ``Optional[X]``, ``list[X]``, ``Union[X, Y]``, etc.
            include_enums: When True (default), Enum subclasses are
                included. Set to False for contexts that only care
                about Pydantic models (e.g. OpenAPI serialization,
                ErDiagram nested-model discovery).

        Returns:
            ``{class_name: class}`` for each unique BaseModel (and
            Enum, if ``include_enums``) discovered. String annotations
            and unresolved ForwardRefs are silently skipped.
        """
        seen: dict[str, type] = {}
        stack: list[Any] = [annotation]
        while stack:
            current = stack.pop()
            for core_type in get_core_types(current):
                if isinstance(core_type, str):
                    continue
                name = getattr(core_type, "__name__", None)
                if name is None or name in seen:
                    continue
                if safe_issubclass(core_type, BaseModel):
                    seen[name] = core_type
                    stack.extend(
                        f.annotation for f in core_type.model_fields.values()
                    )
                elif include_enums and is_enum_type(core_type):
                    seen[name] = core_type
        return seen

    def map_to_graphql_type(
        self,
        python_type: type,
        is_input: bool = False
    ) -> GraphQLTypeInfo:
        """
        Map a Python type to GraphQL type information.

        Args:
            python_type: Python type (can be list[T], Optional[T], T, etc.)
            is_input: Whether this is for an input type

        Returns:
            GraphQLTypeInfo representing the GraphQL type
        """
        core_types = get_core_types(python_type)
        if not core_types:
            return GraphQLTypeInfo(
                kind="SCALAR",
                name="String",
                description="Default String type"
            )

        core_type = core_types[0]

        # Resolve ForwardRef/string to actual entity class
        core_type = self._resolve_type(core_type)

        # Handle unresolved ForwardRef
        if isinstance(core_type, ForwardRef):
            type_name = core_type.__forward_arg__
            return GraphQLTypeInfo(
                kind="INPUT_OBJECT" if is_input else "OBJECT",
                name=type_name,
                description=f"{type_name} type"
            )

        # Handle unresolved string type names
        if isinstance(core_type, str):
            return GraphQLTypeInfo(
                kind="INPUT_OBJECT" if is_input else "OBJECT",
                name=core_type,
                description=f"{core_type} type"
            )

        # Check if it's list[T]
        if _is_list(python_type):
            inner_type = self.map_to_graphql_type(core_type, is_input)
            return GraphQLTypeInfo(
                kind="LIST",
                of_type=GraphQLTypeInfo(
                    kind="NON_NULL",
                    of_type=inner_type
                )
            )

        # Handle Optional[T] - check if None is in the union
        if _is_optional(python_type):
            args = get_args(python_type)
            non_none_args = [a for a in args if a is not type(None)]
            if non_none_args:
                inner = self.map_to_graphql_type(non_none_args[0], is_input)
                return inner  # Optional means no NON_NULL wrapper

        # Handle BaseModel types
        if safe_issubclass(core_type, BaseModel):
            return GraphQLTypeInfo(
                kind="INPUT_OBJECT" if is_input else "OBJECT",
                name=core_type.__name__,
                description=f"{core_type.__name__} type"
            )

        # Handle enum types
        if is_enum_type(core_type):
            return GraphQLTypeInfo(
                kind="ENUM",
                name=core_type.__name__,
                description=f"{core_type.__name__} enum"
            )

        # Handle scalar types
        scalar_name = map_scalar_type(core_type)
        return GraphQLTypeInfo(
            kind="SCALAR",
            name=scalar_name,
            description=self._get_scalar_description(scalar_name)
        )

    def map_to_sdl(self, python_type: type, is_input: bool = False) -> str:
        """
        Map Python type to SDL type string.

        Args:
            python_type: Python type
            is_input: Whether this is for an input type.
                When True, Optional[T] fields produce "T" (no ! suffix).

        Returns:
            SDL type string (e.g., "String!", "[User!]!")
        """
        is_optional = _is_optional(python_type)
        # Literal['open', None] allows null without being a Union — the
        # None member must suppress the ! suffix just like Optional.
        if literal_is_nullable(python_type):
            is_optional = True
        gql_type = self.map_to_graphql_type(python_type, is_input)
        sdl = gql_type.to_sdl()

        # Add NON_NULL wrapper if not already wrapped.
        # For Optional[T] in input types, skip the ! suffix.
        if is_optional and is_input:
            return sdl.rstrip('!')

        if not sdl.endswith('!'):
            sdl = f"{sdl}!"

        return sdl

    def map_to_introspection(self, python_type: type, is_input: bool = False) -> dict[str, Any]:
        """
        Map Python type to introspection format.

        Args:
            python_type: Python type
            is_input: Whether this is for an input type

        Returns:
            Introspection type dictionary
        """
        gql_type = self.map_to_graphql_type(python_type, is_input)
        return gql_type.to_introspection()

    def _get_scalar_description(self, scalar_name: str) -> Optional[str]:
        """Get description for a scalar type."""
        descriptions = {
            "Int": "The `Int` scalar type represents non-fractional signed whole numeric values.",
            "Float": "The `Float` scalar type represents signed double-precision fractional values.",
            "String": "The `String` scalar type represents textual data.",
            "Boolean": "The `Boolean` scalar type represents `true` or `false`.",
            "ID": "The `ID` scalar type represents a unique identifier.",
        }
        return descriptions.get(scalar_name)

    def extract_field_info(
        self,
        field_name: str,
        field_type: type,
        description: Optional[str] = None,
        is_relationship: bool = False,
        relationship_target: Optional[str] = None
    ) -> FieldInfo:
        """
        Extract FieldInfo from a Python field.

        Args:
            field_name: Name of the field
            field_type: Python type of the field
            description: Optional field description
            is_relationship: Whether this is a relationship field
            relationship_target: Target type name for relationships

        Returns:
            FieldInfo object
        """
        gql_type = self.map_to_graphql_type(field_type)

        # Determine if optional (no NON_NULL wrapper)
        is_optional = not (gql_type.kind == "NON_NULL" or
                          (gql_type.of_type and gql_type.of_type.kind == "NON_NULL"))

        # Determine if list
        is_list = gql_type.kind == "LIST" or (
            gql_type.of_type and gql_type.of_type.kind == "LIST"
        )

        # Get the actual type name
        type_name = gql_type.name
        if gql_type.of_type:
            type_name = gql_type.of_type.name or type_name

        return FieldInfo(
            name=field_name,
            python_type=field_type,
            graphql_type_name=type_name or "String",
            is_list=is_list,
            is_optional=is_optional,
            description=description,
            is_relationship=is_relationship,
            relationship_target=relationship_target
        )

    def extract_argument_info(
        self,
        param_name: str,
        param_type: type,
        default_value: Optional[str] = None,
        description: Optional[str] = None
    ) -> ArgumentInfo:
        """
        Extract ArgumentInfo from a method parameter.

        Args:
            param_name: Name of the parameter
            param_type: Python type of the parameter
            default_value: Optional default value string
            description: Optional argument description

        Returns:
            ArgumentInfo object
        """
        gql_type = self.map_to_graphql_type(param_type, is_input=True)

        is_optional = not (gql_type.kind == "NON_NULL" or
                          (gql_type.of_type and gql_type.of_type.kind == "NON_NULL"))

        is_list = gql_type.kind == "LIST" or (
            gql_type.of_type and gql_type.of_type.kind == "LIST"
        )

        type_name = gql_type.name
        if gql_type.of_type:
            type_name = gql_type.of_type.name or type_name

        return ArgumentInfo(
            name=param_name,
            python_type=param_type,
            graphql_type_name=type_name or "String",
            is_list=is_list,
            is_optional=is_optional,
            default_value=default_value,
            description=description
        )
