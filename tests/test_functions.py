# test_functions.py — Tests for cygnet.fn() and the curated functions module.
#
# Covers SQL rendering, parameter accumulation for plain values, comparison
# operator overloads (so a function call can sit in WHERE/HAVING), and the
# curated wrappers in cygnet.functions (COUNT's star-default, the common
# aggregates, etc.).

from __future__ import annotations

import cygnet
import cygnet.functions as f
from tests.conftest import AccountTable


class TestFn:
    def test_fn_with_column_arg(self):
        params: list = []
        sql = cygnet.fn("count")(AccountTable.id).render_sql(params)
        assert sql == "count(accounts.id)"
        assert params == []

    def test_fn_with_value_arg_parameterises(self):
        params: list = []
        sql = cygnet.fn("lower")("FRED").render_sql(params)
        assert sql == "lower($1)"
        assert params == ["FRED"]

    def test_fn_with_zero_args(self):
        params: list = []
        sql = cygnet.fn("now")().render_sql(params)
        assert sql == "now()"
        assert params == []

    def test_fn_variadic(self):
        params: list = []
        sql = cygnet.fn("coalesce")(
            AccountTable.email, AccountTable.name, "n/a"
        ).render_sql(params)
        assert sql == "coalesce(accounts.email, accounts.name, $1)"
        assert params == ["n/a"]

    def test_fn_in_predicate_via_comparison(self):
        """A function call should compare against values to produce a Predicate."""
        params: list = []
        pred = cygnet.fn("lower")(AccountTable.name) == "fred"
        sql = pred.render_sql(params)
        assert sql == "lower(accounts.name) = $1"
        assert params == ["fred"]

    def test_fn_in_compound_predicate(self):
        """Function calls participate in & / | composition."""
        params: list = []
        pred = (cygnet.fn("length")(AccountTable.name) > 3) & (AccountTable.id < 100)
        sql = pred.render_sql(params)
        assert sql == "(length(accounts.name) > $1) AND (accounts.id < $2)"
        assert params == [3, 100]

    def test_fn_invert_wraps_in_not(self):
        params: list = []
        sql = (~cygnet.fn("now")()).render_sql(params)
        assert sql == "NOT (now())"

    def test_fn_in_select_columns(self):
        """A function call can stand in for a column in SELECT."""
        db = _capture_db()
        # Awaitable mode would require __await__; for unit tests we use
        # render_select via .sql() through SelectBuilder for consistency
        # with other .sql() tests elsewhere in the suite.
        sql, params = (
            cygnet.SELECT(db, cygnet.fn("count")(AccountTable.id))
            .FROM(AccountTable)
            .sql()
        )
        assert sql == "SELECT count(accounts.id) FROM accounts"
        assert params == []


class TestCuratedFunctions:
    def test_count_star_default(self):
        """count() with no args produces COUNT(*)."""
        params: list = []
        sql = f.count().render_sql(params)
        assert sql == "count(*)"
        assert params == []

    def test_count_with_column(self):
        params: list = []
        sql = f.count(AccountTable.id).render_sql(params)
        assert sql == "count(accounts.id)"

    def test_sum_avg_min_max(self):
        params: list = []
        assert f.sum(AccountTable.id).render_sql(params) == "sum(accounts.id)"
        assert f.avg(AccountTable.id).render_sql(params) == "avg(accounts.id)"
        assert f.min(AccountTable.id).render_sql(params) == "min(accounts.id)"
        assert f.max(AccountTable.id).render_sql(params) == "max(accounts.id)"

    def test_coalesce(self):
        params: list = []
        sql = f.coalesce(AccountTable.email, AccountTable.name).render_sql(params)
        assert sql == "coalesce(accounts.email, accounts.name)"

    def test_now(self):
        params: list = []
        assert f.now().render_sql(params) == "now()"

    def test_array_agg(self):
        params: list = []
        sql = f.array_agg(AccountTable.name).render_sql(params)
        assert sql == "array_agg(accounts.name)"

    def test_string_function(self):
        params: list = []
        assert f.lower(AccountTable.name).render_sql(params) == "lower(accounts.name)"
        assert f.upper(AccountTable.name).render_sql(params) == "upper(accounts.name)"

    def test_function_in_having(self):
        """Curated functions slot into HAVING via the comparison operators."""
        from tests.conftest import FakeDB

        db = FakeDB(rows=[])
        import asyncio

        async def _run():
            await (
                cygnet.SELECT(db, AccountTable.name)
                .FROM(AccountTable)
                .GROUP_BY(AccountTable.name)
                .HAVING(f.count() > 1)
            )

        asyncio.run(_run())
        assert "HAVING (count(*) > $1)" in db.last_sql
        assert db.last_params == [1]


def _capture_db() -> object:
    """A minimal stand-in for tests that only need .sql() rendering."""
    from tests.conftest import FakeDB

    return FakeDB()
