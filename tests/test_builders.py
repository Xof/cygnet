# test_builders.py — Tests for SQL generation and execution across all builders.
#
# Each test class targets a SQL verb (SELECT, INSERT, UPDATE, DELETE, TRUNCATE)
# or a cross-cutting concern (literals, .sql(), create, save, get, operators).
# Tests use FakeDB to capture the generated SQL and params without hitting a
# database — the integration tests in tests/integration/ cover real PostgreSQL.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import AppKey, DBKey
from tests.conftest import (
    Account,
    AccountTable,
    Event,
    EventTable,
    FakeDB,
    LogTable,
    TaggedAccount,
    TaggedTable,
)


@dataclasses.dataclass
class Customer:
    id: Annotated[int, DBKey]
    name: str


@dataclasses.dataclass
class Order:
    id: Annotated[int, DBKey]
    customer_id: Annotated[int, cygnet.ForeignKey(Customer)]
    amount: float


CustomerTable = cygnet.Table(Customer)
OrderTable = cygnet.Table(Order)


class TestSelectSQL:
    async def test_simple_select_all(self):
        db = FakeDB(rows=[(1, "Fred", "fred@example.com")])
        await cygnet.SELECT(db).FROM(AccountTable)
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email FROM accounts"
        )

    async def test_select_with_where(self):
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).WHERE(AccountTable.name == "Fred")
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email"
            " FROM accounts WHERE (accounts.name = $1)"
        )
        assert db.last_params == ["Fred"]

    async def test_select_multiple_where(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.name == "Fred")
            .WHERE(AccountTable.id > 5)
        )
        assert "WHERE" in db.last_sql
        assert db.last_params == ["Fred", 5]

    async def test_select_columnar(self):
        db = FakeDB(rows=[(1, "Fred")])
        await cygnet.SELECT(db, AccountTable.id, AccountTable.name).FROM(AccountTable)
        assert db.last_sql == "SELECT accounts.id, accounts.name FROM accounts"

    async def test_inner_join(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        )
        assert "INNER JOIN log_entries ON" in db.last_sql

    async def test_left_join(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        )
        assert "LEFT JOIN log_entries ON" in db.last_sql

    async def test_aliased_from_renders_as_clause(self):
        """T.AS('alias') puts `tablename AS alias` in FROM and uses the
        alias on the left of the dot for column refs."""
        db = FakeDB(rows=[])
        AT = AccountTable.AS("a1")
        await cygnet.SELECT(db).FROM(AT).WHERE(AT.name == "Fred")
        assert "FROM accounts AS a1" in db.last_sql
        assert "a1.name = $1" in db.last_sql
        # The unaliased AccountTable must be unaffected — aliasing returns
        # a fresh proxy, never mutates the canonical one.
        assert "accounts.name" not in db.last_sql

    async def test_aliased_self_join_disambiguates_columns(self):
        """The motivating case: joining the same table twice via aliases
        produces non-ambiguous column references that PG can parse."""
        db = FakeDB(rows=[])
        L1 = LogTable.AS("l1")
        L2 = LogTable.AS("l2")
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .JOIN(L1, ON=AccountTable.id == L1.account_id)
            .JOIN(L2, ON=AccountTable.id == L2.account_id)
        )
        sql = db.last_sql
        assert "INNER JOIN log_entries AS l1 ON" in sql
        assert "INNER JOIN log_entries AS l2 ON" in sql
        # Both joins should produce ON clauses scoped to their alias —
        # the unaliased "log_entries.account_id" must not appear.
        assert "log_entries.account_id" not in sql
        assert "l1.account_id" in sql
        assert "l2.account_id" in sql

    async def test_alias_is_not_cached(self):
        """Aliased proxies must not poison the singleton cache used for the
        canonical Table(cls) lookup."""
        AT1 = AccountTable.AS("x")
        AT2 = AccountTable.AS("y")
        # Each .AS() returns a fresh proxy (different aliases produce
        # different objects).
        assert AT1 is not AT2
        # The canonical proxy is unaffected — Table(Account) still returns
        # the same singleton across calls.
        assert cygnet.Table(Account) is AccountTable
        # And the canonical proxy has no alias.
        assert AccountTable._alias is None

    async def test_order_by_asc(self):
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).ORDER_BY(AccountTable.name)
        assert db.last_sql.endswith("ORDER BY accounts.name ASC")

    async def test_order_by_desc(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db).FROM(AccountTable).ORDER_BY(AccountTable.name, DESC=True)
        )
        assert db.last_sql.endswith("ORDER BY accounts.name DESC")

    async def test_limit(self):
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).LIMIT(10)
        assert db.last_sql.endswith("LIMIT 10")

    async def test_group_by_requires_columns(self):
        db = FakeDB(rows=[])
        with pytest.raises(ValueError, match="GROUP_BY requires explicit"):
            cygnet.SELECT(db).FROM(AccountTable).GROUP_BY(AccountTable.name)

    async def test_group_by(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db, AccountTable.name)
            .FROM(AccountTable)
            .GROUP_BY(AccountTable.name)
        )
        assert "GROUP BY accounts.name" in db.last_sql

    async def test_clause_order(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db, AccountTable.name)
            .FROM(AccountTable)
            .GROUP_BY(AccountTable.name)
            .ORDER_BY(AccountTable.name)
            .LIMIT(5)
        )
        sql = db.last_sql
        assert sql.index("GROUP BY") < sql.index("ORDER BY") < sql.index("LIMIT")

    async def test_multiple_where_exact_sql(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.name == "Fred")
            .WHERE(AccountTable.id > 5)
        )
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email FROM accounts "
            "WHERE (accounts.name = $1) AND (accounts.id > $2)"
        )

    async def test_multiple_order_by_columns(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .ORDER_BY(AccountTable.name, AccountTable.id)
        )
        assert db.last_sql.endswith("ORDER BY accounts.name ASC, accounts.id ASC")

    async def test_join_on_renders_column_refs(self):
        """JOIN ON should render column references, not parameter placeholders."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        )
        assert "ON accounts.id = log_entries.account_id" in db.last_sql
        assert db.last_params == []

    async def test_select_with_all(self):
        """SELECT with WHERE(cygnet.all) works — just omits WHERE clause."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).WHERE(cygnet.all)
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email FROM accounts"
        )
        assert db.last_params == []

    async def test_select_all_mixed_with_real_predicate_raises(self):
        """SELECT must reject cygnet.all combined with a real predicate, the
        same rule UPDATE/DELETE enforce.  Previously the _All sentinel was
        silently filtered out and the real predicate ran alone."""
        db = FakeDB(rows=[])
        with pytest.raises(ValueError, match="cygnet.all cannot be combined"):
            await (
                cygnet.SELECT(db)
                .FROM(AccountTable)
                .WHERE(cygnet.all)
                .WHERE(AccountTable.id == 1)
            )

    async def test_select_without_from_raises(self):
        """Awaiting a SELECT without FROM must raise a clear ValueError, not
        the AttributeError-on-NoneType the user used to see."""
        db = FakeDB(rows=[])
        with pytest.raises(ValueError, match="SELECT requires FROM"):
            await cygnet.SELECT(db)

    async def test_limit_rejects_negative(self):
        """LIMIT(-N) must fail at the call site, not in PostgreSQL."""
        db = FakeDB(rows=[])
        with pytest.raises(ValueError, match="LIMIT must be non-negative"):
            cygnet.SELECT(db).FROM(AccountTable).LIMIT(-1)

    async def test_offset(self):
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).LIMIT(10).OFFSET(20)
        assert db.last_sql.endswith("LIMIT 10 OFFSET 20")

    async def test_offset_rejects_negative(self):
        db = FakeDB(rows=[])
        with pytest.raises(ValueError, match="OFFSET must be non-negative"):
            cygnet.SELECT(db).FROM(AccountTable).OFFSET(-1)

    async def test_distinct(self):
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).DISTINCT()
        assert db.last_sql.startswith("SELECT DISTINCT ")

    async def test_having(self):
        """HAVING follows GROUP BY and ANDs across chained calls."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db, AccountTable.name)
            .FROM(AccountTable)
            .GROUP_BY(AccountTable.name)
            .HAVING(cygnet.lit("count(*) > 1"))
        )
        assert "GROUP BY accounts.name" in db.last_sql
        assert "HAVING (count(*) > 1)" in db.last_sql
        idx_group = db.last_sql.index("GROUP BY")
        idx_having = db.last_sql.index("HAVING")
        assert idx_having > idx_group


class TestLiteralSQL:
    async def test_lit_in_where(self):
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(AccountTable).WHERE(cygnet.lit("id > 10"))
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email"
            " FROM accounts WHERE (id > 10)"
        )
        assert db.last_params == []

    async def test_lit_combined_with_predicate(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.name == "Fred")
            .WHERE(cygnet.lit("email IS NOT NULL"))
        )
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email FROM accounts "
            "WHERE (accounts.name = $1) AND (email IS NOT NULL)"
        )
        assert db.last_params == ["Fred"]

    async def test_lit_in_select_columns(self):
        db = FakeDB(rows=[("Fred", 1)])
        await cygnet.SELECT(db, AccountTable.name, cygnet.lit("1 AS one")).FROM(
            AccountTable
        )
        assert db.last_sql == "SELECT accounts.name, 1 AS one FROM accounts"
        assert db.last_params == []

    async def test_lit_only_select(self):
        db = FakeDB(rows=[(5,)])
        await cygnet.SELECT(db, cygnet.lit("COUNT(*)")).FROM(AccountTable)
        assert db.last_sql == "SELECT COUNT(*) FROM accounts"
        assert db.last_params == []

    async def test_lit_in_order_by(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db).FROM(AccountTable).ORDER_BY(cygnet.lit("created_at DESC"))
        )
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email"
            " FROM accounts ORDER BY created_at DESC"
        )
        assert db.last_params == []

    async def test_is_null_in_order_by_with_desc_applies_direction(self):
        """ORDER_BY with a SuffixOp and DESC=True must append DESC.

        Realistic case: NULLs-first ordering via `(col IS NULL) DESC`.  The
        previous implementation only appended ASC/DESC to ColumnProxy
        instances, silently dropping the direction for is_null/ops/op
        expressions.
        """
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .ORDER_BY(cygnet.is_null(AccountTable.name), DESC=True)
        )
        assert db.last_sql.endswith("ORDER BY accounts.name IS NULL DESC")

    async def test_lit_in_group_by(self):
        db = FakeDB(rows=[("Fred", 3)])
        await (
            cygnet.SELECT(db, AccountTable.name, cygnet.lit("COUNT(*) AS cnt"))
            .FROM(AccountTable)
            .GROUP_BY(cygnet.lit("name"))
        )
        assert "GROUP BY name" in db.last_sql
        assert db.last_params == []


class TestInsertSQL:
    async def test_insert_from_object(self):
        db = FakeDB(rows=[(42,)])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        await cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)
        # id must be absent from the column list but present in RETURNING
        col_list = db.last_sql.split("VALUES")[0]
        assert "id" not in col_list
        assert "RETURNING id" in db.last_sql
        assert acc.id == 42

    async def test_insert_from_kwargs(self):
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(name="Fred", email="fred@example.com")
        )
        assert "name" in db.last_sql
        assert "email" in db.last_sql
        assert "RETURNING id" in db.last_sql

    async def test_insert_appkey_none_raises(self):
        db = FakeDB()
        ev = Event(id=None, name="Launch")
        with pytest.raises(ValueError, match="AppKey"):
            await cygnet.INSERT(db).INTO(EventTable).VALUES(ev)

    async def test_insert_appkey_with_value(self):
        db = FakeDB(rows=[])
        ev = Event(id="evt-123", name="Launch")
        await cygnet.INSERT(db).INTO(EventTable).VALUES(ev)
        assert "evt-123" in db.last_params
        assert "RETURNING" not in db.last_sql

    async def test_insert_column_rename(self):
        """INSERT should use the DB column name, not the Python attr name."""
        db = FakeDB(rows=[(1,)])
        obj = TaggedAccount(account_id=None, tag="vip")
        await cygnet.INSERT(db).INTO(TaggedTable).VALUES(obj)
        assert "tag_name" in db.last_sql
        assert "tag" not in db.last_sql.split("(")[1].split("tag_name")[0]

    async def test_insert_explicit_dbkey_value(self):
        """Inserting a DBKey with an explicit (non-None) value includes it."""
        db = FakeDB(rows=[(42,)])
        acc = Account(id=42, name="Fred", email="fred@example.com")
        await cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)
        assert 42 in db.last_params

    async def test_values_obj_and_kwargs_raises(self):
        """Supplying both obj and kwargs raises — pick one."""
        db = FakeDB(rows=[(1,)])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        with pytest.raises(ValueError, match="either an object or kwargs"):
            cygnet.INSERT(db).INTO(AccountTable).VALUES(acc, name="Wilma")

    async def test_values_unknown_kwarg_raises(self):
        """A typo'd kwarg is rejected up front, not silently dropped."""
        db = FakeDB(rows=[(1,)])
        with pytest.raises(ValueError, match="Unknown field"):
            await (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .VALUES(nmae="Fred", email="fred@example.com")
            )

    async def test_bulk_values_emits_multi_row_insert(self):
        """BULK_VALUES emits a single INSERT with one VALUES tuple per object."""
        db = FakeDB(rows=[(1,), (2,), (3,)])
        accs = [
            Account(id=None, name="Fred", email="fred@example.com"),
            Account(id=None, name="Wilma", email="wilma@example.com"),
            Account(id=None, name="Barney", email="barney@example.com"),
        ]
        result = await cygnet.INSERT(db).INTO(AccountTable).BULK_VALUES(accs)
        assert "VALUES ($1, $2), ($3, $4), ($5, $6)" in db.last_sql
        assert "RETURNING id" in db.last_sql
        # Each object's PK should be populated in input order.
        assert accs[0].id == 1
        assert accs[1].id == 2
        assert accs[2].id == 3
        assert result == [1, 2, 3]

    async def test_bulk_values_empty_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="at least one object"):
            cygnet.INSERT(db).INTO(AccountTable).BULK_VALUES([])

    async def test_bulk_values_wrong_type_raises(self):
        @dataclasses.dataclass
        class NotAccount:
            id: Annotated[int, DBKey]
            x: str

        db = FakeDB(rows=[(1,)])
        with pytest.raises(TypeError, match="expected Account"):
            await (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .BULK_VALUES([NotAccount(id=None, x="oops")])
            )

    async def test_bulk_values_with_appkey_no_returning(self):
        db = FakeDB()
        events = [
            Event(id="e1", name="Launch"),
            Event(id="e2", name="Liftoff"),
        ]
        result = await cygnet.INSERT(db).INTO(EventTable).BULK_VALUES(events)
        assert "VALUES ($1, $2), ($3, $4)" in db.last_sql
        assert "RETURNING" not in db.last_sql
        assert result is None

    async def test_bulk_values_combined_with_values_raises(self):
        db = FakeDB(rows=[(1,)])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        with pytest.raises(ValueError, match="cannot combine"):
            cygnet.INSERT(db).INTO(AccountTable).VALUES(acc).BULK_VALUES([acc])

    async def test_bulk_values_returning_count_mismatch_raises(self):
        # Adapter returned only 1 row for 2 objects — surfaces as RuntimeError.
        db = FakeDB(rows=[(1,)])
        accs = [
            Account(id=None, name="Fred", email="fred@example.com"),
            Account(id=None, name="Wilma", email="wilma@example.com"),
        ]
        with pytest.raises(RuntimeError, match="expected 2 RETURNING rows"):
            await cygnet.INSERT(db).INTO(AccountTable).BULK_VALUES(accs)

    async def test_insert_select_basic(self):
        """INSERT INTO target (cols) SELECT cols FROM source — column names
        inferred from the source's ColumnProxy projection."""
        db = FakeDB(rows=[(1,), (2,)])
        # Pretend we're cloning rows: SELECT name, email FROM accounts
        # → INSERT INTO accounts (name, email) SELECT name, email FROM accounts
        source = cygnet.SELECT(db, AccountTable.name, AccountTable.email).FROM(
            AccountTable
        )
        result = await cygnet.INSERT(db).INTO(AccountTable).SELECT(source)
        assert (
            "INSERT INTO accounts (name, email) "
            "SELECT accounts.name, accounts.email FROM accounts" in db.last_sql
        )
        assert "RETURNING id" in db.last_sql
        assert result == [1, 2]

    async def test_insert_select_with_explicit_columns(self):
        """User-supplied column list aligns with the source projection."""
        db = FakeDB(rows=[])
        source = cygnet.SELECT(
            db, cygnet.fn("upper")(AccountTable.name), AccountTable.email
        ).FROM(AccountTable)
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .SELECT(source, columns=["name", "email"])
        )
        assert (
            "INSERT INTO accounts (name, email) "
            "SELECT upper(accounts.name), accounts.email FROM accounts" in db.last_sql
        )

    async def test_insert_select_column_inference_fails_on_opaque(self):
        """Source projecting fn() / lit() can't be inferred — must pass columns."""
        db = FakeDB(rows=[])
        source = cygnet.SELECT(db, cygnet.fn("count")(AccountTable.id)).FROM(
            AccountTable
        )
        with pytest.raises(ValueError, match="can't infer a target column"):
            await cygnet.INSERT(db).INTO(AccountTable).SELECT(source)

    async def test_insert_select_unknown_column_raises(self):
        db = FakeDB(rows=[])
        source = cygnet.SELECT(db, AccountTable.id).FROM(AccountTable)
        with pytest.raises(ValueError, match="Unknown columns"):
            await cygnet.INSERT(db).INTO(AccountTable).SELECT(source, columns=["nope"])

    async def test_insert_select_combined_with_values_raises(self):
        db = FakeDB(rows=[])
        source = cygnet.SELECT(db, AccountTable.id).FROM(AccountTable)
        with pytest.raises(ValueError, match="cannot combine"):
            (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .VALUES(name="x", email="y")
                .SELECT(source)
            )

    async def test_insert_select_param_numbering(self):
        """Inner SELECT's bind params are emitted before INSERT's column
        list (which has none of its own), so $N starts at 1 in the inner."""
        db = FakeDB(rows=[])
        source = (
            cygnet.SELECT(db, AccountTable.name, AccountTable.email)
            .FROM(AccountTable)
            .WHERE(AccountTable.id > 100)
        )
        await cygnet.INSERT(db).INTO(AccountTable).SELECT(source)
        assert "WHERE (accounts.id > $1)" in db.last_sql
        assert db.last_params == [100]


