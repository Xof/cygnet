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

This file also hosts cross-cutting helpers that don't belong inside a
specific builder module — `transaction` (savepoint-based nesting context
manager), `get` (PK fetch), `save` (insert-or-upsert dispatch), `create`
(insert-without-upsert), `follow` (FK traversal), and `TRUNCATE` (no
builder needed).  These are kept here because they sit between the
builders and the executor: they coordinate across multiple builder
operations or sit outside the builder pattern entirely.

Naming convention recap for callers reading the export list:
  - UPPER_CASE  → SQL verbs / statement entry points
    (SELECT, INSERT, UPDATE, DELETE, TRUNCATE)
  - PascalCase  → factories returning a proxy / decorator / class
    (Table, Column, DBKey, AppKey, ForeignKey, table, CTE, Lateral,
     RecursiveCTE)
  - lower_case  → helpers, sentinels, and convenience functions
    (get, save, create, follow, transaction, lit, op, ops, is_null,
     is_not_null, exists, not_exists, fn, cte, lateral, recursive_cte,
     all)
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

# Re-exports are grouped by role: annotations (used in model definitions),
# builders (returned by query-verb factories, not constructed directly by
# users), expression helpers (op/ops/is_null/is_not_null), and the `all`
# sentinel (see predicate.py — required for unrestricted UPDATE/DELETE).
# `cygnet.all` shadows Python's builtin `all` *inside this module only*;
# callers do `cygnet.all` which is unambiguous at the call site.
from .annotations import AppKey, Column, DBKey, ForeignKey, table
from .builders import DeleteBuilder, InsertBuilder, SelectBuilder, UpdateBuilder
from .cte import CTE, Lateral, RecursiveCTE, cte, lateral, recursive_cte
from .executor import Executor
from .expression import (
    DBAdapter,
    exists,
    fn,
    is_not_null,
    is_null,
    not_exists,
    op,
    ops,
)
from .predicate import Literal, all
from .proxy import ColumnProxy, TableProxy

