"""
Tests for GraphQL Input Type support.

Tests the ability to use Pydantic BaseModel as @query and @mutation parameters,
with automatic generation of GraphQL input types.
"""

import pytest
from pydantic import BaseModel
from typing import Optional, List

from pydantic_resolve import base_entity, query, mutation, config_global_resolver
from pydantic_resolve.graphql import GraphQLHandler, SchemaBuilder


# Define Input Types
class CreateAddressInput(BaseModel):
    """Address input type for nested testing"""
    street: str
    city: str
    zip_code: str


class CreateUserInput(BaseModel):
    """User input type"""
    name: str
    email: str
    age: Optional[int] = None
    address: Optional[CreateAddressInput] = None


class CreatePostInput(BaseModel):
    """Post input type"""
    title: str
    content: str
    author_id: int
    tags: Optional[List[str]] = None


class UpdateUserInput(BaseModel):
    """Update user input type"""
    name: Optional[str] = None
    email: Optional[str] = None


# Define Entities
BaseEntity = base_entity()


class UserEntity(BaseModel, BaseEntity):
    """User entity"""
    __relationships__ = []
    id: int
    name: str
    email: str
    age: Optional[int] = None

    @query
    async def get_all(cls, limit: int = 10) -> List['UserEntity']:
        """Get all users"""
        return [
            UserEntity(id=1, name="Alice", email="alice@example.com", age=25),
            UserEntity(id=2, name="Bob", email="bob@example.com", age=30)
        ][:limit]

    @mutation
    async def create_user(cls, input: CreateUserInput) -> 'UserEntity':
        """Create a new user"""
        return UserEntity(
            id=3,
            name=input.name,
            email=input.email,
            age=input.age
        )

    @mutation
    async def update_user(cls, id: int, input: UpdateUserInput) -> Optional['UserEntity']:
        """Update an existing user"""
        # Simulate update
        return UserEntity(
            id=id,
            name=input.name or "Updated",
            email=input.email or "updated@example.com"
        )


class PostEntity(BaseModel, BaseEntity):
    """Post entity"""
    __relationships__ = []
    id: int
    title: str
    content: str
    author_id: int

    @mutation
    async def create_post(cls, input: CreatePostInput) -> 'PostEntity':
        """Create a new post"""
        return PostEntity(
            id=1,
            title=input.title,
            content=input.content,
            author_id=input.author_id
        )

    @query
    async def get_posts(cls, author_id: Optional[int] = None) -> List['PostEntity']:
        """Get posts, optionally filtered by author"""
        posts = [
            PostEntity(id=1, title="Post 1", content="Content 1", author_id=1),
            PostEntity(id=2, title="Post 2", content="Content 2", author_id=2)
        ]
        if author_id:
            return [p for p in posts if p.author_id == author_id]
        return posts

    @mutation
    async def delete_post(cls, id: int) -> bool:
        """Delete a post, return success status"""
        return True


# Configure global resolver


@pytest.fixture(autouse=True)
def setup_global_resolver():
    """Set up global resolver for this test module."""
    config_global_resolver(BaseEntity.get_diagram())
    yield


class TestInputTypeSchemaGeneration:
    """Tests for Input Type schema generation"""

    @pytest.fixture
    def schema_builder(self):
        return SchemaBuilder(BaseEntity.get_diagram())

    def test_input_type_generated_in_schema(self, schema_builder):
        """Test that input types are generated in the schema"""
        schema = schema_builder.build_schema()

        # Check CreateUserInput is generated as input type
        assert "input CreateUserInput {" in schema
        input_block = schema.split("input CreateUserInput")[1].split("}")[0]
        assert "name: String!" in input_block
        assert "email: String!" in input_block
        # age is Optional[int] — nullable, no ! suffix (#320).
        assert "age: Int" in input_block
        assert "age: Int!" not in input_block

    def test_nested_input_type_generated(self, schema_builder):
        """Test that nested input types are generated"""
        schema = schema_builder.build_schema()

        # Check CreateAddressInput is generated (nested in CreateUserInput)
        assert "input CreateAddressInput {" in schema
        assert "street: String!" in schema

        # Check CreateUserInput references CreateAddressInput
        # (address is Optional[CreateAddressInput], so no ! suffix)
        assert "address: CreateAddressInput" in schema

    def test_input_type_with_list_field(self, schema_builder):
        """Test input type with list field"""
        schema = schema_builder.build_schema()

        # Check CreatePostInput is generated
        assert "input CreatePostInput {" in schema
        # tags is Optional[List[str]] so it should be [String!] (optional, no trailing !)
        assert "tags: [String!]" in schema

    def test_input_types_placed_before_output_types(self, schema_builder):
        """Test that input types are placed before output types in schema"""
        schema = schema_builder.build_schema()

        # Find positions
        input_pos = schema.find("input CreateUserInput")
        type_pos = schema.find("type UserEntity")

        # Input types should come before output types
        assert input_pos < type_pos

    def test_mutation_with_input_parameter(self, schema_builder):
        """Test that mutation with input parameter generates correct schema"""
        schema = schema_builder.build_schema()

        # Check mutation definition uses input type. Under the grouped layout
        # the method field lives on the {Entity}Mutation group type, named verbatim.
        # Note: return type inference is a separate concern
        assert "create_user(input: CreateUserInput!)" in schema

    def test_multiple_input_types_generated(self, schema_builder):
        """Test that multiple input types are generated"""
        schema = schema_builder.build_schema()

        # Check all input types are generated
        assert "input CreateUserInput {" in schema
        assert "input UpdateUserInput {" in schema
        assert "input CreatePostInput {" in schema
        assert "input CreateAddressInput {" in schema

    def test_optional_fields_in_input_type(self, schema_builder):
        """Test optional fields in input types"""
        schema = schema_builder.build_schema()

        # UpdateUserInput has optional fields
        # In GraphQL, optional fields don't have ! suffix in the schema
        # But our implementation always adds ! for now
        # This test verifies the current behavior
        assert "input UpdateUserInput {" in schema


