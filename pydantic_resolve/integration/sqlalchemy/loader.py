from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any, Callable

from aiodataloader import DataLoader

from pydantic_resolve.integration.common import (
    _build_loader_identity,
    _finalize_loader_class,
    _get_effective_query_fields,
)

if TYPE_CHECKING:
    from pydantic_resolve.graphql.pagination.types import PageArgs


def _row_get(row: Any, key: str) -> Any:
    mapping = getattr(row, "_mapping", None)
    if mapping is not None and key in mapping:
        return mapping[key]
    return getattr(row, key)


def _apply_filters(stmt: Any, filters: list[Any] | None) -> Any:
    if filters:
        return stmt.where(*filters)
    return stmt


def _apply_load_only(
    stmt: Any,
    target_orm_kls: type,
    fields: list[str],
) -> Any:
    from sqlalchemy import inspect as sa_inspect
    from sqlalchemy.orm import load_only

    mapper = sa_inspect(target_orm_kls)
    rel_keys = {rel.key for rel in mapper.relationships}
    attrs = [
        getattr(target_orm_kls, field)
        for field in fields
        if hasattr(target_orm_kls, field) and field not in rel_keys
    ]
    if attrs:
        return stmt.options(load_only(*attrs))
    return stmt


def _create_lookup_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_col_name: str,
    session_factory: Callable,
    filters: list[Any] | None = None,
    suffix: str,
) -> type[DataLoader]:
    """Shared implementation for M2O and R-O2O lookup loaders."""
    class _Loader(DataLoader):
        async def batch_load_fn(self, keys):
            from sqlalchemy import select

            effective_fields = _get_effective_query_fields(
                self,
                self.target_dto_kls,
                extra_fields=[self.target_col_name],
            )

            async with self.session_factory() as session:
                stmt = select(self.target_orm_kls)
                stmt = _apply_load_only(stmt, self.target_orm_kls, effective_fields)
                stmt = stmt.where(
                    getattr(self.target_orm_kls, self.target_col_name).in_(keys)
                )
                stmt = _apply_filters(stmt, self.filters)
                rows = (await session.scalars(stmt)).all()

            lookup = {getattr(row, self.target_col_name): row for row in rows}
            return [
                lookup[k] if k in lookup else None
                for k in keys
            ]

    _Loader.target_orm_kls = target_orm_kls
    _Loader.target_dto_kls = target_dto_kls
    _Loader.target_col_name = target_col_name
    _Loader.session_factory = staticmethod(session_factory)
    _Loader.filters = filters

    return _finalize_loader_class(
        _Loader,
        _build_loader_identity(source_orm_kls, rel_name, suffix),
        prefix="SA",
    )


def create_many_to_one_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_remote_col_name: str,
    session_factory: Callable,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    return _create_lookup_loader(
        source_orm_kls=source_orm_kls,
        rel_name=rel_name,
        target_orm_kls=target_orm_kls,
        target_dto_kls=target_dto_kls,
        target_col_name=target_remote_col_name,
        session_factory=session_factory,
        filters=filters,
        suffix="M2O",
    )


def create_one_to_many_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_fk_col_name: str,
    session_factory: Callable,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    class _Loader(DataLoader):
        async def batch_load_fn(self, keys):
            from sqlalchemy import select

            effective_fields = _get_effective_query_fields(
                self,
                self.target_dto_kls,
                extra_fields=[self.target_fk_col_name],
            )

            async with self.session_factory() as session:
                stmt = select(self.target_orm_kls)
                stmt = _apply_load_only(stmt, self.target_orm_kls, effective_fields)
                stmt = stmt.where(
                    getattr(self.target_orm_kls, self.target_fk_col_name).in_(keys)
                )
                stmt = _apply_filters(stmt, self.filters)
                rows = (await session.scalars(stmt)).all()

            grouped = defaultdict(list)
            for row in rows:
                grouped[getattr(row, self.target_fk_col_name)].append(row)

            return [grouped.get(k, []) for k in keys]

    _Loader.target_orm_kls = target_orm_kls
    _Loader.target_dto_kls = target_dto_kls
    _Loader.target_fk_col_name = target_fk_col_name
    _Loader.session_factory = staticmethod(session_factory)
    _Loader.filters = filters

    return _finalize_loader_class(
        _Loader,
        _build_loader_identity(source_orm_kls, rel_name, "O2M"),
        prefix="SA",
    )