class TestUpdateSQL:
    async def test_update_kwargs(self):
        db = FakeDB()
        await (
            cygnet.UPDATE(db)
            .SET(AccountTable, name="Wilma")
            .WHERE(AccountTable.id == 1)
        )
        assert "UPDATE accounts SET" in db.last_sql
        assert "WHERE" in db.last_sql
        assert "Wilma" in db.last_params

    async def test_update_object(self):
        db = FakeDB()
        acc = Account(id=1, name="Wilma", email="wilma@example.com")
        await cygnet.UPDATE(db).SET(AccountTable, acc).WHERE(AccountTable.id == 1)
        assert "name" in db.last_sql
        assert "email" in db.last_sql
        # pk should not appear in SET clause, only in WHERE
        assert db.last_sql.count("id") == 1

    async def test_update_wrong_type_raises(self):
        db = FakeDB()

        @dataclasses.dataclass
        class Other:
            id: int

        with pytest.raises(TypeError, match="Expected Account"):
            await cygnet.UPDATE(db).SET(AccountTable, Other(id=1))

    async def test_update_empty_set_raises(self):
        """UPDATE with no fields to set is a bug, not a no-op."""
        db = FakeDB()
        with pytest.raises(ValueError, match="UPDATE SET requires at least one field"):
            await cygnet.UPDATE(db).SET(AccountTable).WHERE(AccountTable.id == 1)

    async def test_update_unknown_kwarg_raises(self):
        """A typo'd kwarg is rejected up front, not silently dropped."""
        db = FakeDB()
        with pytest.raises(ValueError, match="Unknown field"):
            await (
                cygnet.UPDATE(db)
                .SET(AccountTable, nmae="Wilma")
                .WHERE(AccountTable.id == 1)
            )

    async def test_update_returning_emits_returning(self):
        """UPDATE.RETURNING(cols) appends RETURNING and returns rows."""
        db = FakeDB(rows=[(1, "Frederick")])
        result = await (
            cygnet.UPDATE(db)
            .SET(AccountTable, name="Frederick")
            .WHERE(AccountTable.id == 1)
            .RETURNING(AccountTable.id, AccountTable.name)
        )
        assert "RETURNING accounts.id, accounts.name" in db.last_sql
        assert result == [(1, "Frederick")]

    async def test_update_without_returning_returns_none(self):
        """UPDATE without RETURNING preserves the historical None return."""
        db = FakeDB()
        result = await (
            cygnet.UPDATE(db).SET(AccountTable, name="x").WHERE(AccountTable.id == 1)
        )
        assert result is None
        assert "RETURNING" not in db.last_sql

    async def test_update_returning_empty_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="RETURNING requires at least one column"):
            cygnet.UPDATE(db).SET(AccountTable, name="x").WHERE(
                AccountTable.id == 1
            ).RETURNING()

    async def test_update_column_rename(self):
        """UPDATE SET should use the DB column name."""
        db = FakeDB()
        obj = TaggedAccount(account_id=1, tag="vip")
        await cygnet.UPDATE(db).SET(TaggedTable, obj).WHERE(TaggedTable.account_id == 1)
        assert "tag_name" in db.last_sql

    async def test_update_no_where_raises(self):
        """UPDATE with no WHERE must raise ValueError."""
        db = FakeDB()
        with pytest.raises(ValueError, match="requires a WHERE clause"):
            await cygnet.UPDATE(db).SET(AccountTable, name="x")

    async def test_update_with_all_skips_where(self):
        """UPDATE with WHERE(cygnet.all) generates SQL without WHERE clause."""
        db = FakeDB()
        await cygnet.UPDATE(db).SET(AccountTable, name="x").WHERE(cygnet.all)
        assert "WHERE" not in db.last_sql
        assert "UPDATE accounts SET" in db.last_sql

    async def test_update_all_mixed_with_predicates_raises(self):
        """cygnet.all combined with other predicates raises ValueError."""
        db = FakeDB()
        with pytest.raises(ValueError, match="cannot be combined"):
            await (
                cygnet.UPDATE(db)
                .SET(AccountTable, name="x")
                .WHERE(AccountTable.id == 1)
                .WHERE(cygnet.all)
            )


