"""UseCase compose — GraphQL-style multi-method composition entry point.

Accepts a single GraphQL query with a fixed 3-level hierarchy::

    {
      ServiceA {
        methodX(arg: 1) { fieldA fieldB }
        methodY { fieldC }
      }
      ServiceB {
        methodZ { fieldD }
      }
    }

Each (service, method) pair is invoked concurrently. Whether/how to run
``Resolver`` on the returned DTOs (``resolve_*`` / ``AutoLoad``) is the
business method's responsibility — compose does not re-resolve. Compose
only applies the per-method field selection (the third level) via
``build_subset_model`` before serialization.

This module is intentionally self-contained: it reuses public utilities
(``QueryParser``, ``build_subset_model``) but does not modify
``mcp_server.py`` internals. The MCP tool ``compose_query`` in
``mcp_server.py`` is a thin wrapper around
:meth:`UseCaseResources.compose`, which in turn calls the private
:func:`_compose_and_resolve` defined here.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import types
from dataclasses import dataclass
from typing import Any, Union, get_args, get_origin

from pydantic import BaseModel, TypeAdapter

from pydantic_resolve.graphql.exceptions import QueryParseError
from pydantic_resolve.graphql.query_parser import (
    QueryParser,
    find_nested_alias,
    nested_alias_message,
)
from pydantic_resolve.graphql.types import FieldSelection, ParsedQuery
from pydantic_resolve.use_case.business import USE_CASE_METHODS_ATTR
from pydantic_resolve.use_case.context import is_from_context_annotation
from pydantic_resolve.use_case.selection import (
    SelectionError,
    _get_pydantic_core_type,
    _replace_model_type,
    build_subset_model,
)
from pydantic_resolve.utils.types import _resolve_function_type_hints, get_return_annotation


class ComposeError(ValueError):
    """Raised for any compose-time validation or execution failure.

    ``error_type`` matches a :class:`MCPErrors` enum **value** (lowercase
    string, e.g. ``"validation_error"``, ``"type_not_found"``) so the MCP
    tool layer can pick the right code without re-parsing the message.
    """

    def __init__(self, message: str, error_type: str = "validation_error"):
        super().__init__(message)
        self.error_type = error_type


@dataclass
class ServiceExecutionPlan:
    service_name: str
    method_name: str
    service_cls: type
    method: Any
    func: Any
    method_meta: dict
    method_selection: FieldSelection
    return_anno: Any
    # Response keys (alias when present, otherwise the field name): the
    # response dict is keyed the way the client asked, while service/method
    # resolution uses the original names above.
    service_key: str = ""
    method_key: str = ""
    # Projection target prepared at plan time: the return annotation with
    # its DTO core swapped for the selection's subset model. ``None`` when
    # there is nothing to project (scalar/unannotated return). Prepared by
    # ``_prepare_projection`` BEFORE any method executes so selection
    # errors fail fast with no side effects.
    projected_anno: Any = None

    def __post_init__(self) -> None:
        if not self.service_key:
            self.service_key = self.service_name
        if not self.method_key:
            self.method_key = self.method_name


async def _compose_and_resolve(
    app: Any,
    query: str,
    context: dict[str, Any] | None = None,
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Parse, validate, execute, and project a compose query.

    Args:
        app: :class:`UseCaseResources` instance (from ``UseCaseManager.get_app``).
        query: GraphQL data query string. Fixed 3-level hierarchy:
            root → service → method → DTO field selection. Introspection
            queries (``__schema`` / ``__type`` / ``__typename``) are NOT
            handled here — callers must dispatch to
            :func:`pydantic_resolve.use_case.introspection.compose_introspect`
            themselves (typically by checking
            :func:`pydantic_resolve.use_case.introspection.is_introspection_query`
            first).
        context: Request-scoped context dict. Flows into method params
            annotated with ``FromContext``.
        variables: Values for variables declared in the query
            (``query ($id: Int!) ...``). Every declared variable must be
            provided explicitly — declared defaults are never applied.

    Returns:
        ``{"data": {service: {responseKey: result}}, "errors": [...]}``.
        ``responseKey`` is the alias when present, otherwise the method
        name. A failed invocation nulls only its own response key and
        appends an entry to ``errors`` with ``path`` and
        ``extensions.code`` (``QUERY_FAILED`` / ``MUTATION_FAILED`` /
        ``SKIPPED_PRIOR_FAILURE`` / ``PROJECTION_FAILED``).

    Raises:
        ComposeError: For validation failures only — parse errors, unknown
            services/methods, duplicate response keys, missing variables,
            invalid field selections. All of these are detected BEFORE any
            method executes, so a raising query never has side effects.
            Execution failures do NOT raise; they surface in ``errors``
            with partial data. An introspection query passed here will
            surface as ``type_not_found`` (``__schema`` etc. are not
            services).

    Note:
        Compose does NOT run ``Resolver`` on the returned DTOs. Business
        methods are responsible for resolving their own outputs (firing
        ``resolve_*`` / ``AutoLoad``) before returning. Compose only
        applies the per-method field selection.
    """
    parsed = _parse_query(query, variables)
    if not parsed.field_tree:
        raise ComposeError("Query is empty", "validation_error")

    plans = _build_plans(app, parsed)

    plan_to_result, errors = await _execute_plans(app, plans, context)

    # Response keyed by response keys (alias when present) so clients can
    # locate values in the shape they asked for.
    output: dict[str, Any] = {}
    for plan in plans:
        svc_dict = output.setdefault(plan.service_key, {})
        result = plan_to_result[id(plan)]
        if result is _ERRORED:
            svc_dict[plan.method_key] = None
            continue
        try:
            svc_dict[plan.method_key] = _project_one(result, plan)
        except Exception as e:  # noqa: BLE001 — a result that fails
            # projection (e.g. a method returning data that does not fit
            # its own return annotation) nulls only its own response key;
            # already-executed results, including committed mutations, are
            # never erased from the response.
            svc_dict[plan.method_key] = None
            errors.append(
                {
                    "message": (
                        str(e)
                        if isinstance(e, ComposeError)
                        else f"{type(e).__name__}: {e}"
                    ),
                    "path": [plan.service_key, plan.method_key],
                    "extensions": {
                        "code": "PROJECTION_FAILED",
                        "service_method": f"{plan.service_name}.{plan.method_name}",
                    },
                }
            )
    return {"data": output, "errors": errors}


