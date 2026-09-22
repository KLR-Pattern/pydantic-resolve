"""Regression tests: extra fields declared on a DefineSubset class body under Python 3.14.

Python 3.14 (PEP 649/749) removed ``__annotations__`` from the raw class namespace
handed to a metaclass; annotations are now lazy via ``__annotate_func__`` and only
materialize after ``type.__new__``. ``SubsetMeta`` never calls ``type.__new__`` with
the original namespace (it builds the model via ``create_model``), so
``_extract_extra_fields_from_namespace`` used to see an empty dict on 3.14 and
silently drop every field declared directly on the DefineSubset class body —
including its AutoLoad hidden-FK injection and the duplicate-field validation.

``_get_namespace_annotations`` now evaluates ``__annotate_func__`` on 3.14+ and
falls back to ``__annotations__`` on older versions. These tests pin that behavior.
"""

from typing import Optional, Annotated

import pytest
from pydantic import BaseModel

from pydantic_resolve import AutoLoad, DefineSubset, Relationship, base_entity

BASE_ENTITY = base_entity()


class Space(BaseModel):
    id: int
    name: str


class Item(BaseModel, BASE_ENTITY):
    __relationships__ = [
        Relationship(fk="space_id", name="space", target=Space),
    ]

    id: int
    name: str
    space_id: int


class TestDefineSubsetExtraFieldsOnPy314:
    """Fields added directly to a DefineSubset class must become real model fields."""

    def test_plain_extra_field_is_detected(self):
        class ItemSubset(DefineSubset):
            __pydantic_resolve_subset__ = (Item, ["id", "name"])

            remark: str = ""

        assert "remark" in ItemSubset.model_fields

        instance = ItemSubset(id=1, name="n", remark="r")
        assert instance.remark == "r"

    def test_explicit_autoload_field_injects_hidden_fk(self):
        class ItemSubset(DefineSubset):
            __pydantic_resolve_subset__ = (Item, ["id", "name"])

            space: Annotated[Optional[Space], AutoLoad()] = None

        assert "space" in ItemSubset.model_fields

        # detecting the AutoLoad field also triggers hidden FK injection
        assert "space_id" in ItemSubset.model_fields
        assert ItemSubset.model_fields["space_id"].exclude is True
        assert ItemSubset.model_fields["space_id"].default is None

    def test_implicit_autoload_field_injects_hidden_fk(self):
        # field name matches a relationship name -> implicit AutoLoad
        class ItemSubset(DefineSubset):
            __pydantic_resolve_subset__ = (Item, ["id", "name"])

            space: Optional[Space] = None

        assert "space" in ItemSubset.model_fields
        assert "space_id" in ItemSubset.model_fields

    def test_duplicate_extra_field_still_rejected(self):
        with pytest.raises(ValueError, match="duplicates subset field"):

            class BadSubset(DefineSubset):
                __pydantic_resolve_subset__ = (Item, ["id", "name"])

                name: str  # collides with subset field 'name'
