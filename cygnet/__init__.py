"""
CYGNET — The Littlest ORM.

Bring your own objects. Write real SQL.

This module is the public API surface.  All user-facing functions are defined
here (or re-exported from submodules).  Internal modules (meta, proxy,
executor, etc.) are not part of the public API.

Query verbs are UPPER_CASE (SELECT, INSERT, UPDATE, DELETE, TRUNCATE) to
mirror SQL and read naturally in Python: `await cygnet.SELECT(db).FROM(T)`.
The noqa: N802 suppressions silence PEP 8 naming complaints on these
intentionally-named functions.
"""

from __future__ import annotations

from typing import Any, cast

# Re-exports are grouped by role: annotations (used in model definitions),
# builders (returned by query-verb factories, not constructed directly by
# users), expression helpers (op/ops/is_null/is_not_null), and the `all`
# sentinel (see predicate.py — required for unrestricted UPDATE/DELETE).
from .annotations import AppKey, Column, DBKey, ForeignKey, table
from .builders import DeleteBuilder, InsertBuilder, SelectBuilder, UpdateBuilder
from .cte import CTE, Lateral, RecursiveCTE, cte, lateral, recursive_cte
from .executor import Executor
from .expression import exists, fn, is_not_null, is_null, not_exists, op, ops
from .predicate import Literal, all
from .proxy import ColumnProxy, TableProxy

__all__ = [
    # Annotations
    "DBKey",
    "AppKey",
    "Column",
    "ForeignKey",
    "table",
    # Table factory
    "Table",
    # Query verbs
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    # Predicates
    "all",
    "lit",
    "op",
    "ops",
    "is_null",
    "is_not_null",
    "exists",
    "not_exists",
    "fn",
    # Convenience
    "create",
    "follow",
    "get",
    "save",
    "transaction",
    # CTEs
    "CTE",
    "cte",
    "RecursiveCTE",
    "recursive_cte",
    "Lateral",
    "lateral",
]


def Table[T](cls: type[T]) -> TableProxy[T]:  # noqa: N802
    """Create a table proxy from a dataclass.

    Returns a cached singleton per class — cygnet.Table(X) is cygnet.Table(X).
    The TableProxy is generic on the model type so that downstream APIs
    (e.g. cygnet.get) can return the correct concrete type.
    """
    return TableProxy(cls)


# ── Query verb entry points ──────────────────────────────────────────────────
# These are thin wrappers that create builders.  The db object is threaded
# through to the builder and eventually to the Executor, which calls
# db.execute() / db.execute_one().  Cygnet never imports a specific database
# driver — the db protocol is duck-typed (see tests/conftest.py:FakeDB for
# the minimal interface).


def SELECT(db: Any, *columns: Any) -> SelectBuilder:  # noqa: N802
    return SelectBuilder(db, *columns)


def INSERT(db: Any) -> InsertBuilder:  # noqa: N802
    return InsertBuilder(db)


def UPDATE(db: Any) -> UpdateBuilder:  # noqa: N802
    return UpdateBuilder(db)


def DELETE(db: Any) -> DeleteBuilder:  # noqa: N802
    return DeleteBuilder(db)


async def TRUNCATE(db: Any, *tables: TableProxy[Any], cascade: bool = False) -> None:  # noqa: N802
    """Truncate one or more tables. Use cascade=True to drop dependent rows.

    Unlike SELECT/INSERT/UPDATE/DELETE, TRUNCATE has no builder — it's a
    single statement with no clauses to chain, so a direct async function
    is simpler.
    """
    if not tables:
        raise ValueError("TRUNCATE requires at least one table")
    names = ", ".join(t._meta.table_name for t in tables)
    sql = f"TRUNCATE TABLE {names}"
    if cascade:
        sql += " CASCADE"
    await db.execute(sql, [])


def lit(sql: str) -> Literal:
    """Create a raw SQL literal for use in any expression position.

    The SQL is emitted verbatim — no escaping, no parameter substitution.
    This is the escape hatch when Cygnet's expression API doesn't cover
    your SQL construct.  Use with care: the string is trusted.
    """
    return Literal(sql=sql)


# ── Convenience functions ────────────────────────────────────────────────────
# These wrap common patterns (get-by-PK, insert-without-upsert, upsert)
# so callers don't have to spell out the full builder chain for simple cases.


async def get[T](db: Any, table: TableProxy[T], **pk_kwargs: Any) -> T | None:
    """Fetch a single object by primary key. Returns None if not found.

    The pk kwarg name must match the Python attribute name (not the DB
    column name): cygnet.get(db, T, id=1), not cygnet.get(db, T, user_id=1)
    if the attr is `id` but the column is `user_id`.

    Returns T | None thanks to the generic TableProxy[T] — callers get
    the correct concrete type for the model under inspection.
    """
    meta = table._meta
    # Defensive: TableMeta now enforces "exactly one PK" at introspection
    # time, so this branch is unreachable through the public API.  Kept as
    # a clear error in case meta.pk is set None elsewhere in future work.
    if meta.pk is None:
        raise TypeError(f"{meta.cls.__name__} has no primary key")
    # Validate the kwarg name explicitly so a wrong key (e.g. user_id when
    # the attr is id) raises a TypeError naming both the model and the
    # expected kwarg, rather than a bare KeyError on the attr name.
    if meta.pk.attr_name not in pk_kwargs:
        raise TypeError(
            f"{meta.cls.__name__}.get() missing PK kwarg {meta.pk.attr_name!r}"
        )
    val = pk_kwargs[meta.pk.attr_name]
    pred = getattr(table, meta.pk.attr_name) == val
    results = await SELECT(db).FROM(table).WHERE(pred)
    # SELECT.run_select returns list[Any] (each entry is an instance of the
    # model class via _row_to_obj).  Cast at the boundary so the public
    # signature carries T | None without forcing internal SELECT machinery
    # to be generic.
    return cast("T | None", results[0] if results else None)


