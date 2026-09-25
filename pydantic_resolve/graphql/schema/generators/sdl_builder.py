"""
SDL (Schema Definition Language) builder.

This module builds GraphQL SDL strings from ErDiagram,
using the unified type collection and mapping logic.
"""

import inspect
from typing import ForwardRef, get_type_hints

from pydantic import BaseModel

from pydantic_resolve.graphql.schema.generators.base import SchemaGenerator
from pydantic_resolve.graphql.utils import group_type_name
from pydantic_resolve.utils.class_util import safe_issubclass
from pydantic_resolve.utils.er_diagram import Relationship
from pydantic_resolve.utils.types import get_core_types, _is_list
from pydantic_resolve.graphql.type_mapping import (
    is_enum_type,
    get_enum_names,
    describe_literal_values,
)
from pydantic_resolve.graphql.exceptions import FieldNameConflictError


class SDLBuilder(SchemaGenerator):
    """
    Builds GraphQL SDL (Schema Definition Language) strings.

    This is the refactored implementation that uses the unified
    type collection and mapping logic.
    """

    def __init__(self, er_diagram, validate_conflicts=True, enable_pagination=False):
        super().__init__(er_diagram, validate_conflicts)
        self.enable_pagination = enable_pagination

    def generate(self) -> str:
        """
        Build complete GraphQL Schema.

        Returns:
            GraphQL Schema string
        """
        # Runtime field conflict validation
        if self.validate_conflicts:
            self._validate_all_entities()

        enum_defs: list[str] = []
        input_defs: list[str] = []
        type_defs: list[str] = []
        processed_types = set()
        processed_input_types = set()
        processed_enums = set()

        # Build all entity types
        for entity_cfg in self.er_diagram.entities:
            type_def = self._build_type_definition(entity_cfg)
            type_defs.append(type_def)
            processed_types.add(entity_cfg.kls)

            # Collect enum types from entity
            self._add_enum_definitions(entity_cfg.kls, enum_defs, processed_enums)

        # Collect and generate nested Pydantic types (including relationship targets)
        all_collected = self.collector.collect_all_types()
        for type_name, cls in all_collected.items():
            if cls not in processed_types:
                type_def = self._build_type_definition_for_class(cls)
                type_defs.append(type_def)
                processed_types.add(cls)

                # Collect enum types from nested types
                self._add_enum_definitions(cls, enum_defs, processed_enums)

        # Collect and generate Input Types
        input_types = self.collector.collect_input_types()
        for input_type in input_types:
            if input_type not in processed_input_types:
                input_def = self._build_input_definition(input_type)
                input_defs.append(input_def)
                processed_input_types.add(input_type)

                # Collect enum types from input types
                self._add_enum_definitions(input_type, enum_defs, processed_enums)

        # Assemble schema
        schema_parts = []

        # Add enum definitions first (before types)
        if enum_defs:
            schema_parts.append("\n".join(enum_defs))

        if input_defs:
            schema_parts.append("\n".join(input_defs))

        # Add Pagination and Result types for one-to-many relationships
        page_result_defs = self._collect_page_result_sdl_defs(processed_types)
        if page_result_defs:
            schema_parts.append("\n".join(page_result_defs))

        if type_defs:
            schema_parts.append("\n".join(type_defs))

        # Per-entity Query/Mutation group types (e.g. ``type UserQuery { ... }``),
        # emitted before the root types that reference them.
        query_group_blocks, query_root_lines = self._collect_groups("query", "Query")
        mutation_group_blocks, mutation_root_lines = self._collect_groups("mutation", "Mutation")
        if query_group_blocks:
            schema_parts.extend(query_group_blocks)
        if mutation_group_blocks:
            schema_parts.extend(mutation_group_blocks)

        schema = "\n\n".join(schema_parts) + "\n\n"

        # Root Query type (only if any entity has queries). Gated like Mutation
        # so a query-less schema doesn't emit an invalid empty ``type Query {}``
        # — and stays consistent with introspection's queryType=None.
        if query_root_lines:
            schema += "type Query {\n"
            schema += "\n".join(f"  {line}" for line in query_root_lines) + "\n"
            schema += "}\n\n"

        # Root Mutation type (only if any entity has mutations).
        if mutation_root_lines:
            schema += "type Mutation {\n"
            schema += "\n".join(f"  {line}" for line in mutation_root_lines) + "\n"
            schema += "}\n"

        return schema

    def format_type(self, type_info):
        """Format a type definition as SDL string.

        Note: This method exists to satisfy the abstract base class requirement.
        SDLBuilder uses its own internal methods for type generation.
        """
        raise NotImplementedError(
            "SDLBuilder does not use format_type(). "
            "Use generate() or generate_operation_sdl() instead."
        )

    def format_field(self, field_info):
        """Format a field definition as SDL string.

        Note: This method exists to satisfy the abstract base class requirement.
        SDLBuilder uses its own internal methods for field generation.
        """
        raise NotImplementedError(
            "SDLBuilder does not use format_field(). "
            "Use generate() or generate_operation_sdl() instead."
        )

    # --- Internal methods (preserved from SchemaBuilder for backward compatibility) ---

    def _field_line(self, field_name: str, field_type, gql_type: str) -> str:
        """Render one field line, prefixed with an ``Allowed values: ...``
        docstring when the annotation is a scalar ``Literal`` (the only way
        to surface the constraint — GraphQL has no constrained scalar)."""
        description = describe_literal_values(None, field_type)
        line = f"  {field_name}: {gql_type}"
        if description:
            return f'  """{description}"""\n{line}'
        return line

    def _build_type_definition(self, entity_cfg) -> str:
        """Generate GraphQL type definition for a single entity."""
        fields = []

        try:
            type_hints = get_type_hints(entity_cfg.kls)
        except Exception:
            type_hints = {}

        # Get relationship field names to skip them in scalar field processing
        relationship_field_names = set()
        for rel in entity_cfg.relationships:
            if isinstance(rel, Relationship):
                if rel.name:
                    relationship_field_names.add(rel.name)

        # Process scalar fields (skip relationship fields)
        for field_name, field_type in type_hints.items():
            if field_name.startswith('__'):
                continue
            if field_name in relationship_field_names:
                continue

            gql_type = self.mapper.map_to_sdl(field_type)
            fields.append(self._field_line(field_name, field_type, gql_type))

        # Process relationships using unified type mapping
        # Note: relationships without loaders are hidden from GraphQL schema
        for rel in entity_cfg.relationships:
            if isinstance(rel, Relationship):
                if rel.name:
                    if rel.loader is None:
                        continue
                    field_name = rel.name

                    # One-to-many relationship
                    if _is_list(rel.target):
                        if self.enable_pagination and rel.page_loader is not None:
                            # Paginated: articles(limit: Int, offset: Int): ArticleEntityResult!
                            target_name = self._get_target_entity_name(rel)
                            result_type_name = f"{target_name}Result" if target_name else "Result"
                            args_str = "(limit: Int, offset: Int)"
                            if rel.description:
                                fields.append(f'  """{rel.description}"""\n  {field_name}{args_str}: {result_type_name}!')
                            else:
                                fields.append(f"  {field_name}{args_str}: {result_type_name}!")
                        else:
                            # Raw list: posts: [PostEntity!]!
                            gql_type = self.mapper.map_to_sdl(rel.target)
                            if rel.description:
                                fields.append(f'  """{rel.description}"""\n  {field_name}: {gql_type}')
                            else:
                                fields.append(f"  {field_name}: {gql_type}")
                    else:
                        gql_type = self.mapper.map_to_sdl(rel.target)
                        if rel.description:
                            fields.append(f'  """{rel.description}"""\n  {field_name}: {gql_type}')
                        else:
                            fields.append(f"  {field_name}: {gql_type}")

        return f"type {entity_cfg.kls.__name__} {{\n" + "\n".join(fields) + "\n}"

    def _build_type_definition_for_class(self, kls: type) -> str:
        """Generate GraphQL type definition for any Pydantic BaseModel class."""
        fields = []

        try:
            type_hints = get_type_hints(kls)
        except Exception:
            type_hints = {}

        for field_name, field_type in type_hints.items():
            if field_name.startswith('__'):
                continue

            gql_type = self.mapper.map_to_sdl(field_type)
            fields.append(self._field_line(field_name, field_type, gql_type))

        return f"type {kls.__name__} {{\n" + "\n".join(fields) + "\n}"

    def _build_input_definition(self, kls: type) -> str:
        """Generate GraphQL Input type definition."""
        fields = []

        try:
            type_hints = get_type_hints(kls)
        except Exception:
            type_hints = {}

        for field_name, field_type in type_hints.items():
            if field_name.startswith('__'):
                continue

            gql_type = self.mapper.map_to_sdl(field_type, is_input=True)
            fields.append(self._field_line(field_name, field_type, gql_type))

        return f"input {kls.__name__} {{\n" + "\n".join(fields) + "\n}"

    def _extract_query_methods(self, entity: type) -> list[dict]:
        """Extract all @query decorated methods."""
        raw_methods = self._extract_operation_methods(entity, "query")
        return [self._enrich_method_info(m) for m in raw_methods]

    def _extract_mutation_methods(self, entity: type) -> list[dict]:
        """Extract all @mutation decorated methods."""
        raw_methods = self._extract_operation_methods(entity, "mutation")
        return [self._enrich_method_info(m) for m in raw_methods]

    def _enrich_method_info(self, method_info: dict) -> dict:
        """Add SDL-specific formatting to method info from base class."""
        # Enrich params with SDL type strings and definitions
        params = []
        for p in method_info['params']:
            try:
                gql_type = self.mapper.map_to_sdl(p['type'])
            except Exception:
                gql_type = 'Any'

            if p['required']:
                param_str = f"{p['name']}: {gql_type}"
            else:
                param_str = f"{p['name']}: {gql_type.rstrip('!')}"

            params.append({
                'name': p['name'],
                'type': gql_type,
                'required': p['required'],
                'default': p['default'],
                'definition': param_str
            })

        # Enrich return type
        try:
            gql_return_type = self._map_return_type_to_gql(method_info['return_type'])
        except Exception:
            gql_return_type = 'Any'

        return {
            'name': method_info['name'],
            'description': method_info['description'],
            'params': params,
            'return_type': gql_return_type,
            'entity': method_info['entity'],
            'method': method_info['method']
        }

    def _build_query_def(self, method_info: dict) -> str:
        """Build single query definition."""
        name = method_info['name']
        params_str = ""
        if method_info['params']:
            params = ", ".join(p['definition'] for p in method_info['params'])
            params_str = f"({params})"
        return f"{name}{params_str}: {method_info['return_type']}"

    def _build_mutation_def(self, method_info: dict) -> str:
        """Build single mutation definition."""
        name = method_info['name']
        params_str = ""
        if method_info['params']:
            params = ", ".join(p['definition'] for p in method_info['params'])
            params_str = f"({params})"
        return f"{name}{params_str}: {method_info['return_type']}"

    def _collect_groups(
        self, operation_type: str, group_suffix: str
    ) -> tuple[list[str], list[str]]:
        """Build per-entity group type blocks and root mount lines.

        Args:
            operation_type: ``"query"`` or ``"mutation"``.
            group_suffix: ``"Query"`` or ``"Mutation"`` (the type-name suffix).

        Returns:
            ``(group_type_blocks, root_field_lines)``. ``group_type_blocks`` are
            ``type {Entity}Query { ... }`` SDL definitions; ``root_field_lines``
            are ``{Entity}: {Entity}Query!`` entries for the root type. Entities
            with no methods of this kind contribute nothing.
        """
        if operation_type == "query":
            extract = self._extract_query_methods
            build_def = self._build_query_def
        else:
            extract = self._extract_mutation_methods
            build_def = self._build_mutation_def

        # Reuse the handler's scan so standalone SchemaBuilder.build_schema()
        # applies the same eager conflict checks (reserved names, duplicate
        # method field names) and computes the same grouping as the handler.
        from pydantic_resolve.graphql.schema_errors import scan_grouped_methods
        scanned = scan_grouped_methods(self.er_diagram.entities, extract, group_suffix)

        group_blocks: list[str] = []
        root_lines: list[str] = []

        for entity_name, method_infos in scanned.items():
            method_fields = [f"  {build_def(m)}" for m in method_infos]
            gt_name = group_type_name(entity_name, group_suffix)
            body = f"type {gt_name} {{\n{chr(10).join(method_fields)}\n}}"
            # All method_infos on one entity share the same defining class.
            description = self._get_class_description(method_infos[0]['entity'])
            if description:
                body = f'"""{description}"""\n{body}'
            group_blocks.append(body)
            root_lines.append(f"{entity_name}: {gt_name}!")

        return group_blocks, root_lines

    def _map_return_type_to_gql(self, return_type: type) -> str:
        """Map return type to GraphQL type."""
        core_types = get_core_types(return_type)
        if not core_types:
            return self.mapper.map_to_sdl(return_type)

        core_type = core_types[0]

        if _is_list(return_type):
            inner_gql = self.mapper.map_to_sdl(core_type)
            return f"[{inner_gql}]"

        return self.mapper.map_to_sdl(return_type)

    def _validate_all_entities(self) -> None:
        """Validate field name conflicts for all entities."""
        for entity_cfg in self.er_diagram.entities:
            self._validate_entity_fields(entity_cfg)

    def _validate_entity_fields(self, entity_cfg) -> None:
        """Validate field conflicts for a single entity."""
        try:
            scalar_fields = set(get_type_hints(entity_cfg.kls).keys())
        except Exception:
            scalar_fields = set()

        relationship_fields = set()
        for rel in entity_cfg.relationships:
            if isinstance(rel, Relationship) and rel.name:
                relationship_fields.add(rel.name)

        conflicts = scalar_fields & relationship_fields
        if conflicts:
            field_name = next(iter(conflicts))
            raise FieldNameConflictError(
                message=f"Field name conflict in {entity_cfg.kls.__name__}: '{field_name}'",
                entity_name=entity_cfg.kls.__name__,
                field_name=field_name,
                conflict_type="SCALAR_CONFLICT"
            )

    def _build_enum_definition(self, enum_class: type) -> str:
        """Generate GraphQL enum definition.

        Args:
            enum_class: Python Enum class

        Returns:
            GraphQL enum definition string
        """
        values = get_enum_names(enum_class)
        if not values:
            return ""
        values_str = "\n".join(f"  {v}" for v in values)
        return f"enum {enum_class.__name__} {{\n{values_str}\n}}"

    def _collect_enum_types(self, kls: type) -> set:
        """Collect all enum types used in entity fields.

        Args:
            kls: Pydantic BaseModel class

        Returns:
            Set of enum types found in the class
        """
        enums = set()
        try:
            type_hints = get_type_hints(kls)
        except Exception:
            return enums

        for field_name, field_type in type_hints.items():
            if field_name.startswith('__'):
                continue
            core_types_list = get_core_types(field_type)
            for core_type in core_types_list:
                if is_enum_type(core_type):
                    enums.add(core_type)
        return enums

    def _add_enum_definitions(self, kls: type, enum_defs: list[str], processed_enums: set[str]) -> None:
        """Collect and add enum definitions from a class.

        Args:
            kls: Pydantic BaseModel class
            enum_defs: List to append enum definition strings
            processed_enums: Set of already processed enum names
        """
        enum_types = self._collect_enum_types(kls)
        for enum_type in enum_types:
            if enum_type.__name__ not in processed_enums:
                enum_defs.append(self._build_enum_definition(enum_type))
                processed_enums.add(enum_type.__name__)

    def generate_operation_sdl(
        self,
        entity_name: str,
        method_name: str,
        operation_type: str = "Query",
    ) -> str | None:
        """Generate SDL for a single operation and its related types.

        This method is used for MCP progressive disclosure; it generates the
        SDL for a specific query or mutation (identified by its entity group and
        verbatim method name) along with all related type definitions.

        Under the grouped layout the method is a field on the
        ``{Entity}Query`` / ``{Entity}Mutation`` group type, reached via a root
        mount field. The generated SDL reflects that shape so an AI consumer
        can construct a valid query — it is **not** a flat root field.

        Args:
            entity_name: Entity class name (the group the method belongs to).
            method_name: Method name verbatim (the field on the group type).
            operation_type: "Query" or "Mutation" (default: "Query")

        Returns:
            SDL string for the operation and related types, or None if not found.

        Example:
            >>> generator.generate_operation_sdl("UserEntity", "get_all", "Query")
            'type Query {\\n  UserEntity: UserEntityQuery!\\n}\\n\\ntype UserEntityQuery {\\n  get_all(limit: Int): [UserEntity!]!\\n}\\n\\n# Related Types\\ntype UserEntity { ... }'
        """
        # Find the operation method
        method_info = self._find_operation_method(entity_name, method_name, operation_type)
        if not method_info:
            return None

        # Collect related entity types
        related_entities = self._collect_related_entities_from_method(method_info)

        # Build SDL parts
        parts = []

        # Root mount + group type: the method is a field on {Entity}Query /
        # {Entity}Mutation, reached via `<entity>: <group>!` on the root type.
        if operation_type == "Query":
            field_def = self._build_query_def(method_info)
        else:
            field_def = self._build_mutation_def(method_info)
        # Surface the method's docstring as a field description — the most
        # useful piece for an AI consumer of this progressive-disclosure SDL.
        description = method_info.get("description")
        if description:
            field_block = f'  """{description}"""\n  {field_def}'
        else:
            field_block = f"  {field_def}"
        group_type = group_type_name(entity_name, operation_type)
        parts.append(
            f"type {operation_type} {{\n"
            f"  {entity_name}: {group_type}!\n"
            f"}}\n\n"
            f"type {group_type} {{\n"
            f"{field_block}\n"
            f"}}"
        )

        # Build related type definitions
        if related_entities:
            type_defs = []
            processed_types = set()
            processed_enums = set()

            for entity in related_entities:
                if entity.__name__ not in processed_types:
                    type_defs.append(self._build_entity_type(entity))
                    processed_types.add(entity.__name__)
                    # Collect enums from this entity
                    self._add_enum_definitions(entity, type_defs, processed_enums)

            if type_defs:
                parts.append("# Related Types\n" + "\n\n".join(type_defs))

        return "\n\n".join(parts)

    def _find_operation_method(
        self, entity_name: str, method_name: str, operation_type: str
    ) -> dict | None:
        """Find method info for a given (entity, method) pair.

        Args:
            entity_name: Entity class name (the group field on the root type).
            method_name: Method name verbatim (the field on the group type).
            operation_type: "Query" or "Mutation"

        Returns:
            Method info dictionary or None if not found
        """
        for entity_cfg in self.er_diagram.entities:
            if entity_cfg.kls.__name__ != entity_name:
                continue
            if operation_type == "Query":
                methods = self._extract_query_methods(entity_cfg.kls)
            else:
                methods = self._extract_mutation_methods(entity_cfg.kls)

            for method in methods:
                if method['name'] == method_name:
                    return method
        return None

    def _collect_related_entities_from_method(self, method_info: dict) -> set[type]:
        """Collect all related entity types from a method's return type and parameters.

        Args:
            method_info: Method info dictionary

        Returns:
            Set of related entity classes
        """
        related_entities = set()
        visited = set()

        def collect_from_type(python_type) -> None:
            """Recursively collect entity types from a type hint."""
            core_types = get_core_types(python_type)

            for core_type in core_types:
                # Handle ForwardRef by resolving to actual class
                if isinstance(core_type, ForwardRef):
                    type_name = core_type.__forward_arg__
                    resolved = self.mapper._get_entity_by_name(type_name)
                    if resolved:
                        core_type = resolved
                    else:
                        continue

                # Handle string type names that correspond to entity types
                if isinstance(core_type, str):
                    resolved = self.mapper._get_entity_by_name(core_type)
                    if resolved:
                        core_type = resolved
                    else:
                        continue

                if safe_issubclass(core_type, BaseModel):
                    type_name = core_type.__name__
                    if type_name not in visited:
                        # Check if it's an entity in our ER diagram
                        for entity_cfg in self.er_diagram.entities:
                            if entity_cfg.kls == core_type:
                                visited.add(type_name)
                                related_entities.add(core_type)

                                # Recursively collect from entity fields
                                try:
                                    type_hints = get_type_hints(core_type)
                                    for field_type in type_hints.values():
                                        collect_from_type(field_type)
                                except Exception:
                                    pass

                                # Collect from relationships with name
                                for rel in entity_cfg.relationships:
                                    if isinstance(rel, Relationship):
                                        if rel.name:
                                            collect_from_type(rel.target)
                                break  # Found the entity, no need to check other configs

        # Collect from return type
        method = method_info.get('method')
        if method:
            try:
                sig = inspect.signature(method)
                return_type = sig.return_annotation
                if return_type != inspect.Signature.empty:
                    collect_from_type(return_type)
            except Exception:
                pass

            # Collect from parameter types
            try:
                sig = inspect.signature(method)
                for param_name, param in sig.parameters.items():
                    if param_name in ('self', 'cls'):
                        continue
                    if param_name == '_context':
                        continue
                    if param.annotation != inspect.Parameter.empty:
                        collect_from_type(param.annotation)
            except Exception:
                pass

        return related_entities

    def _build_entity_type(self, entity: type) -> str:
        """Build GraphQL type definition for an entity.

        Args:
            entity: Pydantic BaseModel class

        Returns:
            GraphQL type definition string
        """
        fields = []

        # Get entity config for relationship filtering
        entity_cfg = None
        for cfg in self.er_diagram.entities:
            if cfg.kls == entity:
                entity_cfg = cfg
                break

        # Process scalar fields
        try:
            type_hints = get_type_hints(entity)
        except Exception:
            type_hints = {}

        for field_name, field_type in type_hints.items():
            if field_name.startswith('__'):
                continue

            # Skip relationship fields
            if entity_cfg and self._is_relationship_field(entity_cfg, field_name):
                continue

            gql_type = self.mapper.map_to_sdl(field_type)
            fields.append(f"  {field_name}: {gql_type}")

        # Process relationship fields
        # Note: relationships without loaders are hidden from GraphQL schema
        if entity_cfg:
            for rel in entity_cfg.relationships:
                if isinstance(rel, Relationship):
                    if rel.name:
                        if rel.loader is None:
                            continue
                        field_name = rel.name

                        if rel.is_list_relationship and self.enable_pagination and rel.page_loader is not None:
                            target_name = self._get_target_entity_name(rel)
                            result_type_name = f"{target_name}Result" if target_name else "Result"
                            args_str = "(limit: Int, offset: Int)"
                            fields.append(f"  {field_name}{args_str}: {result_type_name}!")
                        else:
                            gql_type = self.mapper.map_to_sdl(rel.target)
                            fields.append(f"  {field_name}: {gql_type}")

        # Build type definition
        type_def = f"type {entity.__name__} {{\n" + "\n".join(fields) + "\n}"
        return type_def

    # ========== Pagination / Result Type Generation ==========

    def _collect_page_result_sdl_defs(self, processed_types: set) -> list[str]:
        """Generate Pagination and Result SDL for all one-to-many relationships.

        Returns:
            List of SDL strings for Pagination (once) and {Entity}Result types.
        """
        if not self.enable_pagination:
            return []

        paginated_rels = self._collect_paginated_relationships()
        if not paginated_rels:
            return []

        defs: list[str] = []
        for _, target_name in paginated_rels:
            result_name = f"{target_name}Result"
            defs.append(
                f"type {result_name} {{\n"
                f"  items: [{target_name}!]!\n"
                f"  pagination: Pagination!\n"
                f"}}"
            )

        defs.insert(0, (
            "type Pagination {\n"
            "  has_more: Boolean!\n"
            "  total_count: Int\n"
            "}"
        ))

        return defs
