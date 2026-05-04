# test_subquery.py — Subquery predicates: EXISTS / NOT EXISTS / IN (SELECT …).
#
# A SelectBuilder satisfies the SQLRenderable protocol via render_sql, which
# wraps the inner SELECT in parens.  That single decision drives all the
# behaviours tested here:
#   - cygnet.exists(b) / cygnet.not_exists(b) wrap with the keyword
#   - cygnet.op(col, 'IN', b) renders col IN (subq) for free
#   - param numbering threads through the shared params list
#   - validation: only SelectBuilder is acceptable to exists/not_exists

from __future__ import annotations

import pytest

import cygnet
from tests.conftest import AccountTable, FakeDB, LogTable


class TestSubqueryRender:
    async def test_select_builder_satisfies_sqlrenderable(self):
        """SelectBuilder.render_sql produces a parenthesised SELECT."""
        db = FakeDB(rows=[])
        sub = cygnet.SELECT(db, LogTable.id).FROM(LogTable)
        params: list = []
        sql = sub.render_sql(params)
        # Surrounding parens are part of the rendering — no consumer
        # needs to add their own.
        assert sql.startswith("(SELECT ")
        assert sql.endswith(")")
        assert "log_entries.id" in sql
        assert "FROM log_entries" in sql

    async def test_exists_renders_with_keyword(self):
        """`cygnet.exists(b)` → EXISTS (inner_sql)."""
        db = FakeDB(rows=[])
        any_log = (
            cygnet.SELECT(db, cygnet.lit("1"))
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
        )
        await cygnet.SELECT(db).FROM(AccountTable).WHERE(cygnet.exists(any_log))
        sql = db.last_sql
        # No double-parens — the subquery's own parens are reused.
        assert "EXISTS (SELECT 1 FROM log_entries" in sql
        assert "EXISTS ((" not in sql

    async def test_not_exists_renders_with_keyword(self):
        """`cygnet.not_exists(b)` → NOT EXISTS (inner_sql) — the anti-join idiom."""
        db = FakeDB(rows=[])
        any_log = (
            cygnet.SELECT(db, cygnet.lit("1"))
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
        )
        await cygnet.SELECT(db).FROM(AccountTable).WHERE(cygnet.not_exists(any_log))
        assert "NOT EXISTS (SELECT 1 FROM log_entries" in db.last_sql

    async def test_invert_exists_toggles_to_not_exists(self):
        """~exists(b) flips the keyword — preferred over wrapping in another NOT
        because the resulting SQL is the canonical anti-join form."""
        db = FakeDB(rows=[])
        any_log = cygnet.SELECT(db, cygnet.lit("1")).FROM(LogTable)
        await cygnet.SELECT(db).FROM(AccountTable).WHERE(~cygnet.exists(any_log))
        sql = db.last_sql
        assert "NOT EXISTS (" in sql
        # The "NOT (EXISTS …)" PrefixOp shape would have an extra paren
        # group; we explicitly avoid that.
        assert "NOT (EXISTS" not in sql

    async def test_double_invert_collapses_to_exists(self):
        """~~exists(b) is just exists(b) — double negation simplifies."""
        db = FakeDB(rows=[])
        any_log = cygnet.SELECT(db, cygnet.lit("1")).FROM(LogTable)
        await cygnet.SELECT(db).FROM(AccountTable).WHERE(~~cygnet.exists(any_log))
        sql = db.last_sql
        assert "EXISTS (" in sql
        assert "NOT EXISTS" not in sql
        assert "NOT (" not in sql

    async def test_exists_composes_with_and_or(self):
        """EXISTS / NOT EXISTS plug into & and | the same way other predicates do."""
        db = FakeDB(rows=[])
        any_log = cygnet.SELECT(db, cygnet.lit("1")).FROM(LogTable)
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(cygnet.exists(any_log) & (AccountTable.name == "x"))
        )
        sql = db.last_sql
        # AND wraps each side in parens via Predicate.render_sql.
        assert "(EXISTS (" in sql
        assert ") AND (accounts.name = $" in sql

    async def test_in_subquery_via_op(self):
        """`cygnet.op(col, 'IN', subq)` renders as `col IN (SELECT …)` — no
        special-case verb needed; the renderable subquery handles its own parens."""
        db = FakeDB(rows=[])
        active_account_ids = (
            cygnet.SELECT(db, AccountTable.id)
            .FROM(AccountTable)
            .WHERE(AccountTable.email == "alice@x.com")
        )
        await (
            cygnet.SELECT(db)
            .FROM(LogTable)
            .WHERE(cygnet.op(LogTable.account_id, "IN", active_account_ids))
        )
        sql = db.last_sql
        assert "log_entries.account_id IN (SELECT accounts.id FROM accounts" in sql

    async def test_not_in_subquery_via_op(self):
        """`NOT IN` works identically — operator string is opaque to op()."""
        db = FakeDB(rows=[])
        ids = cygnet.SELECT(db, AccountTable.id).FROM(AccountTable)
        await (
            cygnet.SELECT(db)
            .FROM(LogTable)
            .WHERE(cygnet.op(LogTable.account_id, "NOT IN", ids))
        )
        assert "log_entries.account_id NOT IN (SELECT" in db.last_sql

    async def test_param_numbering_threads_inner_then_outer(self):
        """Inner subquery params come first (textually they appear in the
        WHERE clause, before any outer-WHERE on a later predicate); $N
        numbering stays monotonic across the whole statement."""
        db = FakeDB(rows=[])
        # Inner WHERE consumes one param ($1)
        active_ids = (
            cygnet.SELECT(db, AccountTable.id)
            .FROM(AccountTable)
            .WHERE(AccountTable.email == "alice@x.com")
        )
        await (
            cygnet.SELECT(db)
            .FROM(LogTable)
            .WHERE(cygnet.op(LogTable.account_id, "IN", active_ids))
            # Outer WHERE consumes $2 (appears textually after the IN-subquery).
            .WHERE(LogTable.id > 100)
        )
        sql = db.last_sql
        params = db.last_params
        assert "(accounts.email = $1)" in sql
        assert "(log_entries.id > $2)" in sql
        assert params == ["alice@x.com", 100]

    async def test_correlated_subquery_references_outer_table(self):
        """The classic correlated EXISTS pattern: inner WHERE references the
        outer table directly (no LATERAL needed)."""
        db = FakeDB(rows=[])
        any_log = (
            cygnet.SELECT(db, cygnet.lit("1"))
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
        )
        await cygnet.SELECT(db).FROM(AccountTable).WHERE(cygnet.exists(any_log))
        sql = db.last_sql
        # The reference to the outer accounts.id renders as a plain column
        # ref — no params, since both sides are columns.
        assert "log_entries.account_id = accounts.id" in sql

    async def test_scalar_subquery_in_select_list(self):
        """A SelectBuilder used as a SELECT-list column emits as a scalar
        subquery (parenthesised inline), so callers can write
        `SELECT col, (subquery) FROM …` without any wrapper."""
        db = FakeDB(rows=[])
        log_count = (
            cygnet.SELECT(db, cygnet.fn("count")(cygnet.lit("*")))
            .FROM(LogTable)
            .WHERE(LogTable.account_id == AccountTable.id)
        )
        await cygnet.SELECT(db, AccountTable.name, log_count).FROM(AccountTable)
        sql = db.last_sql
        assert "SELECT accounts.name, (SELECT count(*) FROM log_entries" in sql


class TestSubqueryValidation:
    async def test_exists_rejects_non_select_builder(self):
        """Catching the wrong-type case at the call site beats a confusing
        AttributeError several frames deep at render time."""
        with pytest.raises(TypeError, match="SelectBuilder"):
            cygnet.exists(AccountTable)  # type: ignore[arg-type]

    async def test_not_exists_rejects_non_select_builder(self):
        with pytest.raises(TypeError, match="SelectBuilder"):
            cygnet.not_exists("SELECT 1")  # type: ignore[arg-type]

    async def test_exists_rejects_insert_builder(self):
        """Only SELECTs are subqueriable; an INSERT/UPDATE/DELETE in a
        subquery position would be malformed SQL."""
        db = FakeDB()
        with pytest.raises(TypeError, match="SelectBuilder"):
            cygnet.exists(cygnet.INSERT(db))  # type: ignore[arg-type]
