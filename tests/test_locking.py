# test_locking.py — Row-level locking clauses on SELECT.
#
# PG syntax: `FOR { UPDATE | NO KEY UPDATE | SHARE | KEY SHARE }
#                  [ OF table_name [, ...] ]
#                  [ NOWAIT | SKIP LOCKED ]`
# Cygnet exposes FOR_UPDATE / FOR_SHARE as separate verbs (matching the
# common cases) with kwargs covering OF / NOWAIT / SKIP LOCKED and the
# rarer NO KEY UPDATE / KEY SHARE variants.

from __future__ import annotations

import pytest

import cygnet
from tests.conftest import AccountTable, FakeDB, LogTable


class TestLockingRender:
    async def test_for_update_appends_clause(self):
        """Plain `.FOR_UPDATE()` emits FOR UPDATE at the very end of the
        SELECT — after ORDER BY / LIMIT / OFFSET."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).FOR_UPDATE()
        sql = db.last_sql
        assert sql.endswith("FOR UPDATE")

    async def test_for_share_appends_clause(self):
        """`.FOR_SHARE()` is the read-only counterpart."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).FOR_SHARE()
        assert db.last_sql.endswith("FOR SHARE")

    async def test_for_update_with_nowait(self):
        """`nowait=True` adds NOWAIT — fail-fast when the row is locked."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).FOR_UPDATE(nowait=True)
        assert db.last_sql.endswith("FOR UPDATE NOWAIT")

    async def test_for_update_with_skip_locked(self):
        """`skip_locked=True` adds SKIP LOCKED — silently skip locked rows.
        Common pattern for queue-worker designs."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).FOR_UPDATE(skip_locked=True)
        assert db.last_sql.endswith("FOR UPDATE SKIP LOCKED")

    async def test_for_update_of_single_table(self):
        """`of=T` (single table) restricts the lock to one table in a
        join — accepts a bare TableProxy, not just a tuple."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .FOR_UPDATE(of=AccountTable)
        )
        assert db.last_sql.endswith("FOR UPDATE OF accounts")

    async def test_for_update_of_multiple_tables(self):
        """`of=[T1, T2]` (or tuple) restricts the lock to several tables;
        names are emitted in the listed order."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .FOR_UPDATE(of=[AccountTable, LogTable])
        )
        assert db.last_sql.endswith("FOR UPDATE OF accounts, log_entries")

    async def test_for_update_no_key_variant(self):
        """`no_key=True` downgrades to `FOR NO KEY UPDATE` — useful when the
        SELECT-then-UPDATE doesn't touch the PK, so concurrent FK-referencing
        inserts shouldn't block."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).FOR_UPDATE(no_key=True)
        assert db.last_sql.endswith("FOR NO KEY UPDATE")

    async def test_for_share_key_variant(self):
        """`key=True` downgrades to `FOR KEY SHARE` — the weakest lock,
        blocks only FOR UPDATE / FOR NO KEY UPDATE on the row."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).FOR_SHARE(key=True)
        assert db.last_sql.endswith("FOR KEY SHARE")

    async def test_for_update_combined_options(self):
        """OF + NOWAIT compose; rendered in PG's required order."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .FOR_UPDATE(of=AccountTable, nowait=True)
        )
        assert db.last_sql.endswith("FOR UPDATE OF accounts NOWAIT")

    async def test_lock_appears_after_limit_offset(self):
        """Locking is the last clause in the SELECT pipeline — after
        ORDER BY, LIMIT, OFFSET."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .ORDER_BY(AccountTable.id)
            .LIMIT(10)
            .OFFSET(5)
            .FOR_UPDATE()
        )
        sql = db.last_sql
        # Sanity: the substring order matches the SQL clause order.
        assert sql.index("ORDER BY") < sql.index("LIMIT")
        assert sql.index("LIMIT") < sql.index("OFFSET")
        assert sql.index("OFFSET") < sql.index("FOR UPDATE")


class TestLockingValidation:
    async def test_nowait_and_skip_locked_mutually_exclusive(self):
        """PG rejects both at parse time; we reject client-side for a
        clearer error."""
        db = FakeDB()
        with pytest.raises(ValueError, match="mutually exclusive"):
            cygnet.SELECT(db).FROM(AccountTable).FOR_UPDATE(
                nowait=True, skip_locked=True
            )

    async def test_for_share_nowait_and_skip_locked_mutually_exclusive(self):
        """Same validation applies symmetrically to FOR_SHARE."""
        db = FakeDB()
        with pytest.raises(ValueError, match="mutually exclusive"):
            cygnet.SELECT(db).FROM(AccountTable).FOR_SHARE(
                nowait=True, skip_locked=True
            )

    async def test_double_lock_call_rejected(self):
        """A second FOR_UPDATE / FOR_SHARE on the same builder is rejected:
        only one lock slot, and the user almost certainly meant one of them."""
        db = FakeDB()
        with pytest.raises(ValueError, match="called twice"):
            cygnet.SELECT(db).FROM(AccountTable).FOR_UPDATE().FOR_SHARE()

    async def test_for_update_of_rejects_non_table_source(self):
        """The `of` list must contain TableProxy / CTE objects — anything
        else (e.g., a dataclass class) gets the standard helpful error."""
        db = FakeDB()
        with pytest.raises(TypeError, match="cygnet.Table"):
            from tests.conftest import Account

            cygnet.SELECT(db).FROM(AccountTable).FOR_UPDATE(of=Account)  # type: ignore[arg-type]


class TestLockingWithExistingFeatures:
    async def test_lock_with_where_clause(self):
        """Locking works alongside WHERE — params and lock clause stay
        independent."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.id == 42)
            .FOR_UPDATE(skip_locked=True)
        )
        sql = db.last_sql
        params = db.last_params
        assert "(accounts.id = $1)" in sql
        assert sql.endswith("FOR UPDATE SKIP LOCKED")
        # Lock clause adds no params.
        assert params == [42]

    async def test_lock_with_join_and_of_clause(self):
        """FOR_UPDATE OF restricts locking to a single table in a multi-table
        join — typical pattern when joining a lookup table just for projection."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db, AccountTable.name, LogTable.message)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .FOR_UPDATE(of=LogTable, nowait=True)
        )
        sql = db.last_sql
        assert "INNER JOIN log_entries" in sql
        assert sql.endswith("FOR UPDATE OF log_entries NOWAIT")