class TestSaveSQL:
    async def test_save_new_dbkey(self):
        db = FakeDB(rows=[(99,)])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        await cygnet.save(db, acc)
        assert "INSERT INTO" in db.last_sql
        assert "RETURNING" in db.last_sql
        assert acc.id == 99

    async def test_save_existing_dbkey(self):
        db = FakeDB()
        acc = Account(id=1, name="Fred", email="fred@example.com")
        await cygnet.save(db, acc)
        assert "ON CONFLICT" in db.last_sql
        assert "DO UPDATE SET" in db.last_sql

    async def test_save_appkey(self):
        db = FakeDB()
        ev = Event(id="evt-abc", name="Launch")
        await cygnet.save(db, ev)
        assert "ON CONFLICT" in db.last_sql

    async def test_save_appkey_none_raises(self):
        db = FakeDB()
        ev = Event(id=None, name="Launch")
        with pytest.raises(ValueError, match="AppKey"):
            await cygnet.save(db, ev)

    async def test_save_no_pk_raises(self):
        """A no-PK class fails at TableMeta construction (called transitively
        from save()), not silently or at SQL emission."""

        @dataclasses.dataclass
        class Keyless:
            name: str

        db = FakeDB()
        with pytest.raises(TypeError, match="no primary key"):
            await cygnet.save(db, Keyless(name="x"))

    async def test_save_pk_only_model(self):
        """save() on a model with only a PK should produce valid SQL."""

        @dataclasses.dataclass
        class PKOnly:
            id: Annotated[str, AppKey]

        db = FakeDB()
        await cygnet.save(db, PKOnly(id="abc"))
        assert "INSERT INTO" in db.last_sql

    async def test_save_column_rename_in_upsert(self):
        """Upsert SQL should use DB column names throughout."""
        db = FakeDB()
        obj = TaggedAccount(account_id=1, tag="vip")
        await cygnet.save(db, obj)
        assert "tag_name" in db.last_sql
        assert "ON CONFLICT" in db.last_sql


