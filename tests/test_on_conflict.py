# test_on_conflict.py — Unit tests for the ON CONFLICT clause family on
# InsertBuilder.  Covers SQL generation, the empty-RETURNING semantics
# split (raises without ON CONFLICT, returns None with it), and the
# scope restriction that ON CONFLICT only pairs with single-row VALUES.

from __future__ import annotations

import pytest

import cygnet
from tests.conftest import Account, AccountTable, Event, EventTable, FakeDB


class TestOnConflictRender:
    async def test_do_nothing_no_target(self):
        """`ON CONFLICT_DO_NOTHING` shorthand: no target, no action body —
        any conflict on any unique constraint is silently skipped."""
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="fred@example.com"))
            .ON_CONFLICT_DO_NOTHING()
        )
        assert "ON CONFLICT DO NOTHING" in db.last_sql
        # No target parens between ON CONFLICT and DO NOTHING.
        assert "ON CONFLICT (" not in db.last_sql
        # RETURNING still emitted on DBKey models.
        assert "RETURNING id" in db.last_sql

    async def test_do_nothing_with_column_target(self):
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="fred@example.com"))
            .ON_CONFLICT(AccountTable.email)
            .DO_NOTHING()
        )
        assert "ON CONFLICT (email) DO NOTHING" in db.last_sql

    async def test_do_nothing_with_constraint_target(self):
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="fred@example.com"))
            .ON_CONFLICT_CONSTRAINT("uq_accounts_email")
            .DO_NOTHING()
        )
        assert "ON CONFLICT ON CONSTRAINT uq_accounts_email DO NOTHING" in db.last_sql

    async def test_do_update_with_kwargs(self):
        """DO UPDATE with literal values from kwargs: SET col = $N."""
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="fred@example.com"))
            .ON_CONFLICT(AccountTable.email)
            .DO_UPDATE(name="Updated Fred")
        )
        sql = db.last_sql
        assert "ON CONFLICT (email) DO UPDATE SET name = " in sql
        # The new SET param is the LAST $N because VALUES params come first.
        assert "Updated Fred" in db.last_params
        # The Updated Fred kwarg should be the last bound param.
        assert db.last_params[-1] == "Updated Fred"

    async def test_do_update_from_excluded(self):
        """SET col = EXCLUDED.col for the listed columns — the
        save()-style "use the new row's value" upsert pattern."""
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="fred@example.com"))
            .ON_CONFLICT(AccountTable.email)
            .DO_UPDATE_FROM_EXCLUDED(AccountTable.name, AccountTable.email)
        )
        sql = db.last_sql
        assert "DO UPDATE SET name = EXCLUDED.name, email = EXCLUDED.email" in sql

    async def test_multi_column_target(self):
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="fred@example.com"))
            .ON_CONFLICT(AccountTable.name, AccountTable.email)
            .DO_NOTHING()
        )
        assert "ON CONFLICT (name, email) DO NOTHING" in db.last_sql

    async def test_param_numbering_continues_after_values(self):
        """DO UPDATE SET kwargs append params AFTER VALUES, so $N
        numbering stays monotonic across the whole statement."""
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="fred@example.com"))
            .ON_CONFLICT(AccountTable.email)
            .DO_UPDATE(name="x", email="y")
        )
        sql = db.last_sql
        # VALUES has 2 params (name + email; id is DBKey-None and excluded);
        # DO UPDATE adds 2 more, so the SET clause should reference $3, $4.
        assert "DO UPDATE SET name = $3, email = $4" in sql


