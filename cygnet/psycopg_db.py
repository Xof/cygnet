# psycopg_db.py — Reference psycopg3 adapter for Cygnet's db protocol.
#
# Wraps a psycopg.AsyncConnection so it satisfies the four-method
# adapter protocol Cygnet expects: execute / execute_one / stream
# plus the _in_transaction flag.  Cygnet generates $N placeholders
# (libpq native style) but psycopg3 wants %s (DB-API style), so this
# adapter translates between them on every call.
#
# This module is the only place Cygnet itself imports psycopg, which
# is why psycopg lives in the optional `[psycopg]` extra rather than
# the package's required dependencies.  Importing this module without
# the extra installed surfaces a helpful error pointing at the install
# command, instead of a bare ModuleNotFoundError on `psycopg`.
#
# The $N → %s translation is pure regex — no SQL parsing — so it stays
# correct as long as Cygnet keeps emitting params left-to-right.  See
# the limitations note on _DOLLAR_RE below.
#
# Caveat: the regex is string-literal-blind.  A ``cygnet.lit("'$1 foo'")``
# payload contains ``$1`` and will be rewritten to ``%s foo`` even
# though it sits inside a single-quoted SQL literal.  Since ``lit()`` is
# documented as "trusted, no escaping", users constructing payloads
# containing ``$\d+`` substrings need to avoid that lexical shape (or use
# ``'$' || '1'``).  Proper SQL tokenisation would close this, but
# Cygnet's stance is that ``lit()`` is the escape hatch — callers own
# its content.

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

try:
    import psycopg
except ImportError as e:  # pragma: no cover — exercised via install matrix
    # psycopg is in the [psycopg] extra, not the core deps.  A clear
    # error here saves the user from deciphering a generic
    # ModuleNotFoundError when they tried to use the reference adapter
    # without installing it.
    raise ImportError(
        "cygnet.psycopg_db requires psycopg. Install the optional extra:\n"
        "    pip install 'cygnet-orm[psycopg]'\n"
        "or bring your own adapter — Cygnet's db protocol is duck-typed "
        "(see the docstring on PsycopgDB for the four required methods)."
    ) from e


