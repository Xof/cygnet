# test_render.py — Pure SQL-rendering microbenchmarks.
#
# These exercise Cygnet's render path without touching a database, not
# even FakeDB.  They isolate the cost of:
#   - building the AST (chained .FROM / .WHERE / .JOIN method calls)
#   - rendering it (Executor.render_select / render_insert / etc.)
#   - parameter accumulation through Predicate trees
#
# Low-variance signals for catching regressions in the hot rendering
# code paths.  Pair with test_overhead.py for the executor-layer cost
# and test_e2e.py for total wall time including PG.

from __future__ import annotations

import pytest

import cygnet
from cygnet import functions as f
from tests.conftest import FakeDB

from .conftest import AccountTable, PostTable

pytestmark = pytest.mark.bench


class TestRenderSelect:
    def test_simple_select(self, benchmark, fake_db: FakeDB):
        """Bare SELECT with one WHERE — the most common shape."""

        def op() -> tuple:
            return (
                cygnet.SELECT(fake_db)
                .FROM(AccountTable)
                .WHERE(AccountTable.id > 100)
                .sql()
            )

        benchmark(op)

    def test_compound_where(self, benchmark, fake_db: FakeDB):
        """Multiple WHERE predicates ANDed together — exercises the
        predicate accumulator and the AND-render path through three
        levels rather than the typical one or two."""

        def op() -> tuple:
            return (
                cygnet.SELECT(fake_db)
                .FROM(AccountTable)
                .WHERE(AccountTable.id > 100)
                .WHERE(AccountTable.name == "Fred")
                .WHERE(AccountTable.email != "")
                .sql()
            )

        benchmark(op)

    def test_explicit_columns(self, benchmark, fake_db: FakeDB):
        """SELECT with explicit column list (no object hydration path)."""

        def op() -> tuple:
            return (
                cygnet.SELECT(fake_db, AccountTable.id, AccountTable.name)
                .FROM(AccountTable)
                .sql()
            )

        benchmark(op)

    def test_join(self, benchmark, fake_db: FakeDB):
        """SELECT with a JOIN — exercises join column emission."""

        def op() -> tuple:
            return (
                cygnet.SELECT(fake_db)
                .FROM(AccountTable)
                .JOIN(PostTable, ON=AccountTable.id == PostTable.account_id)
                .WHERE(AccountTable.id > 50)
                .sql()
            )

        benchmark(op)

    def test_aggregate_with_group_by(self, benchmark, fake_db: FakeDB):
        """COUNT + GROUP BY — exercises function calls and group rendering."""

        def op() -> tuple:
            return (
                cygnet.SELECT(fake_db, AccountTable.name, f.count())
                .FROM(AccountTable)
                .GROUP_BY(AccountTable.name)
                .HAVING(f.count() > 1)
                .sql()
            )

        benchmark(op)


class TestRenderInsert:
    def test_single_insert(self, benchmark, fake_db: FakeDB):
        from .conftest import Account

        def op() -> tuple:
            acc = Account(id=None, name="Fred", email="fred@example.com")
            return cygnet.INSERT(fake_db).INTO(AccountTable).VALUES(acc).sql()

        benchmark(op)

    def test_bulk_insert_100(self, benchmark, fake_db: FakeDB):
        """Bulk INSERT with 100 rows — exercises the multi-VALUES render path."""
        from .conftest import Account

        accounts = [
            Account(id=None, name=f"User {i}", email=f"u{i}@example.com")
            for i in range(100)
        ]

        def op() -> tuple:
            return cygnet.INSERT(fake_db).INTO(AccountTable).BULK_VALUES(accounts).sql()

        benchmark(op)


class TestRenderUpdate:
    def test_update_kwargs(self, benchmark, fake_db: FakeDB):
        def op() -> tuple:
            return (
                cygnet.UPDATE(fake_db)
                .SET(AccountTable, name="Fred")
                .WHERE(AccountTable.id == 42)
                .sql()
            )

        benchmark(op)


class TestRenderDelete:
    def test_delete(self, benchmark, fake_db: FakeDB):
        def op() -> tuple:
            return (
                cygnet.DELETE(fake_db)
                .FROM(AccountTable)
                .WHERE(AccountTable.id == 42)
                .sql()
            )

        benchmark(op)