def create_reverse_one_to_one_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_fk_col_name: str,
    session_factory: Callable,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    return _create_lookup_loader(
        source_orm_kls=source_orm_kls,
        rel_name=rel_name,
        target_orm_kls=target_orm_kls,
        target_dto_kls=target_dto_kls,
        target_col_name=target_fk_col_name,
        session_factory=session_factory,
        filters=filters,
        suffix="RO2O",
    )


def create_many_to_many_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    secondary_table: Any,
    secondary_local_col_name: str,
    secondary_remote_col_name: str,
    target_match_col_name: str,
    session_factory: Callable,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    class _Loader(DataLoader):
        async def batch_load_fn(self, keys):
            from sqlalchemy import select

            effective_fields = _get_effective_query_fields(
                self,
                self.target_dto_kls,
                extra_fields=[self.target_match_col_name],
            )

            async with self.session_factory() as session:
                join_stmt = select(self.secondary_table).where(
                    getattr(self.secondary_table.c, self.secondary_local_col_name).in_(keys)
                )
                join_rows = (await session.execute(join_stmt)).all()

                target_keys = list(
                    {_row_get(row, self.secondary_remote_col_name) for row in join_rows}
                )
                if not target_keys:
                    self._effective_query_fields = effective_fields
                    return [[] for _ in keys]

                target_stmt = select(self.target_orm_kls)
                target_stmt = _apply_load_only(target_stmt, self.target_orm_kls, effective_fields)
                target_stmt = target_stmt.where(
                    getattr(self.target_orm_kls, self.target_match_col_name).in_(target_keys)
                )
                target_stmt = _apply_filters(target_stmt, self.filters)
                target_rows = (await session.scalars(target_stmt)).all()

            target_map = {
                getattr(row, self.target_match_col_name): row
                for row in target_rows
            }

            grouped = defaultdict(list)
            for join_row in join_rows:
                target_obj = target_map.get(_row_get(join_row, self.secondary_remote_col_name))
                if target_obj is not None:
                    grouped[_row_get(join_row, self.secondary_local_col_name)].append(target_obj)

            return [grouped.get(k, []) for k in keys]

    _Loader.target_orm_kls = target_orm_kls
    _Loader.target_dto_kls = target_dto_kls
    _Loader.secondary_table = secondary_table
    _Loader.secondary_local_col_name = secondary_local_col_name
    _Loader.secondary_remote_col_name = secondary_remote_col_name
    _Loader.target_match_col_name = target_match_col_name
    _Loader.session_factory = staticmethod(session_factory)
    _Loader.filters = filters

    return _finalize_loader_class(
        _Loader,
        _build_loader_identity(source_orm_kls, rel_name, "M2M"),
        prefix="SA",
    )


