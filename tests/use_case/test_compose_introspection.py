"""Tests for compose_query introspection (``__schema`` / ``__type`` / ``__typename``).

Covers:
- Schema completeness (services, methods, args, DTO fields)
- Type system (Optional, list, Enum, recursion, shared DTOs)
- Mutation control
- Entry routing (Python auto-route, MCP rejection, partial introspection)
- GraphiQL canonical introspection query
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Optional

import pytest
from pydantic import BaseModel

from pydantic_resolve import query, mutation
from pydantic_resolve.use_case.business import UseCaseService
from pydantic_resolve.use_case.compose import ComposeError
from pydantic_resolve.use_case.compose_schema import method_sdl
from pydantic_resolve.use_case.introspection import (
    compose_introspect,
    is_introspection_query,
)
from pydantic_resolve.use_case.manager import UseCaseManager
from pydantic_resolve.use_case.mcp_server import (
    create_use_case_graphql_mcp_server,
)
from pydantic_resolve.use_case.manager import UseCaseAppConfig


# ──────────────────────────────────────────────────
# DTOs
# ──────────────────────────────────────────────────


class Color(Enum):
    RED = "red"
    GREEN = "green"


class OwnerDTO(BaseModel):
    id: int
    name: str


class TaskDTO(BaseModel):
    id: int
    title: str
    owner_id: int
    color: Color
    owner: Optional[OwnerDTO] = None
    subtasks: list["TaskDTO"] = []

    def resolve_owner(self):
        return OwnerDTO(id=self.owner_id, name=f"User{self.owner_id}")


TaskDTO.model_rebuild()


class SprintDTO(BaseModel):
    id: int
    name: str


class CreateTaskInput(BaseModel):
    title: str
    owner_id: int
    color: Color


# ──────────────────────────────────────────────────
# Edge-case DTOs + services (input-type coverage)
# ──────────────────────────────────────────────────


class CloneDTO(BaseModel):
    id: int
    title: str


class MDLInput(BaseModel):
    title: str
    owner_id: int


class FilterInput(BaseModel):
    keyword: Optional[str] = None
    limit: int = 10
    required: str


class InnerInput(BaseModel):
    value: int


class OuterInput(BaseModel):
    name: str
    inner: InnerInput


class OptionalInput(BaseModel):
    note: Optional[str] = None
    required: int


class ListInput(BaseModel):
    tags: list[str]


class CloneService(UseCaseService):
    """clone service."""

    @mutation
    async def clone(cls, payload: CloneDTO) -> CloneDTO:
        """clone."""
        return payload


class MDLService(UseCaseService):
    """mdl service."""

    @mutation
    async def create(cls, payload: MDLInput) -> str:
        """create."""
        return ""


class FilterService(UseCaseService):
    """filter service."""

    @query
    async def search(cls, filter: FilterInput) -> str:
        """search."""
        return ""


class NestedService(UseCaseService):
    """nested service."""

    @query
    async def foo(cls, payload: OuterInput) -> str:
        """foo."""
        return ""


class OptionalService(UseCaseService):
    """optional service."""

    @query
    async def foo(cls, payload: OptionalInput) -> str:
        """foo."""
        return ""


class ListService(UseCaseService):
    """list service."""

    @query
    async def foo(cls, payload: ListInput) -> str:
        """foo."""
        return ""


# ──────────────────────────────────────────────────
# Services
# ──────────────────────────────────────────────────


class SprintService(UseCaseService):
    """Sprint service."""

    @query
    async def list_sprints(cls) -> list[SprintDTO]:
        return [SprintDTO(id=1, name="A"), SprintDTO(id=2, name="B")]

    @query
    async def get_sprint(cls, sprint_id: int) -> Optional[SprintDTO]:
        return SprintDTO(id=sprint_id, name="X") if sprint_id == 1 else None


class TaskService(UseCaseService):
    """Task service."""

    @query
    async def list_tasks(cls, limit: int = 10) -> list[TaskDTO]:
        return [TaskDTO(id=1, title="t", owner_id=1, color=Color.RED)]

    @query
    async def get_task(cls, task_id: int) -> TaskDTO:
        return TaskDTO(id=task_id, title="t", owner_id=1, color=Color.RED)

    @mutation
    async def create_task(cls, title: str) -> TaskDTO:
        return TaskDTO(id=99, title=title, owner_id=1, color=Color.RED)

    @mutation
    async def create_task_with_input(cls, payload: CreateTaskInput) -> TaskDTO:
        return TaskDTO(
            id=100,
            title=payload.title,
            owner_id=payload.owner_id,
            color=payload.color,
        )


# ──────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────


def _make_manager(*, enable_mutation: bool = True) -> UseCaseManager:
    return UseCaseManager(
        apps=[
            UseCaseAppConfig(
                name="project",
                description="project",
                services=[SprintService, TaskService],
                enable_mutation=enable_mutation,
            ),
        ],
    )


def _introspect_types(app) -> dict[str, dict]:
    """Return {type_name: type_def} for all types in the schema."""
    result = compose_introspect(app)
    return {t["name"]: t for t in result["data"]["__schema"]["types"]}


# ──────────────────────────────────────────────────
# is_introspection_query
# ──────────────────────────────────────────────────


class TestIsIntrospectionQuery:
    def test_detects_schema(self):
        assert is_introspection_query("{ __schema { types { name } } }")

    def test_detects_type(self):
        assert is_introspection_query('{ __type(name: "X") { name } }')

    def test_detects_typename(self):
        assert is_introspection_query("{ __typename }")

    def test_rejects_normal_query(self):
        assert not is_introspection_query("{ SprintService { list_sprints { id } } }")

    def test_rejects_empty(self):
        assert not is_introspection_query("")


# ──────────────────────────────────────────────────
# Schema completeness
# ──────────────────────────────────────────────────


class TestSchemaCompleteness:
    def test_returns_graphql_envelope(self):
        app = _make_manager().get_app("project")
        result = compose_introspect(app)
        assert set(result.keys()) == {"data", "errors"}
        assert result["errors"] is None
        assert "__schema" in result["data"]

    def test_query_type_has_all_services_as_fields(self):
        app = _make_manager().get_app("project")
        types = _introspect_types(app)
        query_type = types["Query"]
        field_names = {f["name"] for f in query_type["fields"]}
        assert field_names >= {"SprintService", "TaskService"}

    def test_query_type_service_fields_reference_service_query_objects(self):
        """Each root-Query service field must point at its ``{Service}Query``
        OBJECT, not regress to a scalar fallback (UseCaseService subclasses
        aren't BaseModels, so TypeMapper alone can't map them)."""
        app = _make_manager().get_app("project")
        types = _introspect_types(app)
        query_type = types["Query"]
        by_name = {f["name"]: f for f in query_type["fields"]}
        # SprintService: SprintServiceQuery! → NON_NULL(OBJECT(SprintServiceQuery))
        sprint = by_name["SprintService"]["type"]
        assert sprint["kind"] == "NON_NULL"
        assert sprint["ofType"]["kind"] == "OBJECT"
        assert sprint["ofType"]["name"] == "SprintServiceQuery"
        task = by_name["TaskService"]["type"]
        assert task["kind"] == "NON_NULL"
        assert task["ofType"]["name"] == "TaskServiceQuery"

    def test_service_object_type_has_methods(self):
        app = _make_manager().get_app("project")
        types = _introspect_types(app)
        sprint_q = types["SprintServiceQuery"]
        method_names = {f["name"] for f in sprint_q["fields"]}
        assert method_names >= {"list_sprints", "get_sprint"}

    def test_method_args_appear_with_correct_types(self):
        app = _make_manager().get_app("project")
        # __type lookup focusing on the args
        result = compose_introspect(
            app,
            '{ __type(name: "SprintServiceQuery") '
            "{ fields { name args { name type { kind name ofType { kind name } } } } } }",
        )
        fields = result["data"]["__type"]["fields"]
        get_sprint = next(f for f in fields if f["name"] == "get_sprint")
        assert get_sprint["args"][0]["name"] == "sprint_id"
        # sprint_id: int (non-null) → NON_NULL Int
        t = get_sprint["args"][0]["type"]
        assert t["kind"] == "NON_NULL"
        assert t["ofType"]["name"] == "Int"

    def test_input_payload_arg_registers_input_object_type(self):
        app = _make_manager().get_app("project")
        types = _introspect_types(app)

        payload_type = types["CreateTaskInput"]
        assert payload_type["kind"] == "INPUT_OBJECT"
        input_fields = {f["name"]: f for f in payload_type["inputFields"]}
        assert set(input_fields) == {"title", "owner_id", "color"}
        assert input_fields["title"]["type"]["kind"] == "NON_NULL"
        assert input_fields["title"]["type"]["ofType"]["name"] == "String"
        assert input_fields["owner_id"]["type"]["ofType"]["name"] == "Int"
        assert input_fields["color"]["type"]["ofType"]["name"] == "Color"

        task_query = types["TaskServiceQuery"]
        create_task_with_input = next(
            f for f in task_query["fields"] if f["name"] == "create_task_with_input"
        )
        payload_arg = next(a for a in create_task_with_input["args"] if a["name"] == "payload")
        assert payload_arg["type"]["kind"] == "NON_NULL"
        assert payload_arg["type"]["ofType"]["kind"] == "INPUT_OBJECT"
        assert payload_arg["type"]["ofType"]["name"] == "CreateTaskInput"

    def test_optional_arg_becomes_nullable(self):
        app = _make_manager().get_app("project")
        result = compose_introspect(
            app,
            '{ __type(name: "TaskServiceQuery") '
            "{ fields { name args { name type { kind name ofType { kind name } } } } } }",
        )
        fields = result["data"]["__type"]["fields"]
        list_tasks = next(f for f in fields if f["name"] == "list_tasks")
        # limit: int = 10 → nullable Int (has default → strip NonNull)
        limit_arg = next(a for a in list_tasks["args"] if a["name"] == "limit")
        assert limit_arg["type"]["kind"] == "SCALAR"
        assert limit_arg["type"]["name"] == "Int"

    def test_arg_default_value_is_graphql_literal(self):
        """``defaultValue`` must be a GraphQL literal, not Python repr.

        GraphQL spec wants ``true`` / ``false`` (lowercase) and
        double-quoted strings. Python repr gives ``True`` / ``'hi'``
        which GraphiQL would reject. Regression for the ``repr()``
        → ``json.dumps()`` fix.
        """
        from pydantic_resolve.use_case.compose_schema import _build_method_args

        class Probe:
            @classmethod
            async def m(
                cls,
                a: int = 42,
                b: str = "hi",
                c: bool = True,
                d: bool = False,
                e: list[int] = [1, 2],
            ):
                ...

        args = {a.name: a for a in _build_method_args(Probe.m.__func__)}
        assert args["a"].default_value == "42"          # int — repr and json match
        assert args["b"].default_value == '"hi"'        # str — JSON double-quoted, not repr's 'hi'
        assert args["c"].default_value == "true"        # bool True → lowercase true
        assert args["d"].default_value == "false"       # bool False → lowercase false
        assert args["e"].default_value == "[1, 2]"      # list → JSON array

    def test_dto_fields_appear(self):
        app = _make_manager().get_app("project")
        result = compose_introspect(
            app,
            '{ __type(name: "TaskDTO") { fields { name type { kind name ofType { kind name } } } } }',
        )
        fields = {f["name"]: f for f in result["data"]["__type"]["fields"]}
        assert "id" in fields
        assert "title" in fields
        # id: int → NON_NULL Int
        assert fields["id"]["type"]["kind"] == "NON_NULL"
        assert fields["id"]["type"]["ofType"]["name"] == "Int"
        # owner: Optional[OwnerDTO] → nullable OBJECT
        assert fields["owner"]["type"]["kind"] == "OBJECT"
        assert fields["owner"]["type"]["name"] == "OwnerDTO"


# ──────────────────────────────────────────────────
# Type system
# ──────────────────────────────────────────────────


class TestTypeSystem:
    def test_list_type_is_non_null_list(self):
        app = _make_manager().get_app("project")
        result = compose_introspect(
            app,
            '{ __type(name: "SprintServiceQuery") '
            "{ fields { name type { kind ofType { kind ofType { kind ofType { kind name } } } } } } }",
        )
        fields = result["data"]["__type"]["fields"]
        list_sprints = next(f for f in fields if f["name"] == "list_sprints")
        # list[SprintDTO] → NON_NULL LIST NON_NULL SprintDTO
        t = list_sprints["type"]
        assert t["kind"] == "NON_NULL"
        assert t["ofType"]["kind"] == "LIST"
        assert t["ofType"]["ofType"]["kind"] == "NON_NULL"
        assert t["ofType"]["ofType"]["ofType"]["name"] == "SprintDTO"

    def test_enum_type_supported(self):
        app = _make_manager().get_app("project")
        types = _introspect_types(app)
        assert "Color" in types
        assert types["Color"]["kind"] == "ENUM"
        values = {v["name"] for v in types["Color"]["enumValues"]}
        assert values == {"RED", "GREEN"}

    def test_recursive_dto_does_not_infinite_loop(self):
        app = _make_manager().get_app("project")
        # TaskDTO has subtasks: list[TaskDTO] — must not loop forever.
        types = _introspect_types(app)
        assert "TaskDTO" in types

    def test_shared_dto_appears_once(self):
        app = _make_manager().get_app("project")
        # Both SprintService.list_sprints and TaskService.get_task reference DTOs.
        types = _introspect_types(app)
        # OwnerDTO referenced by TaskDTO.owner; SprintDTO by list_sprints — each appears once.
        assert list(types.keys()).count("OwnerDTO") == 1
        assert list(types.keys()).count("SprintDTO") == 1


# ──────────────────────────────────────────────────
# Mutation control
# ──────────────────────────────────────────────────


class TestMutationControl:
    def test_mutation_excluded_when_disabled(self):
        app = _make_manager(enable_mutation=False).get_app("project")
        result = compose_introspect(
            app,
            '{ __type(name: "TaskServiceQuery") { fields { name } } }',
        )
        names = {f["name"] for f in result["data"]["__type"]["fields"]}
        assert "create_task" not in names
        assert "get_task" in names

    def test_mutation_included_when_enabled(self):
        app = _make_manager(enable_mutation=True).get_app("project")
        result = compose_introspect(
            app,
            '{ __type(name: "TaskServiceQuery") { fields { name } } }',
        )
        names = {f["name"] for f in result["data"]["__type"]["fields"]}
        assert "create_task" in names


# ──────────────────────────────────────────────────
# Entry routing
# ──────────────────────────────────────────────────


class TestEntryRouting:
    @pytest.mark.asyncio
    async def test_introspection_query_no_longer_auto_routed(self):
        """Introspection queries must NOT be handled by ``app.compose``.
        Passing one now surfaces as type_not_found — callers must dispatch
        to compose_introspect themselves via is_introspection_query."""
        app = _make_manager().get_app("project")
        with pytest.raises(ComposeError) as exc_info:
            await app.compose("{ __schema { queryType { name } } }")
        assert exc_info.value.error_type == "type_not_found"
        # __schema is parsed as a service name, which doesn't exist
        assert "__schema" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_data_query_still_works_via_compose(self):
        """Sanity check: removing the auto-route doesn't break data queries."""
        app = _make_manager().get_app("project")
        result = await app.compose("{ SprintService { list_sprints { id name } } }")
        assert "SprintService" in result["data"]
        assert result["errors"] == []

    def test_compose_introspect_explicit_call_returns_envelope(self):
        app = _make_manager().get_app("project")
        result = compose_introspect(app)
        assert result["data"]["__schema"]["queryType"]["name"] == "Query"

    def test_partial_introspection_query_supported(self):
        app = _make_manager().get_app("project")
        result = compose_introspect(
            app, '{ __type(name: "SprintDTO") { name fields { name } } }'
        )
        assert result["data"]["__type"]["name"] == "SprintDTO"
        field_names = {f["name"] for f in result["data"]["__type"]["fields"]}
        assert field_names == {"id", "name"}

    def test_typename_query(self):
        app = _make_manager().get_app("project")
        result = compose_introspect(app, "{ __typename }")
        assert result["data"]["__typename"] == "Query"

    @pytest.mark.asyncio
    async def test_mcp_tool_rejects_introspection_with_hint(self):
        """Layer 3 (compose_query) rejects GraphQL introspection and
        redirects to Layer 2 (describe_compose_schema). Schema discovery
        belongs to Layer 2; Layer 3 owns execution only.
        """
        mcp = create_use_case_graphql_mcp_server(
            apps=[
                UseCaseAppConfig(
                    name="project",
                    description="p",
                    services=[SprintService, TaskService],
                ),
            ],
        )
        result = await mcp.call_tool(
            "compose_query",
            {"app_name": "project", "query": "{ __schema { types { name } } }"},
        )
        data = json.loads(result.content[0].text)
        assert data["success"] is False
        assert "describe_compose_schema" in data["error"]
        assert data["error_type"] == "validation_error"