# Sentinel marking a plan whose execution failed (its response key is
# nulled and an errors entry was recorded).
_ERRORED = object()


# ============================================================================
# Parsing & validation
# ============================================================================


def _parse_query(query: str, variables: dict[str, Any] | None = None) -> ParsedQuery:
    if not query or not query.strip():
        raise ComposeError("Query is empty", "validation_error")
    # One operation per request: compose has no operationName channel (a bare
    # query string), so a multi-operation document must be rejected loudly —
    # previously the parser silently executed only the first operation and
    # dropped the rest. Ported from nexusx #142.
    from graphql import OperationDefinitionNode, parse as gql_parse

    try:
        document = gql_parse(query)
    except Exception:
        # Syntax errors surface with the proper QueryParseError → ComposeError
        # conversion from QueryParser below.
        document = None
    if document is not None:
        operations = [
            d for d in document.definitions if isinstance(d, OperationDefinitionNode)
        ]
        if len(operations) > 1:
            raise ComposeError(
                "Document contains multiple operations; compose accepts exactly "
                "one operation per request. Split the document or send the "
                "operations one at a time.",
                "validation_error",
            )
        # Variables contract check — friendly failure before any execution.
        # Every declared variable must be provided explicitly: declared
        # defaults ($t: String = "x") are NOT auto-applied.
        declared: list[str] = []
        defaulted: set[str] = set()
        for operation in operations:
            for vd in operation.variable_definitions or []:
                declared.append(vd.variable.name.value)
                if vd.default_value is not None:
                    defaulted.add(vd.variable.name.value)
            break
        if declared:
            missing = [
                name for name in declared if variables is None or name not in variables
            ]
            if missing:
                message = (
                    f"Query declares variables {declared} but {len(missing)} were "
                    f"not provided ({missing}). Pass them via the 'variables' "
                    "argument (recommended for any string containing quotes, "
                    "backslashes or newlines)."
                )
                if defaulted & set(missing):
                    message += (
                        " Note: declared default values are never auto-applied —"
                        " every declared variable must be passed explicitly."
                    )
                raise ComposeError(message, "validation_error")
    try:
        return QueryParser().parse(query, variables)
    except QueryParseError as e:
        raise ComposeError(str(e), "validation_error") from e