def create_page_many_to_many_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    secondary_table: Any,
    secondary_local_col_name: str,
    secondary_remote_col_name: str,
    target_match_col_name: str,
    sort_field: str = "id",
    pk_col_name: str = "id",
    session_factory: Callable,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    """Create a loader that paginates per-parent for M2M using ROW_NUMBER().

    Like create_page_one_to_many_loader but works through a secondary
    (association) table.  The PARTITION BY uses the secondary table's
    local column (the source-side FK in the join table).

    SQL strategy:
        SELECT * FROM (
            SELECT target.*,
                   secondary.local_col,
                   ROW_NUMBER() OVER (
                       PARTITION BY secondary.local_col
                       ORDER BY sort_col, pk_col
                   ) AS __rn,
                   COUNT(*) OVER (
                       PARTITION BY secondary.local_col
                   ) AS __tc
            FROM target
            JOIN secondary ON target.match_col = secondary.remote_col
            WHERE secondary.local_col IN (:fk_values)
        ) sub WHERE __rn BETWEEN :start AND :end
    """

    class _Loader(DataLoader):
        async def batch_load_fn(self, keys):
            from sqlalchemy import select, func

            if not keys:
                return []

            first_cmd = keys[0]
            page_args: PageArgs = first_cmd.page_args
            fk_values = [cmd.fk_value for cmd in keys]

            effective_fields = _get_effective_query_fields(
                self,
                self.target_dto_kls,
                extra_fields=[self.target_match_col_name, self.sort_field, self.pk_col_name],
            )

            effective_limit = page_args.effective_limit
            start = page_args.offset + 1
            end = start + effective_limit

            async with self.session_factory() as session:
                sec_local_col = getattr(self.secondary_table.c, self.secondary_local_col_name)
                sec_remote_col = getattr(self.secondary_table.c, self.secondary_remote_col_name)
                target_match_col = getattr(self.target_orm_kls, self.target_match_col_name)
                sort_col = getattr(self.target_orm_kls, self.sort_field)
                pk_col = getattr(self.target_orm_kls, self.pk_col_name)

                rn_label = "_pr_rn"
                tc_label = "_pr_tc"

                # Inner query: join target with secondary, compute window functions
                inner = select(
                    self.target_orm_kls,
                    sec_local_col.label(self.secondary_local_col_name),
                    func.row_number().over(
                        partition_by=sec_local_col,
                        order_by=[sort_col, pk_col],
                    ).label(rn_label),
                    func.count().over(
                        partition_by=sec_local_col,
                    ).label(tc_label),
                ).join(
                    self.secondary_table,
                    target_match_col == sec_remote_col,
                ).where(
                    sec_local_col.in_(fk_values),
                )
                inner = _apply_load_only(inner, self.target_orm_kls, effective_fields)
                inner = _apply_filters(inner, self.filters)
                subq = inner.subquery()

                rn_col = subq.c[rn_label]
                sec_local_sub = subq.c[self.secondary_local_col_name]
                sort_col_sub = subq.c[self.sort_field]
                pk_col_sub = subq.c[self.pk_col_name]

                outer = select(subq).where(rn_col.between(start, end)).order_by(
                    sec_local_sub, sort_col_sub, pk_col_sub,
                )
                rows = (await session.execute(outer)).all()

                # Group results by source FK
                grouped = defaultdict(list)
                total_counts: dict[Any, int] = {}
                for row in rows:
                    row_dict = row._mapping
                    fk_val = row_dict[self.secondary_local_col_name]
                    rn = row_dict[rn_label]
                    tc = row_dict[tc_label]
                    grouped[fk_val].append((row_dict, rn))
                    total_counts[fk_val] = tc

                # Fallback for parents with offset > total
                missing_fks = [cmd.fk_value for cmd in keys if cmd.fk_value not in total_counts]
                if missing_fks:
                    count_q = (
                        select(sec_local_col, func.count().label(tc_label))
                        .where(sec_local_col.in_(missing_fks))
                    )
                    count_q = _apply_filters(count_q, self.filters)
                    count_q = count_q.group_by(sec_local_col)
                    for row in (await session.execute(count_q)).all():
                        total_counts[row[0]] = row[1]

                return [
                    _build_page_result(
                        rows=[r for r, _ in grouped.get(cmd.fk_value, [])],
                        page_args=page_args,
                        total_count=total_counts.get(cmd.fk_value, 0),
                        has_next_page=total_counts.get(cmd.fk_value, 0) >= end if cmd.fk_value in total_counts else False,
                    )
                    for cmd in keys
                ]

    _Loader.target_orm_kls = target_orm_kls
    _Loader.target_dto_kls = target_dto_kls
    _Loader.secondary_table = secondary_table
    _Loader.secondary_local_col_name = secondary_local_col_name
    _Loader.secondary_remote_col_name = secondary_remote_col_name
    _Loader.target_match_col_name = target_match_col_name
    _Loader.sort_field = sort_field
    _Loader.pk_col_name = pk_col_name
    _Loader.session_factory = staticmethod(session_factory)
    _Loader.filters = filters

    return _finalize_loader_class(
        _Loader,
        _build_loader_identity(source_orm_kls, rel_name, "PM2M"),
        prefix="SA",
    )


