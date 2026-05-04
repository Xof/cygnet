# test_cross_table_dml.py — UPDATE … SET col = expr, UPDATE … FROM,
# and DELETE … USING.  All three lean on the existing expression
# protocol: SET values that have render_sql go into the SQL directly,
# table sources land in the FROM/USING clauses and let WHERE carry the
# join condition.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
import cygnet.functions as f
from cygnet.annotations import DBKey
from tests.conftest import AccountTable, FakeDB


@dataclasses.dataclass
class Counter:
    id: Annotated[int, DBKey]
    name: str
    n: int


@dataclasses.dataclass
class Tally:
    id: Annotated[int, DBKey]
    counter_id: int
    extra: int


CounterTable = cygnet.Table(Counter)
TallyTable = cygnet.Table(Tally)


class TestUpdateExpressions:
    """SET values can be SQLRenderable expressions, not just literals."""

    async def test_self_referential_increment(self):
        """`SET n = n + 1` — the increment idiom that previously
        required dropping to cygnet.lit()."""
        db = FakeDB()
        await (
            cygnet.UPDATE(db)
            .SET(CounterTable, n=CounterTable.n + 1)
            .WHERE(CounterTable.id == 1)
        )
        sql = db.last_sql
        # The arithmetic Predicate renders as `counters.n + $N`.
        assert "SET n = counters.n + $1" in sql
        # WHERE param follows SET param: $1 is the increment, $2 is the id.
        assert "WHERE (counters.id = $2)" in sql
        assert db.last_params == [1, 1]

    async def test_function_call_on_set(self):
        """SET col = upper(col) — exercises FunctionCall in SET."""
        db = FakeDB()
        await (
            cygnet.UPDATE(db)
            .SET(CounterTable, name=f.upper(CounterTable.name))
            .WHERE(CounterTable.id == 1)
        )
        assert "SET name = upper(counters.name)" in db.last_sql

    async def test_literal_value_still_works(self):
        """Plain kwargs (literals) keep going through the $N path —
        the new expression branch is additive."""
        db = FakeDB()
        await cygnet.UPDATE(db).SET(CounterTable, n=42).WHERE(CounterTable.id == 1)
        assert "SET n = $1" in db.last_sql
        assert db.last_params == [42, 1]

    async def test_mixed_literal_and_expression(self):
        """A SET clause can mix literal kwargs with expression kwargs;
        $N numbering interleaves correctly across both kinds."""
        db = FakeDB()
        await (
            cygnet.UPDATE(db)
            .SET(CounterTable, name="updated", n=CounterTable.n + 5)
            .WHERE(CounterTable.id == 1)
        )
        sql = db.last_sql
        # Field iteration order is meta.fields, which for Counter is
        # id, name, n — so name comes before n.
        assert "SET name = $1, n = counters.n + $2" in sql
        # WHERE id binds to $3.
        assert "WHERE (counters.id = $3)" in sql
        assert db.last_params == ["updated", 5, 1]

    async def test_compound_expression(self):
        """Nested arithmetic — (n + 1) * 2."""
        db = FakeDB()
        await (
            cygnet.UPDATE(db)
            .SET(CounterTable, n=(CounterTable.n + 1) * 2)
            .WHERE(CounterTable.id == 1)
        )
        assert "SET n = counters.n + $1 * $2" in db.last_sql
        assert db.last_params == [1, 2, 1]


