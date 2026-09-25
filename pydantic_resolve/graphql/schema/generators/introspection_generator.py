"""
Introspection generator for GraphQL schema.

This module generates introspection data for GraphiQL compatibility,
using the unified type collection and mapping logic.
"""

import inspect
import re
from enum import Enum
from typing import Any, Callable, Optional, get_type_hints

from pydantic import BaseModel

from pydantic_resolve.graphql.schema.generators.base import SchemaGenerator
from pydantic_resolve.graphql.utils import group_type_name
import pydantic_resolve.constant as const
from pydantic_resolve.graphql.schema.type_registry import TypeInfo, FieldInfo, ArgumentInfo, SCALAR_TYPES
from pydantic_resolve.graphql.types import (
    GraphQLArgument,
    GraphQLField,
    GraphQLType,
    GraphQLTypeRef,
    IntrospectionData,
)
from pydantic_resolve.utils.class_util import safe_issubclass
from pydantic_resolve.utils.er_diagram import Relationship
from pydantic_resolve.utils.types import get_core_types
from pydantic_resolve.graphql.type_mapping import (
    map_scalar_type,
    is_list_type,
    is_enum_type,
    get_enum_names,
    describe_literal_values,
)


class IntrospectionGenerator(SchemaGenerator):
    """
    Generates GraphQL introspection data for GraphiQL.

    This implementation uses the unified type collection logic
    from SchemaGenerator base class.
    """

    def __init__(
        self,
        er_diagram,
        query_map: Optional[dict[str, dict[str, tuple[type, Callable]]]] = None,
        mutation_map: Optional[dict[str, dict[str, tuple[type, Callable]]]] = None,
        enable_pagination: bool = False
    ):
        """
        Initialize the introspection generator.

        Args:
            er_diagram: Entity relationship diagram
            query_map: Grouped mapping {entity_name: {method_name: (entity, method)}}
            mutation_map: Grouped mapping {entity_name: {method_name: (entity, method)}}
            enable_pagination: When True, one-to-many fields use Result types
        """
        super().__init__(er_diagram)
        self.query_map = query_map or {}
        self.mutation_map = mutation_map or {}
        self.enable_pagination = enable_pagination
        self._collected_types: dict[str, type] = {}
        self._input_types: set[type] = set()
        # Entity class lookup by name — used for {Entity}Query/{Entity}Mutation
        # group-type descriptions.
        self._entity_map = {cfg.kls.__name__: cfg.kls for cfg in er_diagram.entities}

    def generate(self) -> IntrospectionData:
        """
        Generate full introspection schema.

        Returns:
            Introspection __schema dictionary
        """
        has_queries = any(self.query_map.values())
        has_mutations = any(self.mutation_map.values())
        return {
            "queryType": {"name": "Query", "kind": "OBJECT"} if has_queries else None,
            "mutationType": {"name": "Mutation", "kind": "OBJECT"} if has_mutations else None,
            "subscriptionType": None,
            "types": self._get_all_introspection_types(),
            "directives": []
        }

    def format_type(self, type_info: TypeInfo) -> GraphQLType:
        """Format a type definition as introspection dict."""
        result: GraphQLType = {
            "kind": type_info.kind,
            "name": type_info.name,
            "description": type_info.description,
            "fields": None if type_info.is_input else [self.format_field(f) for f in type_info.fields.values()],
            "inputFields": [self._format_input_field(f) for f in type_info.fields.values()] if type_info.is_input else None,
            "interfaces": type_info.interfaces if not type_info.is_input else None,
            "enumValues": type_info.enumValues,
            "possibleTypes": None
        }
        return result

    def format_field(self, field_info: FieldInfo) -> GraphQLField:
        """Format a field definition as introspection dict."""
        return {
            "name": field_info.name,
            "description": field_info.description,
            "args": [self._format_arg(arg) for arg in field_info.args],
            "type": self._build_type_ref(field_info),
            "isDeprecated": field_info.is_deprecated,
            "deprecationReason": field_info.deprecation_reason
        }

    # --- Main introspection methods ---

    async def execute(self, query: str) -> dict[str, Any]:
        """
        Execute an introspection query.

        Args:
            query: GraphQL query string

        Returns:
            Introspection response with data and errors
        """
        query_info = self._parse_introspection_query(query)
        data: dict[str, Any] = {}

        if query_info["requests_schema"]:
            data["__schema"] = self.generate()

        if query_info["requests_type"]:
            type_name = self._extract_type_name_from_query(query)
            if type_name:
                data["__type"] = self._get_introspection_type(type_name)

        return {"data": data, "errors": None}

    def is_introspection_query(self, query: str) -> bool:
        """Check if this is an introspection query."""
        query_stripped = query.strip()
        introspection_keywords = ["__schema", "__type", "__typename"]
        return any(keyword in query_stripped for keyword in introspection_keywords)

    # --- Internal implementation ---

    def _parse_introspection_query(self, query: str) -> dict[str, bool]:
        """Parse introspection query and extract requested fields."""
        try:
            requests_type = "__type(" in query or '__type (' in query.replace('"', "'")
            return {
                "requests_schema": "__schema" in query,
                "requests_type": requests_type
            }
        except Exception:
            return {"requests_schema": True, "requests_type": True}

    def _extract_type_name_from_query(self, query: str) -> Optional[str]:
        """Extract type name from query."""
        match = re.search(r'__type\s*\(\s*name\s*:\s*["\']([^"\']+)["\']', query)
        if match:
            return match.group(1)
        match = re.search(r"__type\s*\(\s*name\s*:\s*'([^']+)'", query)
        if match:
            return match.group(1)
        return None

    def _get_all_introspection_types(self) -> list[GraphQLType]:
        """Get all types for introspection."""
        types = []

        # Add scalar types (convert TypeInfo to dict format)
        for name in SCALAR_TYPES:
            scalar = SCALAR_TYPES[name]
            types.append({
                "kind": scalar.kind,
                "name": scalar.name,
                "description": scalar.description,
                "fields": None,
                "inputFields": None,
                "interfaces": None,
                "enumValues": None,
                "possibleTypes": None
            })

        # Collect all entity types
        self._collect_all_types()

        # Add enum types (ENUM)
        collected_enums = self._collect_all_enum_types()
        for enum_class in collected_enums:
            types.append(self._build_enum_type(enum_class))

        # Add entity types (OBJECT)
        for type_name, entity in self._collected_types.items():
            types.append(self._build_object_type(entity))

        # Add input types (INPUT_OBJECT)
        for input_type in self._input_types:
            types.append(self._build_input_type(input_type))

        # Add Pagination and Result types for one-to-many relationships
        page_types = self._build_page_types()
        types.extend(page_types)

        # Add per-entity group types ({Entity}Query / {Entity}Mutation). Each
        # group's fields are the entity's @query/@mutation methods, named verbatim.
        for entity_name, methods in self.query_map.items():
            if methods:
                types.append(self._build_entity_group_type(entity_name, methods, "Query"))
        for entity_name, methods in self.mutation_map.items():
            if methods:
                types.append(self._build_entity_group_type(entity_name, methods, "Mutation"))

        # Add Query type (root has one mount field per entity group)
        if any(self.query_map.values()):
            types.append({
                "kind": "OBJECT",
                "name": "Query",
                "description": "Root query type",
                "fields": self._get_query_fields(),
                "inputFields": None,
                "interfaces": [],
                "enumValues": None,
                "possibleTypes": None
            })

        # Add Mutation type
        if any(self.mutation_map.values()):
            types.append({
                "kind": "OBJECT",
                "name": "Mutation",
                "description": "Root mutation type",
                "fields": self._get_mutation_fields(),
                "inputFields": None,
                "interfaces": [],
                "enumValues": None,
                "possibleTypes": None
            })

        return types

    def _get_introspection_type(self, type_name: str) -> Optional[GraphQLType]:
        """Get introspection info for a specific type."""
        # Check scalar types (convert TypeInfo to dict format)
        if type_name in SCALAR_TYPES:
            scalar = SCALAR_TYPES[type_name]
            return {
                "kind": scalar.kind,
                "name": scalar.name,
                "description": scalar.description,
                "fields": None,
                "inputFields": None,
                "interfaces": None,
                "enumValues": None,
                "possibleTypes": None
            }

        # Collect all types
        self._collect_all_types()

        # Check enum types
        collected_enums = self._collect_all_enum_types()
        for enum_class in collected_enums:
            if enum_class.__name__ == type_name:
                return self._build_enum_type(enum_class)

        # Check entity types
        if type_name in self._collected_types:
            return self._build_object_type(self._collected_types[type_name])

        # Check input types
        for input_type in self._input_types:
            if input_type.__name__ == type_name:
                return self._build_input_type(input_type)

        # Check Pagination and Result types
        for page_type in self._build_page_types():
            if page_type["name"] == type_name:
                return page_type

        # Check per-entity group types ({Entity}Query / {Entity}Mutation)
        for entity_name, methods in self.query_map.items():
            if methods and group_type_name(entity_name, "Query") == type_name:
                return self._build_entity_group_type(entity_name, methods, "Query")
        for entity_name, methods in self.mutation_map.items():
            if methods and group_type_name(entity_name, "Mutation") == type_name:
                return self._build_entity_group_type(entity_name, methods, "Mutation")

        # Check Query type
        if type_name == "Query" and any(self.query_map.values()):
            return {
                "kind": "OBJECT",
                "name": "Query",
                "description": "Root query type",
                "fields": self._get_query_fields(),
                "inputFields": None,
                "interfaces": [],
                "enumValues": None,
                "possibleTypes": None
            }

        # Check Mutation type
        if type_name == "Mutation" and any(self.mutation_map.values()):
            return {
                "kind": "OBJECT",
                "name": "Mutation",
                "description": "Root mutation type",
                "fields": self._get_mutation_fields(),
                "inputFields": None,
                "interfaces": [],
                "enumValues": None,
                "possibleTypes": None
            }

        return None

    def _collect_all_types(self) -> None:
        """Collect all types from ERD and query/mutation maps."""
        if self._collected_types:
            return  # Already collected

        # Use shared TypeCollector for entity, relationship target, and nested types
        self._collected_types = self.collector.collect_all_types()

        # Collect input types
        self._input_types = self._collect_input_types_from_maps()

    def _collect_input_types_from_maps(self) -> set[type]:
        """Collect input types from query/mutation maps."""
        return self.collector.collect_input_types(self.query_map, self.mutation_map)

    def _build_object_type(self, entity: type) -> GraphQLType:
        """Build introspection OBJECT type."""
        return {
            "kind": "OBJECT",
            "name": entity.__name__,
            "description": self._get_class_description(entity),
            "fields": self._get_entity_fields(entity),
            "inputFields": None,
            "interfaces": [],
            "enumValues": None,
            "possibleTypes": None
        }

    def _build_input_type(self, kls: type) -> GraphQLType:
        """Build introspection INPUT_OBJECT type."""
        return {
            "kind": "INPUT_OBJECT",
            "name": kls.__name__,
            "description": self._get_class_description(kls),
            "fields": None,
            "inputFields": self._get_input_fields(kls),
            "interfaces": None,
            "enumValues": None,
            "possibleTypes": None
        }

    def _get_entity_fields(self, entity: type) -> list[dict]:
        """Get introspection fields for an entity."""
        fields = []

        try:
            type_hints = get_type_hints(entity)
        except Exception:
            type_hints = {}

        # Get entity config for relationship filtering
        entity_cfg = None
        for cfg in self.er_diagram.entities:
            if cfg.kls == entity:
                entity_cfg = cfg
                break

        # Process scalar fields
        for field_name, field_type in type_hints.items():
            if field_name.startswith('__'):
                continue

            # Skip relationship fields
            if entity_cfg and self._is_relationship_field(entity_cfg, field_name):
                continue

            type_def = self._build_graphql_type(field_type)
            description = describe_literal_values(
                self._get_field_description(entity, field_name), field_type
            )

            fields.append({
                "name": field_name,
                "description": description,
                "args": [],
                "type": type_def,
                "isDeprecated": False,
                "deprecationReason": None
            })

        # Process relationships
        # Note: relationships without loaders are hidden from introspection
        if entity_cfg:
            for rel in entity_cfg.relationships:
                if isinstance(rel, Relationship):
                    if not rel.name:
                        continue
                    if rel.loader is None:
                        continue

                    field_name = rel.name

                    # One-to-many: check if pagination is enabled
                    if rel.is_list_relationship:
                        target_classes = get_core_types(rel.target)
                        target_name = target_classes[0].__name__ if target_classes else "Unknown"

                        # If pagination enabled and page_loader exists, generate paginated field
                        if self.enable_pagination and rel.page_loader is not None:
                            result_name = f"{target_name}Result"
                            fields.append({
                                "name": field_name,
                                "description": rel.description,
                                "args": self._build_pagination_args(),
                                "type": {
                                    "kind": "NON_NULL",
                                    "name": None,
                                    "ofType": {
                                        "kind": "OBJECT",
                                        "name": result_name,
                                        "ofType": None
                                    }
                                },
                                "isDeprecated": False,
                                "deprecationReason": None
                            })
                        else:
                            # Regular list field without pagination
                            type_def = self._build_graphql_type(rel.target)
                            fields.append({
                                "name": field_name,
                                "description": rel.description,
                                "args": [],
                                "type": type_def,
                                "isDeprecated": False,
                                "deprecationReason": None
                            })
                    else:
                        type_def = self._build_graphql_type(rel.target)

                        fields.append({
                            "name": field_name,
                            "description": rel.description,
                            "args": [],
                            "type": type_def,
                            "isDeprecated": False,
                            "deprecationReason": None
                        })

        return fields

    def _build_pagination_args(self) -> list[dict]:
        """Build pagination arguments for one-to-many relationship fields."""
        return [
            {"name": "limit", "description": None, "type": {"kind": "SCALAR", "name": "Int", "ofType": None}, "defaultValue": None},
            {"name": "offset", "description": None, "type": {"kind": "SCALAR", "name": "Int", "ofType": None}, "defaultValue": None},
        ]

    def _build_page_types(self) -> list[dict]:
        """Generate Pagination and Result introspection types for one-to-many relationships."""
        if not self.enable_pagination:
            return []

        paginated_rels = self._collect_paginated_relationships()
        if not paginated_rels:
            return []

        types: list[dict] = []
        for _, target_name in paginated_rels:
            types.append(self._build_result_introspection_type(target_name))

        types.insert(0, self._build_pagination_introspection_type())

        return types

    def _build_pagination_introspection_type(self) -> dict:
        """Build Pagination introspection type."""
        return {
            "kind": "OBJECT",
            "name": "Pagination",
            "description": "Pagination information for list results.",
            "fields": [
                {
                    "name": "has_more",
                    "description": None,
                    "args": [],
                    "type": {"kind": "NON_NULL", "name": None, "ofType": {"kind": "SCALAR", "name": "Boolean", "ofType": None}},
                    "isDeprecated": False,
                    "deprecationReason": None,
                },
                {
                    "name": "total_count",
                    "description": None,
                    "args": [],
                    "type": {"kind": "SCALAR", "name": "Int", "ofType": None},
                    "isDeprecated": False,
                    "deprecationReason": None,
                },
            ],
            "inputFields": None,
            "interfaces": [],
            "enumValues": None,
            "possibleTypes": None,
        }

    def _build_result_introspection_type(self, entity_name: str) -> dict:
        """Build Result introspection type for a given entity."""
        result_name = f"{entity_name}Result"
        return {
            "kind": "OBJECT",
            "name": result_name,
            "description": f"Paginated result for {entity_name}.",
            "fields": [
                {
                    "name": "items",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "NON_NULL",
                        "name": None,
                        "ofType": {
                            "kind": "LIST",
                            "name": None,
                            "ofType": {
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {"kind": "OBJECT", "name": entity_name, "ofType": None},
                            },
                        },
                    },
                    "isDeprecated": False,
                    "deprecationReason": None,
                },
                {
                    "name": "pagination",
                    "description": None,
                    "args": [],
                    "type": {
                        "kind": "NON_NULL",
                        "name": None,
                        "ofType": {"kind": "OBJECT", "name": "Pagination", "ofType": None},
                    },
                    "isDeprecated": False,
                    "deprecationReason": None,
                },
            ],
            "inputFields": None,
            "interfaces": [],
            "enumValues": None,
            "possibleTypes": None,
        }

    def _get_input_fields(self, kls: type) -> list[dict]:
        """Get introspection inputFields for an input type."""
        fields = []

        try:
            type_hints = get_type_hints(kls)
        except Exception:
            type_hints = {}

        for field_name, field_type in type_hints.items():
            if field_name.startswith('__'):
                continue

            type_def = self._build_input_graphql_type(field_type)
            description = describe_literal_values(
                self._get_field_description(kls, field_name), field_type
            )

            fields.append({
                "name": field_name,
                "description": description,
                "type": type_def,
                "defaultValue": None
            })

        return fields

    def _get_query_fields(self) -> list[dict]:
        """Get root Query fields: one mount field per entity group.

        Under the grouped layout the root Query has one field per entity
        (``Entity: EntityQuery!``); the methods themselves live on the
        ``{Entity}Query`` group types (see :meth:`_build_entity_group_type`).
        """
        return [
            self._build_group_mount_field(name, "Query")
            for name, methods in self.query_map.items()
            if methods
        ]

    def _get_mutation_fields(self) -> list[dict]:
        """Get root Mutation fields: one mount field per entity group."""
        return [
            self._build_group_mount_field(name, "Mutation")
            for name, methods in self.mutation_map.items()
            if methods
        ]

    def _entity_description(self, entity_name: str) -> Optional[str]:
        """Return the stripped docstring for an entity class, or None."""
        entity = self._entity_map.get(entity_name)
        if entity is None:
            return None
        doc = getattr(entity, "__doc__", None)
        if not doc:
            return None
        doc = doc.strip()
        return doc or None

    def _build_group_mount_field(self, entity_name: str, group_suffix: str) -> dict:
        """Build the root field that mounts an entity's group type.

        Produces ``EntityName: EntityNameQuery!`` (NON_NULL OBJECT). The
        entity's docstring lives on the group OBJECT type (see
        :meth:`_build_entity_group_type`), not on this mount field, matching
        the SDL renderer.
        """
        return {
            "name": entity_name,
            "description": None,
            "args": [],
            "type": {
                "kind": "NON_NULL",
                "name": None,
                "ofType": {
                    "kind": "OBJECT",
                    "name": group_type_name(entity_name, group_suffix),
                    "ofType": None,
                },
            },
            "isDeprecated": False,
            "deprecationReason": None,
        }

    def _build_entity_group_type(
        self,
        entity_name: str,
        methods: dict[str, tuple[type, Callable]],
        group_suffix: str,
    ) -> GraphQLType:
        """Build the ``{Entity}Query`` / ``{Entity}Mutation`` group OBJECT.

        Its fields are the entity's @query/@mutation methods, named verbatim,
        and its description is the entity's docstring (matching the SDL
        renderer, which puts the docstring on the group type). ``methods`` is
        the inner grouped map ``{method_name: (entity, method)}``.
        """
        return {
            "kind": "OBJECT",
            "name": group_type_name(entity_name, group_suffix),
            "description": self._entity_description(entity_name),
            "fields": self._get_operation_fields(methods, group_suffix),
            "inputFields": None,
            "interfaces": [],
            "enumValues": None,
            "possibleTypes": None,
        }

    def _format_default_value(self, default_value: Any) -> str:
        """Format a Python default value as a GraphQL literal string.

        Handles:
        - Enum values: UserRole.USER -> "USER"
        - Strings: "hello" -> "\"hello\""
        - Numbers: 42 -> "42"
        - Booleans: True -> "true"
        - None: None -> "null"
        """
        if default_value is None:
            return "null"

        # Handle enum values - return just the member name
        if isinstance(default_value, Enum):
            return default_value.name

        # Handle strings - need to be quoted
        if isinstance(default_value, str):
            return f'"{default_value}"'

        # Handle booleans - lowercase
        if isinstance(default_value, bool):
            return "true" if default_value else "false"

        # Handle numbers
        if isinstance(default_value, (int, float)):
            return str(default_value)

        # Fallback to string representation
        return str(default_value)

    def _get_operation_fields(self, operation_map: dict, operation_type: str) -> list[dict]:
        """Get introspection fields for Query or Mutation type."""
        fields = []

        for field_name, (entity, method) in operation_map.items():
            sig = inspect.signature(method)

            # Build args
            args = []
            for param_name, param in sig.parameters.items():
                if param_name in ('self', 'cls'):
                    continue

                # _context is framework-injected, hidden from GraphQL schema
                if param_name == '_context':
                    continue

                param_type_def = None
                if param.annotation != inspect.Parameter.empty:
                    param_type_def = self._build_input_graphql_type(param.annotation)

                if param_type_def is None:
                    param_type_def = {"kind": "SCALAR", "name": "String", "ofType": None}

                has_default = param.default != inspect.Parameter.empty

                args.append({
                    "name": param_name,
                    "description": describe_literal_values(None, param.annotation)
                    if param.annotation != inspect.Parameter.empty
                    else None,
                    "type": param_type_def,
                    "defaultValue": None if not has_default else self._format_default_value(param.default)
                })

            # Build return type
            return_type_def = self._build_return_type(sig.return_annotation, entity)

            description = None
            if operation_type == "Query":
                description = getattr(method, const.GRAPHQL_QUERY_DESCRIPTION_ATTR, None)
            elif operation_type == "Mutation":
                description = getattr(method, const.GRAPHQL_MUTATION_DESCRIPTION_ATTR, None)

            fields.append({
                "name": field_name,
                "description": description,
                "args": args,
                "type": return_type_def,
                "isDeprecated": False,
                "deprecationReason": None
            })

        return fields

    def _build_return_type(self, return_type: type, entity: type) -> GraphQLTypeRef:
        """Build return type for operation fields."""
        if return_type == inspect.Parameter.empty:
            return {"kind": "SCALAR", "name": "String", "ofType": None}

        # Handle scalar types (bool, int, str, float, etc.)
        if return_type is bool:
            return {"kind": "SCALAR", "name": "Boolean", "ofType": None}
        elif return_type is int:
            return {"kind": "SCALAR", "name": "Int", "ofType": None}
        elif return_type is float:
            return {"kind": "SCALAR", "name": "Float", "ofType": None}
        elif return_type is str:
            return {"kind": "SCALAR", "name": "String", "ofType": None}

        return_type_str = str(return_type)

        # Handle list types
        if "list" in return_type_str.lower():
            return {
                "kind": "LIST",
                "name": None,
                "description": None,
                "ofType": {
                    "kind": "OBJECT",
                    "name": entity.__name__ if hasattr(entity, '__name__') else "String",
                    "ofType": None
                }
            }

        # Handle entity/object types
        if hasattr(entity, '__name__'):
            return {
                "kind": "OBJECT",
                "name": entity.__name__,
                "description": None,
                "ofType": None
            }

        return {"kind": "SCALAR", "name": "String", "ofType": None}

    def _build_graphql_type(self, field_type: Any) -> GraphQLTypeRef:
        """Build GraphQL type definition for output types."""
        core_types = get_core_types(field_type)
        if not core_types:
            return {"kind": "SCALAR", "name": "String", "description": None, "ofType": None}

        core_type = core_types[0]

        if is_list_type(field_type):
            if safe_issubclass(core_type, BaseModel):
                return {
                    "kind": "LIST",
                    "name": None,
                    "description": None,
                    "ofType": {
                        "kind": "OBJECT",
                        "name": core_type.__name__,
                        "description": f"{core_type.__name__} entity",
                        "ofType": None
                    }
                }
            # Check if it's an enum type
            elif is_enum_type(core_type):
                return {
                    "kind": "LIST",
                    "name": None,
                    "description": None,
                    "ofType": {
                        "kind": "ENUM",
                        "name": core_type.__name__,
                        "description": None,
                        "ofType": None
                    }
                }
            else:
                scalar_name = map_scalar_type(core_type)
                return {
                    "kind": "LIST",
                    "name": None,
                    "description": None,
                    "ofType": {
                        "kind": "SCALAR",
                        "name": scalar_name,
                        "description": None,
                        "ofType": None
                    }
                }
        else:
            # Check if it's an enum type
            if is_enum_type(core_type):
                return {
                    "kind": "ENUM",
                    "name": core_type.__name__,
                    "description": None,
                    "ofType": None
                }
            elif safe_issubclass(core_type, BaseModel):
                return {
                    "kind": "OBJECT",
                    "name": core_type.__name__,
                    "description": f"{core_type.__name__} entity",
                    "ofType": None
                }
            else:
                scalar_name = map_scalar_type(core_type)
                return {
                    "kind": "SCALAR",
                    "name": scalar_name,
                    "description": None,
                    "ofType": None
                }

    def _build_input_graphql_type(self, field_type: Any) -> GraphQLTypeRef:
        """Build GraphQL type definition for input types."""
        core_types = get_core_types(field_type)
        if not core_types:
            return {"kind": "SCALAR", "name": "String", "description": None, "ofType": None}

        core_type = core_types[0]

        if is_list_type(field_type):
            if safe_issubclass(core_type, BaseModel):
                return {
                    "kind": "LIST",
                    "name": None,
                    "description": None,
                    "ofType": {
                        "kind": "INPUT_OBJECT",
                        "name": core_type.__name__,
                        "description": f"{core_type.__name__} input",
                        "ofType": None
                    }
                }
            # Check if it's an enum type
            elif is_enum_type(core_type):
                return {
                    "kind": "LIST",
                    "name": None,
                    "description": None,
                    "ofType": {
                        "kind": "ENUM",
                        "name": core_type.__name__,
                        "description": None,
                        "ofType": None
                    }
                }
            else:
                scalar_name = map_scalar_type(core_type)
                return {
                    "kind": "LIST",
                    "name": None,
                    "description": None,
                    "ofType": {
                        "kind": "SCALAR",
                        "name": scalar_name,
                        "description": None,
                        "ofType": None
                    }
                }
        else:
            # Check if it's an enum type
            if is_enum_type(core_type):
                return {
                    "kind": "ENUM",
                    "name": core_type.__name__,
                    "description": None,
                    "ofType": None
                }
            elif safe_issubclass(core_type, BaseModel):
                return {
                    "kind": "INPUT_OBJECT",
                    "name": core_type.__name__,
                    "description": f"{core_type.__name__} input",
                    "ofType": None
                }
            else:
                scalar_name = map_scalar_type(core_type)
                return {
                    "kind": "SCALAR",
                    "name": scalar_name,
                    "description": None,
                    "ofType": None
                }

    def _format_arg(self, arg: ArgumentInfo) -> GraphQLArgument:
        """Format an argument for introspection."""
        return {
            "name": arg.name,
            "description": arg.description,
            "type": {"kind": "SCALAR", "name": arg.graphql_type_name, "ofType": None},
            "defaultValue": arg.default_value
        }

    def _format_input_field(self, field_info: FieldInfo) -> GraphQLField:
        """Format an input field for introspection."""
        return {
            "name": field_info.name,
            "description": field_info.description,
            "type": {"kind": "SCALAR", "name": field_info.graphql_type_name, "ofType": None},
            "defaultValue": None
        }

    def _build_type_ref(self, field_info: FieldInfo) -> GraphQLTypeRef:
        """Build type reference for field."""
        if field_info.is_list:
            return {
                "kind": "LIST",
                "name": None,
                "ofType": {
                    "kind": "OBJECT",
                    "name": field_info.graphql_type_name,
                    "ofType": None
                }
            }
        elif field_info.is_optional:
            return {
                "kind": "OBJECT",
                "name": field_info.graphql_type_name,
                "ofType": None
            }
        else:
            return {
                "kind": "NON_NULL",
                "name": None,
                "ofType": {
                    "kind": "OBJECT",
                    "name": field_info.graphql_type_name,
                    "ofType": None
                }
            }

    def _get_class_description(self, kls: type) -> str:
        """Get description from class docstring."""
        doc = getattr(kls, '__doc__', None)
        if doc:
            doc = doc.strip()
            if doc:
                return doc
        return f"{kls.__name__} entity"

    def _get_field_description(self, kls: type, field_name: str) -> Optional[str]:
        """Get description from Pydantic field."""
        if not hasattr(kls, 'model_fields'):
            return None
        if field_name not in kls.model_fields:
            return None
        field = kls.model_fields[field_name]
        return getattr(field, 'description', None)


    def _build_enum_type(self, enum_class: type) -> GraphQLType:
        """Build introspection ENUM type."""
        enum_values = get_enum_names(enum_class)
        return {
            "kind": "ENUM",
            "name": enum_class.__name__,
            "description": (enum_class.__doc__ or f"{enum_class.__name__} enum").strip(),
            "fields": None,
            "inputFields": None,
            "interfaces": None,
            "enumValues": [
                {"name": v, "description": None, "isDeprecated": False, "deprecationReason": None}
                for v in enum_values
            ],
            "possibleTypes": None
        }

    def _collect_all_enum_types(self) -> list[type]:
        """Collect all enum types from entities, input types, and query/mutation maps."""
        # Collect from entity types and input types
        types_to_scan = list(self._collected_types.values()) + list(self._input_types)
        return self.collector.collect_enum_types(types_to_scan)
