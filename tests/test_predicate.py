# test_predicate.py — Tests for Predicate tree construction and SQL rendering.
#
# Exercises all comparison operators (==, !=, <, >, <=, >=), compound
# predicates (& / |), nesting, column-to-column comparisons (no params),
# and parameter accumulation across multiple render_sql() calls.

from __future__ import annotations

from tests.conftest import AccountTable, LogTable


class TestPredicate:
    def test_equality_renders(self):
        params: list = []
        sql = (AccountTable.name == "Fred").render_sql(params)
        assert sql == "accounts.name = $1"
        assert params == ["Fred"]

    def test_inequality_renders(self):
        params: list = []
        sql = (AccountTable.name != "Fred").render_sql(params)
        assert sql == "accounts.name != $1"
        assert params == ["Fred"]

    def test_lt_gt(self):
        params: list = []
        sql = (AccountTable.id > 5).render_sql(params)
        assert sql == "accounts.id > $1"
        assert params == [5]

    def test_and_compound(self):
        params: list = []
        pred = (AccountTable.name == "Fred") & (AccountTable.id > 1)
        sql = pred.render_sql(params)
        assert sql == "(accounts.name = $1) AND (accounts.id > $2)"
        assert params == ["Fred", 1]

    def test_or_compound(self):
        params: list = []
        pred = (AccountTable.name == "Fred") | (AccountTable.name == "Wilma")
        sql = pred.render_sql(params)
        assert sql == "(accounts.name = $1) OR (accounts.name = $2)"
        assert params == ["Fred", "Wilma"]

    def test_nested_compound(self):
        params: list = []
        pred = ((AccountTable.name == "Fred") & (AccountTable.id > 1)) | (
            AccountTable.email == "x@example.com"
        )
        sql = pred.render_sql(params)
        assert sql == (
            "((accounts.name = $1) AND (accounts.id > $2)) OR (accounts.email = $3)"
        )
        assert params == ["Fred", 1, "x@example.com"]

    def test_lt_renders(self):
        params: list = []
        sql = (AccountTable.id < 5).render_sql(params)
        assert sql == "accounts.id < $1"
        assert params == [5]

    def test_le_renders(self):
        params: list = []
        sql = (AccountTable.id <= 5).render_sql(params)
        assert sql == "accounts.id <= $1"
        assert params == [5]

    def test_ge_renders(self):
        params: list = []
        sql = (AccountTable.id >= 5).render_sql(params)
        assert sql == "accounts.id >= $1"
        assert params == [5]

    def test_column_to_column_renders_as_columns(self):
        """Both sides should render as column refs, not parameters."""
        params: list = []
        pred = AccountTable.id == LogTable.account_id
        sql = pred.render_sql(params)
        assert sql == "accounts.id = log_entries.account_id"
        assert params == []

    def test_params_accumulate_across_predicates(self):
        params: list = []
        p1 = (AccountTable.name == "Fred").render_sql(params)
        p2 = (AccountTable.id == 1).render_sql(params)
        assert "$1" in p1
        assert "$2" in p2
        assert params == ["Fred", 1]

    def test_invert_negates(self):
        """~predicate produces NOT (predicate) — Pythonic equivalent of
        cygnet.op('NOT', ...)."""
        params: list = []
        sql = (~(AccountTable.id == 1)).render_sql(params)
        assert sql == "NOT (accounts.id = $1)"
        assert params == [1]

    def test_invert_combines_with_and(self):
        """An inverted predicate must still participate in & / |."""
        params: list = []
        pred = ~(AccountTable.name == "Fred") & (AccountTable.id > 1)
        sql = pred.render_sql(params)
        assert sql == "(NOT (accounts.name = $1)) AND (accounts.id > $2)"
        assert params == ["Fred", 1]

    def test_double_invert_renders_double_not(self):
        """~~p emits NOT (NOT (p)) without simplification."""
        params: list = []
        sql = (~~(AccountTable.id == 1)).render_sql(params)
        assert sql == "NOT (NOT (accounts.id = $1))"
        assert params == [1]
