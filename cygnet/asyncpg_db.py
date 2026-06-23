# asyncpg_db.py — Reference asyncpg adapter for Cygnet's db protocol.
#
# Wraps an asyncpg.Connection to satisfy Cygnet's duck-typed adapter protocol
# (execute / execute_one + the _in_transaction / _transaction_task flags).
# Unlike psycopg, asyncpg speaks libpq's native $N placeholders — exactly what
# Cygnet emits — so there is NO $N->%s translation.  asyncpg returns Record
# objects; we convert each to a plain tuple at the boundary so Cygnet's
# positional hydration (cls(*row)) stays on its fast path instead of paying the
# costlier Record-unpacking penalty.
#
# Transaction control: Cygnet drives BEGIN/COMMIT/SAVEPOINT/... through
# execute() as raw SQL.  asyncpg's fetch() accepts those directly (each returns
# an empty result set — verified against asyncpg), so execute() routes
# everything uniformly through fetch(); no special-casing of control statements.
#
# stream() and column_defaults() are intentionally NOT implemented in this
# first cut (Cygnet treats both as optional protocol methods — SelectBuilder
# .stream() probes via hasattr, and DEFAULT-aware INSERT codegen is gated on
# column_defaults being present).  See the asyncpg-adapter design doc.
#
# asyncpg lives in the optional [asyncpg] extra; importing this module without
# it raises a clear, actionable ImportError (mirrors psycopg_db).
from __future__ import annotations

from typing import Any

try:
    import asyncpg  # type: ignore[import-untyped]
except ImportError as e:  # pragma: no cover — exercised via install matrix
    raise ImportError(
        "cygnet.asyncpg_db requires asyncpg. Install the optional extra:\n"
        "    pip install 'cygnet-orm[asyncpg]'\n"
        "or bring your own adapter — Cygnet's db protocol is duck-typed "
        "(see the docstring on AsyncpgDB for the required methods)."
    ) from e


class AsyncpgDB:
    """Reference Cygnet adapter wrapping an asyncpg.Connection.

        conn = await asyncpg.connect(dsn)
        db = AsyncpgDB(conn)
        accounts = await cygnet.SELECT(db).FROM(AccountTable)

    asyncpg uses native $N placeholders (no %s translation) and returns Record
    objects; execute()/execute_one() convert them to plain tuples so Cygnet
    hydrates via its fast positional path.

    `_in_transaction` starts False and is flipped by `cygnet.transaction(db)` at
    the outermost BEGIN/COMMIT; don't share an instance across concurrent tasks
    (the flag is not task-local, by design — same contract as PsycopgDB).
    """

    def __init__(self, conn: asyncpg.Connection) -> None:
        self._conn = conn
        self._in_transaction = False
        self._transaction_task: Any = None

    async def execute(
        self, sql: str, params: list[Any] | None = None
    ) -> list[tuple[Any, ...]]:
        # asyncpg.fetch takes positional args; Cygnet passes a $N-ordered list.
        # Transaction-control statements also flow through here and return [].
        records = await self._conn.fetch(sql, *(params or []))
        return [tuple(r) for r in records]

    async def execute_one(
        self, sql: str, params: list[Any] | None = None
    ) -> tuple[Any, ...] | None:
        row = await self._conn.fetchrow(sql, *(params or []))
        return tuple(row) if row is not None else None
