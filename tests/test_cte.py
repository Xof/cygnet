# test_cte.py — Tests for cygnet.cte() WITH-clause support.
#
# Covers SQL generation (single CTE, multiple CTEs, CTE in FROM, CTE in
# JOIN), column inference, explicit-column override, and the param-
# numbering invariant when an inner CTE has its own bind values.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import DBKey
from tests.conftest import AccountTable, FakeDB


@dataclasses.dataclass
class Score:
    id: Annotated[int, DBKey]
    account_id: int
    points: int


ScoreTable = cygnet.Table(Score)


class TestCTESQL:
    async def test_single_cte_in_from(self):
        """A simple CTE used as the FROM source."""
        db = FakeDB(rows=[])
        active = cygnet.SELECT(db, AccountTable.id, AccountTable.name).FROM(
            AccountTable
        )
        active_cte = cygnet.cte("active", active)
        await cygnet.SELECT(db, active_cte.name).WITH(active_cte).FROM(active_cte)
        assert db.last_sql.startswith(
            "WITH active AS (SELECT accounts.id, accounts.name FROM accounts) "
            "SELECT active.name FROM active"
        )

    async def test_cte_columns_inferred_from_explicit_columns(self):
        """When the inner SELECT uses ColumnProxy refs, attr access works."""
        db = FakeDB(rows=[])
        inner = cygnet.SELECT(db, AccountTable.id, AccountTable.name).FROM(AccountTable)
        c = cygnet.cte("a", inner)
        # Inferred columns: ["id", "name"]
        assert hasattr(c, "id")
        assert hasattr(c, "name")
        # The proxy renders as `cte_name.col_name`.
        params: list = []
        assert c.id.render_sql(params) == "a.id"
        assert c.name.render_sql(params) == "a.name"

    async def test_cte_columns_inferred_from_bare_select(self):
        """A bare SELECT(db).FROM(T) inherits T's full field list."""
        db = FakeDB(rows=[])
        inner = cygnet.SELECT(db).FROM(AccountTable)
        c = cygnet.cte("a", inner)
        # Account has id, name, email
        assert hasattr(c, "id")
        assert hasattr(c, "name")
        assert hasattr(c, "email")

    async def test_cte_explicit_columns_override(self):
        db = FakeDB(rows=[])
        inner = cygnet.SELECT(db, cygnet.fn("count")(AccountTable.id)).FROM(
            AccountTable
        )
        # fn() result has no inferable name; user must supply one.
        c = cygnet.cte("counts", inner, columns=["total"])
        params: list = []
        assert c.total.render_sql(params) == "counts.total"

    async def test_cte_inference_fails_on_opaque_expression(self):
        db = FakeDB(rows=[])
        inner = cygnet.SELECT(db, cygnet.lit("count(*)")).FROM(AccountTable)
        with pytest.raises(ValueError, match="can't determine a name"):
            cygnet.cte("c", inner)

    async def test_cte_in_where_via_column_ref(self):
        """The CTE's columns slot into WHERE just like a TableProxy's."""
        db = FakeDB(rows=[])
        inner = cygnet.SELECT(db, AccountTable.id, AccountTable.name).FROM(AccountTable)
        c = cygnet.cte("active", inner)
        await cygnet.SELECT(db, c.name).WITH(c).FROM(c).WHERE(c.id > 10)
        assert "WHERE (active.id > $1)" in db.last_sql
        assert db.last_params == [10]

    async def test_multiple_ctes_one_with_call(self):
        """`WITH(a, b)` emits both, comma-separated."""
        db = FakeDB(rows=[])
        a = cygnet.cte(
            "a",
            cygnet.SELECT(db, AccountTable.id).FROM(AccountTable),
        )
        b = cygnet.cte(
            "b",
            cygnet.SELECT(db, ScoreTable.id, ScoreTable.points).FROM(ScoreTable),
        )
        await (
            cygnet.SELECT(db, a.id, b.points)
            .WITH(a, b)
            .FROM(a)
            .JOIN(b, ON=a.id == b.id)
        )
        sql = db.last_sql
        assert sql.startswith("WITH a AS (")
        assert "), b AS (" in sql
        assert "SELECT a.id, b.points FROM a INNER JOIN b ON a.id = b.id" in sql

    async def test_multiple_with_calls_chain(self):
        """Two .WITH() calls accumulate, same as one variadic call."""
        db = FakeDB(rows=[])
        a = cygnet.cte(
            "a",
            cygnet.SELECT(db, AccountTable.id).FROM(AccountTable),
        )
        b = cygnet.cte(
            "b",
            cygnet.SELECT(db, ScoreTable.id).FROM(ScoreTable),
        )
        await cygnet.SELECT(db, a.id).WITH(a).WITH(b).FROM(a)
        assert "WITH a AS (" in db.last_sql
        assert "), b AS (" in db.last_sql

    async def test_cte_param_numbering_monotonic(self):
        """Bind params from the inner CTE come before outer params, with
        $N indices flowing through monotonically."""
        db = FakeDB(rows=[])
        inner = (
            cygnet.SELECT(db, AccountTable.id, AccountTable.name)
            .FROM(AccountTable)
            .WHERE(AccountTable.email == "fred@example.com")
        )
        c = cygnet.cte("filtered", inner)
        await cygnet.SELECT(db, c.name).WITH(c).FROM(c).WHERE(c.id > 100)
        # Inner WHERE consumes $1, outer WHERE consumes $2.
        assert "(accounts.email = $1)" in db.last_sql
        assert "(filtered.id > $2)" in db.last_sql
        assert db.last_params == ["fred@example.com", 100]

    async def test_cte_in_join(self):
        """A CTE can be the right side of a JOIN."""
        db = FakeDB(rows=[])
        c = cygnet.cte(
            "scores",
            cygnet.SELECT(db, ScoreTable.account_id, ScoreTable.points).FROM(
                ScoreTable
            ),
        )
        await (
            cygnet.SELECT(db, AccountTable.name, c.points)
            .WITH(c)
            .FROM(AccountTable)
            .JOIN(c, ON=AccountTable.id == c.account_id)
        )
        sql = db.last_sql
        assert "INNER JOIN scores ON accounts.id = scores.account_id" in sql


