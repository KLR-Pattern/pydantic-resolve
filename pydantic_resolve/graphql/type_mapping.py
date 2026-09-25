"""
Type mapping utilities for GraphQL.

Provides centralized type conversion between Python and GraphQL types.
"""

from enum import Enum
from datetime import datetime, date, time
from uuid import UUID
from typing import Any, Literal, Optional, get_origin, get_args, Union
import types as _types
from pydantic_resolve.utils.class_util import safe_issubclass
from pydantic_resolve.utils.types import get_core_types, _is_optional, _is_list
from pydantic import BaseModel


# Python basic types to GraphQL types mapping
PYTHON_TO_GQL_TYPES = {
    int: 'Int',
    str: 'String',
    float: 'Float',
    bool: 'Boolean',
}

# Temporal + UUID scalars, matched by subclass. Ordered: ``datetime`` is a
# subclass of ``date``, so it must come first or a datetime field would wrongly
# map to ``Date``. Transport stays string-serialized (ISO 8601 / UUID str).
_SCALAR_SUBCLASS_MAP = (
    (UUID, "UUID"),
    (datetime, "DateTime"),
    (time, "Time"),
    (date, "Date"),
)


def is_enum_type(python_type: type) -> bool:
    """
    Check if type is an Enum subclass.

    Args:
        python_type: Python type

    Returns:
        True if the type is an Enum subclass, False otherwise

    Examples:
        >>> from enum import Enum
        >>> class Status(Enum):
        ...     ACTIVE = "active"
        >>> is_enum_type(Status)
        True
        >>> is_enum_type(str)
        False
    """
    try:
        return safe_issubclass(python_type, Enum)
    except TypeError:
        return False


def get_enum_names(enum_class: type) -> list[str]:
    """
    Get all enum member names from an Enum class.

    Args:
        enum_class: An Enum subclass

    Returns:
        List of enum member names

    Examples:
        >>> from enum import Enum
        >>> class Status(Enum):
        ...     ACTIVE = "active"
        ...     INACTIVE = "inactive"
        >>> get_enum_values(Status)
        ['ACTIVE', 'INACTIVE']
    """
    if not is_enum_type(enum_class):
        return []
    return [member.name for member in enum_class]


def map_python_to_graphql(python_type: type, include_required: bool = True) -> str:
    """
    Map Python type to GraphQL type string

    Args:
        python_type: Python type
        include_required: Whether to include ! (non-null marker)

    Returns:
        GraphQL type string (e.g., "String!", "[Int]!")

    Examples:
        >>> map_python_to_graphql(int)
        "Int!"
        >>> map_python_to_graphql(list[int])
        "[Int]!"
        >>> map_python_to_graphql(Optional[str])
        "String"
    """
    # Check if it's Optional type (Union with None)
    is_optional = _is_optional(python_type)

    # Scalar Literal: unwrap to the shared scalar type. A ``None`` member
    # (Literal['open', None]) means the value may be null even though the
    # annotation is not a Union — treat it like Optional for the ! suffix.
    literal = literal_info(python_type)
    if literal is not None:
        python_type, has_none = literal
    else:
        has_none = False

    # For Optional types, don't include required suffix
    if is_optional or has_none:
        include_required = False

    required_suffix = "!" if include_required else ""

    # Use get_core_types to handle all wrapper types
    core_types = get_core_types(python_type)
    if not core_types:
        return "String" + required_suffix  # Default to String

    core_type = core_types[0]

    # Check if it's list type
    if _is_list(python_type):
        # list[T] -> [T!]!
        inner_gql = map_python_to_graphql(core_type, include_required=True)
        return f"[{inner_gql}]{required_suffix}"
    else:
        # T -> T!
        # Check if it's an enum type first
        if is_enum_type(core_type):
            return f"{core_type.__name__}{required_suffix}"
        elif safe_issubclass(core_type, BaseModel):
            return f"{core_type.__name__}{required_suffix}"
        else:
            # Scalar type
            scalar_name = map_scalar_type(core_type)
            return f"{scalar_name}{required_suffix}"