class TestGetSQL:
    async def test_get_produces_correct_where(self):
        db = FakeDB(rows=[(1, "Fred", "fred@example.com")])
        result = await cygnet.get(db, AccountTable, id=1)
        assert "WHERE" in db.last_sql
        assert db.last_params == [1]
        assert isinstance(result, Account)
        assert result.name == "Fred"

    async def test_get_returns_none_when_missing(self):
        db = FakeDB(rows=[])
        result = await cygnet.get(db, AccountTable, id=999)
        assert result is None

    async def test_get_no_pk_raises(self):
        """The no-PK rejection now fires when the proxy is constructed,
        before get() ever runs."""

        @dataclasses.dataclass
        class Keyless:
            name: str

        with pytest.raises(TypeError, match="no primary key"):
            cygnet.Table(Keyless)

    async def test_get_wrong_kwarg_raises(self):
        """get() with a kwarg that doesn't match the PK attr names the model
        and the expected kwarg, rather than raising a bare KeyError."""
        db = FakeDB(rows=[])
        with pytest.raises(TypeError, match=r"Account\.get\(\) missing PK kwarg 'id'"):
            await cygnet.get(db, AccountTable, wrong_name=1)


class TestDeleteSQL:
    async def test_delete_with_where(self):
        db = FakeDB()
        await cygnet.DELETE(db).FROM(AccountTable).WHERE(AccountTable.id == 1)
        assert db.last_sql == "DELETE FROM accounts WHERE (accounts.id = $1)"
        assert db.last_params == [1]

    async def test_delete_returning_emits_returning(self):
        """DELETE.RETURNING(cols) appends RETURNING and returns deleted rows."""
        db = FakeDB(rows=[("fred@example.com",)])
        result = await (
            cygnet.DELETE(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.id == 1)
            .RETURNING(AccountTable.email)
        )
        assert "RETURNING accounts.email" in db.last_sql
        assert result == [("fred@example.com",)]

    async def test_delete_multiple_where(self):
        db = FakeDB()
        await (
            cygnet.DELETE(db)
            .FROM(AccountTable)
            .WHERE(AccountTable.name == "Fred")
            .WHERE(AccountTable.id > 5)
        )
        assert db.last_sql == (
            "DELETE FROM accounts WHERE (accounts.name = $1) AND (accounts.id > $2)"
        )
        assert db.last_params == ["Fred", 5]

    async def test_delete_with_all(self):
        db = FakeDB()
        await cygnet.DELETE(db).FROM(AccountTable).WHERE(cygnet.all)
        assert db.last_sql == "DELETE FROM accounts"
        assert db.last_params == []

    async def test_delete_no_where_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="requires a WHERE clause"):
            await cygnet.DELETE(db).FROM(AccountTable)

    async def test_delete_all_mixed_with_predicates_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="cannot be combined"):
            await (
                cygnet.DELETE(db)
                .FROM(AccountTable)
                .WHERE(AccountTable.id == 1)
                .WHERE(cygnet.all)
            )

    async def test_delete_with_lit(self):
        db = FakeDB()
        await cygnet.DELETE(db).FROM(AccountTable).WHERE(cygnet.lit("id > 10"))
        assert db.last_sql == "DELETE FROM accounts WHERE (id > 10)"
        assert db.last_params == []


