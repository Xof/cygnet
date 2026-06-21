# test_lateral.py — JOIN LATERAL / LEFT JOIN LATERAL on SelectBuilder.
# LATERAL subqueries reference columns from preceding FROM/JOIN tables;
# Cygnet's `cygnet.lateral(name, inner)` wraps a SelectBuilder into a
# CTE-shaped object whose column attrs flow into the outer SELECT.

from __future__ import annotations

import pytest

import cygnet
from tests.conftest import AccountTable, FakeDB, LogTable


class TestLateralRender:
    async def test_inner_join_lateral_renders_subquery_inline(self):
        """The inner SelectBuilder's SQL is inlined into the JOIN clause
        (unlike CTEs which prefix as WITH)."""
        db = FakeDB(rows=[])
        # "Most recent log per account" — the canonical LATERAL pattern.
        recent = (
            cygnet.SELECT(db, LogTable.message)
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
            .ORDER_BY(LogTable.id, DESC=True)
            .LIMIT(1)
        )
        recent_lat = cygnet.lateral("recent", recent, columns=["message"])

        await (
            cygnet.SELECT(db, AccountTable.name, recent_lat.message)
            .FROM(AccountTable)
            .JOIN_LATERAL(recent_lat)
        )
        sql = db.last_sql
        # The lateral subquery is inlined; outer SELECT references its
        # alias-prefixed column.
        assert "INNER JOIN LATERAL (" in sql
        assert ") recent ON true" in sql
        assert "recent.message" in sql
        # Inner correlation refs the outer table directly:
        assert "log_entries.account_id = accounts.id" in sql

    async def test_left_join_lateral_for_optional_subquery(self):
        """LEFT JOIN LATERAL produces NULL rows for outer rows whose
        subquery returns nothing — useful for "top-N per group" where
        some groups are empty."""
        db = FakeDB(rows=[])
        recent = (
            cygnet.SELECT(db, LogTable.message)
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
            .LIMIT(1)
        )
        lat = cygnet.lateral("recent", recent, columns=["message"])
        await (
            cygnet.SELECT(db, AccountTable.name, lat.message)
            .FROM(AccountTable)
            .LEFT_JOIN_LATERAL(lat)
        )
        assert "LEFT JOIN LATERAL (" in db.last_sql

    async def test_explicit_on_clause(self):
        """ON defaults to `true` (PG syntax requirement) but the user
        can override it for the rare case where the LATERAL needs an
        outer-side filter."""
        db = FakeDB(rows=[])
        recent = (
            cygnet.SELECT(db, LogTable.message)
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
            .LIMIT(1)
        )
        lat = cygnet.lateral("r", recent, columns=["message"])
        await (
            cygnet.SELECT(db, AccountTable.name, lat.message)
            .FROM(AccountTable)
            .LEFT_JOIN_LATERAL(lat, ON=AccountTable.id > 0)
        )
        # The custom ON clause shows up instead of the default `true`.
        # Predicate rendering for a single comparison is bare (no outer
        # parens) — consistent with how regular JOIN ... ON clauses render.
        assert "LEFT JOIN LATERAL (" in db.last_sql
        assert "ON accounts.id > $" in db.last_sql
        assert ") r ON " in db.last_sql  # alias before ON, not `true`

    async def test_param_numbering_monotonic_through_lateral(self):
        """Inner subquery params come BEFORE the outer SELECT's WHERE
        params, with $N flowing through monotonically."""
        db = FakeDB(rows=[])
        recent = (
            cygnet.SELECT(db, LogTable.message)
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
            .WHERE(LogTable.id > 100)  # inner consumes $1
            .LIMIT(1)
        )
        lat = cygnet.lateral("r", recent, columns=["message"])
        await (
            cygnet.SELECT(db, AccountTable.name, lat.message)
            .FROM(AccountTable)
            .LEFT_JOIN_LATERAL(lat)
            .WHERE(AccountTable.id > 50)  # outer consumes $2
        )
        sql = db.last_sql
        assert "(log_entries.id > $1)" in sql
        assert "(accounts.id > $2)" in sql
        assert db.last_params == [100, 50]

    async def test_column_inference_from_inner_select(self):
        """When the inner SelectBuilder has explicit ColumnProxy refs,
        column names are inferred (same as CTE)."""
        db = FakeDB(rows=[])
        inner = (
            cygnet.SELECT(db, LogTable.id, LogTable.message)
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
        )
        lat = cygnet.lateral("r", inner)  # no explicit columns
        # Both inferred names work as column refs.
        params: list = []
        assert lat.id.render_sql(params) == "r.id"
        assert lat.message.render_sql(params) == "r.message"


class TestLateralValidation:
    async def test_join_lateral_rejects_non_lateral(self):
        """JOIN_LATERAL is specifically for Lateral objects; passing a
        plain TableProxy gets a clear error rather than a silent
        miss-render."""
        db = FakeDB()
        with pytest.raises(TypeError, match="Lateral"):
            cygnet.SELECT(db).FROM(AccountTable).JOIN_LATERAL(LogTable)  # type: ignore[arg-type]

    async def test_left_join_lateral_rejects_non_lateral(self):
        db = FakeDB()
        with pytest.raises(TypeError, match="Lateral"):
            cygnet.SELECT(db).FROM(AccountTable).LEFT_JOIN_LATERAL(  # type: ignore[arg-type]
                LogTable
            )


class TestLateralWithExistingFeatures:
    async def test_lateral_alongside_regular_join(self):
        """A SELECT can mix regular JOINs and LATERAL JOINs in any order.
        The executor's per-join isinstance check picks the right
        rendering for each."""
        db = FakeDB(rows=[])
        recent = (
            cygnet.SELECT(db, LogTable.message)
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
            .LIMIT(1)
        )
        lat = cygnet.lateral("r", recent, columns=["message"])
        await (
            cygnet.SELECT(db, AccountTable.name, lat.message)
            .FROM(AccountTable)
            .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .LEFT_JOIN_LATERAL(lat)
        )
        sql = db.last_sql
        assert "LEFT JOIN log_entries ON" in sql
        assert "LEFT JOIN LATERAL (" in sql
        # LATERAL appears AFTER the regular LEFT JOIN (declaration order).
        assert sql.index("LEFT JOIN log_entries") < sql.index("LEFT JOIN LATERAL")
