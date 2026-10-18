"""Shared, ORM-independent helpers for generated relationship loaders."""

from __future__ import annotations

import re

from aiodataloader import DataLoader


def _normalize_identifier(value: str) -> str:
    """Convert arbitrary names to valid class-name fragments."""
    normalized = re.sub(r"[^0-9a-zA-Z_]", "_", value)
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    return normalized or "anonymous"


def _build_loader_identity(source_orm_kls: type, rel_name: str, suffix: str) -> str:
    source_name = _normalize_identifier(getattr(source_orm_kls, "__name__", "source"))
    rel_part = _normalize_identifier(rel_name)
    return f"{source_name}_{rel_part}_{suffix}"


def _finalize_loader_class(
    cls: type[DataLoader],
    identity: str,
    prefix: str,
) -> type[DataLoader]:
    class_name = f"{prefix}_{identity}"
    cls.__name__ = class_name
    cls.__qualname__ = class_name
    # Keep the ORM module where the class was defined as part of its identity.
    return cls


def _dedupe_fields(fields: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for field in fields:
        if field not in seen:
            deduped.append(field)
            seen.add(field)
    return deduped


def _get_default_fields(target_dto_kls: type) -> list[str]:
    return list(target_dto_kls.model_fields.keys())


def _get_effective_query_fields(
    loader: DataLoader,
    target_dto_kls: type,
    extra_fields: list[str] | None = None,
) -> list[str]:
    query_meta = getattr(loader, "_query_meta", None)
    requested_fields = query_meta["fields"] if query_meta else _get_default_fields(target_dto_kls)
    effective_fields = _dedupe_fields([*requested_fields, *(extra_fields or [])])
    loader._effective_query_fields = effective_fields
    return effective_fields