# ──────────────────────────────────────────────────
# GraphiQL compatibility
# ──────────────────────────────────────────────────


class TestGraphiQLCompatibility:
    def test_canonical_graphiql_introspection_query_works(self):
        """The exact query GraphiQL sends on boot must succeed."""
        app = _make_manager().get_app("project")
        canonical = """
        query IntrospectionQuery {
          __schema {
            queryType { name }
            mutationType { name }
            subscriptionType { name }
            types { ...FullType }
            directives {
              name
              description
              locations
              args { ...InputValue }
            }
          }
        }
        fragment FullType on __Type {
          kind
          name
          description
          fields(includeDeprecated: true) {
            name
            description
            args { ...InputValue }
            type { ...TypeRef }
            isDeprecated
            deprecationReason
          }
          inputFields { ...InputValue }
          interfaces { ...TypeRef }
          enumValues(includeDeprecated: true) {
            name
            description
            isDeprecated
            deprecationReason
          }
          possibleTypes { ...TypeRef }
        }
        fragment InputValue on __InputValue {
          name
          description
          type { ...TypeRef }
          defaultValue
        }
        fragment TypeRef on __Type {
          kind
          name
          ofType {
            kind
            name
            ofType {
              kind
              name
              ofType {
                kind
                name
                ofType {
                  kind
                  name
                  ofType {
                    kind
                    name
                    ofType {
                      kind
                      name
                      ofType { kind name }
                    }
                  }
                }
              }
            }
          }
        }
        """
        result = compose_introspect(app, canonical)
        assert result["errors"] is None
        schema = result["data"]["__schema"]
        assert schema["queryType"]["name"] == "Query"
        # mutationType is None in compose (no mutation root)
        assert schema["mutationType"] is None
        # Standard GraphQL directives must be present.
        directive_names = {d["name"] for d in schema["directives"]}
        assert {"skip", "include", "deprecated", "oneOf"} <= directive_names