async def follow(db: Any, obj: Any, fk_column: Any) -> Any:
    """Load the object that a foreign key points to.

    Returns None if the FK value is None or no matching row exists.
    Raises ValueError if fk_column is not a foreign key.
    Raises TypeError if obj is not an instance of the FK column's table.
    """
    if not isinstance(fk_column, ColumnProxy):
        raise ValueError(f"{fk_column!r} is not a column proxy")

    field = fk_column._field
    source_meta = fk_column._table._meta

    if not isinstance(obj, source_meta.cls):
        raise TypeError(
            f"Expected {source_meta.cls.__name__}, got {type(obj).__name__}"
        )

    if field.foreign_key is None:
        raise ValueError(
            f"{source_meta.cls.__name__}.{field.attr_name} is not a foreign key"
        )

    fk_value = getattr(obj, field.attr_name)
    if fk_value is None:
        return None

    target_proxy: TableProxy[Any] = TableProxy(field.foreign_key.target)
    # FK validation in _introspect() guarantees the target has a PK.
    assert target_proxy._meta.pk is not None
    target_pk = target_proxy._meta.pk
    return await get(db, target_proxy, **{target_pk.attr_name: fk_value})


async def create(db: Any, obj: Any) -> Any:
    """
    INSERT obj into its table. No ON CONFLICT — duplicates raise from the DB.

    Returns the object with PK populated (for DBKey).
    """
    return await Executor(db).run_create(obj)


async def save(db: Any, obj: Any) -> None:
    """
    Persist obj to its table.

    - DBKey + pk is None  → INSERT ... RETURNING (pk populated on obj)
    - DBKey + pk is set   → INSERT ... ON CONFLICT DO UPDATE
    - AppKey + pk is None → ValueError
    - AppKey + pk is set  → INSERT ... ON CONFLICT DO UPDATE

    The DBKey/AppKey distinction drives the dispatch: a None PK on a DBKey
    field means "the database will assign one" (safe to INSERT), but a None
    PK on an AppKey field means the caller forgot to set it (error).  Upsert
    semantics (ON CONFLICT DO UPDATE) are the default whenever a PK is
    present — save() is idempotent in that sense, unlike create().
    """
    await Executor(db).run_save(obj)


class transaction:
    """
    Async context manager for database transactions.

    Outermost usage opens BEGIN/COMMIT.
    Nested usage transparently promotes to SAVEPOINT/RELEASE.
    Any exception triggers ROLLBACK or ROLLBACK TO SAVEPOINT.

    Nesting detection relies on a `_in_transaction` flag on the db object.
    This is a simple boolean, not a counter — nested transactions always
    use SAVEPOINTs, and only the outermost context manager issues BEGIN/COMMIT.
    The `__aenter__` returns the same db object (not a wrapper), so all
    queries inside the block use the same connection.

    Invariant: the db adapter is responsible for initializing
    `_in_transaction` (usually False on a fresh connection).  Cygnet toggles
    it only on BEGIN/COMMIT/ROLLBACK at the outermost level; SAVEPOINT
    operations leave it alone, which is what makes nesting work without a
    counter.  Concurrent use of the same db handle across tasks is not
    supported — the flag is not task-local.

    Usage::

        async with cygnet.transaction(db) as tx:
            await cygnet.INSERT(tx).INTO(AccountTable).VALUES(acc)
            async with cygnet.transaction(tx) as tx2:
                await cygnet.UPDATE(tx2).SET(LogTable, entry).WHERE(...)
    """

    def __init__(self, db: Any) -> None:
        self._db = db
        self._savepoint: str | None = None

    async def __aenter__(self) -> Any:
        # Reset _savepoint at every enter so a transaction instance can be
        # reused across multiple `async with` blocks without a stale
        # savepoint name leaking from a prior nested entry into a fresh
        # BEGIN/COMMIT cycle.
        self._savepoint = None
        if getattr(self._db, "_in_transaction", False):
            # Already in a transaction → use SAVEPOINT for nesting.
            # id(self) produces a unique name per context manager instance,
            # avoiding collisions even with deeply nested savepoints.  The
            # id is stable for the lifetime of `self`, which covers enter
            # through exit — we need the same name in both halves to issue
            # the matching RELEASE / ROLLBACK TO SAVEPOINT.
            self._savepoint = f"sp_{id(self)}"
            await self._db.execute(f"SAVEPOINT {self._savepoint}")
        else:
            await self._db.execute("BEGIN")
            self._db._in_transaction = True
        # Returns the original db, not self — the caller uses the same
        # connection handle for queries inside the block.
        return self._db

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        if self._savepoint:
            if exc_type:
                await self._db.execute(f"ROLLBACK TO SAVEPOINT {self._savepoint}")
            else:
                await self._db.execute(f"RELEASE SAVEPOINT {self._savepoint}")
        else:
            # The outermost context manager owns _in_transaction; reset it
            # in a `finally` so a failure inside ROLLBACK/COMMIT itself
            # doesn't strand the flag at True and silently turn the next
            # transaction on this connection into a SAVEPOINT against a
            # server-side-nonexistent transaction.
            try:
                if exc_type:
                    await self._db.execute("ROLLBACK")
                else:
                    await self._db.execute("COMMIT")
            finally:
                self._db._in_transaction = False
