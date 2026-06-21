# test_set_ops.py — Tests for DISTINCT ON, UNION / INTERSECT / EXCEPT
# (and their ALL variants), and the trailing-ORDER BY/LIMIT semantics
# that bind to the whole compound rather than the last operand.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import DBKey
from tests.conftest import AccountTable, FakeDB


@dataclasses.dataclass
@cygnet.table("cities")
class City:
    id: Annotated[int, DBKey]
    name: str
    country: str


CityTable = cygnet.Table(City)


class TestDistinctOn:
    async def test_distinct_on_single_column(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .DISTINCT_ON(CityTable.country)
            .FROM(CityTable)
            .ORDER_BY(CityTable.country, CityTable.name)
        )
        assert "SELECT DISTINCT ON (cities.country) " in db.last_sql

    async def test_distinct_on_multiple_columns(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .DISTINCT_ON(CityTable.country, CityTable.name)
            .FROM(CityTable)
        )
        assert "DISTINCT ON (cities.country, cities.name) " in db.last_sql

    async def test_distinct_on_empty_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="at least one column"):
            cygnet.SELECT(db).DISTINCT_ON()

    async def test_distinct_and_distinct_on_are_mutually_exclusive(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="mutually exclusive"):
            cygnet.SELECT(db).DISTINCT().DISTINCT_ON(CityTable.country)
        with pytest.raises(ValueError, match="mutually exclusive"):
            cygnet.SELECT(db).DISTINCT_ON(CityTable.country).DISTINCT()


class TestSetOps:
    async def test_union(self):
        db = FakeDB(rows=[])
        left = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        right = cygnet.SELECT(db, CityTable.name).FROM(CityTable)
        await left.UNION(right)
        sql = db.last_sql
        assert "SELECT accounts.name FROM accounts" in sql
        assert " UNION " in sql
        assert "SELECT cities.name FROM cities" in sql

    async def test_union_all_preserves_duplicates(self):
        db = FakeDB(rows=[])
        left = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        right = cygnet.SELECT(db, CityTable.name).FROM(CityTable)
        await left.UNION_ALL(right)
        assert " UNION ALL " in db.last_sql

    async def test_intersect_and_except(self):
        db = FakeDB(rows=[])
        left = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        await left.INTERSECT(cygnet.SELECT(db, CityTable.name).FROM(CityTable))
        assert " INTERSECT " in db.last_sql

        db2 = FakeDB(rows=[])
        left2 = cygnet.SELECT(db2, AccountTable.name).FROM(AccountTable)
        await left2.EXCEPT_(cygnet.SELECT(db2, CityTable.name).FROM(CityTable))
        assert " EXCEPT " in db2.last_sql

    async def test_chained_set_ops(self):
        """`a UNION b UNION ALL c` chains in declaration order."""
        db = FakeDB(rows=[])
        a = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        b = cygnet.SELECT(db, CityTable.name).FROM(CityTable)
        c = cygnet.SELECT(db, AccountTable.email).FROM(AccountTable)
        await a.UNION(b).UNION_ALL(c)
        sql = db.last_sql
        assert " UNION " in sql
        assert " UNION ALL " in sql
        # The first UNION should appear before the UNION ALL.
        assert sql.index(" UNION ") < sql.index(" UNION ALL ")

    async def test_compound_order_by_limit_applies_to_whole(self):
        """ORDER BY / LIMIT chained AFTER the set-op apply to the
        compound result, emitted at the end of the rendered SQL."""
        db = FakeDB(rows=[])
        left = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        right = cygnet.SELECT(db, CityTable.name).FROM(CityTable)
        await left.UNION(right).ORDER_BY(cygnet.lit("name")).LIMIT(10)
        sql = db.last_sql
        # Order: left SELECT, UNION, right SELECT, ORDER BY, LIMIT.
        assert sql.index(" UNION ") < sql.index(" ORDER BY ") < sql.index(" LIMIT ")

    async def test_set_op_param_numbering_monotonic(self):
        """Bind params from each operand are appended to a single shared
        list, so $N stays monotonic across the compound."""
        db = FakeDB(rows=[])
        left = (
            cygnet.SELECT(db, AccountTable.name)
            .FROM(AccountTable)
            .WHERE(AccountTable.id > 10)
        )
        right = (
            cygnet.SELECT(db, CityTable.name)
            .FROM(CityTable)
            .WHERE(CityTable.country == "DE")
        )
        await left.UNION(right)
        # Left consumes $1, right consumes $2.
        assert "(accounts.id > $1)" in db.last_sql
        assert "(cities.country = $2)" in db.last_sql
        assert db.last_params == [10, "DE"]

    async def test_set_op_with_explicit_columns_returns_tuples(self):
        """The compound result shape follows the leftmost SELECT's
        column shape: explicit columns → tuples."""
        rows = [("Fred",), ("Wilma",), ("Berlin",)]
        db = FakeDB(rows=rows)
        left = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        right = cygnet.SELECT(db, CityTable.name).FROM(CityTable)
        result = await left.UNION(right)
        assert result == rows

    async def test_plain_operand_is_parenthesised(self):
        """B7 / OQ8: operands render wrapped in parens."""
        db = FakeDB(rows=[])
        left = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        right = cygnet.SELECT(db, CityTable.name).FROM(CityTable)
        await left.UNION(right)
        assert "UNION (SELECT cities.name FROM cities)" in db.last_sql

    async def test_operand_order_by_limit_scoped_by_parens(self):
        """B7 / OQ8: an operand's own ORDER BY / LIMIT must sit INSIDE its
        parentheses so it binds to that operand, not leak into the compound
        (which silently rebound it to the whole UNION, or duplicated the
        left's compound-level ORDER BY into a syntax error)."""
        db = FakeDB(rows=[])
        left = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        right = (
            cygnet.SELECT(db, CityTable.name)
            .FROM(CityTable)
            .ORDER_BY(CityTable.name)
            .LIMIT(5)
        )
        await left.UNION(right)
        assert (
            "UNION (SELECT cities.name FROM cities ORDER BY cities.name ASC LIMIT 5)"
        ) in db.last_sql

    async def test_nested_operand_set_op_is_grouped(self):
        """B7: a set-op used AS an operand keeps its own grouping via the
        operand parens — `z INTERSECT (x UNION y)`, not the flattened
        `(z INTERSECT x) UNION y` that PG infers from unparenthesised SQL."""
        db = FakeDB(rows=[])
        x = cygnet.SELECT(db, AccountTable.name).FROM(AccountTable)
        y = cygnet.SELECT(db, CityTable.name).FROM(CityTable)
        z = cygnet.SELECT(db, AccountTable.email).FROM(AccountTable)
        await z.INTERSECT(x.UNION(y))
        assert (
            "INTERSECT (SELECT accounts.name FROM accounts "
            "UNION (SELECT cities.name FROM cities))"
        ) in db.last_sql