class TestRecursiveCTESQL:
    async def test_recursive_cte_count_to_n(self):
        """Counter from 1 to n via classic anchor + recursive step."""
        db = FakeDB(rows=[])
        c = cygnet.recursive_cte("counter", columns=["n"])
        c.anchor = cygnet.SELECT(db, cygnet.lit("1"))
        c.step = cygnet.SELECT(db, c.n + 1).FROM(c).WHERE(c.n < 10)
        await cygnet.SELECT(db, c.n).WITH(c).FROM(c)
        sql = db.last_sql
        assert sql.startswith("WITH RECURSIVE counter(n) AS (")
        assert "SELECT 1" in sql
        assert "UNION ALL" in sql
        # The recursive step references counter.n on both sides.
        assert "counter.n + $1" in sql
        assert "counter.n < $2" in sql
        assert db.last_params == [1, 10]

    async def test_recursive_cte_requires_columns(self):
        with pytest.raises(ValueError, match="explicit columns"):
            cygnet.recursive_cte("c", columns=[])

    async def test_recursive_cte_missing_anchor_raises(self):
        db = FakeDB(rows=[])
        c = cygnet.recursive_cte("c", columns=["n"])
        # Set step but not anchor.
        c.step = cygnet.SELECT(db, c.n + 1).FROM(c).WHERE(c.n < 5)
        with pytest.raises(ValueError, match="missing its anchor or step"):
            await cygnet.SELECT(db, c.n).WITH(c).FROM(c)

    async def test_recursive_cte_missing_step_raises(self):
        db = FakeDB(rows=[])
        c = cygnet.recursive_cte("c", columns=["n"])
        c.anchor = cygnet.SELECT(db, cygnet.lit("1"))
        with pytest.raises(ValueError, match="missing its anchor or step"):
            await cygnet.SELECT(db, c.n).WITH(c).FROM(c)

    async def test_mixed_recursive_and_regular_uses_recursive_keyword(self):
        """If any CTE in the list is recursive, PG requires WITH RECURSIVE
        on the whole list (not per-CTE)."""
        db = FakeDB(rows=[])
        regular = cygnet.cte(
            "regular",
            cygnet.SELECT(db, AccountTable.id, AccountTable.name).FROM(AccountTable),
        )
        rec = cygnet.recursive_cte("rec", columns=["n"])
        rec.anchor = cygnet.SELECT(db, cygnet.lit("1"))
        rec.step = cygnet.SELECT(db, rec.n + 1).FROM(rec).WHERE(rec.n < 5)
        await (
            cygnet.SELECT(db, regular.name, rec.n)
            .WITH(regular, rec)
            .FROM(regular)
            .JOIN(rec, ON=regular.id == rec.n)
        )
        sql = db.last_sql
        assert sql.startswith("WITH RECURSIVE regular AS (")
        assert "), rec(n) AS (" in sql