class TestCreateSQL:
    async def test_create_dbkey(self):
        """create() with DBKey inserts with RETURNING, populates PK."""
        db = FakeDB(rows=[(42,)])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        result = await cygnet.create(db, acc)
        assert "INSERT INTO" in db.last_sql
        assert "RETURNING id" in db.last_sql
        assert "ON CONFLICT" not in db.last_sql
        assert result.id == 42
        assert result is acc

    async def test_create_appkey(self):
        """create() with AppKey inserts without RETURNING."""
        db = FakeDB(rows=[])
        ev = Event(id="evt-123", name="Launch")
        result = await cygnet.create(db, ev)
        assert "INSERT INTO" in db.last_sql
        assert "RETURNING" not in db.last_sql
        assert "ON CONFLICT" not in db.last_sql
        assert "evt-123" in db.last_params
        assert result is ev

    async def test_create_appkey_none_raises(self):
        """create() with AppKey and None value raises ValueError."""
        db = FakeDB()
        ev = Event(id=None, name="Launch")
        with pytest.raises(ValueError, match="AppKey"):
            await cygnet.create(db, ev)

    async def test_create_no_on_conflict(self):
        """create() must never generate ON CONFLICT."""
        db = FakeDB(rows=[(1,)])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        await cygnet.create(db, acc)
        assert "ON CONFLICT" not in db.last_sql
        assert "EXCLUDED" not in db.last_sql

    async def test_create_raises_if_returning_empty(self):
        """If RETURNING produces no row, create() must raise rather than
        leaving the object's PK silently None."""
        db = FakeDB(rows=[])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        with pytest.raises(RuntimeError, match="produced no row"):
            await cygnet.create(db, acc)


