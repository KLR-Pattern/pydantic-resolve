from pydantic_resolve.utils.types import (
    _is_list,
    get_core_types,
    shelling_type,
    get_class_field_annotations,
    _is_optional,
    get_return_annotation,
)
from typing import Optional, Union, List, Dict, Tuple, Set
from pydantic import BaseModel
import pytest

@pytest.mark.parametrize(
    "annotation,expected",
    [
        (Optional[int], True),
        (Union[int, None], True),
        (Union[None, int], True),
        (int, False),
        (List[int], False),
        (Union[int, str], False),
        (Union[int, str, None], True),  # Union with None
        (Union[None, int, str], True),  # None at the beginning
    ]
)
def test_is_optional(annotation, expected):
    assert _is_optional(annotation) == expected


@pytest.mark.parametrize(
    "annotation,expected",
    [
        (List[int], True),
        (List[str], True),
        (List[Optional[int]], True),
        (int, False),
        (Optional[int], False),
        (Union[int, str], False),
        (Dict[str, int], False),
        (Tuple[int, str], False),
        (Set[int], False),
    ]
)
def test_is_list(annotation, expected):
    assert _is_list(annotation) == expected


@pytest.mark.parametrize(
    "tp,expected",
    [
        # Basic types
        (int, (int,)),
        (str, (str,)),
        (float, (float,)),
        
        # Optional types
        (Optional[int], (int,)),
        (Union[int, None], (int,)),
        (Union[None, int], (int,)),
        
        # List types
        (List[int], (int,)),
        (List[str], (str,)),
        (List[Optional[int]], (int,)),
        
        # Nested list types
        (List[List[int]], (int,)),
        (List[List[Optional[str]]], (str,)),
        
        # Union types (multiple non-None types)
        (Union[int, str], (int, str)),
        (Union[int, str, float], (int, str, float)),
        (Union[str, int], (str, int)),  # Order matters
        
        # Union with None
        (Union[int, str, None], (int, str)),
        (Union[None, int, str], (int, str)),
        (Union[int, None, str], (int, str)),
        
        # Complex nested types
        (Optional[List[int]], (int,)),
        (List[Union[int, str]], (int, str)),
        (Optional[List[Union[int, str]]], (int, str)),
        
        # Edge cases
        (Union[None], ()),  # Only None
        (List[Union[None]], ()),  # List of only None
    ]
)
def test_get_core_types(tp, expected):
    result = get_core_types(tp)
    # Compare as multisets: on Python 3.14 typing deduplicates subscripted
    # generics by set-equality (_tp_cache), so a union's member order is
    # decided by whichever same-member alias the process materialized first
    # (e.g. List[Union[int, str]] is List[Union[str, int]]). Order is not
    # part of get_core_types' contract.
    assert sorted(map(repr, result)) == sorted(map(repr, expected))


@pytest.mark.parametrize(
    "tp,expected",
    [
        # Basic unwrapping
        (Optional[int], int),
        (List[int], int),
        (Optional[List[int]], int),
        (List[Optional[int]], int),
        
        # Nested structures
        (List[List[int]], int),
        (Optional[List[List[str]]], str),
        
        # Simple types
        (int, int),
        (str, str),
    ]
)
def test_shelling_type(tp, expected):
    assert shelling_type(tp) == expected


def test_get_class_field_annotations():
    class TestClass:
        field1: int
        field2: str
        field3: Optional[float]
    
    annotations = get_class_field_annotations(TestClass)
    expected_fields = {'field1', 'field2', 'field3'}
    assert set(annotations) == expected_fields


def test_get_class_field_annotations_empty():
    class EmptyClass:
        pass
    
    annotations = get_class_field_annotations(EmptyClass)
    assert list(annotations) == []


def test_get_class_field_annotations_with_inheritance():
    class BaseClass:
        base_field: int

    class DerivedClass(BaseClass):
        derived_field: str

    # Annotations are merged across the MRO so subclasses inherit
    # their parents' annotated fields.
    base_annotations = get_class_field_annotations(BaseClass)
    derived_annotations = get_class_field_annotations(DerivedClass)

    assert set(base_annotations) == {'base_field'}
    assert set(derived_annotations) == {'base_field', 'derived_field'}


def test_get_class_field_annotations_subclass_with_no_own_fields():
    # Mirrors copy_dataloader_kls: `class NewLoader(Parent): pass`
    # must still expose the parent's annotated fields, otherwise
    # loader param injection in _create_loader_instance breaks.
    class Parent:
        user_id: int

    class Child(Parent):
        pass

    assert set(get_class_field_annotations(Child)) == {'user_id'}


def test_is_optional_compatibility():
    """Test that _is_optional works correctly across Python versions"""
    assert _is_optional(Optional[int])
    assert _is_optional(Union[int, None])
    assert _is_optional(Union[None, int])
    assert not _is_optional(int)
    assert not _is_optional(List[int])
    assert not _is_optional(Union[int, str])


# Additional edge case tests
def test_complex_nested_types():
    """Test complex nested type scenarios"""
    # Triple nested
    nested_type = List[List[List[int]]]
    assert get_core_types(nested_type) == (int,)
    
    # Mixed nesting with Optional
    mixed_type = Optional[List[Optional[List[Optional[int]]]]]
    assert get_core_types(mixed_type) == (int,)
    
    # Union in List
    union_in_list = List[Union[int, str, None]]
    assert get_core_types(union_in_list) == (int, str)


def test_union_edge_cases():
    """Test edge cases for Union types"""
    # Union with duplicate types (if this is valid in your type system)
    # Note: This might not be a real scenario, but testing robustness
    
    # Multiple None types (edge case)
    single_none = Union[type(None)]
    result = get_core_types(single_none)
    assert result == () or result == (type(None),)  # Depending on implementation
    
    # Large Union
    large_union = Union[int, str, float, bool, bytes]
    assert len(get_core_types(large_union)) == 5


@pytest.mark.parametrize(
    "annotation",
    [
        Dict[str, int],
        Tuple[int, str],
        Set[int],
        # Add more complex types if needed
    ]
)
def test_non_list_container_types(annotation):
    """Test that non-list container types are not treated as lists"""
    assert not _is_list(annotation)
    # These should return as single types
    assert get_core_types(annotation) == (annotation,)


# ── get_return_annotation tests ──


class _UserDTO(BaseModel):
    id: int
    name: str


class _SimpleService:
    @classmethod
    def get_user(cls, user_id: int) -> _UserDTO:
        ...

    @classmethod
    def list_users(cls) -> list[_UserDTO]:
        ...

    @classmethod
    def get_optional(cls) -> Optional[_UserDTO]:
        ...

    def regular_method(self) -> str:
        ...

    @classmethod
    def no_return_annotation(cls):
        ...

    @classmethod
    def none_return(cls) -> None:
        ...


def test_return_annotation_classmethod_dto():
    assert get_return_annotation(_SimpleService.get_user) is _UserDTO


def test_return_annotation_list():
    result = get_return_annotation(_SimpleService.list_users)
    assert result == list[_UserDTO]


def test_return_annotation_optional():
    result = get_return_annotation(_SimpleService.get_optional)
    assert result == Optional[_UserDTO]


def test_return_annotation_regular_method():
    assert get_return_annotation(_SimpleService.regular_method) is str


def test_return_annotation_none_return():
    assert get_return_annotation(_SimpleService.none_return) is type(None)


def test_return_annotation_missing():
    assert get_return_annotation(_SimpleService.no_return_annotation) is None


def test_return_annotation_plain_function():
    def greet(name: str) -> str:
        return f"hello {name}"

    assert get_return_annotation(greet) is str