__all__ = [
    # Annotations
    "DBKey",
    "AppKey",
    "Column",
    "ForeignKey",
    "table",
    # Adapter contract
    "DBAdapter",
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
    "flush_column_defaults",
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
#
# Each builder is awaitable: `await SELECT(db).FROM(T)` triggers execution
# via the builder's __await__.  Builders also expose `.sql()` for caller
# inspection without execution.
#
# UPDATE and DELETE refuse to run without an explicit .WHERE() — pass
# cygnet.all to act on every row intentionally.  See predicate.py for the
# sentinel; the guard lives in the builder, not here.


def SELECT(db: DBAdapter, *columns: Any) -> SelectBuilder:  # noqa: N802
    return SelectBuilder(db, *columns)


def INSERT(db: DBAdapter) -> InsertBuilder:  # noqa: N802
    return InsertBuilder(db)


def UPDATE(db: DBAdapter) -> UpdateBuilder:  # noqa: N802
    return UpdateBuilder(db)


def DELETE(db: DBAdapter) -> DeleteBuilder:  # noqa: N802
    return DeleteBuilder(db)


async def TRUNCATE(  # noqa: N802
    db: DBAdapter, *tables: TableProxy[Any], cascade: bool = False
) -> None:
    """Truncate one or more tables. Use cascade=True to drop dependent rows.

    Unlike SELECT/INSERT/UPDATE/DELETE, TRUNCATE has no builder — it's a
    single statement with no clauses to chain, so a direct async function
    is simpler.
    """
    # TRUNCATE doesn't go through the Executor: there are no rows to map
    # back to objects and no params to bind, so this is a direct
    # db.execute() with the empty-params list the protocol requires.
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

    Caveat for adapters that translate placeholder syntax: the reference
    psycopg adapter (cygnet.psycopg_db.PsycopgDB) rewrites every ``$\\d+``
    substring in the final SQL to psycopg's ``%s`` form — including any
    such substrings inside a ``lit()`` payload.  If you need a literal
    ``$1`` string in a SQL fragment going through that adapter, write it
    as ``'$' || '1'`` or similar.  Custom adapters that don't translate
    placeholders are unaffected.
    """
    return Literal(sql=sql)


def flush_column_defaults(db: DBAdapter | None = None) -> None:
    """Evict cached column-DEFAULT introspection results.

    Cygnet caches the set of columns carrying a non-NULL DEFAULT on
    first INSERT against each (adapter, table) pair, then reuses it on
    every subsequent INSERT — the round-trip to PG's catalog amortises
    away.  The cache is stable for the lifetime of the schema, but
    adapters in long-running services (pooled connections, daemons)
    typically outlive the schemas they were populated against.

    After a migration that ALTERs a DEFAULT clause, call this function
    so the next INSERT re-introspects.  Otherwise Cygnet will keep
    omitting columns whose DEFAULT was dropped (writing NULL where the
    schema expected a value) or vice versa.  Cygnet has no way to
    detect migrations on its own.

    With ``db=None``: clears every adapter's entries (covers "any
    connection that goes through this process is post-migration").
    With a specific adapter: evicts only that adapter — useful for
    sharded / per-tenant migration patterns.  Either form is a no-op
    when the adapter has no cached entries.
    """
    Executor.flush_column_defaults(db)


# ── Convenience functions ────────────────────────────────────────────────────
# These wrap common patterns (get-by-PK, insert-without-upsert, upsert)
# so callers don't have to spell out the full builder chain for simple cases.
#
# The save / create / INSERT distinction is intentional and worth keeping
# straight when reading caller code:
#   - INSERT(db).INTO(T).VALUES(...) — generic builder; user controls
#     ON CONFLICT, RETURNING, etc.  Most flexible, most verbose.
#   - create(db, obj) — INSERT with no ON CONFLICT.  Duplicates raise
#     IntegrityError from the driver.  PK populated on obj for DBKey.
#   - save(db, obj) — INSERT ... ON CONFLICT DO UPDATE (upsert) when a
#     PK is known; plain INSERT ... RETURNING when DBKey + PK is None.
#     Idempotent; the default "persist this object" call.
# follow() and get() are read-side conveniences; they always SELECT and
# never mutate.


async def get[T](db: DBAdapter, table: TableProxy[T], **pk_kwargs: Any) -> T | None:
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


async def follow(db: DBAdapter, obj: Any, fk_column: Any) -> Any:
    """Load the object that a foreign key points to.

    Returns None if the FK value is None or no matching row exists.
    Raises ValueError if fk_column is not a foreign key.
    Raises TypeError if obj is not an instance of the FK column's table.
    """
    if not isinstance(fk_column, ColumnProxy):
        raise ValueError(f"{fk_column!r} is not a column proxy")

    # Order of checks below: type-validate fk_column → type-validate obj →
    # check FK metadata → read FK value → null-short-circuit → SELECT.
    # Validating obj before reading field metadata gives a clearer error
    # for the common "passed the wrong instance" mistake.
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


async def create(db: DBAdapter, obj: Any) -> Any:
    """
    INSERT obj into its table. No ON CONFLICT — duplicates raise from the DB.

    Returns the object with PK populated (for DBKey).
    """
    return await Executor(db).run_create(obj)


async def save(db: DBAdapter, obj: Any) -> None:
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


# State machine summary for transaction:
#
#   db._in_transaction = False  ──BEGIN──▶  db._in_transaction = True
#                       ▲                                  │
#                       └────COMMIT or ROLLBACK────────────┘
#
# A nested `async with cygnet.transaction(db)` finds the flag already
# True and issues SAVEPOINT spN / RELEASE / ROLLBACK TO instead of
# BEGIN / COMMIT / ROLLBACK.  The flag is *not* a counter — nesting depth
# isn't tracked here; SAVEPOINTs nest naturally on the server side, and
# the unique-per-instance savepoint name (`sp_{id(self)}`) keeps the
# release pair matched.
#
# Why a bool and not a counter: a counter would require Cygnet to
# manage the depth explicitly, which doesn't add safety (the server
# rejects mismatched COMMITs anyway) but does add a failure mode where
# a counter and the server's view diverge after an unexpected exception.
# The flag-plus-savepoint approach is monotonic and self-healing.
#
# This is NOT task-local on purpose: Cygnet expects one db adapter
# instance per asyncio task.  Sharing one PsycopgDB across concurrent
# tasks is now actively detected (S10): outermost __aenter__ captures
# `asyncio.current_task()` onto `db._transaction_task`, and nested
# __aenter__ verifies the same task is owning the transaction —
# cross-task nesting raises with a clear message instead of silently
# turning into a SAVEPOINT against another task's transaction.  The
# guard is best-effort: it requires the outer layer to use
# cygnet.transaction (not an externally-managed BEGIN), and treats a
# None current_task (e.g., outside any task context) as "no claim".
# See PsycopgDB's docstring for the same warning at the adapter layer.
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
    supported — the flag is not task-local — but Cygnet actively detects
    cross-task misuse (S10): the outermost ``__aenter__`` records the
    owning ``asyncio.current_task()`` on the db, and a nested entry from
    a different task raises ``RuntimeError`` rather than silently
    SAVEPOINTing inside the other task's transaction.

    Instance reuse: a single `transaction(db)` instance can be reused
    across sequential `async with` blocks — `__aenter__` resets
    `self._savepoint` so the prior nested savepoint name never leaks
    into a subsequent outermost BEGIN/COMMIT.  Concurrent re-entry of
    the SAME transaction instance is unsupported (the `self._savepoint`
    field would race); construct a fresh `transaction(db)` per task if
    you need parallel transactional contexts on the same db handle —
    and remember that the db adapter itself is also not task-local
    (see the previous paragraph).

    Usage::

        async with cygnet.transaction(db) as tx:
            await cygnet.INSERT(tx).INTO(AccountTable).VALUES(acc)
            async with cygnet.transaction(tx) as tx2:
                await cygnet.UPDATE(tx2).SET(LogTable, entry).WHERE(...)
    """

    def __init__(self, db: DBAdapter) -> None:
        self._db = db
        # Set to a savepoint name on __aenter__ when nested; stays None at
        # the outermost level.  Acts as the "am I the outermost?" signal
        # in __aexit__ — see the branch there.
        self._savepoint: str | None = None

    async def __aenter__(self) -> Any:
        # Reset _savepoint at every enter so a transaction instance can be
        # reused across multiple `async with` blocks without a stale
        # savepoint name leaking from a prior nested entry into a fresh
        # BEGIN/COMMIT cycle.
        self._savepoint = None
        # S10: task-locality guard.  asyncio.current_task() returns None
        # outside any task context (rare for async code — needs to be
        # awaited from somewhere), in which case we skip the check rather
        # than fight the runtime.  The captured-vs-current comparison
        # uses `is`, not equality, because tasks have no meaningful
        # __eq__ — identity is the right relation.
        current_task = asyncio.current_task()
        # getattr-with-default rather than direct access: adapters in the
        # wild (or freshly-constructed fakes in tests) may not have set
        # the attribute yet.  Treat absence as "not in a transaction".
        if getattr(self._db, "_in_transaction", False):
            # Already in a transaction → SAVEPOINT path, but first verify
            # we're in the same task that opened the outer transaction.
            # If `owner` is None (older code populated _in_transaction
            # without going through cygnet.transaction), be permissive:
            # the user has opted out of the guard by managing transactions
            # externally and we have no signal to compare against.
            owner = getattr(self._db, "_transaction_task", None)
            if (
                owner is not None
                and current_task is not None
                and owner is not current_task
            ):
                raise RuntimeError(
                    "cygnet.transaction: nested entry from a different "
                    "asyncio task than the one that opened the outer "
                    "transaction — db adapters are not task-safe.  Use "
                    "one db adapter (and connection) per task, or "
                    "serialise transactional access across tasks."
                )
            # id(self) produces a unique name per context manager instance,
            # avoiding collisions even with deeply nested savepoints.  The
            # id is stable for the lifetime of `self`, which covers enter
            # through exit — we need the same name in both halves to issue
            # the matching RELEASE / ROLLBACK TO SAVEPOINT.
            self._savepoint = f"sp_{id(self)}"
            await self._db.execute(f"SAVEPOINT {self._savepoint}")
        else:
            # Outermost layer: open a real transaction and claim the flag.
            # Ordering matters: BEGIN is sent first so a failing execute()
            # leaves the flag at False and the next transaction() call
            # opens cleanly instead of trying to SAVEPOINT against nothing.
            await self._db.execute("BEGIN")
            self._db._in_transaction = True
            # Claim the task identity for the duration of the outermost
            # transaction.  Stored on the db (not self) because the
            # `_in_transaction` state is on the db — keeping ownership
            # next to the state it protects.
            self._db._transaction_task = current_task
        # Returns the original db, not self — the caller uses the same
        # connection handle for queries inside the block.
        return self._db

    async def __aexit__(
        self,
        exc_type: type | None,
        exc_val: BaseException | None,
        exc_tb: Any,
    ) -> None:
        # Branch on "did __aenter__ open a savepoint?" rather than re-reading
        # _in_transaction: the flag is True for both inner and outer layers
        # while a nested transaction is active, so it can't distinguish them.
        if self._savepoint:
            # Nested layer: leave _in_transaction alone — the outermost
            # context manager owns it.  We do NOT swallow exc_type; returning
            # None re-raises it after the savepoint is dealt with.
            if exc_type:
                # Roll the savepoint back, then RELEASE it (S33).  ROLLBACK TO
                # SAVEPOINT leaves the savepoint defined in PostgreSQL, so
                # without the RELEASE it lingers on the savepoint stack for the
                # rest of the outer transaction whenever the outer block
                # catches the inner exception and continues.  Both run inside a
                # try so a failing savepoint command chains the original
                # exception (B8) rather than silently replacing it: with no
                # explicit `from`, the new error would propagate with only an
                # implicit __context__, losing the user's real error as the
                # __cause__ that tooling and `raise ... from` semantics expect.
                try:
                    await self._db.execute(f"ROLLBACK TO SAVEPOINT {self._savepoint}")
                    await self._db.execute(f"RELEASE SAVEPOINT {self._savepoint}")
                except Exception as savepoint_err:
                    raise savepoint_err from exc_val
            else:
                await self._db.execute(f"RELEASE SAVEPOINT {self._savepoint}")
        else:
            # The outermost context manager owns _in_transaction; reset it
            # in a `finally` so a failure inside ROLLBACK/COMMIT itself
            # doesn't strand the flag at True and silently turn the next
            # transaction on this connection into a SAVEPOINT against a
            # server-side-nonexistent transaction.  The task-ownership
            # claim is paired with the flag and cleared in the same
            # finally block — sequential cross-task reuse needs both
            # gone, not just _in_transaction.
            try:
                if exc_type:
                    # On the error path, a failing ROLLBACK must not silently
                    # replace the user's original exception (B8): chain it so
                    # the real error survives as __cause__.  When ROLLBACK
                    # succeeds, returning None re-raises exc_val normally.
                    try:
                        await self._db.execute("ROLLBACK")
                    except Exception as rollback_err:
                        raise rollback_err from exc_val
                else:
                    await self._db.execute("COMMIT")
            finally:
                self._db._in_transaction = False
                self._db._transaction_task = None
