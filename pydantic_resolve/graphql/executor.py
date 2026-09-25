"""
GraphQL query and mutation execution.

Handles the two-phase execution for queries:
- Phase 1 (Serial): Parse query, build response models (no I/O)
- Phase 2 (Concurrent): Parallel execution of (query_method + transform + resolve)

For mutations:
- Serial execution of each mutation (mutation_method + transform + resolve)
"""

import asyncio
import inspect
import logging
import os
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Callable, Optional, Union, get_args, get_origin
from uuid import UUID

from pydantic import BaseModel

import pydantic_resolve.constant as const
from pydantic_resolve.resolver import Resolver
from pydantic_resolve.utils.class_util import safe_issubclass
from pydantic_resolve.utils.types import (
    _resolve_function_type_hints,
    get_core_types,
)
from pydantic_resolve.graphql.exceptions import GraphQLError
from pydantic_resolve.graphql.query_parser import QueryParser, reject_all_aliases
from pydantic_resolve.graphql.utils import group_type_name
from pydantic_resolve.graphql.response_builder import ResponseBuilder

logger = logging.getLogger(__name__)


# Scalar types whose GraphQL wire form is a string but which callers expect as
# real Python objects (UUID/datetime/date/time). Ordered: datetime subclasses
# date, so datetime must precede date.
_COERCIBLE_SCALARS = (UUID, datetime, time, date)


def _is_coercible_scalar(py_type: Any) -> bool:
    """True if ``py_type`` is a temporal/UUID scalar we coerce from string."""
    return any(safe_issubclass(py_type, t) for t in _COERCIBLE_SCALARS)


class EnumWireError(ValueError):
    """An invalid enum wire value reached argument conversion.

    Raised instead of letting the blanket conversion catch downgrade the
    failure to a warning and pass the raw string into the method body.
    """

    def __init__(self, enum_cls: type, value: Any) -> None:
        self.enum_cls = enum_cls
        self.value = value
        super().__init__(
            f"Invalid {enum_cls.__name__} value '{value}'; expected one of: "
            f"{', '.join(enum_cls.__members__)}"
        )


