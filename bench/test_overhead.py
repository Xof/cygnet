# test_overhead.py — Full-path Cygnet benchmarks against FakeDB.
#
# Where test_render.py measures `.sql()` only (parameter-list build +
# string render), this file measures the whole `await builder` path
# including FakeDB.execute / row-to-object mapping.  That covers code
# the rendering tests don't:
#   - Executor.run_select / run_insert / run_update orchestration
#   - _map_select / _row_to_obj hydration
#   - the awaitable-builder __await__ machinery
#
# FakeDB returns preloaded rows synchronously, so PG / network latency
# is excluded — every measurement is pure Cygnet Python.

from __future__ import annotations

import pytest

import cygnet
from tests.conftest import FakeDB

from .conftest import Account, AccountTable, PostTable, run_async

pytestmark = pytest.mark.bench


class TestSelect:
    def test_select_one_row(self, benchmark, loop, fake_db_populated: FakeDB):
        """Awaited SELECT with a single-row result; exercises the
        common "fetch-by-PK" mapping path."""

        def op() -> list:
            async def go() -> list:
                return await cygnet.SELECT(fake_db_populated).FROM(AccountTable)

            return run_async(loop, go)

        benchmark(op)

    def test_select_100_rows_with_mapping(
        self, benchmark, loop, fake_db_populated: FakeDB
    ):
        """SELECT producing 100 hydrated dataclass instances —
        measures _row_to_obj cost across a realistic batch."""

        def op() -> list:
            async def go() -> list:
                return await cygnet.SELECT(fake_db_populated).FROM(AccountTable)

            return run_async(loop, go)

        benchmark(op)
        # Sanity: the populated FakeDB returns 100 rows.
        # (Not asserted to keep the benchmark hot path lean.)

    def test_select_columnar_no_mapping(
        self, benchmark, loop, fake_db_populated: FakeDB
    ):
        """SELECT(db, T.id, T.name) — explicit columns return raw
        tuples, skipping the row-to-object mapping branch.  Subtracting
        this from test_select_100_rows_with_mapping gives the per-row
        hydration cost."""

        def op() -> list:
            async def go() -> list:
                return await cygnet.SELECT(
                    fake_db_populated, AccountTable.id, AccountTable.name
                ).FROM(AccountTable)

            return run_async(loop, go)

        benchmark(op)


class TestInsert:
    def test_insert_one(self, benchmark, loop):
        """Single-row INSERT through the full builder + executor path."""

        def op() -> int:
            db = FakeDB(rows=[(1,)])
            acc = Account(id=None, name="Fred", email="fred@example.com")

            async def go() -> int:
                return await cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)

            return run_async(loop, go)

        benchmark(op)

    def test_bulk_insert_100(self, benchmark, loop):
        """BULK_VALUES with 100 objects + RETURNING populating each PK."""

        def op() -> list:
            db = FakeDB(rows=[(i,) for i in range(1, 101)])
            accounts = [
                Account(id=None, name=f"User {i}", email=f"u{i}@example.com")
                for i in range(100)
            ]

            async def go() -> list:
                return await cygnet.INSERT(db).INTO(AccountTable).BULK_VALUES(accounts)

            return run_async(loop, go)

        benchmark(op)


class TestUpdate:
    def test_update_kwargs(self, benchmark, loop, fake_db: FakeDB):
        def op() -> None:
            async def go() -> None:
                await (
                    cygnet.UPDATE(fake_db)
                    .SET(AccountTable, name="Fred")
                    .WHERE(AccountTable.id == 42)
                )

            return run_async(loop, go)

        benchmark(op)


class TestJoin:
    def test_join_with_mapping(self, benchmark, loop):
        """INNER JOIN producing tuples of (Account, Post).  Exercises
        the joined-row mapping branch in _map_select, including the
        per-row column-slicing arithmetic."""
        # 50 rows, each (account_cols..., post_cols...).
        rows = [
            (
                i,
                f"User {i}",
                f"u{i}@example.com",
                100 + i,
                i,
                f"Post {i}",
                "body",
            )
            for i in range(1, 51)
        ]
        db = FakeDB(rows=rows)

        def op() -> list:
            async def go() -> list:
                return await (
                    cygnet.SELECT(db)
                    .FROM(AccountTable)
                    .JOIN(PostTable, ON=AccountTable.id == PostTable.account_id)
                )

            return run_async(loop, go)

        benchmark(op)
