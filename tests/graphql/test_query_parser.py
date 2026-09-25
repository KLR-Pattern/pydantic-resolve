"""
测试 GraphQL 查询解析器
"""

import pytest
from pydantic_resolve.graphql import QueryParser, QueryParseError


class TestQueryParser:
    """测试 QueryParser"""

    def setup_method(self):
        """设置测试环境"""
        self.parser = QueryParser()

    def test_parse_simple_query(self):
        """测试解析简单查询"""
        query = "{ users { id name } }"
        parsed = self.parser.parse(query)

        assert 'users' in parsed.field_tree
        assert 'id' in parsed.field_tree['users'].sub_fields
        assert 'name' in parsed.field_tree['users'].sub_fields

    def test_parse_query_with_arguments(self):
        """测试解析带参数的查询"""
        query = "{ users(limit: 10) { id } }"
        parsed = self.parser.parse(query)

        assert 'users' in parsed.field_tree
        # 参数值会被正确转换为整数
        assert 'limit' in parsed.field_tree['users'].arguments
        assert parsed.field_tree['users'].arguments['limit'] == 10

    def test_parse_query_with_variable_resolves(self):
        """变量引用应从 variables dict 解析为对应值。"""
        query = """
        query GetUsers($limit: Int!) {
            users(limit: $limit) { id }
        }
        """

        parsed = self.parser.parse(query, variables={"limit": 10})
        assert parsed.field_tree["users"].arguments["limit"] == 10

    def test_parse_query_with_missing_variable_raises_error(self):
        """引用了未提供值的变量应抛出明确错误。"""
        query = """
        query GetUsers($limit: Int!) {
            users(limit: $limit) { id }
        }
        """

        with pytest.raises(QueryParseError, match="no value was provided"):
            self.parser.parse(query)

    def test_parse_nested_query(self):
        """测试解析嵌套查询"""
        query = "{ users { id posts { title } } }"
        parsed = self.parser.parse(query)

        assert 'posts' in parsed.field_tree['users'].sub_fields
        assert 'title' in parsed.field_tree['users'].sub_fields['posts'].sub_fields

    def test_parse_invalid_query(self):
        """测试解析无效查询"""
        query = "{ invalid { id } }"

        # 解析器现在只检查语法，不验证实体是否存在
        # 未知查询的验证由处理器负责
        parsed = self.parser.parse(query)

        # 验证解析成功（语法正确）
        assert parsed is not None
        assert 'invalid' in parsed.field_tree

    def test_parse_empty_query(self):
        """测试解析空查询"""
        query = "{ }"

        with pytest.raises(QueryParseError):
            self.parser.parse(query)

    def test_parse_fragment_spread(self):
        """测试解析 FragmentSpread"""
        query = """
        query {
            users {
                ...UserFields
            }
        }

        fragment UserFields on UserEntity {
            id
            name
        }
        """
        parsed = self.parser.parse(query)

        assert 'users' in parsed.field_tree
        assert 'id' in parsed.field_tree['users'].sub_fields
        assert 'name' in parsed.field_tree['users'].sub_fields

    def test_parse_inline_fragment(self):
        """测试解析 InlineFragment"""
        query = """
        query {
            users {
                ... on UserEntity {
                    id
                    name
                }
            }
        }
        """
        parsed = self.parser.parse(query)

        assert 'users' in parsed.field_tree
        assert 'id' in parsed.field_tree['users'].sub_fields
        assert 'name' in parsed.field_tree['users'].sub_fields

    def test_alias_captured_and_keyed_by_response_key(self):
        """parser 接受别名：sub_fields 以 response key 索引，.name 保留原名。"""
        parsed = self.parser.parse("{ a: users { id } }")
        sel = parsed.field_tree["a"]
        assert sel.name == "users"
        assert sel.alias == "a"

    def test_entity_gate_rejects_root_alias(self):
        """entity-first 门：任意层级的别名都会被拒绝。"""
        from pydantic_resolve.graphql.query_parser import reject_all_aliases

        parsed = self.parser.parse("{ a: users { id } }")
        with pytest.raises(QueryParseError, match="not supported"):
            reject_all_aliases(parsed.field_tree)

    def test_entity_gate_rejects_nested_alias(self):
        """entity-first 门：嵌套别名同样拒绝。"""
        from pydantic_resolve.graphql.query_parser import reject_all_aliases

        parsed = self.parser.parse("{ users { a: name } }")
        with pytest.raises(QueryParseError, match="nested level"):
            reject_all_aliases(parsed.field_tree)

    def test_duplicate_root_field_rejected(self):
        """根层同名字段不允许出现两次。"""
        with pytest.raises(QueryParseError, match="Duplicate response key 'users'"):
            self.parser.parse("{ users { id } users { name } }")

    def test_duplicate_nested_field_rejected(self):
        """service / method 层同名也不允许重复。"""
        with pytest.raises(QueryParseError, match="Duplicate response key 'name'"):
            self.parser.parse("{ users { id name name } }")

    def test_duplicate_method_with_different_args_rejected(self):
        """同名 method 即使参数不同也不允许 —— 避免参数被静默覆盖、调用被丢失。"""
        with pytest.raises(QueryParseError, match="Duplicate response key 'get_sprint'"):
            self.parser.parse(
                "{ SprintService { get_sprint(sprint_id: 1) { name } "
                "get_sprint(sprint_id: 2) { id } } }"
            )

    def test_duplicate_field_via_fragment_spread_rejected(self):
        """fragment 展开后与同层字段冲突，同样应报错。"""
        query = """
        query {
            users {
                id
                ...UserFields
            }
        }
        fragment UserFields on UserEntity {
            id
            name
        }
        """
        with pytest.raises(QueryParseError, match="Duplicate response key 'id'"):
            self.parser.parse(query)

    def test_duplicate_field_via_inline_fragment_rejected(self):
        """inline fragment 展开后与同层字段冲突，同样应报错。"""
        query = """
        query {
            users {
                id
                ... on UserEntity { id }
            }
        }
        """
        with pytest.raises(QueryParseError, match="Duplicate response key 'id'"):
            self.parser.parse(query)
