from typing import Any, Literal, get_args, get_origin, Annotated
from pydantic import BaseModel, create_model, model_validator
from pydantic.fields import FieldInfo
import copy
import pydantic_resolve.constant as const
from pydantic_resolve.utils.expose import ExposeAs
from pydantic_resolve.utils.collector import SendTo
from pydantic_resolve.utils.er_diagram import LoaderInfo, Relationship
from pydantic_resolve.utils import class_util
class SubsetConfig(BaseModel):
    kls: type[BaseModel]  # parent class

    # fields and omit_fields are exclusive
    fields: list[str] | Literal["all"] | None = None
    omit_fields: list[str] | None = None

    # set Field(exclude=True) for these fields
    excluded_fields: list[str] | None = None

    expose_as: list[tuple[str, str]] | None = None
    send_to: list[tuple[str, tuple[str, ...] | str]] | None = None

    @model_validator(mode='after')
    def validate_fields(self):
        if self.fields is not None and self.omit_fields is not None:
            raise ValueError("fields and omit_fields are exclusive")

        if self.fields is None and self.omit_fields is None:
            raise ValueError("fields or omit_fields must be provided")
        return self


def _extract_field_infos(kls: type[BaseModel], field_names: list[str]) -> dict[str, Any]:
    field_definitions = {}

    for field_name in field_names:
        field = kls.model_fields.get(field_name)
        if not field:
            raise AttributeError(f'field "{field_name}" does not exist in {kls.__name__}')

        field_definitions[field_name] = (field.annotation, field)

    return field_definitions


def _get_kls_config(kls: type[BaseModel]) -> Any:
    return getattr(kls, 'model_config', None)


def _get_kls_validators(kls: type[BaseModel], field_names: list[str]) -> dict[str, Any]:

    validators = {}
    decorators = getattr(kls, '__pydantic_decorators__', None)

    if decorators and hasattr(decorators, 'field_validators'):
        for validator_name, decorator_info in decorators.field_validators.items():
            # example: @field_validator('email', 'name') -> decorator_info.info.fields = ['email', 'name']
            if any(field in field_names for field in decorator_info.info.fields):
                validators[validator_name] = getattr(kls, validator_name)

    return validators


def _apply_validators(model_class: type[BaseModel], validators: dict[str, Any]) -> None:
    for name, validator in validators.items():
        setattr(model_class, name, validator)


def _get_namespace_annotations(namespace: dict[str, Any]) -> dict[str, Any]:
    """Read annotations from a raw class namespace across Python versions.

    Python 3.14 (PEP 649/749) no longer puts an eager ``__annotations__`` dict in
    the namespace handed to a metaclass; it stores a lazy ``__annotate_func__``
    instead, and the annotations dict only materializes after ``type.__new__``.
    SubsetMeta never passes the namespace through ``type.__new__`` (the model is
    built via ``create_model``), so the annotate function must be evaluated here.
    """
    annotate_func = namespace.get('__annotate_func__')
    if annotate_func is not None:
        from annotationlib import Format
        try:
            return annotate_func(Format.VALUE) or {}
        except NameError:
            # annotations referencing names not yet defined: fall back to
            # ForwardRef objects, mirroring how pydantic defers them
            return annotate_func(Format.FORWARDREF) or {}
    return namespace.get('__annotations__', None) or {}


def _extract_extra_fields_from_namespace(namespace: dict[str, Any], disallow: set[str]) -> dict[str, tuple[Any, Any]]:
    """Extract extra field definitions declared on the subset class body.

    Only annotated attributes are considered as Pydantic fields. The result is a
    dict mapping field name -> (annotation, default | Ellipsis) suitable for create_model.

    Args:
        namespace: The class namespace passed into the metaclass with attributes and annotations.
        disallow: Field names that are already selected from the parent and thus cannot be redefined here.

    Raises:
        ValueError: If an extra field duplicates a subset field name.
    """
    annotations: dict[str, Any] = _get_namespace_annotations(namespace)
    extras: dict[str, tuple[Any, Any]] = {}

    for fname, anno in annotations.items():
        if fname in (const.ENSURE_SUBSET_DEFINITION, const.ENSURE_SUBSET_DEFINITION_SHORT):
            continue
        if fname in disallow:
            raise ValueError(f'additional field "{fname}" duplicates subset field')
        default = namespace.get(fname, ...)
        extras[fname] = (anno, default)

    return extras


