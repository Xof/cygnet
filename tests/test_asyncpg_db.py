# test_asyncpg_db.py — No-DB unit tests for the asyncpg adapter (AsyncpgDB).
#
# The integration suite (tests/integration/test_asyncpg_adapter.py) skips
# without CYGNET_TEST_DSN, so AsyncpgDB gets zero execution in the default unit
# CI job.  These tests close that gap with a fake asyncpg connection — no real
# PostgreSQL, no asyncpg.connect — locking the two contracts a mock can prove:
#   1. execute()/execute_one() convert asyncpg Records to PLAIN tuples at the
#      boundary (the property Cygnet's positional hydration fast path relies on).
#   2. The DELIBERATE omission of stream() and column_defaults() documented in
#      AsyncpgDB's docstring.  These are probed via hasattr by SelectBuilder
#      .stream() (stream fallback) and the DEFAULT-aware INSERT codegen gate, so
#      their ABSENCE is a load-bearing contract — assert it explicitly.
#
# asyncio_mode="auto" (pyproject) auto-collects bare `async def test_*`.
# importorskip mirrors the integration file so a bare install without the
# [asyncpg] extra skips cleanly instead of erroring on import.
from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("asyncpg")
from cygnet.asyncpg_db import AsyncpgDB  # noqa: E402


class FakeConn:
    """Minimal stand-in for asyncpg.Connection.

    fetch/fetchrow return whatever rows are preloaded, mirroring asyncpg's
    positional-args API (sql, *args).  Rows may be tuples or any iterable
    AsyncpgDB will pass through tuple(); _Record exercises the Record->tuple
    conversion against a non-tuple input.
    """

    def __init__(
        self, fetch_rows: list[Any] | None = None, fetchrow_row: Any = None
    ) -> None:
        self._fetch_rows = fetch_rows or []
        self._fetchrow_row = fetchrow_row

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self._fetch_rows

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._fetchrow_row


class _Record:
    """Tuple-convertible Record-like row (asyncpg Records are NOT tuples but
    convert via tuple()).  Proves AsyncpgDB normalizes non-tuple rows too."""

    def __init__(self, *values: Any) -> None:
        self._values = values

    def __iter__(self):
        return iter(self._values)


async def test_execute_returns_plain_tuples() -> None:
    conn = FakeConn(fetch_rows=[(1, "a"), (2, "b")])
    db = AsyncpgDB(conn)

    result = await db.execute("SELECT id, name FROM t")

    assert result == [(1, "a"), (2, "b")]
    assert type(result[0]) is tuple
    assert type(result[1]) is tuple


async def test_execute_converts_record_like_rows_to_tuples() -> None:
    # asyncpg returns Record objects, not tuples; the boundary must normalize.
    conn = FakeConn(fetch_rows=[_Record(1, "a"), _Record(2, "b")])
    db = AsyncpgDB(conn)

    result = await db.execute("SELECT id, name FROM t")

    assert result == [(1, "a"), (2, "b")]
    assert all(type(row) is tuple for row in result)


async def test_execute_empty_result() -> None:
    # Transaction-control statements (BEGIN/COMMIT/...) route through fetch and
    # return an empty result set; execute() must yield [].
    db = AsyncpgDB(FakeConn(fetch_rows=[]))

    assert await db.execute("BEGIN") == []


async def test_execute_one_returns_tuple() -> None:
    db = AsyncpgDB(FakeConn(fetchrow_row=(1, "a")))

    result = await db.execute_one("SELECT id, name FROM t WHERE id = $1", [1])

    assert result == (1, "a")
    assert type(result) is tuple


async def test_execute_one_converts_record_like_row() -> None:
    db = AsyncpgDB(FakeConn(fetchrow_row=_Record(1, "a")))

    result = await db.execute_one("SELECT id, name FROM t WHERE id = $1", [1])

    assert result == (1, "a")
    assert type(result) is tuple


async def test_execute_one_returns_none_when_no_row() -> None:
    # fetchrow yields None for no match; execute_one must propagate None
    # (not an empty tuple) so callers can distinguish "no row".
    db = AsyncpgDB(FakeConn(fetchrow_row=None))

    assert await db.execute_one("SELECT 1 WHERE false") is None


def test_stream_is_not_implemented() -> None:
    # Documented omission: SelectBuilder.stream() probes via hasattr and falls
    # back when absent.  Lock the contract so the fallback stays correct.
    assert not hasattr(AsyncpgDB(FakeConn()), "stream")


def test_column_defaults_is_not_implemented() -> None:
    # Documented omission: DEFAULT-aware INSERT codegen is gated on
    # column_defaults being present.  Lock its absence.
    assert not hasattr(AsyncpgDB(FakeConn()), "column_defaults")


def test_init_protocol_shape() -> None:
    # Cheap protocol-shape check: the transaction flags Cygnet relies on start
    # in their documented initial state.
    db = AsyncpgDB(FakeConn())
    assert db._in_transaction is False
    assert db._transaction_task is None