class TestInsertReturningGuards:
    async def test_insert_raises_if_returning_empty(self):
        """run_insert with DBKey + missing RETURNING row must raise."""
        db = FakeDB(rows=[])
        acc = Account(id=None, name="Fred", email="fred@example.com")
        with pytest.raises(RuntimeError, match="produced no row"):
            await cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)


class TestBuilderSQL:
    def test_select_sql(self):
        db = FakeDB()
        sql, params = (
            cygnet.SELECT(db).FROM(AccountTable).WHERE(AccountTable.id == 1).sql()
        )
        assert sql == (
            "SELECT accounts.id, accounts.name, accounts.email"
            " FROM accounts WHERE (accounts.id = $1)"
        )
        assert params == [1]

    def test_select_sql_full_chain(self):
        db = FakeDB()
        sql, params = (
            cygnet.SELECT(db, AccountTable.name)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .WHERE(AccountTable.name == "Fred")
            .GROUP_BY(AccountTable.name)
            .ORDER_BY(AccountTable.name)
            .LIMIT(10)
            .sql()
        )
        assert "SELECT accounts.name FROM accounts" in sql
        assert "INNER JOIN log_entries ON" in sql
        assert "WHERE" in sql
        assert "GROUP BY accounts.name" in sql
        assert "ORDER BY accounts.name ASC" in sql
        assert "LIMIT 10" in sql
        assert params == ["Fred"]

    def test_delete_sql(self):
        db = FakeDB()
        sql, params = (
            cygnet.DELETE(db).FROM(AccountTable).WHERE(AccountTable.id == 1).sql()
        )
        assert sql == "DELETE FROM accounts WHERE (accounts.id = $1)"
        assert params == [1]

    def test_delete_sql_no_where_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="requires a WHERE clause"):
            cygnet.DELETE(db).FROM(AccountTable).sql()

    def test_delete_sql_with_all(self):
        db = FakeDB()
        sql, params = cygnet.DELETE(db).FROM(AccountTable).WHERE(cygnet.all).sql()
        assert sql == "DELETE FROM accounts"
        assert params == []

    def test_update_sql(self):
        db = FakeDB()
        sql, params = (
            cygnet.UPDATE(db)
            .SET(AccountTable, name="Wilma")
            .WHERE(AccountTable.id == 1)
            .sql()
        )
        assert "UPDATE accounts SET name = $1" in sql
        assert "WHERE (accounts.id = $2)" in sql
        assert params == ["Wilma", 1]

    def test_update_sql_no_where_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="requires a WHERE clause"):
            cygnet.UPDATE(db).SET(AccountTable, name="x").sql()

    def test_update_sql_empty_set_raises(self):
        """sql() must apply the same empty-SET safety rail as execution."""
        db = FakeDB()
        with pytest.raises(ValueError, match="UPDATE SET requires at least one field"):
            cygnet.UPDATE(db).SET(AccountTable).WHERE(AccountTable.id == 1).sql()

    def test_insert_sql_kwargs(self):
        db = FakeDB()
        sql, params = (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .VALUES(name="Fred", email="fred@example.com")
            .sql()
        )
        assert "INSERT INTO accounts" in sql
        assert "RETURNING id" in sql
        assert "Fred" in params
        assert "fred@example.com" in params

    def test_insert_sql_object(self):
        db = FakeDB()
        acc = Account(id=None, name="Fred", email="fred@example.com")
        sql, params = cygnet.INSERT(db).INTO(AccountTable).VALUES(acc).sql()
        assert "INSERT INTO accounts" in sql
        assert "RETURNING id" in sql
        assert "Fred" in params

    def test_insert_sql_appkey_none_raises(self):
        db = FakeDB()
        ev = Event(id=None, name="Launch")
        with pytest.raises(ValueError, match="AppKey"):
            cygnet.INSERT(db).INTO(EventTable).VALUES(ev).sql()

    def test_insert_sql_appkey_no_returning(self):
        db = FakeDB()
        ev = Event(id="evt-123", name="Launch")
        sql, params = cygnet.INSERT(db).INTO(EventTable).VALUES(ev).sql()
        assert "INSERT INTO events" in sql
        assert "RETURNING" not in sql
        assert "evt-123" in params


