# test_transaction.py — Tests for cygnet.transaction() context manager.
#
# Uses a separate FakeTransactionalDB (not the conftest FakeDB) that logs
# every SQL statement as a string, including transaction control commands
# (BEGIN, COMMIT, ROLLBACK, SAVEPOINT, RELEASE).  Tests verify the correct
# sequence of statements for normal commits, exception rollbacks, nested
# savepoints, and _in_transaction flag management.

from __future__ import annotations

from typing import Any

import pytest

import cygnet


class FakeTransactionalDB:
    """Records all executed statements including BEGIN/COMMIT/SAVEPOINT."""

    def __init__(self) -> None:
        self.log: list[str] = []
        self._in_transaction = False

    async def execute(self, sql: str, params: list | None = None) -> list:
        self.log.append(sql)
        return []

    async def execute_one(self, sql: str, params: list | None = None) -> Any:
        self.log.append(sql)
        return None


class TestTransaction:
    async def test_commit_on_success(self):
        db = FakeTransactionalDB()
        async with cygnet.transaction(db):
            pass
        assert db.log == ["BEGIN", "COMMIT"]

    async def test_rollback_on_exception(self):
        db = FakeTransactionalDB()
        with pytest.raises(ValueError):
            async with cygnet.transaction(db):
                raise ValueError("boom")
        assert db.log == ["BEGIN", "ROLLBACK"]

    async def test_nested_becomes_savepoint(self):
        db = FakeTransactionalDB()
        async with cygnet.transaction(db) as tx:
            async with cygnet.transaction(tx):
                pass
        assert db.log[0] == "BEGIN"
        assert any("SAVEPOINT" in s and "ROLLBACK" not in s for s in db.log)
        assert any("RELEASE SAVEPOINT" in s for s in db.log)
        assert db.log[-1] == "COMMIT"

    async def test_nested_rollback_to_savepoint(self):
        db = FakeTransactionalDB()
        with pytest.raises(ValueError):
            async with cygnet.transaction(db) as tx:
                async with cygnet.transaction(tx):
                    raise ValueError("inner boom")
        assert any("ROLLBACK TO SAVEPOINT" in s for s in db.log)
        assert db.log[-1] == "ROLLBACK"

    async def test_inner_rollback_outer_commits(self):
        """Primary savepoint use case: inner fails, outer catches and commits."""
        db = FakeTransactionalDB()
        async with cygnet.transaction(db) as tx:
            try:
                async with cygnet.transaction(tx):
                    raise ValueError("inner boom")
            except ValueError:
                pass  # outer catches, continues
        assert db.log[0] == "BEGIN"
        assert any("ROLLBACK TO SAVEPOINT" in s for s in db.log)
        assert db.log[-1] == "COMMIT"

    async def test_new_transaction_after_rollback(self):
        """After a rollback, _in_transaction should be reset so a new one works."""
        db = FakeTransactionalDB()
        with pytest.raises(ValueError):
            async with cygnet.transaction(db):
                raise ValueError("boom")
        assert not db._in_transaction
        async with cygnet.transaction(db):
            pass
        assert db.log == ["BEGIN", "ROLLBACK", "BEGIN", "COMMIT"]

    async def test_transaction_returns_same_db(self):
        db = FakeTransactionalDB()
        async with cygnet.transaction(db) as tx:
            assert tx is db

    async def test_transaction_instance_reuse_does_not_leak_savepoint(self):
        """Reusing a transaction object across two `async with` blocks must
        not emit RELEASE SAVEPOINT against a fresh BEGIN/COMMIT.
        """
        db = FakeTransactionalDB()
        # First use: nested, so the inner instance acquires a savepoint.
        async with cygnet.transaction(db) as tx:
            inner = cygnet.transaction(tx)
            async with inner:
                pass
        # Second use of the SAME inner instance: now top-level, must
        # produce BEGIN/COMMIT — not RELEASE SAVEPOINT against the new BEGIN.
        async with inner:
            pass
        # Last two log entries should be BEGIN, COMMIT — not a stray
        # RELEASE SAVEPOINT from the prior savepoint name.
        assert db.log[-2:] == ["BEGIN", "COMMIT"]
        assert not any("RELEASE SAVEPOINT" in s for s in db.log[-2:])

    async def test_cross_task_nesting_raises(self):
        """S10: a second asyncio task that nests into a transaction
        opened by a *different* task must raise.  The ``_in_transaction``
        flag lives on the db adapter, not the task; without the guard a
        silently-nested SAVEPOINT would run inside the other task's
        transaction and corrupt commit boundaries.
        """
        import asyncio

        db = FakeTransactionalDB()

        # Two Events deterministically interleave the tasks:
        # 1. task_a enters its transaction and signals task_a_inside.
        # 2. The test task tries to enter (must raise).
        # 3. task_a_can_exit is set so task_a can finish cleanly.
        task_a_inside = asyncio.Event()
        task_a_can_exit = asyncio.Event()

        async def task_a():
            async with cygnet.transaction(db):
                task_a_inside.set()
                await task_a_can_exit.wait()

        task_a_handle = asyncio.create_task(task_a())
        await task_a_inside.wait()
        try:
            with pytest.raises(RuntimeError, match="different asyncio task"):
                async with cygnet.transaction(db):
                    pass
        finally:
            task_a_can_exit.set()
            await task_a_handle

    async def test_sequential_cross_task_transactions_work(self):
        """Cross-task usage is only a problem when *concurrent*; sequential
        transactions on the same db from different tasks must work, since
        the outermost ``__aexit__`` clears both ``_in_transaction`` and
        ``_transaction_task``.
        """
        import asyncio

        db = FakeTransactionalDB()

        async def task_a():
            async with cygnet.transaction(db):
                pass

        await asyncio.create_task(task_a())
        # task_a is finished; ownership is cleared.  Entering here (in
        # the test task) must take the outermost BEGIN path, not raise.
        async with cygnet.transaction(db):
            pass
        assert db.log == ["BEGIN", "COMMIT", "BEGIN", "COMMIT"]

    async def test_in_transaction_resets_when_commit_raises(self):
        """If COMMIT itself raises, _in_transaction must still be reset so a
        subsequent transaction on the same connection takes the BEGIN path
        rather than (incorrectly) treating itself as nested.
        """

        class FailFirstCommitDB(FakeTransactionalDB):
            def __init__(self) -> None:
                super().__init__()
                self._commits_seen = 0

            async def execute(self, sql: str, params: list | None = None) -> list:
                self.log.append(sql)
                if sql == "COMMIT":
                    self._commits_seen += 1
                    if self._commits_seen == 1:
                        raise RuntimeError("commit failed")
                return []

        db = FailFirstCommitDB()
        with pytest.raises(RuntimeError, match="commit failed"):
            async with cygnet.transaction(db):
                pass
        assert db._in_transaction is False
        # The next transaction must take the BEGIN path.  If the prior
        # finally block was missing, _in_transaction would still be True
        # and this would emit SAVEPOINT instead.
        async with cygnet.transaction(db):
            pass
        assert db.log[-2:] == ["BEGIN", "COMMIT"]
