"""
Test entities with scalar Literal fields for GraphQL type mapping tests.

Covers every Literal flavor that must map to a GraphQL scalar: int, str,
bool, and a None member (nullable). Used by test_literal_type_mapping.py.
"""

from typing import List, Literal
from pydantic import BaseModel
from pydantic_resolve import base_entity, query


BaseEntity = base_entity()


class TaskFilter(BaseModel):
    """Input model with a Literal field (rendered as an input type)."""
    status: Literal["open", "closed"] = "open"


class NullableFilter(BaseModel):
    """Input model whose Literal has a None member (nullable input field)."""
    note: Literal["draft", None] = None


class TaskLiteEntity(BaseModel, BaseEntity):
    """Entity mixing every scalar Literal flavor."""
    __relationships__ = []

    id: int
    priority: Literal[1, 2, 3] = 1
    status: Literal["open", "closed"] = "open"
    flag: Literal[True, False] = True
    # None member: the value may be null although this is not a Union.
    note: Literal["draft", None] = None

    @query
    async def get_all(cls) -> List["TaskLiteEntity"]:
        """Get all tasks."""
        return [TaskLiteEntity(id=1)]

    @query
    async def by_status(cls, status: Literal["open", "closed"]) -> List["TaskLiteEntity"]:
        """Get tasks by literal status."""
        return []

    @query
    async def search(cls, flt: TaskFilter) -> List["TaskLiteEntity"]:
        """Get tasks via a filter input."""
        return []

    @query
    async def search_note(cls, flt: NullableFilter) -> List["TaskLiteEntity"]:
        """Get tasks via a nullable-note filter input."""
        return []