def _build_plans(app: Any, parsed: ParsedQuery) -> list[ServiceExecutionPlan]:
    plans: list[ServiceExecutionPlan] = []
    for service_key, service_selection in parsed.field_tree.items():
        # Resolve the service by its original name (alias when present);
        # the response is keyed by ``service_key``.
        service_name = service_selection.name or service_key
        service_cls = _resolve_service(app, service_name)
        _reject_arguments(service_selection, f"Service '{service_name}'")

        if not service_selection.sub_fields:
            raise ComposeError(
                f"Service '{service_name}' has no methods selected",
                "validation_error",
            )

        for method_key, method_selection in service_selection.sub_fields.items():
            method_name = method_selection.name or method_key
            # Only method-level aliases are supported — a nested alias would
            # mis-project the DTO (projection walks by field name).
            nested = find_nested_alias(method_selection)
            if nested is not None:
                raise ComposeError(
                    nested_alias_message(*nested),
                    "validation_error",
                )
            method_meta = _resolve_method(service_cls, method_name, service_name)
            _check_mutation_permission(app, method_meta, service_name, method_name)

            method = getattr(service_cls, method_name)
            func = getattr(method, "__func__", method)
            return_anno = get_return_annotation(method)

            plans.append(ServiceExecutionPlan(
                service_name=service_name,
                method_name=method_name,
                service_cls=service_cls,
                method=method,
                func=func,
                method_meta=method_meta,
                method_selection=method_selection,
                return_anno=return_anno,
                service_key=service_key,
                method_key=method_key,
            ))
    # Selection validation happens at plan time, BEFORE execution: unknown
    # fields, missing selections, and DTO-leaf arguments fail fast with no
    # side effects. Anything that can still fail afterwards (runtime data
    # not fitting the prepared projection) nulls only its own response key.
    for plan in plans:
        _prepare_projection(plan)
    return plans


def _resolve_service(app: Any, service_name: str) -> type:
    services = app.services
    if service_name not in services:
        available = list(services.keys())
        raise ComposeError(
            f"Service '{service_name}' not found in app '{app.name}'. "
            f"Available services: {available}",
            "type_not_found",
        )
    return services[service_name]


def _resolve_method(service_cls: type, method_name: str, service_name: str) -> dict:
    methods = getattr(service_cls, USE_CASE_METHODS_ATTR, {})
    if method_name not in methods:
        available = list(methods.keys())
        raise ComposeError(
            f"Method '{method_name}' not found in service '{service_name}'. "
            f"Available methods: {available}",
            "operation_not_found",
        )
    return methods[method_name]


def _check_mutation_permission(
    app: Any, method_meta: dict, service_name: str, method_name: str
) -> None:
    if not app.enable_mutation and method_meta.get("kind") == "mutation":
        raise ComposeError(
            f"Method '{method_name}' is a mutation and mutations are disabled "
            f"for app '{app.name}'.",
            "operation_not_found",
        )


def _reject_arguments(selection: FieldSelection, location: str) -> None:
    if selection.arguments:
        raise ComposeError(
            f"Arguments are not allowed on {location}.",
            "validation_error",
        )


# ============================================================================
# Execution
# ============================================================================


async def _exec_method(
    app: Any, plan: ServiceExecutionPlan, context: dict[str, Any] | None
) -> Any:
    kwargs = _prepare_method_kwargs(plan, context)
    try:
        return await plan.method(**kwargs)
    except ComposeError:
        raise
    except Exception as e:
        kind = plan.method_meta.get("kind", "query")
        err_type = (
            "mutation_execution_error" if kind == "mutation"
            else "query_execution_error"
        )
        raise ComposeError(
            f"Error executing {plan.service_name}.{plan.method_name}: {e}",
            err_type,
        ) from e


