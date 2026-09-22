"""UseCaseService base class and BusinessMeta metaclass.

Provides the foundation for defining business service classes whose
methods decorated with @query or @mutation are automatically discovered
and exposed via MCP.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterator
from typing import Any

import pydantic_resolve.constant as const

USE_CASE_METHODS_ATTR = "__use_case_methods__"


def _get_method_kind(func: Any) -> str | None:
    """Return 'query' or 'mutation' if func is marked by decorator, else None."""
    if getattr(func, const.GRAPHQL_QUERY_ATTR, False):
        return "query"
    if getattr(func, const.GRAPHQL_MUTATION_ATTR, False):
        return "mutation"
    return None


def _get_method_description(func: Any) -> str:
    """Return the description stored by @query/@mutation decorator."""
    return (
        getattr(func, const.GRAPHQL_QUERY_DESCRIPTION_ATTR, None)
        or getattr(func, const.GRAPHQL_MUTATION_DESCRIPTION_ATTR, None)
        or ""
    )


class BusinessMeta(type):
    """Metaclass that collects @query/@mutation decorated methods for introspection.

    Scans the class namespace for async classmethods marked with ``@query`` or
    ``@mutation`` decorators and stores their metadata in
    ``__use_case_methods__`` for use by the MCP server's progressive
    disclosure tools.
    """

    def __new__(mcs, name: str, bases: tuple, namespace: dict, **kwargs):
        cls = super().__new__(mcs, name, bases, namespace, **kwargs)

        # Allow UseCaseService itself to be created without __use_case_methods__
        if name == "UseCaseService" and not any(
            isinstance(b, BusinessMeta) for b in bases
        ):
            setattr(cls, USE_CASE_METHODS_ATTR, {})
            return cls

        # Collect decorated methods from this class and bases
        methods: dict[str, dict[str, Any]] = {}

        # First collect from bases
        for base in bases:
            if hasattr(base, USE_CASE_METHODS_ATTR):
                methods.update(getattr(base, USE_CASE_METHODS_ATTR))

        # Then collect from current class
        _EXCLUDED_METHODS = {"get_tag_name"}
        for attr_name, attr_value in namespace.items():
            # Skip private/protected and excluded methods
            if attr_name.startswith("_") or attr_name in _EXCLUDED_METHODS:
                continue

            func = _unwrap_classmethod(attr_value)
            if func is None:
                continue

            # Only discover methods marked with @query or @mutation
            kind = _get_method_kind(func)
            if kind is None:
                continue

            if not inspect.iscoroutinefunction(func):  # asyncio.* variant is removed in 3.16
                continue

            methods[attr_name] = {
                "method": attr_value,
                "kind": kind,
                "description": _get_method_description(func),
            }

        setattr(cls, USE_CASE_METHODS_ATTR, methods)
        return cls


def _unwrap_classmethod(value: Any) -> Any | None:
    """Unwrap a classmethod to get the underlying function, if any."""
    if isinstance(value, classmethod):
        return value.__func__
    return None


def iter_use_case_methods(
    service_cls: type,
    *,
    enable_mutation: bool = True,
) -> Iterator[tuple[str, str, dict[str, Any]]]:
    """Iterate ``(name, kind, meta)`` for each exposed method on a service.

    Skips mutations when ``enable_mutation`` is False. ``kind`` is the
    resolved method kind (``"query"`` or ``"mutation"``), defaulting to
    ``"query"`` when ``meta`` is malformed — matches the defensive
    fallback every caller was already doing by hand.
    """
    methods: dict[str, Any] = getattr(service_cls, USE_CASE_METHODS_ATTR, {})
    for name, meta in methods.items():
        kind = meta.get("kind", "query") if isinstance(meta, dict) else "query"
        if kind == "mutation" and not enable_mutation:
            continue
        yield name, kind, meta


class UseCaseService(metaclass=BusinessMeta):
    """Base class for business service definitions.

    Subclasses define async methods decorated with ``@query`` or ``@mutation``
    that represent use case operations. The BusinessMeta metaclass automatically
    discovers these methods and makes them available for introspection.

    Example::

        from pydantic_resolve import query, mutation

        class SprintService(UseCaseService):
            '''Sprint management service.'''

            @query
            async def list_sprints(cls) -> list[SprintSummary]:
                '''Get all sprints.'''
                ...

            @mutation
            async def create_sprint(cls, name: str) -> SprintSummary:
                '''Create a new sprint.'''
                ...
    """

    __use_case_methods__: dict[str, dict[str, Any]]

    @classmethod
    def get_tag_name(cls) -> str:
        """Return the tag name for this service.

        Returns the class name by default. Override to customize.
        """
        return cls.__name__