class TestUpdateFrom:
    """UPDATE … FROM other_table — cross-table updates where SET values
    come from another row.  Join condition goes in WHERE."""

    async def test_simple_from_clause(self):
        db = FakeDB()
        await (
            cygnet.UPDATE(db)
            .SET(CounterTable, n=TallyTable.extra)
            .FROM(TallyTable)
            .WHERE(CounterTable.id == TallyTable.counter_id)
        )
        sql = db.last_sql
        assert "UPDATE counters SET n = tallys.extra" in sql
        assert " FROM tallys " in sql
        assert "WHERE (counters.id = tallys.counter_id)" in sql

    async def test_from_with_alias(self):
        db = FakeDB()
        T2 = TallyTable.AS("t")
        await (
            cygnet.UPDATE(db)
            .SET(CounterTable, n=T2.extra)
            .FROM(T2)
            .WHERE(CounterTable.id == T2.counter_id)
        )
        sql = db.last_sql
        assert "FROM tallys AS t" in sql
        assert "n = t.extra" in sql

    async def test_multiple_from_tables(self):
        """Variadic FROM accepts multiple tables in one call."""
        db = FakeDB()
        T2 = TallyTable.AS("t1")
        T3 = TallyTable.AS("t2")
        await (
            cygnet.UPDATE(db)
            .SET(CounterTable, n=T2.extra)
            .FROM(T2, T3)
            .WHERE(CounterTable.id == T2.counter_id)
        )
        assert "FROM tallys AS t1, tallys AS t2" in db.last_sql

    async def test_chained_from_calls_accumulate(self):
        db = FakeDB()
        T2 = TallyTable.AS("t1")
        T3 = TallyTable.AS("t2")
        await (
            cygnet.UPDATE(db)
            .SET(CounterTable, n=T2.extra)
            .FROM(T2)
            .FROM(T3)
            .WHERE(CounterTable.id == T2.counter_id)
        )
        assert "FROM tallys AS t1, tallys AS t2" in db.last_sql

    async def test_from_table_source_validated(self):
        """FROM uses the same _check_table_source guard as everywhere
        else — passing a dataclass class gets the same suggestion."""
        db = FakeDB()
        with pytest.raises(TypeError, match=r"cygnet\.Table"):
            cygnet.UPDATE(db).SET(CounterTable, n=1).FROM(Tally)


class TestDeleteUsing:
    """DELETE … USING other_table — cross-table deletes."""

    async def test_simple_using(self):
        db = FakeDB()
        await (
            cygnet.DELETE(db)
            .FROM(CounterTable)
            .USING(TallyTable)
            .WHERE(CounterTable.id == TallyTable.counter_id)
        )
        sql = db.last_sql
        assert "DELETE FROM counters USING tallys" in sql
        assert "WHERE (counters.id = tallys.counter_id)" in sql

    async def test_using_with_alias(self):
        db = FakeDB()
        T2 = TallyTable.AS("t")
        await (
            cygnet.DELETE(db)
            .FROM(CounterTable)
            .USING(T2)
            .WHERE(CounterTable.id == T2.counter_id)
        )
        assert "USING tallys AS t" in db.last_sql

    async def test_using_validates_table_source(self):
        db = FakeDB()
        with pytest.raises(TypeError, match=r"cygnet\.Table"):
            cygnet.DELETE(db).FROM(CounterTable).USING(Tally)

    async def test_using_returning(self):
        """USING + RETURNING: capture deleted-row data plus joined
        columns from the USING side."""
        db = FakeDB(rows=[(1,), (2,)])
        result = await (
            cygnet.DELETE(db)
            .FROM(CounterTable)
            .USING(TallyTable)
            .WHERE(CounterTable.id == TallyTable.counter_id)
            .RETURNING(CounterTable.id)
        )
        assert "RETURNING counters.id" in db.last_sql
        assert result == [(1,), (2,)]


class TestExistingPathsUnaffected:
    """Sanity: literal-value UPDATE and plain DELETE (no USING) still
    render the same SQL they did before."""

    async def test_simple_update_unchanged(self):
        db = FakeDB()
        await (
            cygnet.UPDATE(db)
            .SET(AccountTable, name="Wilma")
            .WHERE(AccountTable.id == 1)
        )
        assert db.last_sql == "UPDATE accounts SET name = $1 WHERE (accounts.id = $2)"

    async def test_plain_delete_unchanged(self):
        db = FakeDB()
        await cygnet.DELETE(db).FROM(AccountTable).WHERE(AccountTable.id == 1)
        assert db.last_sql == "DELETE FROM accounts WHERE (accounts.id = $1)"