async def _execute_plans(
    app: Any,
    plans: list[ServiceExecutionPlan],
    context: dict[str, Any] | None,
) -> tuple[dict[int, Any], list[dict[str, Any]]]:
    """Run plans with GraphQL-compliant execution semantics.

    - ``@query`` methods run concurrently via ``asyncio.gather``.
    - ``@mutation`` methods run serially in declaration order.

    The relative ordering between the query batch and the mutation batch
    is NOT guaranteed. If you need create-then-read semantics, issue the
    mutation and the read as separate compose calls.

    Args:
        app: UseCaseResources.
        plans: All ServiceExecutionPlans for this query.
        context: Request context (flows into FromContext params).

    Returns:
        ``({id(plan): result}, errors)`` — each plan in ``plans`` is
        guaranteed to have an entry; failed plans map to ``_ERRORED`` and
        carry an errors entry (a failed query nulls only its own response
        key). Cancellation is re-raised, never converted to a field error.

    Mutation semantics (three-state, fail-stop): a succeeded call keeps
    its result; a failed call nulls only its own key with
    ``MUTATION_FAILED``; every later mutation in the request is skipped
    with ``SKIPPED_PRIOR_FAILURE`` — already-executed writes are never
    erased from the response.
    """
    query_plans = [p for p in plans if p.method_meta.get("kind") != "mutation"]
    mutation_plans = [p for p in plans if p.method_meta.get("kind") == "mutation"]

    errors: list[dict[str, Any]] = []
    plan_to_result: dict[int, Any] = {}

    # Queries: concurrent, per-field isolation — one failing invocation
    # nulls only its own response key.
    if query_plans:
        query_results = await asyncio.gather(
            *[_exec_method(app, p, context) for p in query_plans],
            return_exceptions=True,
        )
        for plan, value in zip(query_plans, query_results):
            if isinstance(value, BaseException) and not isinstance(value, Exception):
                # Cancellation / KeyboardInterrupt — not a field failure.
                raise value
            if isinstance(value, Exception):
                plan_to_result[id(plan)] = _ERRORED
                errors.append(
                    {
                        "message": (
                            str(value)
                            if isinstance(value, ComposeError)
                            else f"{type(value).__name__}: {value}"
                        ),
                        "path": [plan.service_key, plan.method_key],
                        "extensions": {
                            "code": "QUERY_FAILED",
                            "service_method": f"{plan.service_name}.{plan.method_name}",
                        },
                    }
                )
            else:
                plan_to_result[id(plan)] = value

    # Mutations run sequentially — GraphQL spec requires this so that
    # writes within a single operation are observable in order — with
    # three-state feedback and operation-scope fail-stop.
    mutation_failed = False
    for plan in mutation_plans:
        if mutation_failed:
            plan_to_result[id(plan)] = _ERRORED
            errors.append(
                {
                    "message": (
                        f"Skipped '{plan.method_key}' because a prior mutation failed"
                    ),
                    "path": [plan.service_key, plan.method_key],
                    "extensions": {"code": "SKIPPED_PRIOR_FAILURE"},
                }
            )
            continue
        try:
            plan_to_result[id(plan)] = await _exec_method(app, plan, context)
        except Exception as e:  # noqa: BLE001 — keep three-state shape
            plan_to_result[id(plan)] = _ERRORED
            errors.append(
                {
                    "message": (
                        str(e)
                        if isinstance(e, ComposeError)
                        else f"{type(e).__name__}: {e}"
                    ),
                    "path": [plan.service_key, plan.method_key],
                    "extensions": {
                        "code": "MUTATION_FAILED",
                        "service_method": f"{plan.service_name}.{plan.method_name}",
                    },
                }
            )
            mutation_failed = True

    return plan_to_result, errors


