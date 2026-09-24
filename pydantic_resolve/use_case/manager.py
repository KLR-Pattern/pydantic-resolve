"""Multi-app manager for UseCase MCP server.

Manages multiple UseCase applications, each containing a group of UseCaseService
subclasses. Follows the same pattern as GraphQL MCP's MultiAppManager.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, TYPE_CHECKING

from pydantic import BaseModel

from pydantic_resolve.use_case.business import UseCaseService
from pydantic_resolve.use_case.compose import _compose_and_resolve
from pydantic_resolve.use_case.compose_schema import build_compose_schema

if TYPE_CHECKING:
    from pydantic_resolve.graphql.schema.type_registry import TypeInfo


class UseCaseAppConfig(BaseModel):
    """Configuration for a UseCase application in MCP server.

    Attributes:
        name: Application name (required)
        services: List of UseCaseService subclasses for this app (required)
        description: Optional application description
        enable_mutation: Whether mutation methods are exposed via MCP (default: True).
            When False, mutation methods are hidden from
            ``describe_compose_schema``, ``describe_compose_method``,
            and ``compose_query``.
        context_extractor: Optional callback that extracts request-scoped context
            (e.g. user identity from Authorization header). Receives the FastMCP
            Context object and returns a dict injected as ``_context`` parameter
            into UseCaseService methods. Can be sync or async.
    """

    model_config = {"arbitrary_types_allowed": True}

    name: str
    services: list[type["UseCaseService"]]
    description: str | None = None
    enable_mutation: bool = True
    context_extractor: Callable[[Any], dict | Awaitable[dict]] | None = None


@dataclass
class UseCaseResources:
    """Container for all resources needed to serve a UseCase application via MCP.

    Attributes:
        name: Application name
        description: Application description
        services: Mapping of service name to service class
        compose_schema: Cached ``{type_name: TypeInfo}`` registry (built once
            at registration, never mutated after). Read by introspection and
            ``describe_compose_method``.
        context_extractor: Optional callback to extract request-scoped context
    """

    name: str
    description: str
    services: dict[str, type["UseCaseService"]] = field(default_factory=dict)
    compose_schema: dict[str, "TypeInfo"] | None = None
    context_extractor: Callable[[Any], dict | Awaitable[dict]] | None = field(default=None)
    enable_mutation: bool = True

    @property
    def service_names(self) -> set[str]:
        """Get set of service names."""
        return set(self.services.keys())

    async def compose(
        self,
        query: str,
        context: dict[str, Any] | None = None,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL data query against this app's services.

        Thin wrapper around :func:`_compose_and_resolve` — see that function
        for the full pipeline (parse → plan → execute → project).

        Args:
            query: GraphQL data query string (3-level hierarchy:
                Service → Method → DTO field selection).
            context: Request-scoped context dict. Flows into method params
                annotated with ``FromContext``.

        Returns:
            Nested dict ``{service: {method: result}}``.

        Raises:
            ComposeError: For any validation or execution failure.
        """
        return await _compose_and_resolve(self, query, context, variables)


class UseCaseManager:
    """Manages multiple UseCase applications for MCP server.

    This manager handles:
    - Registration of multiple apps from UseCaseAppConfig list
    - App lookup by name (with case-insensitive fallback)
    - Resource creation for each app (service mapping)

    Each app is independent and contains its own group of services.
    """

    def __init__(self, apps: list[UseCaseAppConfig]):
        """Initialize the manager with app configurations.

        Args:
            apps: List of UseCaseAppConfig instances.

        Raises:
            ValueError: If an app with the same name already exists
        """
        self.apps: dict[str, UseCaseResources] = {}
        self._app_names_lower: dict[str, str] = {}  # lowercase -> original case

        for app_config in apps:
            resources = self._create_app_resources(app_config)
            self._register_app(resources)

    def _create_app_resources(self, config: UseCaseAppConfig) -> UseCaseResources:
        """Create UseCaseResources from UseCaseAppConfig.

        Args:
            config: Application configuration

        Returns:
            UseCaseResources instance with service mapping
        """
        name = config.name
        description = config.description or ""
        services = config.services

        # Build service name -> class mapping
        service_map: dict[str, type[UseCaseService]] = {}
        for svc in services:
            service_map[svc.__name__] = svc

        resources = UseCaseResources(
            name=name,
            description=description,
            services=service_map,
            context_extractor=config.context_extractor,
            enable_mutation=config.enable_mutation,
        )
        # Eager-build the graphql-core schema once; it never changes after
        # registration. All introspection / SDL paths read from this cache.
        resources.compose_schema = build_compose_schema(resources)
        return resources

    def _register_app(self, resources: UseCaseResources) -> None:
        """Register an app's resources.

        Args:
            resources: UseCaseResources to register

        Raises:
            ValueError: If an app with the same name already exists
        """
        name = resources.name
        name_lower = name.lower()

        if name_lower in self._app_names_lower:
            raise ValueError(f"App with name '{name}' already exists")

        self.apps[name] = resources
        self._app_names_lower[name_lower] = name

    def get_app(self, name: str) -> UseCaseResources:
        """Get app resources by name.

        Supports smart routing:
        - Exact match: "MyApp" -> "MyApp"
        - Case-insensitive match: "myapp" -> "MyApp"

        Args:
            name: Application name

        Returns:
            UseCaseResources for the matching app

        Raises:
            ValueError: If app not found
        """
        # Try exact match first
        if name in self.apps:
            return self.apps[name]

        # Try case-insensitive match
        name_lower = name.lower()
        if name_lower in self._app_names_lower:
            return self.apps[self._app_names_lower[name_lower]]

        raise ValueError(
            f"App '{name}' not found. Available apps: {list(self.apps.keys())}"
        )