class TestInputTypeArgumentConversion:
    """Tests for Input Type argument conversion during execution"""

    @pytest.fixture
    def handler(self):
        return GraphQLHandler(BaseEntity.get_diagram())

    @pytest.mark.asyncio
    async def test_mutation_converts_dict_to_input_model(self, handler):
        """Test that mutation arguments are converted from dict to Pydantic model"""
        query = """
        mutation {
            UserEntity {
                create_user(input: { name: "Test User", email: "test@example.com", age: 25 }) {
                    id
                    name
                    email
                    age
                }
            }
        }
        """

        result = await handler.execute(query)

        assert result["errors"] is None
        assert result["data"]["UserEntity"]["create_user"]["name"] == "Test User"
        assert result["data"]["UserEntity"]["create_user"]["email"] == "test@example.com"
        assert result["data"]["UserEntity"]["create_user"]["age"] == 25

    @pytest.mark.asyncio
    async def test_mutation_with_nested_input(self, handler):
        """Test mutation with nested input type"""
        query = """
        mutation {
            UserEntity {
                create_user(input: {
                    name: "Test User",
                    email: "test@example.com",
                    address: { street: "123 Main St", city: "NYC", zip_code: "10001" }
                }) {
                    id
                    name
                    email
                }
            }
        }
        """

        result = await handler.execute(query)

        assert result["errors"] is None
        assert result["data"]["UserEntity"]["create_user"]["name"] == "Test User"

    @pytest.mark.asyncio
    async def test_mutation_with_optional_input_fields(self, handler):
        """Test mutation with optional input fields"""
        query = """
        mutation {
            UserEntity {
                update_user(id: 1, input: { name: "Updated Name" }) {
                    id
                    name
                    email
                }
            }
        }
        """

        result = await handler.execute(query)

        assert result["errors"] is None
        assert result["data"]["UserEntity"]["update_user"]["name"] == "Updated Name"

    @pytest.mark.asyncio
    async def test_query_with_scalar_arguments_still_works(self, handler):
        """Test that queries with scalar arguments still work"""
        query = """
        query {
            UserEntity {
                get_all(limit: 1) {
                    id
                    name
                }
            }
        }
        """

        result = await handler.execute(query)

        assert result["errors"] is None
        # Note: scalar argument filtering may not work in current implementation
        # This test verifies the query doesn't error
        assert "UserEntity" in result["data"]


class TestInputTypeIntrospection:
    """Tests for Input Type introspection"""

    @pytest.fixture
    def handler(self):
        return GraphQLHandler(BaseEntity.get_diagram())

    @pytest.mark.asyncio
    async def test_input_types_in_introspection(self, handler):
        """Test that input types appear in introspection query"""
        query = """
        {
            __schema {
                types {
                    kind
                    name
                }
            }
        }
        """

        result = await handler.execute(query)

        assert result["errors"] is None
        type_names = [t["name"] for t in result["data"]["__schema"]["types"]]

        # Check input types are included
        assert "CreateUserInput" in type_names
        assert "UpdateUserInput" in type_names
        assert "CreatePostInput" in type_names

    @pytest.mark.skip(reason="Requires __type query support - not yet implemented")
    async def test_input_type_kind_is_input_object(self, handler):
        """Test that input types have kind INPUT_OBJECT"""
        # This test requires __type(name: "...") query support
        # which is not yet fully implemented. Skip for now.
        pass

    @pytest.mark.asyncio
    async def test_mutation_args_show_input_type(self, handler):
        """Test that mutation arguments show INPUT_OBJECT type"""
        query = """
        {
            __type(name: "UserEntityMutation") {
                fields {
                    name
                    args {
                        name
                        type {
                            kind
                            name
                        }
                    }
                }
            }
        }
        """

        result = await handler.execute(query)

        assert result["errors"] is None

        # Methods live on the {Entity}Mutation group type, named verbatim.
        mutation_fields = result["data"]["__type"]["fields"]
        create_user_field = next(f for f in mutation_fields if f["name"] == "create_user")

        # Check the input argument
        input_arg = next(a for a in create_user_field["args"] if a["name"] == "input")
        assert input_arg["type"]["kind"] == "INPUT_OBJECT"
        assert input_arg["type"]["name"] == "CreateUserInput"

    @pytest.mark.asyncio
    async def test_mutation_with_bool_return_type(self, handler):
        """Test that mutation returning bool has correct SCALAR: Boolean type in introspection"""
        query = """
        {
            __type(name: "PostEntityMutation") {
                fields {
                    name
                    type {
                        kind
                        name
                    }
                }
            }
        }
        """

        result = await handler.execute(query)

        assert result["errors"] is None

        # Methods live on the {Entity}Mutation group type, named verbatim.
        mutation_fields = result["data"]["__type"]["fields"]
        delete_post_field = next(f for f in mutation_fields if f["name"] == "delete_post")

        # Check the return type is SCALAR Boolean
        assert delete_post_field["type"]["kind"] == "SCALAR"
        assert delete_post_field["type"]["name"] == "Boolean"