def _validate_subset_fields(fields: Any):
    if not isinstance(fields, (list, tuple)):
        raise TypeError('fields must be a list or tuple of field names')

    hit = set()
    for f in fields:
        if not isinstance(f, str):
            raise TypeError('each field name must be a string')
        else:
            if f in hit:
                raise ValueError(f'duplicate field name "{f}" in subset fields')
            hit.add(f)


def create_subset(parent: type[BaseModel], fields: list[str], name: str = "SubsetModel") -> type[BaseModel]:
    """
    Create a subset model from a parent BaseModel using Pydantic's create_model.

    Args:
        parent: Parent BaseModel class to create subset from
        fields: List of field names to include in subset
        name: Name of the new subset class (default: "SubsetModel")

    Returns:
        A new BaseModel class containing only the specified fields
    """
    if not issubclass(parent, BaseModel):
        raise TypeError('parent must be a pydantic BaseModel')

    _validate_subset_fields(fields)
    field_infos = _extract_field_infos(parent, fields)
    validators = _get_kls_validators(parent, fields)
    create_model_kwargs = {}

    config = _get_kls_config(parent)
    if config:
        create_model_kwargs['__config__'] = config

    subset_class = create_model(name, **field_infos, **create_model_kwargs)

    _apply_validators(subset_class, validators)
    setattr(subset_class, const.ENSURE_SUBSET_REFERENCE, parent)

    return subset_class


def _apply_config_modifiers_to_field(
    config: SubsetConfig,
    field_name: str,
    field_default: Any,
) -> Any:
    """Apply SubsetConfig modifiers (excluded_fields, expose_as, send_to) to a single field.

    Only deep-copies when at least one modifier matches. Returns the original
    object unchanged if no modifiers apply.
    """
    result = field_default
    modified = False

    if config.excluded_fields and field_name in config.excluded_fields:
        result = copy.deepcopy(result)
        result.exclude = True
        modified = True

    if config.expose_as:
        for ef_name, alias in config.expose_as:
            if ef_name == field_name:
                if not modified:
                    result = copy.deepcopy(result)
                    modified = True
                result.metadata.append(ExposeAs(alias))

    if config.send_to:
        for sf_name, target in config.send_to:
            if sf_name == field_name:
                if not modified:
                    result = copy.deepcopy(result)
                    modified = True
                result.metadata.append(SendTo(target))

    return result


def _collect_relationship_candidates_from_mro(
    parent_kls: type[BaseModel],
) -> dict[str, list[Relationship]] | None:
    """Collect candidate relationships visible to parent_kls.

    For inline base_entity() diagrams there is a single authoritative diagram.
    For external ErDiagram registrations, multiple entity configs may exist for the
    same parent class; those candidates are merged and ambiguity is checked lazily
    when a specific relationship is requested.
    """
    # Path 1: base_entity() diagram via MRO
    for base in parent_kls.__mro__:
        if hasattr(base, 'get_diagram') and hasattr(base, 'entities'):
            try:
                diagram = base.get_diagram()
                relationship_candidates: dict[str, list[Relationship]] = {}
                for entity_cfg in diagram.entities:
                    if not class_util.is_compatible_type(parent_kls, entity_cfg.kls):
                        continue
                    for rel in entity_cfg.relationships:
                        relationship_candidates.setdefault(rel.name, []).append(rel)
                return relationship_candidates or None
            except Exception:
                continue

    # Path 2: external ErDiagram registry
    from pydantic_resolve.utils.er_diagram import _er_entity_registry
    relationship_candidates: dict[str, list[Relationship]] = {}
    for base in parent_kls.__mro__:
        for entity_cfg in _er_entity_registry.get(base, []):
            if not class_util.is_compatible_type(parent_kls, entity_cfg.kls):
                continue
            for rel in entity_cfg.relationships:
                relationship_candidates.setdefault(rel.name, []).append(rel)

    return relationship_candidates or None