# ──────────────────────────────────────────────────
# Input-type edge cases
# ──────────────────────────────────────────────────


def _make_edge_app(*services, name: str = "edge"):
    """Build a single-app manager from the given edge-case services.

    Keeps these scenarios isolated from the module-level SprintService /
    TaskService fixtures so the assertions depend only on the scenario
    under test.
    """
    manager = UseCaseManager(
        apps=[
            UseCaseAppConfig(
                name=name,
                description=name,
                services=list(services),
                enable_mutation=True,
            ),
        ],
    )
    return manager.get_app(name)


def _types(app) -> dict[str, dict]:
    return {t["name"]: t for t in compose_introspect(app)["data"]["__schema"]["types"]}


class TestInputTypeEdgeCases:
    """Edge cases for INPUT_OBJECT handling.

    Each test pins down an expected behavior so we can detect regressions
    and decide policy. Tests expected to fail against the current
    implementation are flagged in the docstring with ``Currently:`` so the
    failure mode is interpretable.
    """

    # ── Case 1: same BaseModel used as both return and arg ──

    def test_dto_used_as_both_return_and_arg_arg_is_input_object(self):
        """GraphQL spec requires field-arg types to be INPUT_OBJECT and
        return types to be OBJECT; they cannot share a type definition.

        Currently: ``registry`` is keyed by class name and
        ``build_compose_schema`` processes ``return_anno`` before ``args``.
        The arg-side registration is skipped at
        ``_collect_reachable_types`` (``if name in registry: continue``),
        so the schema has only an OBJECT ``CloneDTO`` — but the arg's
        type ref is computed fresh in ``_render_arg`` (with
        ``is_input=True``) and reports ``INPUT_OBJECT``. The arg ref and
        the type definition disagree, which is the spec violation.
        """
        app = _make_edge_app(CloneService)
        types = _types(app)

        method = next(
            f for f in types["CloneServiceQuery"]["fields"] if f["name"] == "clone"
        )
        payload_arg = next(a for a in method["args"] if a["name"] == "payload")
        arg_kind = payload_arg["type"]["ofType"]["kind"]
        arg_name = payload_arg["type"]["ofType"]["name"]

        # The schema must contain a type matching the arg's type ref.
        # Spec violation: arg says INPUT_OBJECT but schema only has OBJECT.
        assert arg_name in types, f"arg references {arg_name} but it's not in the schema"
        assert types[arg_name]["kind"] == arg_kind, (
            f"arg ref says kind={arg_kind} but schema defines {arg_name} "
            f"as kind={types[arg_name]['kind']}"
        )

    # ── Case 2: method_sdl does not expand INPUT_OBJECT ──

    def test_method_sdl_expands_input_object_referenced_by_args(self):
        """``describe_compose_method`` should emit ``input X { ... }`` for
        any INPUT_OBJECT referenced by the method's args, so callers see
        the full shape of what they need to construct.

        Currently: ``_collect_reachable_sdl_types`` only collects
        ``("OBJECT", "ENUM")``, so the input type referenced by an arg is
        never defined in the SDL output.
        """
        app = _make_edge_app(MDLService)
        sdl = method_sdl(app.compose_schema, "MDLService", "create")
        assert sdl is not None
        # The input type referenced by the arg must be defined in the SDL.
        assert "input MDLInput {" in sdl
        # And its fields must be expanded.
        assert "title: String!" in sdl
        assert "owner_id: Int!" in sdl

    # ── Case 3: input field default value is dropped ──

    def test_input_field_default_value_preserved(self):
        """pydantic field defaults (``limit: int = 10``,
        ``keyword: Optional[str] = None``) should surface in
        ``inputFields[i].defaultValue`` as a GraphQL literal, matching
        how ``_build_method_args`` handles method-arg defaults.

        Currently: ``_render_input_field`` hardcodes
        ``"defaultValue": None``, so defaults are silently lost.
        """
        app = _make_edge_app(FilterService)
        types = _types(app)
        fields = {f["name"]: f for f in types["FilterInput"]["inputFields"]}
        # Int default renders as a GraphQL IntLiteral.
        assert fields["limit"]["defaultValue"] == "10"
        # None default renders as ``null``.
        assert fields["keyword"]["defaultValue"] == "null"
        # Required field has no default.
        assert fields["required"]["defaultValue"] is None

    # ── Case 4: nested BaseModel field inside an input ──

    def test_nested_basemodel_in_input_registers_as_input_object(self):
        """A BaseModel field within an input must also be registered as
        INPUT_OBJECT (recursing through ``_collect_reachable_types``).

        This case currently passes — the recursive call forwards
        ``is_input`` correctly — but is not covered by any existing
        test, so this guards against regressions.
        """
        app = _make_edge_app(NestedService)
        types = _types(app)
        # Inner must be INPUT_OBJECT (not OBJECT), and reachable from the
        # outer input's field type ref.
        assert types["InnerInput"]["kind"] == "INPUT_OBJECT"
        outer_fields = {f["name"]: f for f in types["OuterInput"]["inputFields"]}
        assert outer_fields["inner"]["type"]["ofType"]["kind"] == "INPUT_OBJECT"
        assert outer_fields["inner"]["type"]["ofType"]["name"] == "InnerInput"

    # ── Case 5: Optional field inside an input is nullable ──

    def test_optional_input_field_is_nullable(self):
        """``Optional[X]`` field within an input must produce a nullable
        type ref (no trailing ``!``). Guards against the input path
        accidentally forcing NON_NULL on optional fields.
        """
        app = _make_edge_app(OptionalService)
        types = _types(app)
        fields = {f["name"]: f for f in types["OptionalInput"]["inputFields"]}
        # Optional[str] → no NON_NULL wrapper.
        assert fields["note"]["type"]["kind"] == "SCALAR"
        assert fields["note"]["type"]["name"] == "String"
        # Required int → NON_NULL wrapper.
        assert fields["required"]["type"]["kind"] == "NON_NULL"
        assert fields["required"]["type"]["ofType"]["name"] == "Int"

    # ── Case 6: List field — pin down nullability semantics ──

    def test_list_input_field_nullability(self):
        """Pin down the nullability of ``list[T]`` fields inside an input.

        Current implementation produces ``[T!]!`` (outer NON_NULL,
        inner NON_NULL too) — driven by ``TypeMapper``'s ``LIST``
        branch wrapping the inner in ``NON_NULL`` unconditionally.

        This test makes the chosen semantics explicit; if we later
        decide inner elements should be nullable (``[T]!``), this
        test will flag the change.
        """
        app = _make_edge_app(ListService)
        types = _types(app)
        fields = {f["name"]: f for f in types["ListInput"]["inputFields"]}
        tags_type = fields["tags"]["type"]
        # Outer is NON_NULL (because the field itself is not Optional).
        assert tags_type["kind"] == "NON_NULL"
        outer = tags_type["ofType"]
        # Then LIST.
        assert outer["kind"] == "LIST"
        # Inner element is NON_NULL ([String!]!, not [String]!).
        assert outer["ofType"]["kind"] == "NON_NULL"
        assert outer["ofType"]["ofType"]["kind"] == "SCALAR"
        assert outer["ofType"]["ofType"]["name"] == "String"
