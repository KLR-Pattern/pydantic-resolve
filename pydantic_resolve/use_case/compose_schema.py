"""Build a ``TypeInfo`` registry describing a UseCase app's compose surface.

The registry mirrors the fixed 3-level compose query hierarchy::

    type Query {
      SprintService: SprintServiceQuery!
      TaskService: TaskServiceQuery!
    }

    type SprintServiceQuery {
      list_sprints: [SprintDTO!]!
      get_sprint(sprint_id: Int!): SprintDTO
    }

Each registered ``UseCaseService`` becomes a ``TypeInfo`` (kind=OBJECT,
``{Service}Query``). Its fields are the ``@query`` / ``@mutation``
methods; method args become ``ArgumentInfo`` s; the return annotation
drives which DTO/Enum types get added to the registry.

The registry is what ``compose_introspect`` walks to produce the
GraphQL introspection JSON (GraphiQL-compatible) and what ``method_sdl``
walks to produce the focused per-method SDL string.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

from pydantic import BaseModel
from pydantic_core import PydanticUndefined

from pydantic_resolve.graphql.schema.type_mapper import TypeMapper
from pydantic_resolve.graphql.schema.type_registry import (
    ArgumentInfo,
    FieldInfo,
    TypeInfo,
    SCALAR_TYPES,
)
from pydantic_resolve.graphql.type_mapping import (
    is_enum_type,
    literal_info,
    describe_literal_values,
)
from pydantic_resolve.use_case.business import UseCaseService, iter_use_case_methods
from pydantic_resolve.use_case.context import is_from_context_annotation
from pydantic_resolve.utils.class_util import safe_issubclass
from pydantic_resolve.utils.types import (
    _is_optional,
    _resolve_function_type_hints,
    get_core_types,
    get_return_annotation,
)


# TypeMapper is stateless (er_diagram=None for the UseCase path); reuse one
# instance instead of paying for construction on every render call.
_TYPE_MAPPER = TypeMapper()


def build_compose_schema(app: Any) -> dict[str, TypeInfo]:
    """Build the ``TypeInfo`` registry for a UseCase app.

    Args:
        app: ``UseCaseResources`` from ``UseCaseManager.get_app``.

    Returns:
        Mapping ``{type_name: TypeInfo}`` covering every type reachable
        from any registered service: services themselves (as
        ``{Service}Query``), every DTO they return, every Enum those
        DTOs reference, plus the root ``Query`` type and the 5 standard
        GraphQL scalars.
    """
    registry: dict[str, TypeInfo] = {}

    # Seed with the 5 standard GraphQL scalars so introspection reports them.
    for name, ti in SCALAR_TYPES.items():
        registry[name] = ti

    query_fields: dict[str, FieldInfo] = {}

    for service_name, service_cls in app.services.items():
        service_type_name = f"{service_name}Query"
        service_type = TypeInfo(
            name=service_type_name,
            kind="OBJECT",
            python_class=service_cls,
            description=(service_cls.__doc__ or None),
        )
        registry[service_type_name] = service_type

        # Build per-method metadata once; register types in two phases below.
        method_metas: list[tuple[str, dict[str, Any], Any, list[ArgumentInfo]]] = []
        for method_name, _kind, meta in iter_use_case_methods(
            service_cls, enable_mutation=app.enable_mutation
        ):
            method = meta["method"]
            func = getattr(method, "__func__", method)
            return_anno = get_return_annotation(method)
            args = _build_method_args(func)
            method_metas.append((method_name, meta, return_anno, args))

        # Phase 1: register all OBJECT types reachable from returns. Doing
        # this before the arg phase means the arg phase always sees the
        # OBJECT entry and can rename the input version on conflict.
        for _name, _meta, return_anno, _args in method_metas:
            if return_anno is not None:
                _collect_reachable_types(return_anno, registry)

        # Phase 2: register INPUT_OBJECT types from args (renamed on conflict).
        for _name, _meta, _return_anno, args in method_metas:
            for arg in args:
                _collect_reachable_types(arg.python_type, registry, is_input=True)

        for method_name, meta, return_anno, args in method_metas:
            return_graphql_name = _graphql_type_name(return_anno, default="String")
            service_type.fields[method_name] = FieldInfo(
                name=method_name,
                python_type=(return_anno if return_anno is not None else str),
                graphql_type_name=return_graphql_name,
                description=(meta.get("description") if isinstance(meta, dict) else None),
                args=args,
            )

        # Root Query field for this service
        query_fields[service_name] = FieldInfo(
            name=service_name,
            python_type=service_cls,
            graphql_type_name=service_type_name,
        )

    registry["Query"] = TypeInfo(
        name="Query",
        kind="OBJECT",
        description="Root of the compose_query schema.",
        fields=query_fields,
    )

    return registry


# ============================================================================
# Introspection rendering: TypeInfo registry → GraphQL introspection JSON
# ============================================================================


def render_introspection(registry: dict[str, TypeInfo]) -> dict[str, Any]:
    """Render the registry as a full ``__schema`` introspection payload.

    Returns the inner ``__schema`` value (no ``data`` envelope). The
    shape matches what graphql-core would produce and what GraphiQL
    expects.
    """
    return {
        "queryType": {"name": "Query", "kind": "OBJECT"},
        "mutationType": None,
        "subscriptionType": None,
        "types": [_render_type(t, registry) for t in registry.values()],
        "directives": _STANDARD_DIRECTIVES,
    }


# The 5 built-in GraphQL directives — same set graphql-core reports.
# Hard-coded so callers see them even though compose_schema itself never
# applies them (GraphiQL lists them regardless).
_NON_NULL_BOOLEAN: dict[str, Any] = {
    "kind": "NON_NULL",
    "name": None,
    "ofType": {"kind": "SCALAR", "name": "Boolean"},
}
_STANDARD_DIRECTIVES: list[dict[str, Any]] = [
    {
        "name": "skip",
        "description": "Directs the executor to skip this field or fragment when the `if` argument is true.",
        "locations": ["FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"],
        "args": [
            {
                "name": "if",
                "description": "Skipped when true.",
                "type": _NON_NULL_BOOLEAN,
                "defaultValue": None,
            }
        ],
    },
    {
        "name": "include",
        "description": "Directs the executor to include this field or fragment only when the `if` argument is true.",
        "locations": ["FIELD", "FRAGMENT_SPREAD", "INLINE_FRAGMENT"],
        "args": [
            {
                "name": "if",
                "description": "Included when true.",
                "type": _NON_NULL_BOOLEAN,
                "defaultValue": None,
            }
        ],
    },
    {
        "name": "deprecated",
        "description": "Marks an element of a GraphQL schema as no longer supported.",
        "locations": ["FIELD_DEFINITION", "ENUM_VALUE"],
        "args": [
            {
                "name": "reason",
                "description": "Explains why this element was deprecated, usually also including a suggestion for how to access supported similar data. Formatted using the Markdown syntax, as specified by [CommonMark](https://commonmark.org/).",
                "type": {"kind": "SCALAR", "name": "String"},
                "defaultValue": '"No longer supported"',
            }
        ],
    },
    {
        "name": "specifiedBy",
        "description": "Exposes a URL that specifies the behavior of this scalar.",
        "locations": ["SCALAR"],
        "args": [
            {
                "name": "url",
                "description": "The URL that specifies the behavior of this scalar.",
                "type": {
                    "kind": "NON_NULL",
                    "name": None,
                    "ofType": {"kind": "SCALAR", "name": "String"},
                },
                "defaultValue": None,
            }
        ],
    },
    {
        "name": "oneOf",
        "description": "Indicates exactly one field must be supplied and this field must be null.",
        "locations": ["INPUT_OBJECT"],
        "args": [],
    },
]


def render_type_by_name(registry: dict[str, TypeInfo], name: str) -> dict[str, Any] | None:
    """Render a single type by name for ``__type(name: ...)`` queries."""
    t = registry.get(name)
    return _render_type(t, registry) if t is not None else None


def _render_type(type_info: TypeInfo, registry: dict[str, TypeInfo]) -> dict[str, Any]:
    """Render a TypeInfo as a GraphQL introspection type definition dict."""
    if type_info.kind == "ENUM":
        enum_values = [
            {
                "name": v,
                "description": None,
                "isDeprecated": False,
                "deprecationReason": None,
            }
            for v in (type_info.enum_values or [])
        ]
    else:
        enum_values = None

    fields = None
    if type_info.kind == "OBJECT":
        fields = [
            _render_field(f, registry) for f in type_info.fields.values()
        ] or None

    input_fields = None
    if type_info.kind == "INPUT_OBJECT":
        input_fields = [
            _render_input_field(f, registry) for f in type_info.fields.values()
        ] or None

    return {
        "kind": type_info.kind,
        "name": type_info.name,
        "description": type_info.description,
        "fields": fields,
        "inputFields": input_fields,
        "interfaces": [] if type_info.kind == "OBJECT" else None,
        "enumValues": enum_values,
        "possibleTypes": None,
    }


def _render_field(field_info: FieldInfo, registry: dict[str, TypeInfo]) -> dict[str, Any]:
    return {
        "name": field_info.name,
        "description": field_info.description,
        "args": [_render_arg(a, registry) for a in field_info.args],
        "type": _build_type_ref(
            field_info.python_type,
            registry,
            force_non_null=not _is_optional(field_info.python_type),
        ),
        "isDeprecated": field_info.is_deprecated,
        "deprecationReason": field_info.deprecation_reason,
    }


def _render_arg(arg_info: ArgumentInfo, registry: dict[str, TypeInfo]) -> dict[str, Any]:
    # An arg is nullable when its annotation is Optional OR it has a default.
    is_optional = _is_optional(arg_info.python_type) or arg_info.default_value is not None
    return {
        "name": arg_info.name,
        "description": arg_info.description,
        "type": _build_type_ref(
            arg_info.python_type,
            registry,
            force_non_null=not is_optional,
            is_input=True,
        ),
        "defaultValue": arg_info.default_value,
    }


def _render_input_field(field_info: FieldInfo, registry: dict[str, TypeInfo]) -> dict[str, Any]:
    return {
        "name": field_info.name,
        "description": field_info.description,
        "type": _build_type_ref(
            field_info.python_type,
            registry,
            force_non_null=not _is_optional(field_info.python_type),
            is_input=True,
        ),
        "defaultValue": field_info.default_value,
    }


def _build_type_ref(
    python_type: Any,
    registry: dict[str, TypeInfo] | None = None,
    *,
    force_non_null: bool,
    is_input: bool = False,
) -> dict[str, Any]:
    """Build a ``GraphQLTypeRef``-shaped dict for a Python annotation.

    ``force_non_null`` wraps the result in an outer ``NON_NULL`` layer
    (i.e. adds the trailing ``!`` in SDL). Callers decide based on
    whether the field/arg is required.

    ``registry`` is consulted when ``is_input=True`` to pick up the
    registered INPUT_OBJECT name — which may differ from the Python class
    name when the same BaseModel serves as both an OBJECT (return) and
    an INPUT_OBJECT (arg), in which case the input version is renamed
    ``{Name}Input`` to satisfy the GraphQL spec.
    """
    # UseCaseService subclasses reference their corresponding
    # ``{Service}Query`` OBJECT — TypeMapper doesn't know about services,
    # so without this branch it would fall back to SCALAR String.
    if (
        isinstance(python_type, type)
        and safe_issubclass(python_type, UseCaseService)
    ):
        inner: dict[str, Any] = {
            "kind": "OBJECT",
            "name": f"{python_type.__name__}Query",
            "description": None,
            "ofType": None,
        }
        if force_non_null:
            return {
                "kind": "NON_NULL",
                "name": None,
                "description": None,
                "ofType": inner,
            }
        return inner

    # Scalar Literal annotations map to the scalar shared by their values;
    # a None inside the literal means the value may be null (skip NON_NULL).
    literal = literal_info(python_type)
    if literal is not None:
        python_type, has_none = literal
        if has_none:
            force_non_null = False
    gql_type = _TYPE_MAPPER.map_to_graphql_type(python_type, is_input=is_input)
    inner = gql_type.to_introspection()
    if is_input and registry is not None:
        _override_input_name(inner, python_type, registry)
    if force_non_null:
        return {
            "kind": "NON_NULL",
            "name": None,
            "description": None,
            "ofType": inner,
        }
    return inner


def _override_input_name(
    inner: dict[str, Any], python_type: Any, registry: dict[str, TypeInfo]
) -> None:
    """Walk the introspection type-ref chain to its leaf and, if it points
    at a BaseModel registered as INPUT_OBJECT, replace the leaf name with
    the registry's name (handles the ``{Name}Input`` rename-on-conflict).
    """
    core_types = get_core_types(python_type)
    if not core_types or not isinstance(core_types[0], type):
        return
    cls = core_types[0]
    if not safe_issubclass(cls, BaseModel):
        return
    input_t = next(
        (t for t in registry.values()
         if t.kind == "INPUT_OBJECT" and t.python_class is cls),
        None,
    )
    if input_t is None or input_t.name == cls.__name__:
        return
    # Walk to the leaf (past NON_NULL / LIST wrappers) and override the name.
    node = inner
    while node.get("ofType") is not None:
        node = node["ofType"]
    node["name"] = input_t.name


def _graphql_type_name(annotation: Any, *, default: str) -> str:
    """Return the leaf GraphQL type name for an annotation (for FieldInfo)."""
    if annotation is None or annotation is inspect.Parameter.empty:
        return default
    literal = literal_info(annotation)
    if literal is not None:
        annotation = literal[0]
    gql = _TYPE_MAPPER.map_to_graphql_type(annotation)
    return gql.leaf_name or default


# ============================================================================
# Method-SDL rendering (focused per-method view, used by describe_compose_method)
# ============================================================================


def method_sdl(
    registry: dict[str, TypeInfo],
    service_name: str,
    method_name: str,
) -> str | None:
    """Focused SDL for one method: signature + return type's transitive closure.

    Returns a GraphQL SDL string showing:
    - The method signature (args + return type) as a comment header
    - Full type definitions for the return DTO and every nested DTO
      reachable through its fields (handles cycles)

    Returns ``None`` if the method or its return type can't be located
    in ``registry``.
    """
    service_type = registry.get(f"{service_name}Query")
    if service_type is None:
        return None
    field_info = service_type.fields.get(method_name)
    if field_info is None:
        return None

    reachable: dict[str, TypeInfo] = {}
    _collect_reachable_sdl_types(field_info.python_type, registry, reachable)
    for arg in field_info.args:
        _collect_reachable_sdl_types(arg.python_type, registry, reachable)

    sdl_parts: list[str] = []
    # Method signature as a comment header
    args_sdl = ", ".join(
        f"{a.name}: {_type_ref_to_sdl(_build_type_ref(a.python_type, registry, force_non_null=(a.default_value is None and not _is_optional(a.python_type)), is_input=True))}"
        for a in field_info.args
    )
    return_sdl = _type_ref_to_sdl(
        _build_type_ref(field_info.python_type, registry, force_non_null=not _is_optional(field_info.python_type))
    )
    sdl_parts.append(f"# {service_name}.{method_name}({args_sdl}): {return_sdl}")
    for type_name, type_def in sorted(reachable.items()):
        sdl_parts.append(_render_type_sdl(type_def))

    return "\n\n".join(sdl_parts)


def _collect_reachable_sdl_types(
    annotation: Any,
    registry: dict[str, TypeInfo],
    seen: dict[str, TypeInfo],
) -> None:
    """DFS-walk ``annotation``, recording every OBJECT / INPUT_OBJECT / ENUM TypeInfo reachable.

    Scalars are skipped (they're standard GraphQL types, always present).
    Only types that actually need an SDL definition (OBJECT, INPUT_OBJECT,
    ENUM) are collected — and only if they're already in ``registry``.
    """
    for core_type in get_core_types(annotation):
        if isinstance(core_type, str):
            continue
        name = getattr(core_type, "__name__", None)
        if name is None or name in seen:
            continue
        type_info = registry.get(name)
        if type_info is None or type_info.kind not in ("OBJECT", "INPUT_OBJECT", "ENUM"):
            continue
        seen[name] = type_info
        # Enums have no fields to recurse through; OBJECTs / INPUT_OBJECTs do.
        if type_info.kind in ("OBJECT", "INPUT_OBJECT"):
            for field in type_info.fields.values():
                _collect_reachable_sdl_types(field.python_type, registry, seen)


def _render_type_sdl(type_info: TypeInfo) -> str:
    """Dispatch SDL rendering by kind (OBJECT / INPUT_OBJECT / ENUM)."""
    if type_info.kind == "ENUM":
        return _render_enum_type_sdl(type_info)
    if type_info.kind == "INPUT_OBJECT":
        return _render_struct_type_sdl(type_info, keyword="input")
    return _render_struct_type_sdl(type_info, keyword="type")


def _render_struct_type_sdl(type_info: TypeInfo, *, keyword: str) -> str:
    """Render an OBJECT or INPUT_OBJECT TypeInfo as spec-compliant SDL.

    ``keyword`` is ``"type"`` for OBJECT or ``"input"`` for INPUT_OBJECT;
    the field-rendering logic is otherwise identical. Description sits
    above the header (not inside, which would violate the GraphQL SDL
    spec); field descriptions sit above each field. Block strings
    (``\"\"\"…\"\"\"``) preserve multi-line / markdown.
    """
    parts: list[str] = []
    if type_info.description:
        parts.append(_block_string(type_info.description))
    field_lines: list[str] = []
    for field in type_info.fields.values():
        if field.description:
            field_lines.append(_indent(_block_string(field.description), 2))
        gql = _TYPE_MAPPER.map_to_graphql_type(field.python_type)
        sdl = gql.to_sdl()
        if not _is_optional(field.python_type) and not sdl.endswith("!"):
            sdl = f"{sdl}!"
        field_lines.append(f"  {field.name}: {sdl}")
    parts.append(f"{keyword} {type_info.name} {{\n" + "\n".join(field_lines) + "\n}")
    return "\n".join(parts)


def _render_enum_type_sdl(type_info: TypeInfo) -> str:
    """Render an ENUM TypeInfo as ``enum X { ... }`` SDL.

    Each enum value can carry its own description per spec; this
    implementation currently doesn't capture per-value descriptions
    ( TypeInfo.enum_values holds bare strings), so values render bare.
    """
    parts: list[str] = []
    if type_info.description:
        parts.append(_block_string(type_info.description))
    value_lines = [f"  {v}" for v in (type_info.enum_values or [])]
    body = "\n".join(value_lines)
    parts.append(f"enum {type_info.name} {{\n{body}\n}}")
    return "\n".join(parts)


def _block_string(text: str) -> str:
    """Wrap a description as a GraphQL block string (``\"\"\"…\"\"\"``)."""
    return f'"""{text.strip()}"""'


def _indent(text: str, n: int) -> str:
    """Indent every non-empty line by ``n`` spaces (block-string friendly)."""
    pad = " " * n
    return "\n".join(f"{pad}{line}" if line else line for line in text.splitlines())


def _type_ref_to_sdl(type_ref: dict[str, Any]) -> str:
    """Render an introspection-style type ref dict as SDL syntax.

    Inverse of ``_build_type_ref``: walks ``kind`` / ``ofType`` to
    produce e.g. ``[Int!]!``.
    """
    kind = type_ref.get("kind")
    name = type_ref.get("name")
    if kind == "NON_NULL":
        return f"{_type_ref_to_sdl(type_ref['ofType'])}!"
    if kind == "LIST":
        return f"[{_type_ref_to_sdl(type_ref['ofType'])}]"
    return name or "String"


# ============================================================================
# Internals
# ============================================================================


def _field_default_literal(field_info: Any) -> str | None:
    """Render a pydantic field's default as a GraphQL literal.

    Returns ``None`` when the field has no default (``PydanticUndefined``),
    matching how ``_build_method_args`` renders method-arg defaults via
    ``json.dumps``. ``Optional[str] = None`` → ``"null"``; ``int = 10`` →
    ``"10"``; ``str = "hi"`` → ``"\"hi\""``.
    """
    default = field_info.default
    if default is PydanticUndefined:
        return None
    return json.dumps(default)


def _build_method_args(func: Any) -> list[ArgumentInfo]:
    """Build ArgumentInfo list for a method's parameters.

    Skips ``cls`` and parameters annotated with ``FromContext``.
    Builds ArgumentInfo directly — ``TypeMapper.extract_argument_info``
    has a latent ``is_optional`` bug (it always returns True because
    ``map_to_graphql_type`` never wraps the outer level in NON_NULL),
    and ``_render_arg`` re-derives type info from ``python_type`` anyway,
    so calling it was both wasted compute and a bug trap.
    """
    sig = inspect.signature(func)
    hints = _resolve_function_type_hints(func)

    args: list[ArgumentInfo] = []
    for name, param in sig.parameters.items():
        if name == "cls":
            continue
        anno = hints.get(name, param.annotation)
        if is_from_context_annotation(anno):
            continue
        if anno is inspect.Parameter.empty or anno is None:
            anno = str

        has_default = param.default is not inspect.Parameter.empty
        args.append(
            ArgumentInfo(
                name=name,
                python_type=anno,
                graphql_type_name=_graphql_type_name(anno, default="String"),
                description=describe_literal_values(None, anno),
                # GraphQL spec wants literals like `true` / `"hi"` / `42`,
                # not Python repr like `True` / `'hi'`. json.dumps matches
                # the spec for the JSON-encodable subset (which covers all
                # realistic arg defaults).
                default_value=(
                    json.dumps(param.default) if has_default else None
                ),
            )
        )
    return args


def _collect_reachable_types(
    annotation: Any, registry: dict[str, TypeInfo], *, is_input: bool = False
) -> None:
    """Register TypeInfo for every BaseModel / Enum reachable from ``annotation``.

    Delegates the annotation walk to ``TypeMapper.collect_referenced_types``
    (shared with the ErDiagram path). This function only handles the
    UseCase-specific TypeInfo construction.
    """
    for name, cls in _TYPE_MAPPER.collect_referenced_types(annotation).items():
        if is_input:
            # If this class already has an INPUT_OBJECT entry, skip (the
            # entry may live under ``{cls.__name__}`` or ``{Name}Input``
            # after a rename — match on python_class, not name).
            already_input = any(
                t.kind == "INPUT_OBJECT" and t.python_class is cls
                for t in registry.values()
            )
            if already_input:
                continue
            # If the class name is already taken by an OBJECT (the
            # BaseModel is also used as a return type), rename the input
            # version to ``{Name}Input`` so the GraphQL spec's "type name
            # uniquely identifies kind" rule holds.
            existing = registry.get(name)
            if existing is not None and existing.kind == "OBJECT":
                name = f"{name}Input"
        if name in registry:
            continue
        if safe_issubclass(cls, BaseModel):
            type_info = TypeInfo(
                name=name,
                kind="INPUT_OBJECT" if is_input else "OBJECT",
                python_class=cls,
                description=(cls.__doc__ or None),
                is_input=is_input,
            )
            registry[name] = type_info
            for field_name, field_info in cls.model_fields.items():
                field_anno = field_info.annotation
                _collect_reachable_types(field_anno, registry, is_input=is_input)
                type_info.fields[field_name] = FieldInfo(
                    name=field_name,
                    python_type=field_anno,
                    graphql_type_name=_graphql_type_name(field_anno, default="String"),
                    description=describe_literal_values(
                        getattr(field_info, "description", None), field_anno
                    ),
                    default_value=_field_default_literal(field_info),
                )
        elif is_enum_type(cls):
            registry[name] = TypeInfo(
                name=name,
                kind="ENUM",
                python_class=cls,
                description=(cls.__doc__ or None),
                enum_values=[m.name for m in cls],
            )


__all__ = [
    "build_compose_schema",
    "render_introspection",
    "render_type_by_name",
    "method_sdl",
]