def _prepare_method_kwargs(
    plan: ServiceExecutionPlan, context: dict[str, Any] | None
) -> dict[str, Any]:
    func = plan.func
    raw_args = dict(plan.method_selection.arguments or {})
    from_context_params = _get_from_context_params(func)

    sig = inspect.signature(func)
    hints = _resolve_function_type_hints(func)
    valid_params = {n for n in sig.parameters if n != "cls"}

    # FromContext params are server-injected (auth, tenant, etc.) and must
    # never be settable from query arguments — otherwise a client could
    # impersonate another user via e.g. ``whoami(user_id: 999)``.
    leaked_context_args = raw_args.keys() & from_context_params
    if leaked_context_args:
        raise ComposeError(
            f"Argument(s) {sorted(leaked_context_args)} on "
            f"{plan.service_name}.{plan.method_name} are server-injected "
            f"(FromContext) and cannot be set from the query.",
            "validation_error",
        )

    for arg_name in raw_args:
        if arg_name not in valid_params:
            raise ComposeError(
                f"Unexpected argument '{arg_name}' for method "
                f"'{plan.service_name}.{plan.method_name}'. "
                f"Valid arguments: {sorted(valid_params)}",
                "validation_error",
            )

    kwargs: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        if name == "cls":
            continue
        anno = hints.get(name, param.annotation)

        if name in raw_args:
            kwargs[name] = _coerce_strict(raw_args[name], anno, name, plan)
        elif name in from_context_params:
            if context is not None and name in context:
                # Same pydantic validation as query args — wrong type surfaces
                # as validation_error here, not deep inside the method body.
                kwargs[name] = _coerce_strict(context[name], anno, name, plan)
            elif param.default is inspect.Parameter.empty:
                raise ComposeError(
                    f"Required FromContext parameter '{name}' not found in context "
                    f"for {plan.service_name}.{plan.method_name}",
                    "validation_error",
                )
        elif param.default is inspect.Parameter.empty:
            raise ComposeError(
                f"Missing required argument '{name}' for method "
                f"'{plan.service_name}.{plan.method_name}'.",
                "validation_error",
            )
    return kwargs


def _promote_enum_names(value: Any, annotation: Any) -> Any:
    """Promote GraphQL enum wire names to enum members before validation.

    The compose schema renders enum members by *name* and GraphQL enum
    literals/variables carry names — but Pydantic validates enums by
    *value* (``TypeAdapter(Level).validate_python("HIGH")`` fails when the
    member value is ``"high"``). Walk the annotation shape and swap any wire
    name matching ``Enum.__members__`` for the member instance. Member
    *values* keep flowing through untouched, so both wire conventions work.

    Ported from nexusx 6.3.1 (fix/compose enum wire-name coercion).
    """
    if value is None or annotation is None or annotation is inspect.Parameter.empty:
        return value
    origin = get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:  # Optional[X] — recurse into X
            return _promote_enum_names(value, args[0])
        return value  # genuine unions: leave to Pydantic
    if origin is list and isinstance(value, list):
        args = get_args(annotation)
        if args:
            return [_promote_enum_names(item, args[0]) for item in value]
        return value
    if not isinstance(annotation, type):
        return value
    if issubclass(annotation, enum.Enum):
        if isinstance(value, str) and value in annotation.__members__:
            return annotation[value]
        return value
    if issubclass(annotation, BaseModel) and isinstance(value, dict):
        # INPUT_OBJECT literals arrive as dicts; enum fields follow the same
        # name wire convention, so recurse along the model's field hints.
        return {
            key: (
                _promote_enum_names(item, field.annotation)
                if (field := annotation.model_fields.get(key)) is not None
                else item
            )
            for key, item in value.items()
        }
    return value


def _enum_wire_hint(annotation: Any) -> str:
    """Hint appended to coercion errors for enum-bearing annotations.

    Pydantic's message names the member *value*, which never appears in the
    schema — list the member names so an agent can self-correct.
    """
    leaf = annotation
    for _ in range(8):  # peel Optional/list wrappers; 8 is plenty
        origin = get_origin(leaf)
        if origin is Union or origin is types.UnionType or origin is list:
            args = [arg for arg in get_args(leaf) if arg is not type(None)]
            if not args:
                break
            leaf = args[0]
        else:
            break
    if isinstance(leaf, type) and issubclass(leaf, enum.Enum):
        return (
            f" {leaf.__name__} accepts member names on the GraphQL side "
            f"(one of: {', '.join(leaf.__members__)}) or member values."
        )
    return ""


