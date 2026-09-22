"""Regression tests: class-field annotation reading under Python 3.14 (PEP 649/749).

IMPORTANT: this module must NOT use ``from __future__ import annotations`` —
under PEP 563 Python 3.14 keeps the legacy eager string ``__annotations__`` in
the class ``__dict__``, which would hide the very code path under test. Plain
modules compute annotations lazily and store nothing in ``__dict__``, which used
to make ``get_class_field_annotations`` return nothing on 3.14 — silently
disabling loader ``_context`` detection (``_loader_requires_context``) and
``copy_dataloader_kls`` param inheritance.
"""

from aiodataloader import DataLoader

from pydantic_resolve.analysis import _loader_requires_context
from pydantic_resolve.utils.dataloader import copy_dataloader_kls
from pydantic_resolve.utils.types import get_class_field_annotations


class TestGetClassFieldAnnotationsOnPy314:
    def test_own_annotations_are_visible(self):
        class Sample:
            user_id: int
            permission: str

        assert set(get_class_field_annotations(Sample)) == {"user_id", "permission"}

    def test_no_annotations_returns_empty(self):
        class Empty:
            def method(self):
                pass

        assert list(get_class_field_annotations(Empty)) == []

    def test_mro_accumulation_and_override(self):
        class Base:
            user_id: int

        class Child(Base):
            permission: str

        assert set(get_class_field_annotations(Child)) == {"user_id", "permission"}

    def test_loader_requires_context_detection(self):
        class LoaderWithContext(DataLoader):
            _context: dict

            async def batch_load_fn(self, keys):
                return keys

        class LoaderWithoutContext(DataLoader):
            async def batch_load_fn(self, keys):
                return keys

        assert _loader_requires_context(LoaderWithContext) is True
        assert _loader_requires_context(LoaderWithoutContext) is False

    def test_copy_dataloader_kls_inherits_parent_annotations(self):
        class ParentLoader(DataLoader):
            user_id: int
            permission: str

            async def batch_load_fn(self, keys):
                return keys

        Copied = copy_dataloader_kls("Copied", ParentLoader)
        # includes DataLoader base annotations (batch, max_batch_size, cache) via MRO
        assert set(get_class_field_annotations(Copied)) >= {"user_id", "permission"}
        assert _loader_requires_context(ParentLoader) is False
