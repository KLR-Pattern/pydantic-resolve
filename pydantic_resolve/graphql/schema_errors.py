"""Schema-construction errors + shared scan for the entity GraphQL path.

All checks fire eagerly at scan time — inside ``GraphQLHandler.__init__``
when the grouped query/mutation maps are built, and (via the same
:func:`scan_grouped_methods` helper) when SDL is generated standalone
through ``SchemaBuilder.build_schema()`` — before any SDL or introspection
is produced, so a broken entity graph never silently drops or clobbers a
method.

Under the grouped layout the entity class name becomes both the root
Query/Mutation field name and the ``{Entity}Query`` / ``{Entity}Mutation``
type name, and the method name is used verbatim as the field on that group
type. That makes name collisions destructive (silently clobbering a group or
a method), so they are rejected up front instead of last-writer-wins.

Entity-name uniqueness (two distinct classes sharing ``__name__``) is
already enforced by ``ErDiagram._validate_configs`` at construction time, so
it is not re-checked here.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

from pydantic_resolve.graphql.utils import group_type_name


# GraphQL root operation type names — an entity class named one of these would
# collide with the synthesized root types.
_RESERVED_ROOT_NAMES = frozenset({"Query", "Mutation", "Subscription"})


class EntitySchemaError(Exception):
    """Base class for entity-path schema construction errors."""


class DuplicateMethodError(EntitySchemaError):
    """Two methods on the same entity produce the same GraphQL field name."""


class ReservedMethodFieldError(EntitySchemaError):
    """A ``@query``/``@mutation`` method name starts with ``__``.

    The ``__`` prefix is reserved by GraphQL introspection (``__schema``,
    ``__type``, ``__typename``) and cannot be used as a field name.
    """


class ReservedEntityError(EntitySchemaError):
    """An entity class is named ``Query`` / ``Mutation`` / ``Subscription``.

    Those names collide with the GraphQL root operation types and would
    produce confusing, introspection-breaking schemas.
    """


class GroupTypeCollisionError(EntitySchemaError):
    """A ``{Entity}Query`` / ``{Entity}Mutation`` group type name collides with
    another entity's object type name.

    Every entity produces an OBJECT type named after its class, and every
    entity with ``@query``/``@mutation`` methods produces a ``{Entity}Query``/
    ``{Entity}Mutation`` group type. If those two sets overlap (e.g. entities
    ``User`` and ``UserQuery``) the SDL/introspection ends up with two
    same-named type definitions. Reject it up front instead of emitting
    ambiguous schema.
    """


def scan_grouped_methods(
    entities: Iterable,
    extract_fn: Callable[[type], list[dict]],
    group_suffix: str,
) -> dict[str, list[dict]]:
    """Scan entities into a grouped ``{entity_name: [method_info, ...]}`` map.

    Single source of truth for both ``GraphQLHandler`` (operation-map
    building) and the schema generators (SDL), so every path that renders
    operations applies the same eager conflict checks and computes the same
    grouping. A broken entity graph therefore fails fast regardless of entry
    point — handler init *or* standalone ``SchemaBuilder.build_schema()``.

    Args:
        entities: Iterable of Entity configs (anything exposing ``.kls``).
        extract_fn: ``Callable[type] -> list[dict]`` returning an entity's
            ``@query``/``@mutation`` method infos (each carrying a ``'name'``
            key, the verbatim GraphQL field name).
        group_suffix: ``"Query"`` or ``"Mutation"`` — the group type-name
            suffix being built, used for the group-type collision check.

    Returns:
        ``{entity_name: [method_info, ...]}``, preserving declaration order.
        Entities with no methods of this kind are omitted.

    Raises:
        ReservedEntityError: an entity contributing methods is named
            ``Query`` / ``Mutation`` / ``Subscription``.
        ReservedMethodFieldError: a method field name starts with ``__``.
        DuplicateMethodError: two methods on one entity share a field name.
        GroupTypeCollisionError: a ``{Entity}{group_suffix}`` group type name
            collides with another entity's object type name.
    """
    # Every entity produces an OBJECT type named after its class, so a group
    # type name that matches any entity class name would duplicate a type.
    entities = tuple(entities)
    entity_type_names = {entity_cfg.kls.__name__ for entity_cfg in entities}

    grouped: dict[str, list[dict]] = {}
    for entity_cfg in entities:
        kls = entity_cfg.kls
        methods = extract_fn(kls)
        if not methods:
            # Entities without a method of this kind produce no group — skip
            # entirely (no group type, no root mount, no name validation).
            continue

        entity_name = kls.__name__

        if entity_name in _RESERVED_ROOT_NAMES:
            raise ReservedEntityError(
                f"Entity class '{entity_name}' clashes with a GraphQL root "
                f"operation type name; rename the class (defined in {kls.__module__})."
            )

        group_type = group_type_name(entity_name, group_suffix)
        if group_type in entity_type_names:
            raise GroupTypeCollisionError(
                f"Entity '{entity_name}' produces {group_suffix} group type "
                f"'{group_type}', which collides with the entity class "
                f"'{group_type}' also in the diagram; rename one of them."
            )

        bucket = grouped.setdefault(entity_name, [])
        seen_fields = {m["name"] for m in bucket}
        for method_info in methods:
            field_name = method_info["name"]
            if field_name.startswith("__"):
                raise ReservedMethodFieldError(
                    f"Method '{field_name}' on entity '{entity_name}' "
                    f"({kls.__module__}.{entity_name}) starts with '__', "
                    f"which is reserved by GraphQL introspection; rename the method."
                )
            if field_name in seen_fields:
                raise DuplicateMethodError(
                    f"Method '{field_name}' appears more than once on entity "
                    f"'{entity_name}' ({kls.__module__}.{entity_name})."
                )
            seen_fields.add(field_name)
            bucket.append(method_info)

    return grouped
