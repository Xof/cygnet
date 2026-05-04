# test_expression.py — Tests for the SQLRenderable protocol, render_sql()
# implementations, and the operator factory functions (op, ops, is_null, etc.).
#
# These tests exercise the expression layer in isolation — no builders,
# no executor, no FakeDB.  Each test creates expressions directly and
# calls render_sql() with a fresh params list.

from __future__ import annotations

from typing import Any

import pytest

import cygnet
from cygnet.expression import PrefixOp, SQLRenderable, SuffixOp
from tests.conftest import AccountTable, LogTable, TaggedTable


class TestSQLRenderableProtocol:
    def test_protocol_is_satisfiable(self):
        """A class with render_sql(params) -> str satisfies the protocol."""

        class FakeExpr:
            def render_sql(self, params: list[Any]) -> str:
                return "fake"

        expr: SQLRenderable = FakeExpr()
        assert expr.render_sql([]) == "fake"


class TestColumnProxyRenderSQL:
    def test_renders_qualified_name(self):
        params: list = []
        sql = AccountTable.name.render_sql(params)
        assert sql == "accounts.name"
        assert params == []

    def test_renders_with_table_override(self):
        params: list = []
        sql = LogTable.message.render_sql(params)
        assert sql == "log_entries.message"
        assert params == []

    def test_renders_column_rename(self):
        params: list = []
        sql = TaggedTable.tag.render_sql(params)
        assert sql == "taggedaccounts.tag_name"
        assert params == []


class TestLiteralRenderSQL:
    def test_renders_raw_sql(self):
        params: list = []
        lit = cygnet.lit("COUNT(*)")
        sql = lit.render_sql(params)
        assert sql == "COUNT(*)"
        assert params == []

    def test_ignores_params(self):
        params = ["existing"]
        lit = cygnet.lit("NOW()")
        sql = lit.render_sql(params)
        assert sql == "NOW()"
        assert params == ["existing"]


class TestPredicateRenderSQL:
    def test_simple_predicate(self):
        params: list = []
        pred = AccountTable.name == "Fred"
        sql = pred.render_sql(params)
        assert sql == "accounts.name = $1"
        assert params == ["Fred"]

    def test_column_to_column(self):
        params: list = []
        pred = AccountTable.id == LogTable.account_id
        sql = pred.render_sql(params)
        assert sql == "accounts.id = log_entries.account_id"
        assert params == []

    def test_compound_and(self):
        params: list = []
        pred = (AccountTable.name == "Fred") & (AccountTable.id > 1)
        sql = pred.render_sql(params)
        assert sql == "(accounts.name = $1) AND (accounts.id > $2)"
        assert params == ["Fred", 1]


class TestPrefixOp:
    def test_renders_prefix(self):
        params: list = []
        expr = AccountTable.name == "Fred"
        prefix = PrefixOp(op="NOT", operand=expr)
        sql = prefix.render_sql(params)
        assert sql == "NOT (accounts.name = $1)"
        assert params == ["Fred"]

    def test_and_compound(self):
        params: list = []
        prefix = PrefixOp(op="NOT", operand=AccountTable.name == "Fred")
        compound = prefix & (AccountTable.id > 1)
        sql = compound.render_sql(params)
        assert sql == "(NOT (accounts.name = $1)) AND (accounts.id > $2)"
        assert params == ["Fred", 1]

    def test_or_compound(self):
        params: list = []
        prefix = PrefixOp(op="NOT", operand=AccountTable.name == "Fred")
        compound = prefix | (AccountTable.id > 1)
        sql = compound.render_sql(params)
        assert sql == "(NOT (accounts.name = $1)) OR (accounts.id > $2)"
        assert params == ["Fred", 1]


class TestSuffixOp:
    def test_renders_suffix(self):
        params: list = []
        suffix = SuffixOp(operand=AccountTable.email, op="IS NULL")
        sql = suffix.render_sql(params)
        assert sql == "accounts.email IS NULL"
        assert params == []

    def test_and_compound(self):
        params: list = []
        suffix = SuffixOp(operand=AccountTable.email, op="IS NULL")
        compound = suffix & (AccountTable.name == "Fred")
        sql = compound.render_sql(params)
        assert sql == "(accounts.email IS NULL) AND (accounts.name = $1)"
        assert params == ["Fred"]

    def test_or_compound(self):
        params: list = []
        suffix = SuffixOp(operand=AccountTable.email, op="IS NOT NULL")
        compound = suffix | (AccountTable.name == "Fred")
        sql = compound.render_sql(params)
        assert sql == "(accounts.email IS NOT NULL) OR (accounts.name = $1)"
        assert params == ["Fred"]


class TestOpFunction:
    def test_infix_3arg(self):
        params: list = []
        pred = cygnet.op(AccountTable.name, "ILIKE", "%fred%")
        sql = pred.render_sql(params)
        assert sql == "accounts.name ILIKE $1"
        assert params == ["%fred%"]

    def test_prefix_2arg(self):
        params: list = []
        expr = cygnet.op("NOT", AccountTable.name == "Fred")
        sql = expr.render_sql(params)
        assert sql == "NOT (accounts.name = $1)"
        assert params == ["Fred"]

    def test_precreated_1arg(self):
        ILIKE = cygnet.op("ILIKE")
        params: list = []
        pred = ILIKE(AccountTable.name, "%fred%")
        sql = pred.render_sql(params)
        assert sql == "accounts.name ILIKE $1"
        assert params == ["%fred%"]

    def test_zero_args_raises(self):
        with pytest.raises(TypeError, match="requires 1, 2, or 3 arguments"):
            cygnet.op()

    def test_four_args_raises(self):
        with pytest.raises(TypeError, match="requires 1, 2, or 3 arguments"):
            cygnet.op("a", "b", "c", "d")


class TestOpsFunction:
    def test_suffix(self):
        params: list = []
        expr = cygnet.ops(AccountTable.email, "IS NULL")
        sql = expr.render_sql(params)
        assert sql == "accounts.email IS NULL"
        assert params == []


class TestIsNullIsNotNull:
    def test_is_null(self):
        params: list = []
        expr = cygnet.is_null(AccountTable.email)
        sql = expr.render_sql(params)
        assert sql == "accounts.email IS NULL"
        assert params == []

    def test_is_not_null(self):
        params: list = []
        expr = cygnet.is_not_null(AccountTable.email)
        sql = expr.render_sql(params)
        assert sql == "accounts.email IS NOT NULL"
        assert params == []