def literal_info(annotation: Any) -> tuple[type, bool] | None:
    """Validate a scalar ``Literal`` annotation; return ``(scalar_type, has_none)``.

    Returns ``None`` for non-Literal annotations. Raises ``ValueError`` for
    Literals that cannot map to a single GraphQL scalar (mixed value types,
    enum members, all-None). GraphQL SDL has no constrained-scalar kind, so
    a Literal maps to the scalar shared by its values and Pydantic keeps
    enforcing the allowed values at runtime.

    Shared single source for the validation rules; the compose schema
    builder (``use_case/compose_schema.py``) and the entity-first path
    (``schema/type_mapper.py`` / generators) both call this so the rules
    cannot drift. Ported from nexusx #151 via #313.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is _types.UnionType:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) != 1:
            return None
        core = args[0]
    else:
        core = annotation
    if get_origin(core) is not Literal:
        return None

    all_values = get_args(core)
    values = [value for value in all_values if value is not None]
    if not values:
        raise ValueError(
            f"Literal annotations must contain a non-None value; got {annotation!r}."
        )
    value_types = {type(value) for value in values}
    if len(value_types) != 1:
        names = ", ".join(sorted(t.__name__ for t in value_types))
        raise ValueError(
            f"Literal values must share one Python type; got {names} in "
            f"{annotation!r}. Use an Enum instead: enum members can mix "
            "value types and map to a GraphQL enum."
        )
    literal_type = next(iter(value_types))
    if safe_issubclass(literal_type, Enum):
        raise ValueError(
            f"Literal values must use a supported scalar type; got "
            f"{literal_type.__name__} in {annotation!r}. "
            "Use the enum class directly instead of Literal[enum_member]."
        )
    if not map_scalar_type(literal_type):
        raise ValueError(
            f"Literal values must use a supported scalar type; got "
            f"{literal_type.__name__} in {annotation!r}."
        )
    return literal_type, len(values) != len(all_values)


def literal_is_nullable(annotation: Any) -> bool:
    """True when ``annotation`` is a ``Literal`` whose members include ``None``.

    ``Literal['open', None]`` allows null at runtime but is not a Union, so
    ``_is_optional`` does not see it — callers deciding NON_NULL wrappers
    must consult this instead (mirrors the Optional[T] treatment).
    """
    info = literal_info(annotation)
    return info is not None and info[1]


def literal_allowed_values(annotation: Any) -> tuple[Any, ...] | None:
    """Return the allowed values of a scalar ``Literal`` annotation.

    Unwraps ``Optional`` / ``list`` wrappers so field and argument
    descriptions can mention the constraint even though the GraphQL type is
    the plain underlying scalar.
    """
    origin = get_origin(annotation)
    if origin is Union or origin is _types.UnionType:
        args = [arg for arg in get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return literal_allowed_values(args[0])
        return None
    if origin is list:
        args = get_args(annotation)
        if args:
            return literal_allowed_values(args[0])
        return None
    if origin is Literal:
        values = tuple(v for v in get_args(annotation) if v is not None)
        return values or None
    return None


def describe_literal_values(
    description: Optional[str], annotation: Any
) -> Optional[str]:
    """Append ``Allowed values: ...`` to a description for ``Literal`` annotations."""
    values = literal_allowed_values(annotation)
    if not values:
        return description
    suffix = "Allowed values: " + ", ".join(str(value) for value in values)
    if description:
        return f"{description} {suffix}"
    return suffix


def map_scalar_type(python_type: type) -> str:
    """
    Map Python scalar type to GraphQL scalar type name

    Args:
        python_type: Python type

    Returns:
        GraphQL scalar type name ("Int", "String", "Boolean", "Float") or enum name

    Examples:
        >>> map_scalar_type(int)
        "Int"
        >>> map_scalar_type(str)
        "String"
    """
    # Check if it's an enum type - return enum class name as GraphQL type
    if is_enum_type(python_type):
        return python_type.__name__

    # Scalar Literal: map to the scalar shared by its values (validating the
    # Literal first — a mixed/enum/all-None Literal raises here instead of
    # silently falling through to the lenient String fallback below).
    if get_origin(python_type) is Literal:
        literal_type, _ = literal_info(python_type)
        return map_scalar_type(literal_type)

    # Check direct mapping
    if python_type in PYTHON_TO_GQL_TYPES:
        return PYTHON_TO_GQL_TYPES[python_type]

    # Temporal + UUID scalars (subclass-matched; ordered datetime before date)
    for py_cls, gql_name in _SCALAR_SUBCLASS_MAP:
        if safe_issubclass(python_type, py_cls):
            return gql_name

    # Check via string (handle type strings, etc.)
    type_str = str(python_type).lower()
    if "int" in type_str:
        return "Int"
    elif "bool" in type_str:
        return "Boolean"
    elif "float" in type_str:
        return "Float"
    else:
        return "String"


def get_graphql_type_description(gql_type: str) -> str:
    """
    Get description of GraphQL scalar type

    Args:
        gql_type: GraphQL type name

    Returns:
        Type description string
    """
    descriptions = {
        "Int": "The `Int` scalar type represents non-fractional signed whole numeric values.",
        "Float": "The `Float` scalar type represents signed double-precision fractional values.",
        "String": "The `String` scalar type represents textual data.",
        "Boolean": "The `Boolean` scalar type represents `true` or `false`.",
        "ID": "The `ID` scalar type represents a unique identifier.",
        "UUID": "The `UUID` scalar type represents a UUID, serialized as a string.",
        "DateTime": "The `DateTime` scalar type represents an ISO 8601 datetime, serialized as a string.",
        "Date": "The `Date` scalar type represents an ISO 8601 date, serialized as a string.",
        "Time": "The `Time` scalar type represents an ISO 8601 time, serialized as a string.",
    }
    return descriptions.get(gql_type)


def is_union_type(type_hint: type) -> bool:
    """
    Check if type is Union (including Optional)

    Args:
        type_hint: Type hint

    Returns:
        Whether it's a Union type
    """
    origin = get_origin(type_hint)
    return origin is Union


def is_list_type(type_hint: type) -> bool:
    """
    Check if type is list

    Args:
        type_hint: Type hint

    Returns:
        Whether it's a list type
    """
    return _is_list(type_hint)


def unwrap_optional(type_hint: type):
    """
    Extract T from Optional[T] or Union[T, None]

    Args:
        type_hint: Type hint

    Returns:
        If it's Optional[T], return T
        Otherwise return the original type

    Examples:
        >>> unwrap_optional(Optional[int])
        int
        >>> unwrap_optional(int)
        int
    """
    core_types = get_core_types(type_hint)
    # Filter out NoneType
    non_none_types = [t for t in core_types if t is not type(None)]
    return non_none_types[0] if non_none_types else type_hint


def extract_list_element_type(list_type: type):
    """
    Extract element type T from list[T]

    Args:
        list_type: List type

    Returns:
        Element type, or None if not a list

    Examples:
        >>> extract_list_element_type(list[int])
        int
    """
    if not is_list_type(list_type):
        return None

    args = get_args(list_type)
    if args:
        return args[0]
    return None
