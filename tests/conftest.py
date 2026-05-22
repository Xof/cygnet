# conftest.py — Shared fixtures for Cygnet's unit test suite.
#
# Defines the model dataclasses and table proxies used across all test files,
# plus FakeDB: a minimal in-memory mock that captures SQL and params without
# touching a real database.  FakeDB conforms to Cygnet's db adapter protocol
# (execute + execute_one) and records every call for assertion.

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

import cygnet
from cygnet.annotations import AppKey, Column, DBKey

# ── Shared model fixtures ─────────────────────────────────────────────────────


@dataclasses.dataclass
class Account:
    id: Annotated[int, DBKey]
    name: str
    email: str


@dataclasses.dataclass
@cygnet.table("log_entries")
class LogEntry:
    id: Annotated[int, DBKey]
    account_id: int
    message: str


@dataclasses.dataclass
class Event:
    id: Annotated[str, AppKey]
    name: str


@dataclasses.dataclass
class TaggedAccount:
    account_id: Annotated[int, DBKey]
    tag: Annotated[str, Column("tag_name")]


# Carries parameterised generics so stubs codegen can be tested against
# `list[str]` / `dict[str, int]` shapes (B4 / S15).  Cygnet itself doesn't
# care what Python type a column carries — JSONB and array columns are
# commonly typed this way at the dataclass level and serialized by the
# adapter.  Kept minimal: just enough surface for the stub-format tests.
@dataclasses.dataclass
class Doc:
    id: Annotated[int, DBKey]
    tags: list[str]
    metadata: dict[str, int]


AccountTable = cygnet.Table(Account)
LogTable = cygnet.Table(LogEntry)
EventTable = cygnet.Table(Event)
TaggedTable = cygnet.Table(TaggedAccount)
DocTable = cygnet.Table(Doc)


# ── Fake db that captures calls ───────────────────────────────────────────────


class FakeDB:
    """Captures SQL and params; returns whatever rows you pre-load.

    This is the reference implementation of Cygnet's db adapter protocol.
    Any real adapter (see cygnet/psycopg_db.py:PsycopgDB) must provide
    the same execute/execute_one signatures.  stream() is
    optional; SelectBuilder.stream() probes for it via hasattr.
    _in_transaction is required for cygnet.transaction() nesting detection.
    """

    def __init__(self, rows: list | None = None) -> None:
        self.calls: list[tuple[str, list]] = []
        self._rows = rows or []
        self._in_transaction = False

    async def execute(self, sql: str, params: list | None = None) -> list:
        self.calls.append((sql, params or []))
        return self._rows

    async def execute_one(self, sql: str, params: list | None = None) -> Any:
        self.calls.append((sql, params or []))
        return self._rows[0] if self._rows else None

    async def stream(self, sql: str, params: list | None = None) -> Any:
        # Yields the same pre-loaded rows execute() would return; the
        # capture lives in self.calls so tests can assert on the SQL
        # whether the consumer used await-list or async-for-stream.
        self.calls.append((sql, params or []))
        for row in self._rows:
            yield row

    @property
    def last_sql(self) -> str:
        return self.calls[-1][0]

    @property
    def last_params(self) -> list:
        return self.calls[-1][1]


class DefaultsFakeDB(FakeDB):
    """FakeDB extended with a ``column_defaults`` probe — enables Cygnet's
    DEFAULT-aware INSERT codegen against a non-PG fixture.

    Lives in conftest (alongside FakeDB) rather than in a single test file
    so consumers writing custom adapters can borrow the contract: the
    optional ``column_defaults(table_name) -> set[str]`` method is the
    sole shape that unlocks the "omit None-valued DEFAULTed columns from
    INSERT, RETURNING-refresh them" codegen path in
    ``executor._extract_insert_fields`` / ``run_insert`` / ``run_save``.

    Usage:

        db = DefaultsFakeDB(
            rows=[(1, "2026-01-01T00:00:00Z")],
            defaults={"widgets": {"created_at"}},
        )

    Callers preload ``rows`` to satisfy the RETURNING ``execute_one`` and
    declare ``defaults`` per-table to control which columns Cygnet sees
    as DEFAULT-bearing.  An adapter that mis-shapes ``column_defaults``
    (returns a list, returns None, returns whitespace in names) can be
    spotted by diffing against this reference.
    """

    def __init__(
        self,
        rows: list | None = None,
        defaults: dict[str, set[str]] | None = None,
    ) -> None:
        super().__init__(rows=rows)
        # Map from table_name -> set of column names that have DEFAULTs.
        # If a table isn't in the map, column_defaults returns an empty set.
        self._defaults = defaults or {}

    async def column_defaults(self, table_name: str) -> set[str]:
        return self._defaults.get(table_name, set())