def create_page_one_to_many_loader(
    *,
    source_orm_kls: type,
    rel_name: str,
    target_orm_kls: type,
    target_dto_kls: type,
    target_fk_col_name: str,
    sort_field: str = "id",
    pk_col_name: str = "id",
    session_factory: Callable,
    filters: list[Any] | None = None,
) -> type[DataLoader]:
    """Create a loader that paginates per-parent using ROW_NUMBER().

    The batch_load_fn receives PageLoadCommand keys. All keys in a
    single batch share the same PageArgs (guaranteed by GraphQL
    query structure).

    SQL strategy:
        SELECT * FROM (
            SELECT *,
                   ROW_NUMBER() OVER (PARTITION BY fk_col ORDER BY sort_field) AS __rn
            FROM target_table
            WHERE fk_col IN (:fk_values)
        ) sub WHERE __rn BETWEEN :start AND :end

    Returns Page dict structures keyed by fk_value.
    """

    class _Loader(DataLoader):
        async def batch_load_fn(self, keys):
            from sqlalchemy import select, func

            if not keys:
                return []

            # All keys share the same pagination args
            first_cmd = keys[0]
            page_args: PageArgs = first_cmd.page_args
            fk_values = [cmd.fk_value for cmd in keys]

            effective_fields = _get_effective_query_fields(
                self,
                self.target_dto_kls,
                extra_fields=[self.target_fk_col_name, self.sort_field, self.pk_col_name],
            )

            # Use offset from page_args instead of cursor decoding
            effective_limit = page_args.effective_limit
            start = page_args.offset + 1
            end = start + effective_limit

            async with self.session_factory() as session:
                # Build base query with ROW_NUMBER window function
                fk_col = getattr(self.target_orm_kls, self.target_fk_col_name)
                sort_col = getattr(self.target_orm_kls, self.sort_field)

                rn_label = "_pr_rn"
                tc_label = "_pr_tc"

                pk_col = getattr(self.target_orm_kls, self.pk_col_name)

                row_num_col = func.row_number().over(
                    partition_by=fk_col,
                    order_by=[sort_col, pk_col],
                ).label(rn_label)

                total_count_col = func.count().over(
                    partition_by=fk_col,
                ).label(tc_label)

                inner = select(
                    self.target_orm_kls,
                    row_num_col,
                    total_count_col,
                )
                inner = _apply_load_only(inner, self.target_orm_kls, effective_fields)
                inner = inner.where(fk_col.in_(fk_values))
                inner = _apply_filters(inner, self.filters)
                subq = inner.subquery()

                rn_col = subq.c[rn_label]

                fk_col_sub = subq.c[self.target_fk_col_name]
                sort_col_sub = subq.c[self.sort_field]
                pk_col_sub = subq.c[self.pk_col_name]

                outer = select(subq).where(rn_col.between(start, end)).order_by(
                    fk_col_sub, sort_col_sub, pk_col_sub,
                )

                rows = (await session.execute(outer)).all()

                # Group results
                grouped = defaultdict(list)
                total_counts: dict[Any, int] = {}
                fk_col_name = self.target_fk_col_name
                for row in rows:
                    row_dict = row._mapping
                    rn = row_dict[rn_label]
                    tc = row_dict[tc_label]
                    fk_val = row_dict[fk_col_name]
                    grouped[fk_val].append((row_dict, rn))
                    total_counts[fk_val] = tc

                # Fallback: when offset exceeds total_count for a parent,
                # no rows are returned and total_counts is missing that key.
                # Query counts separately for those parents.
                missing_fks = [cmd.fk_value for cmd in keys if cmd.fk_value not in total_counts]
                if missing_fks:
                    count_q = (
                        select(fk_col, func.count().label(tc_label))
                        .where(fk_col.in_(missing_fks))
                    )
                    count_q = _apply_filters(count_q, self.filters)
                    count_q = count_q.group_by(fk_col)
                    for row in (await session.execute(count_q)).all():
                        total_counts[row[0]] = row[1]

                return [
                    _build_page_result(
                        rows=[r for r, _ in grouped.get(cmd.fk_value, [])],
                        page_args=page_args,
                        total_count=total_counts.get(cmd.fk_value, 0),
                        has_next_page=total_counts.get(cmd.fk_value, 0) >= end if cmd.fk_value in total_counts else False,
                    )
                    for cmd in keys
                ]

    _Loader.target_orm_kls = target_orm_kls
    _Loader.target_dto_kls = target_dto_kls
    _Loader.target_fk_col_name = target_fk_col_name
    _Loader.sort_field = sort_field
    _Loader.pk_col_name = pk_col_name
    _Loader.session_factory = staticmethod(session_factory)
    _Loader.filters = filters

    return _finalize_loader_class(
        _Loader,
        _build_loader_identity(source_orm_kls, rel_name, "PO2M"),
        prefix="SA",
    )


def _build_page_result(
    rows: list,
    page_args: PageArgs,
    total_count: int | None,
    has_next_page: bool,
) -> dict:
    """Build a Page result dict from queried rows."""
    from pydantic_resolve.graphql.pagination.types import Pagination

    effective_limit = page_args.effective_limit

    # Trim to effective page size (we may have fetched effective_limit + 1)
    page_rows = rows[:effective_limit]

    pagination = Pagination(
        has_more=has_next_page,
        total_count=total_count,
    )

    result = {
        "items": page_rows,
        "pagination": pagination,
    }
    return result