class TestTruncateSQL:
    async def test_truncate_single_table(self):
        db = FakeDB()
        await cygnet.TRUNCATE(db, AccountTable)
        assert db.last_sql == "TRUNCATE TABLE accounts"

    async def test_truncate_multiple_tables(self):
        db = FakeDB()
        await cygnet.TRUNCATE(db, AccountTable, LogTable)
        assert db.last_sql == "TRUNCATE TABLE accounts, log_entries"

    async def test_truncate_cascade(self):
        db = FakeDB()
        await cygnet.TRUNCATE(db, AccountTable, cascade=True)
        assert db.last_sql == "TRUNCATE TABLE accounts CASCADE"

    async def test_truncate_multiple_cascade(self):
        db = FakeDB()
        await cygnet.TRUNCATE(db, AccountTable, LogTable, cascade=True)
        assert db.last_sql == "TRUNCATE TABLE accounts, log_entries CASCADE"

    async def test_truncate_no_tables_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="at least one table"):
            await cygnet.TRUNCATE(db)


class TestOperatorSQL:
    async def test_op_infix_in_where(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(cygnet.op(AccountTable.name, "ILIKE", "%fred%"))
        )
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email"
            " FROM accounts WHERE (accounts.name ILIKE $1)"
        )
        assert db.last_params == ["%fred%"]

    async def test_is_null_in_where(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(cygnet.is_null(AccountTable.email))
        )
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email"
            " FROM accounts WHERE (accounts.email IS NULL)"
        )
        assert db.last_params == []

    async def test_compound_op_in_where(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(cygnet.is_null(AccountTable.email) & (AccountTable.name == "Fred"))
        )
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email"
            " FROM accounts "
            "WHERE ((accounts.email IS NULL) "
            "AND (accounts.name = $1))"
        )
        assert db.last_params == ["Fred"]

    async def test_prefix_op_in_where(self):
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .WHERE(cygnet.op("NOT", AccountTable.name == "Fred"))
        )
        assert db.last_sql == (
            "SELECT accounts.id, accounts.name, accounts.email"
            " FROM accounts WHERE (NOT (accounts.name = $1))"
        )
        assert db.last_params == ["Fred"]