class TestOnConflictValidation:
    async def test_on_conflict_requires_columns(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="at least one column"):
            cygnet.INSERT(db).INTO(AccountTable).ON_CONFLICT()

    async def test_on_conflict_and_constraint_mutually_exclusive(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="mutually exclusive"):
            cygnet.INSERT(db).INTO(AccountTable).ON_CONFLICT(
                AccountTable.email
            ).ON_CONFLICT_CONSTRAINT("uq")

    async def test_do_nothing_without_target_uses_shorthand(self):
        """DO_NOTHING without preceding ON_CONFLICT/ON_CONFLICT_CONSTRAINT
        suggests the user meant ON_CONFLICT_DO_NOTHING."""
        db = FakeDB()
        with pytest.raises(ValueError, match="ON_CONFLICT_DO_NOTHING"):
            cygnet.INSERT(db).INTO(AccountTable).DO_NOTHING()

    async def test_do_update_requires_target(self):
        """PG can't do DO UPDATE without knowing the conflict target."""
        db = FakeDB()
        with pytest.raises(ValueError, match="preceding ON_CONFLICT"):
            cygnet.INSERT(db).INTO(AccountTable).DO_UPDATE(name="x")

    async def test_do_update_requires_fields(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="at least one field"):
            cygnet.INSERT(db).INTO(AccountTable).ON_CONFLICT(
                AccountTable.email
            ).DO_UPDATE()

    async def test_do_update_unknown_field_raises(self):
        """Same field validation as UPDATE/SET — typos are rejected
        client-side, not silently dropped."""
        db = FakeDB(rows=[(1,)])
        with pytest.raises(ValueError, match="Unknown field"):
            await (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .VALUES(Account(id=None, name="Fred", email="fred@example.com"))
                .ON_CONFLICT(AccountTable.email)
                .DO_UPDATE(nmae="typo")
            )

    async def test_do_update_from_excluded_requires_cols(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="at least one column"):
            cygnet.INSERT(db).INTO(AccountTable).ON_CONFLICT(
                AccountTable.email
            ).DO_UPDATE_FROM_EXCLUDED()

    async def test_on_conflict_rejected_with_bulk_values(self):
        """BULK_VALUES + ON_CONFLICT is out of scope (initial pass);
        raise rather than emit potentially-incorrect SQL."""
        db = FakeDB(rows=[(1,)])
        accs = [Account(id=None, name="Fred", email="fred@example.com")]
        with pytest.raises(ValueError, match="not yet supported"):
            await (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .BULK_VALUES(accs)
                .ON_CONFLICT_DO_NOTHING()
            )

    async def test_on_conflict_rejected_with_select(self):
        db = FakeDB(rows=[(1,)])
        source = cygnet.SELECT(db, AccountTable.name, AccountTable.email).FROM(
            AccountTable
        )
        with pytest.raises(ValueError, match="not yet supported"):
            await (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .SELECT(source)
                .ON_CONFLICT_DO_NOTHING()
            )


class TestOnConflictRuntime:
    async def test_empty_returning_returns_none(self):
        """ON CONFLICT DO NOTHING on a row that already exists: PG
        returns no rows for RETURNING, and Cygnet returns None instead
        of raising (which is what plain INSERT does on empty RETURNING)."""
        # Empty rows simulates the conflict-skipped case.
        db = FakeDB(rows=[])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        result = await (
            cygnet.INSERT(db).INTO(AccountTable).VALUES(acc).ON_CONFLICT_DO_NOTHING()
        )
        assert result is None
        # The object's PK is left as None — caller can detect the skip.
        assert acc.id is None

    async def test_normal_insert_still_raises_on_empty_returning(self):
        """Without ON CONFLICT, an empty RETURNING is still a bug —
        the Phase 2 silent-failure rule remains in force."""
        db = FakeDB(rows=[])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        with pytest.raises(RuntimeError, match="produced no row"):
            await cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)

    async def test_appkey_with_do_nothing(self):
        """AppKey models don't emit RETURNING, so the empty-RETURNING
        question doesn't apply.  Just verify the SQL renders correctly."""
        db = FakeDB()
        await (
            cygnet.INSERT(db)
            .INTO(EventTable)
            .VALUES(Event(id="e1", name="Launch"))
            .ON_CONFLICT(EventTable.id)
            .DO_NOTHING()
        )
        assert "ON CONFLICT (id) DO NOTHING" in db.last_sql
        assert "RETURNING" not in db.last_sql


class TestOnConflictActionClobber:
    """S35: re-setting the ON CONFLICT action is rejected, mirroring the
    second-call rejection on FOR_UPDATE/FOR_SHARE.  Chaining two terminal
    actions previously clobbered the first silently."""

    async def test_do_update_then_do_nothing_raises(self):
        b = (
            cygnet.INSERT(FakeDB(rows=[(1,)]))
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="f@x.com"))
            .ON_CONFLICT(AccountTable.email)
            .DO_UPDATE(name="x")
        )
        with pytest.raises(ValueError, match="action already set"):
            b.DO_NOTHING()

    async def test_do_nothing_then_do_update_raises(self):
        b = (
            cygnet.INSERT(FakeDB(rows=[(1,)]))
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="f@x.com"))
            .ON_CONFLICT_DO_NOTHING()
        )
        with pytest.raises(ValueError, match="action already set"):
            b.DO_UPDATE(name="x")

    async def test_single_action_still_works(self):
        """Sanity: a single action is fine — the guard fires only on a
        second one."""
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(Account(id=None, name="Fred", email="f@x.com"))
            .ON_CONFLICT(AccountTable.email)
            .DO_NOTHING()
        )
        assert "ON CONFLICT (email) DO NOTHING" in db.last_sql