class QueryExecutor:
    """
    Handles GraphQL query and mutation execution.

    For queries - two-phase execution:
    - Phase 1 (Serial): Parse query, build response models (no I/O)
    - Phase 2 (Concurrent): Parallel execution of (query_method + transform + resolve)

    For mutations - serial execution:
    - Each mutation executes sequentially (mutation_method + transform + resolve)
    """

    def __init__(
        self,
        parser: QueryParser,
        builder: ResponseBuilder,
        resolver_class: type[Resolver],
        enable_from_attribute_in_type_adapter: bool = False,
        resolved_hooks: list[Callable] | None = None,
    ):
        """
        Args:
            parser: Query parser instance
            builder: Response builder instance
            resolver_class: Resolver class to use
            enable_from_attribute_in_type_adapter: Enable Pydantic from_attributes mode
            resolved_hooks: List of hooks to execute after each resolve field
        """
        self.parser = parser
        self.builder = builder
        self.resolver_class = resolver_class
        self.enable_from_attribute_in_type_adapter = enable_from_attribute_in_type_adapter
        self.resolved_hooks = resolved_hooks or []

    async def execute_query(
        self,
        query: str,
        query_map: dict[str, dict[str, tuple[type, Callable]]],
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Execute a grouped query: ``{ Entity { method(args) { fields } } }``.

        Two-phase execution, mirroring the grouped schema. Each top-level field
        is an entity group; the method fields live one level deeper. Dispatch is
        therefore two-level: group → method. Results nest as
        ``data[entity_name][method_name]``.
        """
        logger.info("Starting custom query execution with concurrent optimization")

        # 1. Parse query
        parsed = self.parser.parse(query)
        # Entity-first projects DTOs by field name — aliases are rejected
        # (compose supports method-level aliases; see query_parser gates).
        reject_all_aliases(parsed.field_tree)
        logger.debug(f"Query parsed: {len(parsed.field_tree)} root groups found")

        # 2. Initialize results
        errors = []
        data = {}

        # ===== Phase 1: Serial Preparation (Metadata Only) =====
        logger.info("[Phase 1] Starting serial preparation phase (metadata only)")

        # Each task: (entity_name, method_name, return_entity, method, method_selection, response_model)
        execution_tasks = []
        group_suffix = "Query"

        for entity_name, group_sel in parsed.field_tree.items():
            method_group = query_map.get(entity_name)

            # Unknown entity group.
            if method_group is None:
                errors.append({
                    "message": f"Cannot query field '{entity_name}' on type '{group_suffix}'",
                    "extensions": {"code": "UNKNOWN_QUERY"},
                    "path": [entity_name],
                })
                logger.warning(f"[Phase 1] Unknown query group: {entity_name}")
                continue

            # Bare group: `{ Entity }` with no method subselection.
            if group_sel.sub_fields is None:
                errors.append(self._bare_group_field_error(entity_name, method_group, group_suffix))
                continue

            for method_name, method_sel in group_sel.sub_fields.items():
                try:
                    method_info = method_group.get(method_name)
                    if method_info is None:
                        errors.append({
                            "message": (
                                f"Cannot query field '{method_name}' on type "
                                f"'{group_type_name(entity_name, group_suffix)}'"
                            ),
                            "extensions": {"code": "UNKNOWN_QUERY"},
                            "path": [entity_name, method_name],
                        })
                        logger.warning(f"[Phase 1] Unknown query method: {entity_name}.{method_name}")
                        continue

                    return_entity, query_method = method_info

                    # Build response model (no I/O, just type construction)
                    response_model = self.builder.build_response_model(
                        entity=return_entity,
                        field_selection=method_sel
                    )
                    logger.debug(f"[Phase 1] Response model built: {entity_name}.{method_name}")

                    execution_tasks.append((
                        entity_name,
                        method_name,
                        return_entity,
                        query_method,
                        method_sel,
                        response_model,
                    ))

                except GraphQLError as e:
                    err = e.to_dict()
                    err.setdefault("path", [entity_name, method_name])
                    errors.append(err)
                except Exception as e:
                    logger.exception(f"Unexpected error in Phase 1 for {entity_name}.{method_name}")
                    errors.append({
                        "message": str(e),
                        "extensions": {"code": type(e).__name__},
                        "path": [entity_name, method_name],
                    })

        logger.info(f"[Phase 1] Completed: {len(execution_tasks)} queries prepared, {len(errors)} errors")

        # ===== Phase 2: Concurrent Execution (query_method + transform + resolve) =====
        logger.info("[Phase 2] Starting concurrent execution phase")

        if execution_tasks:
            # Execute all (query_method + transform + resolve) concurrently
            execution_map = await self._execute_concurrent_queries(execution_tasks, context=context)

            # Collect results and errors, nested per entity group.
            for (entity_name, method_name), (result_data, error_dict) in execution_map.items():
                if error_dict:
                    errors.append(error_dict)
                else:
                    data.setdefault(entity_name, {})[method_name] = result_data

        logger.info(f"[Phase 2] Completed: {len(data)} query groups resolved successfully")

        # 3. Format response
        response = {
            "data": data if data else None,
            "errors": errors if errors else None
        }

        logger.info(f"Query execution complete: {len(data) if data else 0} groups, {len(errors) if errors else 0} errors")
        return response

    async def execute_mutation(
        self,
        query: str,
        mutation_map: dict[str, dict[str, tuple[type, Callable]]],
        context: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Execute a grouped mutation: ``mutation { Entity { method(args) { fields } } }``.

        Serial two-level dispatch (group → method), same transform/resolve
        pipeline as before. Results nest as ``data[entity_name][method_name]``.
        """
        logger.info("Starting custom mutation execution")

        # 1. Parse mutation
        parsed = self.parser.parse(query)
        # Entity-first projects DTOs by field name — aliases are rejected
        # (compose supports method-level aliases; see query_parser gates).
        reject_all_aliases(parsed.field_tree)
        logger.debug(f"Mutation parsed: {len(parsed.field_tree)} root groups found")

        # 2. Initialize results
        errors = []
        data = {}

        # ===== Phase 1 + Phase 2: Serial execution of each mutation =====
        logger.info("Starting serial mutation execution with two-phase resolution")
        group_suffix = "Mutation"

        for entity_name, group_sel in parsed.field_tree.items():
            method_group = mutation_map.get(entity_name)

            if method_group is None:
                errors.append({
                    "message": f"Cannot query field '{entity_name}' on type '{group_suffix}'",
                    "extensions": {"code": "UNKNOWN_MUTATION"},
                    "path": [entity_name],
                })
                logger.warning(f"Unknown mutation group: {entity_name}")
                continue

            if group_sel.sub_fields is None:
                errors.append(self._bare_group_field_error(entity_name, method_group, group_suffix))
                continue

            for method_name, method_sel in group_sel.sub_fields.items():
                try:
                    method_info = method_group.get(method_name)
                    if method_info is None:
                        errors.append({
                            "message": (
                                f"Cannot query field '{method_name}' on type "
                                f"'{group_type_name(entity_name, group_suffix)}'"
                            ),
                            "extensions": {"code": "UNKNOWN_MUTATION"},
                            "path": [entity_name, method_name],
                        })
                        logger.warning(f"Unknown mutation method: {entity_name}.{method_name}")
                        continue

                    return_entity, mutation_method = method_info
                    label = f"{entity_name}.{method_name}"

                    # === Phase 1: Execute mutation method ===
                    args = method_sel.arguments or {}
                    root_data = await self._execute_method(mutation_method, args, "mutation", return_entity, context=context)
                    logger.debug(f"Mutation method executed: {label}")

                    # === Phase 1: Build response model ===
                    response_model = self.builder.build_response_model(
                        entity=return_entity,
                        field_selection=method_sel
                    )
                    logger.debug(f"Response model built: {label}")

                    # === Phase 1: Transform to response model ===
                    if isinstance(root_data, list):
                        typed_data = [
                            response_model.model_validate(
                                d.model_dump() if hasattr(d, 'model_dump') else d
                            )
                            for d in root_data
                        ]
                    elif root_data is not None:
                        typed_data = response_model.model_validate(
                            root_data.model_dump() if hasattr(root_data, 'model_dump') else root_data
                        )
                    else:
                        typed_data = None

                    logger.debug(f"Data transformed: {label}")

                    # Inject pagination args into model instances
                    if typed_data is not None:
                        instances = typed_data if isinstance(typed_data, list) else [typed_data]
                        self.builder.inject_pagination_args(
                            instances=instances,
                            entity=return_entity,
                            field_selection=method_sel,
                        )

                    # === Phase 2: Resolve related data ===
                    if typed_data is not None:
                        resolver = self.resolver_class(
                            enable_from_attribute_in_type_adapter=self.enable_from_attribute_in_type_adapter,
                            context=context,
                            resolved_hooks=self.resolved_hooks,
                        )

                        if isinstance(typed_data, list):
                            resolved = await resolver.resolve(typed_data)
                            result = [
                                r.model_dump(mode='json', by_alias=False)
                                for r in resolved
                            ] if resolved else []
                        else:
                            resolved = await resolver.resolve(typed_data)
                            result = (
                                resolved.model_dump(mode='json', by_alias=False)
                            ) if resolved else None
                    else:
                        result = None

                    data.setdefault(entity_name, {})[method_name] = result
                    logger.debug(f"Mutation resolved: {label}")

                except GraphQLError as e:
                    err = e.to_dict()
                    err.setdefault("path", [entity_name, method_name])
                    errors.append(err)
                except Exception as e:
                    logger.exception(f"Unexpected error executing {entity_name}.{method_name}")
                    errors.append({
                        "message": str(e),
                        "extensions": {"code": type(e).__name__},
                        "path": [entity_name, method_name],
                    })

        logger.info(f"Mutation execution complete: {len(data) if data else 0} groups, {len(errors) if errors else 0} errors")

        # 3. Format response
        response = {
            "data": data if data else None,
            "errors": errors if errors else None
        }

        return response

    @staticmethod
    def _bare_group_field_error(
        entity_name: str,
        method_group: dict[str, tuple[type, Callable]],
        group_suffix: str,
    ) -> dict[str, Any]:
        """Build the friendly error for selecting a bare entity group field.

        Fires when a query selects ``{ Entity }`` with no method subselection.
        ``{ Entity {} }`` (empty braces) is rejected by graphql-core's parser
        before reaching the executor, so only the truly-bare case is caught.

        The example is built from the first method's actual signature, so it
        never fabricates arguments the method does not accept.
        """
        method_names = sorted(method_group)
        first = method_names[0] if method_names else "method_name"

        # Derive an honest argument list from the first method's signature.
        example_args = ""
        method_info = method_group.get(first) if method_names else None
        if method_info is not None:
            try:
                sig = inspect.signature(method_info[1])
                params = [
                    name for name, p in sig.parameters.items()
                    if name not in ("cls", "self", "_context")
                    and p.kind not in (inspect.Parameter.VAR_POSITIONAL,
                                       inspect.Parameter.VAR_KEYWORD)
                ]
                if params:
                    example_args = f"({params[0]}: 1)"
            except (TypeError, ValueError):
                pass

        example = f"{{ {entity_name} {{ {first}{example_args} {{ id }} }} }}"
        return {
            "message": (
                f"Field '{entity_name}' is a grouping field that requires a "
                f"method subselection. Available methods on '{entity_name}': "
                f"{', '.join(method_names)}.\n"
                f"Example: {example}"
            ),
            "path": [entity_name],
            "extensions": {
                "code": "BARE_GROUP_FIELD",
                "entity": entity_name,
                "available_methods": method_names,
            },
        }

    async def _execute_method(
        self,
        method: Callable,
        arguments: dict[str, Any],
        operation_type: str = "query",
        entity: Optional[type] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> Any:
        """
        Execute @query or @mutation method

        Args:
            method: @query/@mutation decorated method (can be classmethod or regular function)
            arguments: Parameter dictionary
            operation_type: "query" or "mutation" for logging

        Returns:
            Method result
        """
        logger.debug(f"Executing {operation_type} method with arguments: {arguments}")
        try:
            # Convert arguments (handle BaseModel types)
            converted_args = self._convert_arguments(method, arguments)

            # Inject _context parameter if method declares it
            if context is not None:
                sig = inspect.signature(method)
                if '_context' in sig.parameters:
                    converted_args['_context'] = context

            # staticmethod object: call underlying function directly without cls
            if isinstance(method, staticmethod):
                return await method.__func__(**converted_args)

            # QueryConfig/MutationConfig wrappers are bound as classmethod and always expect cls.
            if hasattr(method, const.GRAPHQL_CONFIG_BOUND_ATTR):
                return await method(entity, **converted_args)

            # Regular @query/@mutation methods generally declare cls/self explicitly.
            sig = inspect.signature(method)
            params = tuple(sig.parameters.keys())
            if params and params[0] in ("self", "cls"):
                return await method(entity, **converted_args)

            # Fallback for callables that don't need cls.
            return await method(**converted_args)
        except Exception as e:
            logger.error(f"{operation_type.capitalize()} method execution failed: {e}")
            raise GraphQLError(
                f"{operation_type.capitalize()} execution failed: {e}",
                extensions={"code": "EXECUTION_ERROR"}
            )

    def _convert_arguments(
        self,
        method: Callable,
        arguments: dict[str, Any]
    ) -> dict[str, Any]:
        """
        Convert method parameters, transforming dict to corresponding Pydantic BaseModel instances
        and enum names to enum values.

        Args:
            method: Method object
            arguments: Original parameter dictionary

        Returns:
            Converted parameter dictionary
        """
        converted = {}
        try:
            sig = inspect.signature(method)
            # Resolve string annotations (PEP 563 ``from __future__ import
            # annotations``) — a raw ``param.annotation`` would be a string
            # and every isinstance/issubclass check below would silently
            # skip, passing raw wire values into the method body.
            resolved_hints = _resolve_function_type_hints(method)
            for param_name, param in sig.parameters.items():
                if param_name in ('self', 'cls'):
                    continue

                # _context is framework-injected, not from GraphQL query
                if param_name == '_context':
                    continue

                if param_name not in arguments:
                    continue

                value = arguments[param_name]
                param_type = resolved_hints.get(param_name, param.annotation)

                # If parameter type is annotated
                if param_type != inspect.Parameter.empty:
                    core_types = get_core_types(param_type)
                    converted_value = None

                    for core_type in core_types:
                        # Handle Enum types - convert string name to enum member
                        if safe_issubclass(core_type, Enum):
                            if isinstance(value, str):
                                # GraphQL wire value is the member name; fall
                                # back to the member value so both conventions
                                # work, and fail loudly instead of letting a
                                # raw string flow into the method body.
                                # e.g., "USER" -> UserRole.USER
                                try:
                                    converted_value = core_type[value]
                                except KeyError:
                                    try:
                                        converted_value = core_type(value)
                                    except ValueError as ve:
                                        raise EnumWireError(core_type, value) from ve
                                break
                            elif isinstance(value, Enum):
                                converted_value = value
                                break
                        # Handle BaseModel types
                        elif safe_issubclass(core_type, BaseModel):
                            if isinstance(value, dict):
                                converted_value = self._convert_to_model(value, core_type)
                                break
                            elif isinstance(value, list):
                                list_element_type = self._extract_list_element_type(param_type)
                                if list_element_type and safe_issubclass(list_element_type, BaseModel):
                                    converted_value = [
                                        self._convert_to_model(item, list_element_type) if isinstance(item, dict) else item
                                        for item in value
                                    ]
                                break
                        # Handle temporal/UUID scalar params (incl list[T], Optional[T]).
                        # GraphQL sends these as strings; coerce to the real Python
                        # object so e.g. SQLAlchemy UUID/datetime columns don't crash.
                        elif _is_coercible_scalar(core_type):
                            converted_value = self._convert_scalar_value(value, param_type)
                            break

                    if converted_value is not None:
                        converted[param_name] = converted_value
                    else:
                        converted[param_name] = value
                else:
                    converted[param_name] = value

        except EnumWireError:
            # Invalid enum wire values must not be silently downgraded to the
            # raw string (the old blanket catch did exactly that).
            raise
        except Exception as e:
            logger.warning(f"Failed to convert arguments: {e}")
            return arguments

        return converted

    def _convert_scalar_value(self, value: Any, target_type: Any) -> Any:
        """Coerce a GraphQL wire value (string) to a temporal/UUID scalar.

        Recurses into ``list[T]`` and unwraps ``Optional[T]`` / ``Union[T, None]``
        so ``list[UUID]``, ``Optional[datetime]``, and ``list[Optional[date]]``
        all convert element-wise. Non-string leaves are returned unchanged.
        """
        # Unwrap Optional[T] / Union[T, None] -> T
        if get_origin(target_type) is Union:
            non_none = [a for a in get_args(target_type) if a is not type(None)]
            if len(non_none) == 1:
                target_type = non_none[0]

        if get_origin(target_type) is list:
            (elem_type,) = get_args(target_type) or (None,)
            if elem_type is None:
                return value
            return [self._convert_scalar_value(item, elem_type) for item in value]

        return self._coerce_scalar(value, target_type)

    @staticmethod
    def _coerce_scalar(value: Any, py_type: Any) -> Any:
        """Coerce a single string wire value to the target scalar, or return it."""
        if not isinstance(value, str):
            return value
        try:
            # datetime subclasses date -> check datetime first
            if safe_issubclass(py_type, UUID):
                return UUID(value)
            if safe_issubclass(py_type, datetime):
                # datetime.fromisoformat (pre-3.11) rejects trailing 'Z'
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            if safe_issubclass(py_type, time):
                return time.fromisoformat(value)
            if safe_issubclass(py_type, date):
                return date.fromisoformat(value)
        except (ValueError, TypeError):
            # Leave as-is; let the method body / validation surface the error.
            return value
        return value

    def _convert_to_model(self, data: dict, model_class: type) -> BaseModel:
        """
        Recursively convert dict to Pydantic BaseModel instance

        Args:
            data: Dictionary data
            model_class: Target BaseModel class

        Returns:
            BaseModel instance
        """
        # Get all field types of the model
        try:
            type_hints = model_class.__annotations__
        except Exception:
            type_hints = {}

        converted_data = {}
        for field_name, field_value in data.items():
            if field_name in type_hints:
                field_type = type_hints[field_name]
                core_types = get_core_types(field_type)

                # Check if field type is BaseModel
                is_model_field = False
                for core_type in core_types:
                    if safe_issubclass(core_type, BaseModel):
                        if isinstance(field_value, dict):
                            converted_data[field_name] = self._convert_to_model(field_value, core_type)
                        elif isinstance(field_value, list):
                            converted_data[field_name] = [
                                self._convert_to_model(item, core_type) if isinstance(item, dict) else item
                                for item in field_value
                            ]
                        else:
                            converted_data[field_name] = field_value
                        is_model_field = True
                        break

                if not is_model_field:
                    converted_data[field_name] = field_value
            else:
                converted_data[field_name] = field_value

        return model_class(**converted_data)

    def _extract_list_element_type(self, field_type: Any) -> Optional[type]:
        """
        Extract element type T from list[T]

        Args:
            field_type: Field type (possibly list[T])

        Returns:
            Element type, or None if not a list
        """
        from typing import get_args, get_origin

        origin = get_origin(field_type)
        if origin is list:
            args = get_args(field_type)
            if args:
                return args[0]
        return None

    async def _execute_concurrent_queries(
        self,
        execution_tasks: list[tuple[str, str, type, Callable, Any, type]],
        context: Optional[dict[str, Any]] = None,
    ) -> dict[tuple[str, str], tuple[Optional[Any], Optional[dict]]]:
        """
        Execute multiple queries concurrently (query_method + transform + resolve).

        Args:
            execution_tasks: List of (entity_name, method_name, return_entity,
                method, method_selection, response_model) tuples.

        Returns:
            Dict mapping (entity_name, method_name) to (result_data, error_dict).
        """
        if not execution_tasks:
            return {}

        logger.info(f"[Phase 2] Starting concurrent execution of {len(execution_tasks)} queries")

        # Resource control: Only limit concurrent executions if user explicitly sets environment variable
        max_concurrency_str = os.getenv("PYDANTIC_RESOLVE_MAX_CONCURRENT_QUERIES")
        if max_concurrency_str:
            max_concurrency = int(max_concurrency_str)
            semaphore = asyncio.Semaphore(max_concurrency) if max_concurrency > 0 else None
        else:
            semaphore = None

        async def execute_with_semaphore(task):
            entity_name, method_name, return_entity, query_method, field_selection, response_model = task
            if semaphore:
                async with semaphore:
                    return await self._execute_single_query(
                        entity_name, method_name, return_entity, query_method,
                        field_selection, response_model, context=context,
                    )
            else:
                return await self._execute_single_query(
                    entity_name, method_name, return_entity, query_method,
                    field_selection, response_model, context=context,
                )

        # Execute all (query_method + transform + resolve) tasks concurrently
        results = await asyncio.gather(
            *[execute_with_semaphore(task) for task in execution_tasks],
            return_exceptions=True
        )

        # Process results and map to (entity_name, method_name) keys
        keys = [(entity_name, method_name) for entity_name, method_name, _, _, _, _ in execution_tasks]
        execution_map = {}

        for key, result in zip(keys, results):
            if isinstance(result, Exception):
                logger.exception(f"[Phase 2] Unexpected exception for {'.'.join(key)}")
                error_dict = {
                    "message": f"Unexpected error: {str(result)}",
                    "extensions": {"code": type(result).__name__},
                    "path": list(key),
                }
                execution_map[key] = (None, error_dict)
            else:
                execution_map[key] = result

        logger.info("[Phase 2] Completed concurrent execution")
        return execution_map

    async def _execute_single_query(
        self,
        entity_name: str,
        method_name: str,
        entity: type,
        query_method: Callable,
        field_selection: Any,
        response_model: type,
        context: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[Any], Optional[dict]]:
        """
        Execute a single query: query_method -> transform -> resolve.

        This method combines all three steps that were previously split across
        _prepare_query_resolution and _resolve_query_data.

        Args:
            entity_name: Entity group name (for logging + error path).
            method_name: Method field name (for logging + error path).
            query_method: @query decorated method
            field_selection: Parsed field selection with arguments
            response_model: Pre-built response model class

        Returns:
            Tuple of (result_data, error_dict)
            - Success: (result_data, None)
            - Failure: (None, error_dict)
        """
        query_name = f"{entity_name}.{method_name}"
        path = [entity_name, method_name]
        logger.debug(f"[Phase 2] Executing query: {query_name}")

        try:
            # 1. Execute query method (I/O operation)
            args = field_selection.arguments or {}
            root_data = await self._execute_method(query_method, args, "query", entity, context=context)
            logger.debug(f"[Phase 2] Query method executed: {query_name}")

            # 2. Transform to response model
            if isinstance(root_data, list):
                typed_data = [
                    response_model.model_validate(
                        d.model_dump() if hasattr(d, 'model_dump') else d
                    )
                    for d in root_data
                ]
                is_list = True
            elif root_data is not None:
                typed_data = response_model.model_validate(
                    root_data.model_dump() if hasattr(root_data, 'model_dump') else root_data
                )
                is_list = False
            else:
                typed_data = None
                is_list = False

            logger.debug(f"[Phase 2] Data transformed: {query_name}")

            # 2.5. Inject pagination args into model instances
            #      (must happen after model_validate, before resolver.resolve,
            #       because the response model is cached and cannot hold
            #       query-specific PageArgs in its field defaults)
            if typed_data is not None:
                instances = typed_data if isinstance(typed_data, list) else [typed_data]
                self.builder.inject_pagination_args(
                    instances=instances,
                    entity=entity,
                    field_selection=field_selection,
                )

            # 3. Resolve related data
            result_data = None
            if typed_data is not None:
                resolver = self.resolver_class(
                    enable_from_attribute_in_type_adapter=self.enable_from_attribute_in_type_adapter,
                    context=context,
                    resolved_hooks=self.resolved_hooks,
                )

                if is_list:
                    result = await resolver.resolve(typed_data)
                    if result is not None:
                        result_data = [
                            r.model_dump(mode='json', by_alias=False)
                            for r in result
                        ]
                    else:
                        result_data = []
                else:
                    result = await resolver.resolve(typed_data)
                    if result is not None:
                        result_data = result.model_dump(mode='json', by_alias=False)
                    else:
                        result_data = None
            else:
                result_data = [] if is_list else None

            logger.debug(f"[Phase 2] Query resolved: {query_name}")
            return result_data, None

        except GraphQLError as e:
            logger.warning(f"[Phase 2] GraphQL error for {query_name}: {e.message}")
            err = e.to_dict()
            err.setdefault("path", path)
            return None, err
        except Exception as e:
            logger.exception(f"[Phase 2] Error executing {query_name}")
            error_dict = {
                "message": f"Execution failed for {query_name}: {str(e)}",
                "extensions": {"code": type(e).__name__},
                "path": path,
            }
            return None, error_dict
