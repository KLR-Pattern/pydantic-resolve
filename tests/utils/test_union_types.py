import sys
from pydantic_resolve.utils.types import (
    get_core_types, 
)
from typing import Optional, List
import pytest

@pytest.mark.parametrize(
    "tp,expected",
    [
        
        # Optional types
        (int | None, (int,)),
        (None | int, (int,)),

        # Union types (multiple non-None types)
        (int | str, (int, str)),
        (int | str | float, (int, str, float)),
        (str | int, (str, int)),  # Order matters

        # Union with None
        (int | str | None, (int, str)),
        (None | int | str, (int, str)),
        (int | None | str, (int, str)),
        
        # Complex nested types
        (List[int | str], (int, str)),
        (Optional[List[int | str]], (int, str)),
    ]
)
def test_get_core_types_3_10(tp, expected):
    result = get_core_types(tp)
    # Multiset comparison: on Python 3.14 typing's _tp_cache deduplicates
    # subscripted generics by set-equality, so union member order follows
    # whichever same-member alias was materialized first in the process.
    assert sorted(map(repr, result)) == sorted(map(repr, expected))


@pytest.mark.skipif(sys.version_info < (3, 12), reason="PEP 695 type aliases require Python 3.12+")
def test_union_type_alias_and_list():
    # Dynamically exec a type alias using the new syntax so test file stays valid on <3.12 (even though skipped)
    ns: dict = {}
    code = """
class A: ...
class B: ...

type MyAlias = A | B
"""
    exec(code, ns, ns)
    MyAlias = ns['MyAlias']
    A = ns['A']
    B = ns['B']

    # list[MyAlias] should yield (A, B)
    core = get_core_types(list[MyAlias])
    assert set(core) == {A, B}

    # Direct alias should also work
    core2 = get_core_types(MyAlias)
    assert set(core2) == {A, B}
