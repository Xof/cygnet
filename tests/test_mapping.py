# test_mapping.py — Tests for row-to-object mapping in the executor.
#
# Verifies that raw database rows (tuples) are correctly hydrated into
# dataclass instances, including JOIN result decomposition (tuple-of-objects),
# LEFT JOIN NULL handling (None for unmatched right side), and columnar
# (explicit column) queries that return plain tuples.

from __future__ import annotations

import cygnet
from tests.conftest import Account, AccountTable, FakeDB, LogEntry, LogTable


class TestRowMapping:
    async def test_maps_to_dataclass(self):
        db = FakeDB(rows=[(1, "Fred", "fred@example.com")])
        results = await cygnet.SELECT(db).FROM(AccountTable)
        assert len(results) == 1
        acc = results[0]
        assert isinstance(acc, Account)
        assert acc.id == 1
        assert acc.name == "Fred"
        assert acc.email == "fred@example.com"

    async def test_maps_multiple_rows(self):
        db = FakeDB(
            rows=[
                (1, "Fred", "fred@example.com"),
                (2, "Wilma", "wilma@example.com"),
            ]
        )
        results = await cygnet.SELECT(db).FROM(AccountTable)
        assert len(results) == 2
        assert results[1].name == "Wilma"

    async def test_empty_result(self):
        db = FakeDB(rows=[])
        results = await cygnet.SELECT(db).FROM(AccountTable)
        assert results == []

    async def test_inner_join_maps_to_tuples(self):
        db = FakeDB(rows=[(1, "Fred", "fred@example.com", 10, 1, "hello")])
        results = await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        )
        assert len(results) == 1
        acc, log = results[0]
        assert isinstance(acc, Account)
        assert isinstance(log, LogEntry)
        assert log.message == "hello"

    async def test_left_join_none_on_no_match(self):
        db = FakeDB(rows=[(1, "Fred", "fred@example.com", None, None, None)])
        results = await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        )
        acc, log = results[0]
        assert isinstance(acc, Account)
        assert log is None

    async def test_columnar_returns_tuples(self):
        db = FakeDB(rows=[(1, "Fred"), (2, "Wilma")])
        results = await cygnet.SELECT(db, AccountTable.id, AccountTable.name).FROM(
            AccountTable
        )
        assert results == [(1, "Fred"), (2, "Wilma")]

    async def test_left_join_partial_null(self):
        """A LEFT JOIN row where some (not all) right-side columns are NULL
        should still produce an object, not None."""
        # LogEntry has 3 fields: id, account_id, message
        # Only message is NULL here — the row matched, it's not a miss.
        db = FakeDB(rows=[(1, "Fred", "fred@example.com", 10, 1, None)])
        results = await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        )
        acc, log = results[0]
        assert isinstance(acc, Account)
        assert isinstance(log, LogEntry)
        assert log.id == 10
        assert log.message is None

    async def test_left_join_miss_uses_pk_column(self):
        """LEFT JOIN miss-detection looks at the right-side PK, not all-NULL.

        Constructing a row where the PK is NULL but other columns are not
        proves the implementation checks the PK slot specifically — under
        the old all-columns-NULL rule, this row would be misclassified as
        a real match (because not all values are None) and yield a LogEntry
        with id=None.  PG never returns NULL for a non-null PK column from a
        matched row, so PK=None is the unambiguous miss signal.
        """
        db = FakeDB(rows=[(1, "Fred", "fred@example.com", None, 99, "stray")])
        results = await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        )
        acc, log = results[0]
        assert isinstance(acc, Account)
        assert log is None

    async def test_multi_join_mapping(self):
        """Multiple JOINs: each right-side table maps to its own object."""
        db = FakeDB(
            rows=[(1, "Fred", "fred@example.com", 10, 1, "hello", 20, 1, "world")]
        )
        results = await (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        )
        assert len(results) == 1
        acc, log1, log2 = results[0]
        assert isinstance(acc, Account)
        assert isinstance(log1, LogEntry)
        assert isinstance(log2, LogEntry)
        assert log1.message == "hello"
        assert log2.message == "world"
