from typing import Any, Type, Union, Annotated
from uuid import UUID
try:  # Python 3.10+ provides PEP 604 unions using types.UnionType
    from types import UnionType as _UnionType
except ImportError:  # pragma: no cover - prior to 3.10
    _UnionType = ()  # sentinel so membership tests still work

# Python <3.12 compatibility: TypeAliasType exists only from 3.12 (PEP 695)
try:  # pragma: no cover - import guard
    from typing import TypeAliasType  # type: ignore
except Exception:  # pragma: no cover
    class _DummyTypeAliasType:  # minimal sentinel so isinstance checks are safe
        pass
    TypeAliasType = _DummyTypeAliasType  # type: ignore

import inspect
import typing
from typing import get_origin, get_args


def _is_optional(annotation):
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (Union, _UnionType) and type(None) in args:
        return True
    return False


def _is_list(annotation):
    return get_origin(annotation) is list


def shelling_type(tp):
    while _is_optional(tp) or _is_list(tp):
        tp = tp.__args__[0]
    return tp


def get_core_types(tp):
    """
    - get the core type
    - always return a tuple of core types
    """
    # Helpers
    def _unwrap_alias(t):
        """Unwrap PEP 695 type aliases by following __value__ repeatedly."""
        while isinstance(t, TypeAliasType) or (
            t.__class__.__name__ == 'TypeAliasType' and hasattr(t, '__value__')
        ):
            try:
                t = t.__value__
            except Exception:  # pragma: no cover - defensive
                break
        return t

    def _enqueue(items, q):
        for it in items:
            if it is not type(None):  # skip None in unions
                q.append(it)

    # Queue-based shelling to reach concrete core types
    queue: list[object] = [tp]
    result: list[object] = []

    while queue:
        cur = queue.pop(0)
        if cur is type(None):
            continue

        cur = _unwrap_alias(cur)

        # Handle Annotated[T, ...] as a shell
        if get_origin(cur) is Annotated:
            args = get_args(cur)
            if args:
                queue.append(args[0])
            continue

        # Handle Union / Optional / PEP 604 UnionType
        orig = get_origin(cur)
        if orig in (Union, _UnionType):
            args = get_args(cur)
            # push all non-None members back for further shelling
            _enqueue(args, queue)
            continue

        # Handle list shells
        if _is_list(cur):
            args = getattr(cur, "__args__", ())
            if args:
                queue.append(args[0])
            continue

        # If still an alias-like wrapper, unwrap again and re-process
        _cur2 = _unwrap_alias(cur)
        if _cur2 is not cur:
            queue.append(_cur2)
            continue

        # Otherwise treat as a concrete core type (could be a class, typing.Final, etc.)
        result.append(cur)

    return tuple(result)


def _own_annotations(klass: type) -> dict:
    """Annotations defined on klass itself.

    Python 3.14 (PEP 649) no longer stores a dict at
    ``klass.__dict__['__annotations__']``. The class still exposes its own
    annotations through the ``__annotations__`` descriptor. Walking that
    descriptor, instead of only the dict entry, keeps loader ``_context``
    detection and copied DataLoader fields working.
    """
    raw = klass.__dict__.get("__annotations__")
    if isinstance(raw, dict):
        return raw
    try:
        ann = klass.__annotations__
    except Exception:
        return {}
    if isinstance(ann, dict):
        return ann
    return {}


def get_class_field_annotations(cls: Type):
    annotations = {}
    for klass in reversed(cls.__mro__):
        own = _own_annotations(klass)
        if own:
            annotations.update(own)
    return annotations.keys()


def get_type(v):
    return v.annotation


_BUILTIN_TYPES = {
    "int": int,
    "float": float,
    "str": str,
    "bool": bool,
    "dict": dict,
    "list": list,
    "UUID": UUID,
}


def _try_eval_simple_type(anno_str: str) -> Any:
    """Resolve safe builtin string annotations when possible."""
    stripped = anno_str.strip().strip("'\"")
    return _BUILTIN_TYPES.get(stripped, anno_str)


def _resolve_single_annotation(anno: Any, globalns: dict[str, Any]) -> Any:
    """Resolve one annotation without forcing all function hints to resolve."""
    if anno is inspect.Parameter.empty or not isinstance(anno, str):
        return anno

    class _AnnotationBox:
        pass

    _AnnotationBox.__annotations__ = {"value": anno}
    return typing.get_type_hints(
        _AnnotationBox,
        globalns=globalns,
        localns={},
        include_extras=True,
    )["value"]


def _resolve_function_type_hints(func: Any) -> dict[str, Any]:
    """Resolve type hints, falling back to per-annotation resolution."""
    try:
        return typing.get_type_hints(func, include_extras=True)
    except Exception:
        pass

    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return {}

    hints: dict[str, Any] = {}
    globalns = getattr(func, "__globals__", {})
    for name, param in sig.parameters.items():
        if param.annotation is inspect.Parameter.empty:
            continue
        try:
            hints[name] = _resolve_single_annotation(param.annotation, globalns)
        except Exception:
            pass

    if sig.return_annotation is not inspect.Signature.empty:
        try:
            hints["return"] = _resolve_single_annotation(sig.return_annotation, globalns)
        except Exception:
            pass

    return hints


def get_return_annotation(method) -> type | None:
    """Get the return type annotation of a method.

    Handles classmethod, ``from __future__ import annotations``,
    and simple string annotations.  Returns ``None`` when no return
    annotation is available.
    """
    func = method.__func__ if isinstance(method, classmethod) else method

    ret = _resolve_function_type_hints(func).get("return")
    if ret is not None:
        return ret

    # Fallback: inspect signature (covers unresolved string annotations)
    try:
        sig = inspect.signature(func)
    except (ValueError, TypeError):
        return None

    anno = sig.return_annotation
    if anno is inspect.Signature.empty:
        return None

    if isinstance(anno, str):
        return _try_eval_simple_type(anno)

    return anno