def _select_relationship(
    parent_kls: type[BaseModel],
    lookup_key: str,
    relationship_candidates: dict[str, list[Relationship]] | None,
) -> Relationship | None:
    """Select a relationship by name, rejecting ambiguous external mappings."""
    if relationship_candidates is None:
        return None

    candidates = relationship_candidates.get(lookup_key, [])
    if not candidates:
        return None

    fk_values = {rel.fk for rel in candidates}
    if len(fk_values) > 1:
        raise ValueError(
            f'Ambiguous external ErDiagram relationship "{lookup_key}" for '
            f'{parent_kls.__name__}: multiple foreign keys found {sorted(fk_values)}. '
            'DefineSubset cannot infer which external ErDiagram to use. '
            'Use base_entity().get_diagram(), select the FK field explicitly, '
            'or avoid conflicting external diagrams for the same model.'
        )

    return candidates[0]


def _inject_hidden_fk_field(
    field_definitions: dict[str, Any],
    parent_kls: type[BaseModel],
    fk: str,
) -> None:
    """Inject the FK field needed by AutoLoad if it is missing from the subset."""
    if fk in field_definitions or fk not in parent_kls.model_fields:
        return

    parent_field = parent_kls.model_fields[fk]
    field_definitions[fk] = (
        parent_field.annotation,
        FieldInfo(
            annotation=parent_field.annotation,
            default=None,
            exclude=True,
        ),
    )


def _auto_add_fk_fields(
    extra_fields: dict[str, tuple[Any, Any]],
    parent_kls: type[BaseModel],
    field_definitions: dict[str, Any],
) -> None:
    """Auto-add FK fields for fields that will trigger implicit or explicit AutoLoad.

    Phase 1 (explicit): Fields with LoaderInfo annotation — existing behavior.
    Phase 2 (implicit): Fields whose names match a relationship name in the ER Diagram,
    even without LoaderInfo annotation.

    Args:
        extra_fields: New fields declared on the DefineSubset class body.
        parent_kls: The parent BaseModel class being subsetted.
        field_definitions: Field definitions dict being built (mutated in-place).
    """
    relationship_candidates = _collect_relationship_candidates_from_mro(parent_kls)
    if relationship_candidates is None:
        return

    explicit_fields: set[str] = set()

    # === Phase 1: Explicit AutoLoad (LoaderInfo annotation) ===
    for fname, (anno, _default) in extra_fields.items():
        # Extract metadata from Annotated type
        origin = get_origin(anno)
        if origin is not Annotated:
            continue

        args = get_args(anno)
        for arg in args[1:]:  # skip first arg (the actual type)
            if not isinstance(arg, LoaderInfo):
                continue

            explicit_fields.add(fname)

            lookup_key = arg.origin if arg.origin else fname
            relationship = _select_relationship(parent_kls, lookup_key, relationship_candidates)
            if relationship is not None:
                _inject_hidden_fk_field(field_definitions, parent_kls, relationship.fk)
            break

    # === Phase 2: Implicit matching (field name == relationship name) ===
    for fname, (anno, _default) in extra_fields.items():
        if fname in explicit_fields:
            continue

        relationship = _select_relationship(parent_kls, fname, relationship_candidates)
        if relationship is None:
            continue

        _inject_hidden_fk_field(field_definitions, parent_kls, relationship.fk)


