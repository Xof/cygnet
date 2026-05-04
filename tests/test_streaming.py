# test_streaming.py — Unit tests for SelectBuilder.stream() against FakeDB.
#
# Verifies the stream-vs-await contract: same SQL emission, same row
# mapping, but rows arrive one at a time via async iteration instead of
# being materialised into a list.  Integration coverage in
# tests/integration/test_streaming.py exercises the real PG portal-cursor
# path through psycopg.

from __future__ import annotations

import pytest

import cygnet
from tests.conftest import (
    Account,
    AccountTable,
    FakeDB,
    LogEntry,
    LogTable,
)


class TestStream:
    async def test_stream_yields_dataclass_instances(self):
        rows = [
            (1, "Fred", "fred@example.com"),
            (2, "Wilma", "wilma@example.com"),
        ]
        db = FakeDB(rows=rows)

        out = []
        async for acc in cygnet.SELECT(db).FROM(AccountTable).stream():
            assert isinstance(acc, Account)
            out.append(acc)
        assert [a.name for a in out] == ["Fred", "Wilma"]

    async def test_stream_emits_same_sql_as_await(self):
        """`async for ... in builder.stream()` should generate the same
        SQL the awaited form would."""
        rows = [(1, "Fred", "fred@example.com")]
        db = FakeDB(rows=rows)

        async for _ in (
            cygnet.SELECT(db).FROM(AccountTable).WHERE(AccountTable.id > 0).stream()
        ):
            pass
        sql_streamed = db.last_sql

        await cygnet.SELECT(db).FROM(AccountTable).WHERE(AccountTable.id > 0)
        sql_awaited = db.last_sql

        assert sql_streamed == sql_awaited

    async def test_stream_explicit_columns_yields_tuples(self):
        rows = [(1, "Fred"), (2, "Wilma")]
        db = FakeDB(rows=rows)

        out = []
        async for row in (
            cygnet.SELECT(db, AccountTable.id, AccountTable.name)
            .FROM(AccountTable)
            .stream()
        ):
            out.append(row)
        assert out == [(1, "Fred"), (2, "Wilma")]

    async def test_stream_join_yields_object_tuples(self):
        rows = [
            (1, "Fred", "fred@example.com", 10, 1, "msg1"),
            (1, "Fred", "fred@example.com", 11, 1, "msg2"),
        ]
        db = FakeDB(rows=rows)

        out = []
        async for acc, log in (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .stream()
        ):
            assert isinstance(acc, Account)
            assert isinstance(log, LogEntry)
            out.append((acc.name, log.message))
        assert out == [("Fred", "msg1"), ("Fred", "msg2")]

    async def test_stream_left_join_miss_yields_none(self):
        """LEFT JOIN no-match rows still yield None on the right side."""
        rows = [(1, "Fred", "fred@example.com", None, None, None)]
        db = FakeDB(rows=rows)

        seen = []
        async for acc, log in (
            cygnet.SELECT(db)
            .FROM(AccountTable)
            .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
            .stream()
        ):
            seen.append((acc, log))
        assert len(seen) == 1
        assert seen[0][1] is None

    async def test_stream_against_db_without_stream_raises(self):
        class NoStreamDB:
            _in_transaction = False

            async def execute(self, sql, params=None):
                return []

            async def execute_one(self, sql, params=None):
                return None

        db = NoStreamDB()
        with pytest.raises(TypeError, match="does not implement stream"):
            async for _ in cygnet.SELECT(db).FROM(AccountTable).stream():
                pass