class PsycopgDB:
    """Reference Cygnet adapter wrapping a psycopg.AsyncConnection.

    Construct from an open async connection:

        conn = await psycopg.AsyncConnection.connect(dsn)
        db = PsycopgDB(conn)
        accounts = await cygnet.SELECT(db).FROM(AccountTable)

    For pooled use, acquire one connection per task:

        async with pool.connection() as conn:
            db = PsycopgDB(conn)
            ...

    `_in_transaction` starts False on each new instance and is
    flipped by `cygnet.transaction(db)` at outermost BEGIN / COMMIT —
    don't share an instance across concurrent tasks (the flag is not
    task-local, by design).
    """

    # The translation is position-insensitive: $1, $2, etc. all become
    # %s, which works because psycopg3 consumes params in list order —
    # the same order Cygnet appends them.  If Cygnet ever generated
    # out-of-order references ($2 before $1), this adapter would break;
    # the executor's render path always appends params left-to-right,
    # so that can't happen with the current implementation.
    _DOLLAR_RE = re.compile(r"\$\d+")

    def __init__(self, conn: psycopg.AsyncConnection[Any]) -> None:
        self._conn = conn
        self._in_transaction = False
        # Cygnet's task-locality guard (S10) stashes the owning task
        # here at outermost transaction entry.  Initialised to None so
        # the cygnet.DBAdapter Protocol conformance check is honest.
        self._transaction_task: Any = None

    @classmethod
    def _adapt_sql(cls, sql: str) -> str:
        """Replace $1, $2, … with %s for psycopg3."""
        return cls._DOLLAR_RE.sub("%s", sql)

    async def execute(
        self, sql: str, params: list[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        async with self._conn.cursor() as cur:
            await cur.execute(self._adapt_sql(sql), params or [])
            # cur.description is None when the statement didn't return
            # rows (INSERT without RETURNING, UPDATE, DELETE, DDL) —
            # this is the deterministic DB-API contract test for "no
            # result set".  S9 (2026-05-22): replaces the historical
            # ``try: fetchall except ProgrammingError`` swallow, which
            # was vulnerable to psycopg narrowing or renaming the
            # exception class out from under us.  Checking description
            # is also slightly cheaper than letting the exception
            # raise-and-catch.
            if cur.description is None:
                return []
            return await cur.fetchall()

    async def execute_one(
        self, sql: str, params: list[Any] | None = None
    ) -> tuple[Any, ...] | None:
        async with self._conn.cursor() as cur:
            await cur.execute(self._adapt_sql(sql), params or [])
            return await cur.fetchone()

    async def stream(
        self, sql: str, params: list[Any] | None = None
    ) -> AsyncIterator[tuple[Any, ...]]:
        # Portal-based cursor.stream(): rows arrive as they're produced,
        # not after the full result set has been buffered.  PG portal
        # cursors are most predictable inside an explicit transaction —
        # psycopg3 will start an implicit one if none is active, but the
        # implicit lifetime is harder to reason about (when does it
        # commit? what if the consumer exits early?).  Wrapping the
        # consumer in ``async with cygnet.transaction(db)`` gives the
        # cursor a deterministic enclosing scope and a clean teardown.
        async with self._conn.cursor() as cur:
            async for row in cur.stream(self._adapt_sql(sql), params or []):
                yield row

    async def column_defaults(self, table_name: str) -> set[str]:
        """Return the set of columns on ``table_name`` that carry a non-NULL
        DEFAULT clause (NOW(), nextval, literal, expression — any DEFAULT
        whose ``column_default`` in ``information_schema.columns`` is not
        NULL).

        Used by Executor.run_insert to decide which None-valued dataclass
        fields should be omitted from the INSERT column list, so the
        schema's DEFAULT fires instead of being clobbered by an explicit
        NULL parameter.  See executor.py:run_insert and
        executor._extract_insert_fields for the consumer side.

        The result is the caller's responsibility to cache (Executor does);
        this method always re-queries.  This protocol method is *optional*
        on a db adapter — when present, Cygnet enables DEFAULT-aware INSERT
        codegen; when absent (e.g., FakeDB), Cygnet falls back to the
        historical "emit every field, NULL included" behaviour.  That
        opt-in keeps test fakes simple and protects custom adapters that
        don't have a notion of schema introspection.

        Resolves the unqualified table name through PG's own search_path
        logic via ``to_regclass($1)``, so two tables with the same name
        in different schemas (``s1.events`` / ``s2.events``) are
        correctly disambiguated by whichever appears first in
        ``current_schemas(false)``.  Returns the empty set if the table
        doesn't resolve (e.g., unknown name, hidden from the role) —
        callers treat that as "no DEFAULTs known" and fall back to the
        full-field INSERT path.
        """
        # pg_attrdef stores DEFAULT expressions keyed by (adrelid, adnum);
        # joining to pg_attribute on the same key returns the column name
        # for each defaulted column.  Using pg_catalog directly (rather
        # than information_schema.columns) keeps the resolution explicit:
        # to_regclass($1) collapses search_path resolution into a single
        # OID, and the JOIN naturally filters to that one table.  The
        # earlier information_schema query had no schema filter at all
        # and would return the union across every same-named table the
        # role could see (B1).
        #
        # attnum > 0 skips system columns (oid, ctid, etc., attnum < 0);
        # NOT attisdropped excludes columns marked dropped by ALTER TABLE
        # DROP COLUMN, which leave the pg_attribute row in place.
        async with self._conn.cursor() as cur:
            await cur.execute(
                "SELECT a.attname "
                "FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_attrdef ad "
                "  ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum "
                "WHERE a.attrelid = to_regclass(%s) "
                "  AND a.attnum > 0 "
                "  AND NOT a.attisdropped",
                [table_name],
            )
            rows = await cur.fetchall()
        return {row[0] for row in rows}
