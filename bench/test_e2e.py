# test_e2e.py — Cygnet benchmarks against a real PostgreSQL.
#
# These measure total wall-clock latency including PG round-trip and
# row-data wire format — the number that matters for "is my app fast
# enough" sizing.  Higher variance than test_overhead.py because PG
# scheduling, OS cache, and TCP buffering all show up in the timings;
# pytest-benchmark's median + IQR columns are the right ones to read
# (mean gets pulled around by occasional warm-up outliers).
#
# Skipped automatically when CYGNET_TEST_DSN is unset; `just bench-e2e`
# spins up Docker PG and points at it.

from __future__ import annotations

import pytest

import cygnet

from .conftest import Account, AccountTable, PostTable, run_async

pytestmark = pytest.mark.bench


class TestSelectE2E:
    def test_select_one_row_by_pk(self, benchmark, loop, populated_db):
        """The single-row hot path: cygnet.get() under the hood — render +
        round-trip + hydrate one Account."""

        def op() -> Account | None:
            async def go() -> Account | None:
                return await cygnet.get(populated_db, AccountTable, id=42)

            return run_async(loop, go)

        result = benchmark(op)
        assert result is not None

    def test_select_all_100(self, benchmark, loop, populated_db):
        """Full-table SELECT returning 100 hydrated dataclasses."""

        def op() -> list:
            async def go() -> list:
                return await cygnet.SELECT(populated_db).FROM(AccountTable)

            return run_async(loop, go)

        result = benchmark(op)
        assert len(result) == 100

    def test_select_with_join(self, benchmark, loop, populated_db):
        """Account + Post inner join, returning 1000 (Account, Post) pairs."""

        def op() -> list:
            async def go() -> list:
                return await (
                    cygnet.SELECT(populated_db)
                    .FROM(AccountTable)
                    .JOIN(PostTable, ON=AccountTable.id == PostTable.account_id)
                )

            return run_async(loop, go)

        result = benchmark(op)
        assert len(result) == 1000

    def test_select_columnar(self, benchmark, loop, populated_db):
        """Explicit-column SELECT — no object hydration; isolates the
        wire-format + tuple-pass-through cost."""

        def op() -> list:
            async def go() -> list:
                return await cygnet.SELECT(
                    populated_db, AccountTable.id, AccountTable.name
                ).FROM(AccountTable)

            return run_async(loop, go)

        result = benchmark(op)
        assert len(result) == 100


class TestInsertE2E:
    def test_insert_one(self, benchmark, loop, populated_db):
        """Single-row INSERT … RETURNING, populating a PK back onto the obj."""

        def op() -> Account:
            async def go() -> Account:
                acc = Account(
                    id=None,
                    name="Bench User",
                    email="bench@example.com",
                )
                await cygnet.INSERT(populated_db).INTO(AccountTable).VALUES(acc)
                return acc

            return run_async(loop, go)

        result = benchmark(op)
        assert result.id is not None

    def test_bulk_insert_100(self, benchmark, loop, populated_db):
        """One round-trip multi-VALUES INSERT for 100 fresh rows."""

        def op() -> list:
            accounts = [
                Account(
                    id=None,
                    name=f"Bulk {i}",
                    email=f"b{i}@example.com",
                )
                for i in range(100)
            ]

            async def go() -> list:
                return await (
                    cygnet.INSERT(populated_db).INTO(AccountTable).BULK_VALUES(accounts)
                )

            return run_async(loop, go)

        result = benchmark(op)
        assert len(result) == 100


class TestUpdateE2E:
    def test_update_one(self, benchmark, loop, populated_db):
        """UPDATE one row by PK."""

        def op() -> None:
            async def go() -> None:
                await (
                    cygnet.UPDATE(populated_db)
                    .SET(AccountTable, name="Updated")
                    .WHERE(AccountTable.id == 42)
                )

            return run_async(loop, go)

        benchmark(op)
