from __future__ import annotations

from collections import defaultdict
from typing import Any

from aiodataloader import DataLoader

from pydantic_resolve.integration.common import (
    _build_loader_identity,
    _finalize_loader_class,
    _get_effective_query_fields,
)


def _resolve_using_db(using_db: Any) -> Any:
    if callable(using_db):
        return using_db()
    return using_db


def _maybe_use_db(queryset: Any, using_db: Any) -> Any:
    resolved_db = _resolve_using_db(using_db)
    if resolved_db is not None:
        return queryset.using_db(resolved_db)
    return queryset


def _apply_filters(queryset: Any, filters: list[Any] | None) -> Any:
    if filters:
        return queryset.filter(*filters)
    return queryset


def _get_orm_scalar_fields(target_orm_kls: type) -> set[str]:
    scalar_fields = set(target_orm_kls._meta.fields_db_projection.keys())
    scalar_fields.add(target_orm_kls._meta.pk_attr)
    return scalar_fields


def _sanitize_only_fields(loader: DataLoader, target_orm_kls: type, fields: list[str]) -> list[str]:
    scalar_fields = _get_orm_scalar_fields(target_orm_kls)
    sanitized_fields = [field for field in fields if field in scalar_fields]
    loader._effective_query_fields = sanitized_fields
    return sanitized_fields


def _apply_only(queryset: Any, fields: list[str]) -> Any:
    if fields:
        return queryset.only(*fields)
    return queryset


def _create_lookup_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_field_name: str,
    using_db: Any = None,
    filters: list[Any] | None = None,
    suffix: str,
) -> type[DataLoader]:
    """Shared implementation for M2O and R-O2O lookup loaders."""
    identity = _build_loader_identity(source_orm_kls, rel_name, suffix)

    class _Loader(DataLoader):
        async def batch_load_fn(self, keys):
            effective_fields = _get_effective_query_fields(
                self,
                self.target_dto_kls,
                extra_fields=[self.target_field_name],
            )
            effective_fields = _sanitize_only_fields(self, self.target_orm_kls, effective_fields)

            queryset = self.target_orm_kls.filter(
                **{f"{self.target_field_name}__in": list(set(keys))}
            )
            queryset = _maybe_use_db(queryset, self.using_db)
            queryset = _apply_filters(queryset, self.filters)
            queryset = _apply_only(queryset, effective_fields)
            rows = await queryset

            lookup = {getattr(row, self.target_field_name): row for row in rows}
            return [
                lookup[k] if k in lookup else None
                for k in keys
            ]

    _Loader.target_orm_kls = target_orm_kls
    _Loader.target_dto_kls = target_dto_kls
    _Loader.target_field_name = target_field_name
    _Loader.using_db = staticmethod(using_db) if callable(using_db) else using_db
    _Loader.filters = filters

    return _finalize_loader_class(_Loader, identity, prefix="TO")


def create_many_to_one_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_remote_field_name: str,
    using_db: Any = None,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    return _create_lookup_loader(
        source_orm_kls=source_orm_kls,
        rel_name=rel_name,
        target_orm_kls=target_orm_kls,
        target_dto_kls=target_dto_kls,
        target_field_name=target_remote_field_name,
        using_db=using_db,
        filters=filters,
        suffix="M2O",
    )


def create_one_to_many_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_relation_field_name: str,
    using_db: Any = None,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    identity = _build_loader_identity(source_orm_kls, rel_name, "O2M")

    class _Loader(DataLoader):
        async def batch_load_fn(self, keys):
            effective_fields = _get_effective_query_fields(
                self,
                self.target_dto_kls,
                extra_fields=[self.target_relation_field_name],
            )
            effective_fields = _sanitize_only_fields(self, self.target_orm_kls, effective_fields)

            queryset = self.target_orm_kls.filter(
                **{f"{self.target_relation_field_name}__in": list(set(keys))}
            )
            queryset = _maybe_use_db(queryset, self.using_db)
            queryset = _apply_filters(queryset, self.filters)
            queryset = _apply_only(queryset, effective_fields)
            rows = await queryset

            grouped = defaultdict(list)
            for row in rows:
                grouped[getattr(row, self.target_relation_field_name)].append(row)

            return [grouped.get(k, []) for k in keys]

    _Loader.target_orm_kls = target_orm_kls
    _Loader.target_dto_kls = target_dto_kls
    _Loader.target_relation_field_name = target_relation_field_name
    _Loader.using_db = staticmethod(using_db) if callable(using_db) else using_db
    _Loader.filters = filters

    return _finalize_loader_class(_Loader, identity, prefix="TO")


def create_reverse_one_to_one_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_relation_field_name: str,
    using_db: Any = None,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    return _create_lookup_loader(
        source_orm_kls=source_orm_kls,
        rel_name=rel_name,
        target_orm_kls=target_orm_kls,
        target_dto_kls=target_dto_kls,
        target_field_name=target_relation_field_name,
        using_db=using_db,
        filters=filters,
        suffix="RO2O",
    )


def create_many_to_many_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    source_match_field_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    using_db: Any = None,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    from tortoise.query_utils import Prefetch

    identity = _build_loader_identity(source_orm_kls, rel_name, "M2M")
    prefetch_attr = f"__pydantic_resolve_{identity}"

    class _Loader(DataLoader):
        async def batch_load_fn(self, keys):
            target_pk_field = self.target_orm_kls._meta.pk_attr
            effective_fields = _get_effective_query_fields(
                self,
                self.target_dto_kls,
                extra_fields=[target_pk_field],
            )
            effective_fields = _sanitize_only_fields(self, self.target_orm_kls, effective_fields)

            source_queryset = self.source_orm_kls.filter(
                **{f"{self.source_match_field_name}__in": list(set(keys))}
            )
            source_queryset = _maybe_use_db(source_queryset, self.using_db)

            target_queryset = self.target_orm_kls.all()
            target_queryset = _maybe_use_db(target_queryset, self.using_db)
            target_queryset = _apply_filters(target_queryset, self.filters)
            target_queryset = _apply_only(target_queryset, effective_fields)

            source_rows = await source_queryset.prefetch_related(
                Prefetch(rel_name, queryset=target_queryset, to_attr=self.prefetch_attr)
            )

            grouped = {}
            for row in source_rows:
                grouped[getattr(row, self.source_match_field_name)] = [
                    item
                    for item in getattr(row, self.prefetch_attr, [])
                ]

            return [grouped.get(k, []) for k in keys]

    _Loader.source_orm_kls = source_orm_kls
    _Loader.target_orm_kls = target_orm_kls
    _Loader.target_dto_kls = target_dto_kls
    _Loader.source_match_field_name = source_match_field_name
    _Loader.using_db = staticmethod(using_db) if callable(using_db) else using_db
    _Loader.filters = filters
    _Loader.prefetch_attr = prefetch_attr

    return _finalize_loader_class(_Loader, identity, prefix="TO")