class SubsetMeta(type):
    def __new__(cls, name, bases, namespace, **kwargs):
        # subset_info is expected to be:
        # - a tuple of (parent_class, list_of_field_names)
        # - a SubsetConfig
        subset_info = namespace.get(const.ENSURE_SUBSET_DEFINITION) or namespace.get(const.ENSURE_SUBSET_DEFINITION_SHORT)

        # Allow defining the base marker class without warning, bypass
        if name == 'DefineSubset' and not bases:
            return super().__new__(cls, name, bases, namespace, **kwargs)

        if not subset_info:
            raise ValueError(f'Class {name} must define {const.ENSURE_SUBSET_DEFINITION} to use SubsetMeta')

        if isinstance(subset_info, SubsetConfig):
            parent_kls = subset_info.kls

            if subset_info.fields is not None:
                if subset_info.fields == "all":  # special value to include all fields
                    subset_fields = list(parent_kls.model_fields.keys())
                else:
                    subset_fields = subset_info.fields
            else:
                all_fields = list(parent_kls.model_fields.keys())
                omit_set = set(subset_info.omit_fields)
                subset_fields = [f for f in all_fields if f not in omit_set]
        else:
            parent_kls = subset_info[0]
            subset_fields = subset_info[1]

        _validate_subset_fields(subset_fields)
        field_infos = _extract_field_infos(parent_kls, subset_fields)

        if isinstance(subset_info, SubsetConfig):
            for fname, (annotation, field_info) in list(field_infos.items()):
                new_field_info = _apply_config_modifiers_to_field(subset_info, fname, field_info)
                if new_field_info is not field_info:
                    field_infos[fname] = (annotation, new_field_info)

        # Then extract extra fields defined in class body
        extra_fields = _extract_extra_fields_from_namespace(namespace, set(subset_fields))

        # Auto-add FK fields from AutoLoad annotations
        _auto_add_fk_fields(extra_fields, parent_kls, field_infos)

        # Parent validators and config should still apply to subset fields
        create_model_kwargs: dict[str, Any] = {}
        attributes_to_attach: dict[str, Any] = {}
        methods_to_attach: dict[str, Any] = {}

        validators = _get_kls_validators(parent_kls, subset_fields)
        config = _get_kls_config(parent_kls)
        if config:
            create_model_kwargs['__config__'] = config

        # Use the caller's module for better reprs and pickling
        create_model_kwargs['__module__'] = namespace.get('__module__', __name__)

        for ns_key, ns_value in namespace.items():
            # definition like __pydatnic_resolve_expose__ or __pydantic_resolve_collect__ will be kept, just in case.
            # except __pydantic_resolve_subset__  ( __subset__ will also be ignored)
            if ns_key.startswith('__pydantic_resolve') and ns_key != const.ENSURE_SUBSET_DEFINITION:
                attributes_to_attach[ns_key] = ns_value
                continue

            if callable(ns_value) and (ns_key.startswith(const.RESOLVE_PREFIX) or ns_key.startswith(const.POST_PREFIX)):
                methods_to_attach[ns_key] = ns_value

        field_definitions = {}
        field_definitions.update(field_infos)
        field_definitions.update(extra_fields)

        subset_class = create_model(
            name,
            **field_definitions,
            **create_model_kwargs
        )

        # Apply excluded_fields to auto-generated AutoLoad fields after create_model,
        # since create_model does not preserve exclude=True when processing
        # Annotated[..., AutoLoad()] annotations.
        if isinstance(subset_info, SubsetConfig) and subset_info.excluded_fields:
            for fname in subset_info.excluded_fields:
                if fname in subset_class.model_fields:
                    subset_class.model_fields[fname].exclude = True
            subset_class.model_rebuild(force=True)

        _apply_validators(subset_class, validators)

        for method_name, method in methods_to_attach.items():
            setattr(subset_class, method_name, method)
        for attr_name, attr_value in attributes_to_attach.items():
            setattr(subset_class, attr_name, attr_value)
        setattr(subset_class, const.ENSURE_SUBSET_REFERENCE, parent_kls)

        return subset_class


class DefineSubset(metaclass=SubsetMeta):
    pass