class TestFollowBuilderSQL:
    async def test_follow_generates_inner_join(self):
        """FOLLOW() should produce an INNER JOIN with the correct ON condition."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(OrderTable).FOLLOW(OrderTable.customer_id)
        assert "INNER JOIN customers ON" in db.last_sql
        assert "orders.customer_id = customers.id" in db.last_sql

    async def test_left_follow_generates_left_join(self):
        """LEFT_FOLLOW() should produce a LEFT JOIN."""
        db = FakeDB(rows=[])
        await cygnet.SELECT(db).FROM(OrderTable).LEFT_FOLLOW(OrderTable.customer_id)
        assert "LEFT JOIN customers ON" in db.last_sql
        assert "orders.customer_id = customers.id" in db.last_sql

    async def test_follow_returns_tuple(self):
        """FOLLOW() result should be a tuple of (source, target) objects."""
        db = FakeDB(rows=[(1, 10, 99.99, 10, "Alice")])
        results = (
            await cygnet.SELECT(db).FROM(OrderTable).FOLLOW(OrderTable.customer_id)
        )
        assert len(results) == 1
        order, customer = results[0]
        assert isinstance(order, Order)
        assert isinstance(customer, Customer)
        assert order.customer_id == 10
        assert customer.name == "Alice"

    async def test_left_follow_null_returns_none(self):
        """LEFT_FOLLOW() with all-NULL joined columns returns None for the target."""
        db = FakeDB(rows=[(1, None, 99.99, None, None)])
        results = await (
            cygnet.SELECT(db).FROM(OrderTable).LEFT_FOLLOW(OrderTable.customer_id)
        )
        assert len(results) == 1
        order, customer = results[0]
        assert isinstance(order, Order)
        assert customer is None

    async def test_follow_non_fk_raises(self):
        """FOLLOW() on a non-FK column raises ValueError."""
        db = FakeDB()
        with pytest.raises(ValueError, match="not a foreign key"):
            await cygnet.SELECT(db).FROM(OrderTable).FOLLOW(OrderTable.amount)

    async def test_follow_chaining_with_where(self):
        """FOLLOW() can be chained with WHERE."""
        db = FakeDB(rows=[])
        await (
            cygnet.SELECT(db)
            .FROM(OrderTable)
            .FOLLOW(OrderTable.customer_id)
            .WHERE(OrderTable.amount > 100)
        )
        assert "INNER JOIN customers ON" in db.last_sql
        assert "WHERE" in db.last_sql
        assert db.last_params == [100]

    def test_follow_sql_method(self):
        """FOLLOW() works with .sql() for inspection."""
        db = FakeDB()
        sql, params = (
            cygnet.SELECT(db).FROM(OrderTable).FOLLOW(OrderTable.customer_id).sql()
        )
        assert "INNER JOIN customers ON" in sql
        assert "orders.customer_id = customers.id" in sql
        assert params == []

    def test_left_follow_sql_method(self):
        """LEFT_FOLLOW() works with .sql() for inspection."""
        db = FakeDB()
        sql, params = (
            cygnet.SELECT(db).FROM(OrderTable).LEFT_FOLLOW(OrderTable.customer_id).sql()
        )
        assert "LEFT JOIN customers ON" in sql
        assert params == []


class TestFollowSQL:
    async def test_follow_generates_get(self):
        """follow() should query the target table by PK."""
        db = FakeDB(rows=[(10, "Alice")])
        order = Order(id=1, customer_id=10, amount=99.99)
        result = await cygnet.follow(db, order, OrderTable.customer_id)
        assert isinstance(result, Customer)
        assert result.name == "Alice"
        assert db.last_params == [10]
        assert "customers" in db.last_sql

    async def test_follow_none_fk_returns_none(self):
        """follow() with None FK value returns None without querying."""
        db = FakeDB()
        order = Order(id=1, customer_id=None, amount=99.99)
        result = await cygnet.follow(db, order, OrderTable.customer_id)
        assert result is None
        assert len(db.calls) == 0

    async def test_follow_not_found_returns_none(self):
        """follow() returns None when no matching row exists."""
        db = FakeDB(rows=[])
        order = Order(id=1, customer_id=999, amount=99.99)
        result = await cygnet.follow(db, order, OrderTable.customer_id)
        assert result is None

    async def test_follow_non_fk_column_raises(self):
        """follow() on a non-FK column raises ValueError."""
        db = FakeDB()
        order = Order(id=1, customer_id=10, amount=99.99)
        with pytest.raises(ValueError, match="not a foreign key"):
            await cygnet.follow(db, order, OrderTable.amount)

    async def test_follow_wrong_object_type_raises(self):
        """follow() with wrong object type raises TypeError."""
        db = FakeDB()
        acc = Account(id=1, name="Fred", email="fred@example.com")
        with pytest.raises(TypeError, match="Expected Order"):
            await cygnet.follow(db, acc, OrderTable.customer_id)
