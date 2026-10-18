"""Introspection support for the UseCase compose surface.

Two siblings:

- :func:`is_introspection_query` — field-based detector used by
  ``compose_query`` to reject (or by HTTP handlers to route) ``__schema``
  / ``__type`` / ``__typename`` queries.
- :func:`compose_introspect` — hand-builds the GraphQL introspection
  response by walking the app's cached ``compose_schema`` registry.
  Returns the standard ``{data, errors}`` envelope. GraphiQL-compatible.

These live separately from :mod:`compose` because the execution pipeline
in ``compose.py`` handles *data* queries only; introspection is a
parallel path that callers must dispatch to explicitly.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic_resolve.graphql.utils import is_introspection_query
from pydantic_resolve.use_case.compose import ComposeError
from pydantic_resolve.use_case.compose_schema import (
    render_introspection,
    render_type_by_name,
)


def compose_introspect(
    app: Any,
    query: str | None = None,
) -> dict[str, Any]:
    """Build a GraphQL introspection response for the app's compose schema.

    Args:
        app: :class:`UseCaseResources` instance. Reads ``app.compose_schema``
            (a ``{type_name: TypeInfo}`` registry built once at registration).
        query: GraphQL query string. The detector dispatches by keyword:

            * ``__schema`` (or ``query is None``) → full introspection payload
            * ``__type(name: "X")`` → single type lookup
            * ``__typename`` → returns ``"Query"``

            Field selection inside ``__schema { ... }`` is not honored —
            GraphiQL only ever sends the canonical full query, so the
            entire schema is always returned.

    Returns:
        Standard GraphQL response envelope::

            {"data": {...}, "errors": None}

    Raises:
        ComposeError: If the schema registry is missing.
    """
    registry = getattr(app, "compose_schema", None)
    if registry is None:
        raise ComposeError(
            "App has no cached compose_schema; was it built via UseCaseManager?",
            "internal_error",
        )

    actual_query = query if query is not None else "__schema"
    data: dict[str, Any] = {}

    if "__schema" in actual_query:
        data["__schema"] = render_introspection(registry)

    type_name = _extract_type_name_from_query(actual_query)
    if "__type" in actual_query:
        if type_name is None:
            data["__type"] = None
        else:
            data["__type"] = render_type_by_name(registry, type_name)

    if "__typename" in actual_query:
        data["__typename"] = "Query"

    return {"data": data, "errors": None}


_TYPE_NAME_RE = re.compile(r'__type\s*\(\s*name\s*:\s*["\']([^"\']+)["\']')


def _extract_type_name_from_query(query: str) -> str | None:
    """Extract the type name from a ``__type(name: "X")`` query."""
    match = _TYPE_NAME_RE.search(query)
    return match.group(1) if match else None


__all__ = ["is_introspection_query", "compose_introspect"]