def _coerce_strict(
    value: Any, annotation: Any, arg_name: str, plan: ServiceExecutionPlan
) -> Any:
    if value is None:
        return None
    if annotation is inspect.Parameter.empty or annotation is None:
        return value
    try:
        promoted = _promote_enum_names(value, annotation)
        return TypeAdapter(annotation).validate_python(promoted)
    except Exception as e:
        raise ComposeError(
            f"Failed to coerce argument '{arg_name}' for method "
            f"'{plan.service_name}.{plan.method_name}': {e}"
            f"{_enum_wire_hint(annotation)}",
            "validation_error",
        ) from e


def _get_from_context_params(method: Any) -> set[str]:
    hints = _resolve_function_type_hints(method)
    sig = inspect.signature(method)
    return {
        name
        for name in sig.parameters
        if name != "cls" and is_from_context_annotation(hints.get(name))
    }


# ============================================================================
# Selection projection
# ============================================================================


def _prepare_projection(plan: ServiceExecutionPlan) -> None:
    """Validate the selection against the return type and cache the
    projection annotation on ``plan.projected_anno``.

    Runs at plan time, before any method executes, so everything that
    depends only on the query shape (arguments on DTO leaves, missing
    field selection, unknown fields) raises :class:`ComposeError` with no
    side effects. Only the genuinely runtime steps — validating the
    method's actual result and serializing it — remain in
    :func:`_project_one`.
    """
    # Method-level arguments (e.g. get_sprint(sprint_id: 1)) are legitimate.
    # Only reject arguments on DTO leaf selections (sub_fields of the method).
    if plan.method_selection.sub_fields:
        for name, sub in plan.method_selection.sub_fields.items():
            _reject_arguments_recursive(sub, plan, f"{plan.method_name}.{name}")

    core_type = (
        _get_pydantic_core_type(plan.return_anno)
        if plan.return_anno is not None
        else None
    )
    if core_type is None:
        return  # scalar/unannotated return: serialized as-is, no projection

    if not plan.method_selection.sub_fields:
        raise ComposeError(
            f"Method '{plan.service_name}.{plan.method_name}' returns an object "
            f"type and requires field selection (e.g. '{{ id name }}').",
            "validation_error",
        )
    try:
        subset_model = build_subset_model(core_type, plan.method_selection)
    except SelectionError as e:
        raise ComposeError(str(e), "validation_error") from e
    plan.projected_anno = _replace_model_type(plan.return_anno, subset_model)


def _project_one(result: Any, plan: ServiceExecutionPlan) -> Any:
    if result is None:
        return None
    if plan.projected_anno is not None:
        projected = TypeAdapter(plan.projected_anno).validate_python(result)
        return _serialize_result(projected)
    return _serialize_result(result)


def _reject_arguments_recursive(
    selection: FieldSelection, plan: ServiceExecutionPlan, path: str
) -> None:
    if selection.arguments:
        raise ComposeError(
            f"Arguments are not allowed on DTO field '{path}' for "
            f"{plan.service_name}.{plan.method_name}.",
            "validation_error",
        )
    if not selection.sub_fields:
        return
    for name, sub in selection.sub_fields.items():
        child_path = f"{path}.{name}" if path else name
        _reject_arguments_recursive(sub, plan, child_path)


def _serialize_result(result: Any) -> Any:
    if result is None:
        return None
    if isinstance(result, BaseModel):
        return result.model_dump(mode="json")
    if isinstance(result, list):
        return [_serialize_result(item) for item in result]
    if isinstance(result, dict):
        return {key: _serialize_result(value) for key, value in result.items()}
    if isinstance(result, (str, int, float, bool)):
        return result
    if hasattr(result, "model_dump"):
        return result.model_dump(mode="json")
    return TypeAdapter(type(result)).dump_python(result, mode="json")
