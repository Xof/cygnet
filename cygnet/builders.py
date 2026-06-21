# builders.py — Fluent, awaitable query builders.
#
# Each SQL verb (SELECT, INSERT, UPDATE, DELETE) has a builder that
# accumulates clauses via chained method calls, then delegates to
# executor.py for SQL rendering and execution.  Builders are intentionally
# thin: they hold state but contain no SQL generation logic.
#
# All builders are awaitable — `await cygnet.SELECT(db).FROM(T)` works
# because __await__ delegates to an async _execute() method.  This avoids
# a separate .fetch() or .run() call.  The .sql() method is the non-executing
# alternative, returning (sql_string, params_list) for inspection or logging.
#
# Execution is single-shot in the sense that each `await` (or .sql() call)
# builds a fresh params list via the Executor — the builder itself never
# owns the params list.  Awaiting the same builder twice re-renders and
# re-executes the query.  The clause methods are order-independent: they
# only mutate state (lists/fields) and the Executor emits SQL in a fixed
# clause order (SELECT → FROM → JOIN → WHERE → GROUP BY → ORDER BY → LIMIT)
# regardless of the call order in the fluent chain.
#
# Executor is imported at module scope: the dependency direction is
# builders → executor (one-way), and executor never imports back into
# builders.  Earlier versions used in-method imports as a defensive
# guard; verified-acyclic now, the inline imports were dead weight.

from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from dataclasses import dataclass, field, replace

# typing.Literal is needed for the _OnConflictSpec.action annotation; aliased
# because cygnet.predicate.Literal (the SQL-literal renderable) shadows the
# name otherwise.  Both genuinely belong in this file, so the alias is the
# clean separation.
from typing import Any
from typing import Literal as TypeLiteral

from .cte import CTE, Lateral, RecursiveCTE
from .executor import Executor
from .expression import SQLRenderable
from .predicate import Literal, Predicate, _All
from .proxy import ColumnProxy, TableProxy

# A "table-like" source: a real model proxy or one of the CTE variants
# (regular / recursive / lateral) that duck-type a TableProxy.  Used
# in FROM / JOIN / UPDATE FROM / DELETE USING signatures so callers
# can pass any of these.  Lateral is a subclass of CTE so the union
# below already covers it.
TableSource = TableProxy[Any] | CTE | RecursiveCTE


def _reject_aliased_dml(table: Any, verb: str) -> None:
    """Refuse aliased TableProxies in DML target position.

    PG doesn't allow ``AS`` in INSERT / UPDATE / DELETE target position,
    and Cygnet's column refs from an aliased proxy carry the alias on
    the left of the dot — which then doesn't resolve, since the DML
    target was emitted without the alias.  Raising at builder time gives
    a clearer message than letting PG reject the malformed SQL later.
    The aliased proxy is fine for SELECT-side use (self-joins, etc.);
    callers wanting DML pass ``cygnet.Table(<model>)`` instead.
    """
    alias = getattr(table, "_alias", None)
    if alias is not None:
        raise ValueError(
            f"{verb} does not support aliased proxies (got alias {alias!r}); "
            f"pass the unaliased Table(<model>) proxy for DML"
        )


def _check_table_source(arg: Any, where: str) -> None:
    """Validate `arg` is a TableSource at runtime, with helpful errors.

    Method type-hints catch misuse for users running strict mypy, but
    plenty of users don't — at runtime, a wrong-type FROM/INTO/JOIN
    argument turns into an AttributeError 100 lines deep in the
    executor.  Catching it at the builder method gives a message that
    points at what the user actually did wrong.

    The most common mistake is passing the dataclass class itself
    (`cygnet.SELECT(db).FROM(Account)`) instead of the proxy
    (`cygnet.Table(Account)`).  We detect that case and suggest the
    fix explicitly.
    """
    if isinstance(arg, TableProxy | CTE | RecursiveCTE):
        return
    # Dataclass class? Most common mistake — suggest cygnet.Table().
    if isinstance(arg, type) and hasattr(arg, "__dataclass_fields__"):
        raise TypeError(
            f"{where} expects a TableProxy / CTE, got the dataclass class "
            f"{arg.__name__!r} directly.  Wrap it: "
            f"cygnet.Table({arg.__name__})."
        )
    raise TypeError(
        f"{where} expects a TableProxy / CTE / RecursiveCTE, got "
        f"{type(arg).__name__}: {arg!r}"
    )


@dataclass(frozen=True)
class _LockClause:
    """`FOR { UPDATE | SHARE | NO KEY UPDATE | KEY SHARE } [OF …] [NOWAIT|SKIP LOCKED]`.

    Rendered after OFFSET in the SELECT pipeline.  Captured as a frozen
    dataclass so the executor's render path stays uniform with other
    SQLRenderables (lock_clause.render_sql(params) returns the SQL fragment).

    PG rules baked in:
      - mode is one of "UPDATE" / "SHARE" / "NO KEY UPDATE" / "KEY SHARE".
        The builder methods constrain which are reachable.
      - `nowait` and `skip_locked` are mutually exclusive (PG enforces this
        at parse time, but we reject it client-side for a clearer error).
      - `of` restricts the lock to the named tables in a join — leave empty
        to lock every table in the query.
    """

    mode: str
    of: tuple[Any, ...] = field(default_factory=tuple)
    nowait: bool = False
    skip_locked: bool = False

    def render_sql(self, _params: list[Any]) -> str:
        # No parameters — every component is a literal keyword or a
        # table name (which we trust the same way TableProxy._sql_name
        # is trusted everywhere).  _params accepted only to satisfy
        # the SQLRenderable signature.
        sql = f"FOR {self.mode}"
        if self.of:
            of_names = ", ".join(t._sql_name for t in self.of)
            sql += f" OF {of_names}"
        if self.nowait:
            sql += " NOWAIT"
        elif self.skip_locked:
            sql += " SKIP LOCKED"
        return sql


class _Builder:
    """Base builder: holds the db handle and a predicate accumulator."""

    def __init__(self, db: Any) -> None:
        self._db = db
        # Predicates accumulate across chained WHERE() calls; the executor
        # ANDs them at render time.  _All (cygnet.all) may appear here for
        # UPDATE/DELETE as the explicit "every row" sentinel — the executor
        # rejects it if mixed with real predicates.
        self._predicates: list[SQLRenderable | _All] = []

    # Each subclass shadows WHERE() to refine the return type (SelectBuilder,
    # InsertBuilder, UpdateBuilder, DeleteBuilder) so chained calls preserve
    # the concrete builder's type for IDE autocomplete and mypy.  The body
    # is identical across overrides; only the annotation differs.
    def WHERE(self, predicate: SQLRenderable | _All) -> _Builder:
        # Multiple WHERE() calls are ANDed together in executor._render_where.
        # This is intentional: it lets callers conditionally chain filters
        # without manually building compound predicates.
        self._predicates.append(predicate)
        return self


class SelectBuilder(_Builder):
    def __init__(self, db: Any, *columns: SQLRenderable) -> None:
        super().__init__(db)
        # When columns is empty, the executor expands the SELECT list to
        # every field of the primary table (plus every field of any joined
        # tables) using their fully-qualified names, NOT "table.*".  That
        # explicit-column emission is what guarantees the row order matches
        # meta.fields regardless of PG's physical column order, which the
        # _map_select / _row_to_obj mapping depends on.  Non-empty columns
        # mean the caller wants raw tuples, with no object hydration.
        self._columns: tuple[SQLRenderable, ...] = columns
        self._table: TableSource | None = None
        # _joins is (kind, table, on) tuples where kind is one of
        # "INNER", "LEFT", "RIGHT", "FULL" — directly interpolated into
        # the executor's `{kind} JOIN` template.  Order matters: the
        # executor emits joins in insertion order, so JOIN A then LEFT_JOIN B
        # produces `... JOIN A ON ... LEFT JOIN B ON ...` and the right-hand-
        # side column visibility cascades accordingly.  The miss-detection
        # in _map_row branches on kind to decide whether the left side
        # (RIGHT, FULL), the right side (LEFT, FULL), or neither
        # (INNER) can be NULL for a given join.
        self._joins: list[tuple[str, TableSource, Predicate]] = []
        # _order entries are (column, "ASC"|"DESC") — the direction is
        # per-call, not per-column, so ORDER_BY(a, b, DESC=True) flips
        # both.  Mix directions across separate calls if you need that:
        # .ORDER_BY(a).ORDER_BY(b, DESC=True).
        self._order: list[tuple[Any, str]] = []
        self._group: list[Any] = []
        self._having: list[SQLRenderable] = []
        self._limit: int | None = None
        self._offset: int | None = None
        self._distinct: bool = False
        # PG-specific DISTINCT ON (col, col, …): keeps one row per
        # distinct value of the listed columns, picking the row according
        # to the ORDER BY (or arbitrarily if no ORDER BY).  Empty tuple
        # means "no DISTINCT ON" — a separate state from `_distinct =
        # True` so the two can't be confused.  Mutually exclusive with
        # plain DISTINCT; the methods enforce that.
        self._distinct_on: tuple[SQLRenderable, ...] = ()
        # CTEs prefixed onto the SELECT as `WITH name AS (…), …`.  Stored
        # as Any to avoid an import cycle through cte.py; the executor
        # reads ._name and ._builder via duck typing.
        self._ctes: list[Any] = []
        # Set-ops chained onto this SELECT: list of (keyword, other)
        # pairs where keyword is "UNION", "UNION ALL", "INTERSECT",
        # "INTERSECT ALL", "EXCEPT", or "EXCEPT ALL", and `other` is
        # another SelectBuilder.  Each .UNION() / .EXCEPT_() / etc.
        # appends here; the executor emits them after the left SELECT
        # in the order they were chained.
        self._set_ops: list[tuple[str, SelectBuilder]] = []
        # Row-level locking clause (FOR UPDATE / FOR SHARE / variants).
        # Stored as None until FOR_UPDATE / FOR_SHARE is called; only
        # one lock clause per SELECT (PG accepts multiple FOR clauses
        # with different modes, but the use case is exotic enough that
        # we keep a single slot until someone needs more).
        self._lock: _LockClause | None = None

    def WITH(self, *ctes: Any) -> SelectBuilder:  # noqa: N802
        """Prefix one or more CTEs onto this SELECT.

        Multiple .WITH() calls accumulate, and a single call accepts any
        number of CTE objects:
            .WITH(active, recent)
            .WITH(active).WITH(recent)
        """
        self._ctes.extend(ctes)
        return self

    # Override to return SelectBuilder (not _Builder) for fluent chaining.
    def WHERE(self, predicate: SQLRenderable | _All) -> SelectBuilder:
        self._predicates.append(predicate)
        return self

    def FROM(self, table: TableSource) -> SelectBuilder:
        _check_table_source(table, "SELECT … FROM")
        self._table = table
        return self

    # `ON` is keyword-only across the JOIN family.  Forcing it keyword-only
    # makes call sites self-documenting (`JOIN(T, ON=T.a == U.b)`) and
    # prevents the easy mistake of passing the predicate where a table
    # is expected.
    def JOIN(self, table: TableSource, *, ON: Predicate) -> SelectBuilder:
        _check_table_source(table, "JOIN")
        self._joins.append(("INNER", table, ON))
        return self

    def LEFT_JOIN(self, table: TableSource, *, ON: Predicate) -> SelectBuilder:
        _check_table_source(table, "LEFT_JOIN")
        self._joins.append(("LEFT", table, ON))
        return self

    def RIGHT_JOIN(self, table: TableSource, *, ON: Predicate) -> SelectBuilder:  # noqa: N802
        """`RIGHT JOIN` — every row of `table` is preserved; rows of the
        FROM-side without a match yield NULL for FROM-side columns.

        Mostly redundant with `LEFT_JOIN` written from the other table's
        perspective, but readable when the call site is already anchored
        on a FROM table you don't want to swap.  Row-mapping returns
        ``(left_obj_or_None, right_obj)`` tuples; the left-side ``None``
        signals an unmatched right row (FROM PK was NULL in the joined
        result).
        """
        _check_table_source(table, "RIGHT_JOIN")
        self._joins.append(("RIGHT", table, ON))
        return self

    def FULL_JOIN(self, table: TableSource, *, ON: Predicate) -> SelectBuilder:  # noqa: N802
        """`FULL JOIN` (full outer join) — every row of both sides is
        preserved; the unmatched side yields NULL columns.

        Row-mapping returns ``(left_obj_or_None, right_obj_or_None)``
        tuples — at least one side is always non-None in any given row,
        but you must check both when consuming the result.
        """
        _check_table_source(table, "FULL_JOIN")
        self._joins.append(("FULL", table, ON))
        return self

    def JOIN_LATERAL(  # noqa: N802
        self, lat: Lateral, *, ON: SQLRenderable | None = None
    ) -> SelectBuilder:
        """`INNER JOIN LATERAL (subquery) alias ON cond`.

        PG requires ON for LATERAL joins even when there's no real
        correlation to express; default `ON cygnet.lit("true")` keeps
        the common case from being noisy.  Build the inner SELECT
        first, wrap with `cygnet.lateral(name, inner)`, then JOIN.
        """
        if not isinstance(lat, Lateral):
            raise TypeError(
                f"JOIN_LATERAL expects a Lateral (cygnet.lateral(...)), "
                f"got {type(lat).__name__}"
            )
        on: SQLRenderable = ON if ON is not None else Literal(sql="true")
        self._joins.append(("INNER", lat, on))  # type: ignore[arg-type]
        return self

    def LEFT_JOIN_LATERAL(  # noqa: N802
        self, lat: Lateral, *, ON: SQLRenderable | None = None
    ) -> SelectBuilder:
        """LEFT JOIN LATERAL — same as JOIN_LATERAL but produces NULL
        rows for outer rows whose subquery returns nothing.  Common
        for "top-N per group" patterns where some groups have no rows."""
        if not isinstance(lat, Lateral):
            raise TypeError(
                f"LEFT_JOIN_LATERAL expects a Lateral (cygnet.lateral(...)), "
                f"got {type(lat).__name__}"
            )
        on: SQLRenderable = ON if ON is not None else Literal(sql="true")
        self._joins.append(("LEFT", lat, on))  # type: ignore[arg-type]
        return self

    def FOLLOW(self, fk_column: Any) -> SelectBuilder:  # noqa: N802
        """INNER JOIN the table that fk_column references, using the FK relationship.

        Semantically equivalent to a manual JOIN with an auto-generated ON:
        results are returned as tuples (left_obj, right_obj, ...), matching
        the shape the caller gets from JOIN/LEFT_JOIN.
        """
        return self._follow("INNER", fk_column)

    def LEFT_FOLLOW(self, fk_column: Any) -> SelectBuilder:  # noqa: N802
        """LEFT JOIN the table that fk_column references, using the FK relationship.

        Matches LEFT_JOIN's tuple-return shape; unmatched rows yield None
        for the right-hand object (see executor._map_select).
        """
        return self._follow("LEFT", fk_column)

    def _follow(self, join_type: str, fk_column: Any) -> SelectBuilder:
        """Resolve a foreign key column into a JOIN clause.

        This is syntactic sugar over JOIN/LEFT_JOIN: it inspects the FK
        metadata on the column to auto-generate the ON predicate, so the
        caller doesn't have to spell out the join condition manually.
        """
        if not isinstance(fk_column, ColumnProxy):
            raise ValueError(f"{fk_column!r} is not a column proxy")

        field = fk_column._field
        if field.foreign_key is None:
            raise ValueError(
                f"{fk_column._table._meta.cls.__name__}.{field.attr_name} "
                f"is not a foreign key"
            )

        # Build the join target from FK metadata and synthesise
        # `fk_column == target.pk` as the ON predicate.  Always joins on PK
        # because ForeignKey is constrained to PK references at annotation
        # time — the simpler invariant lets _follow stay this short.
        target_proxy: TableProxy[Any] = TableProxy(field.foreign_key.target)
        # FK validation in _introspect() guarantees the target has a PK.
        assert target_proxy._meta.pk is not None
        target_pk_col = getattr(target_proxy, target_proxy._meta.pk.attr_name)
        on_predicate = fk_column == target_pk_col
        self._joins.append((join_type, target_proxy, on_predicate))
        return self

    def ORDER_BY(self, *columns: SQLRenderable, DESC: bool = False) -> SelectBuilder:  # noqa: N803
        direction = "DESC" if DESC else "ASC"
        # extend (not append) so ORDER_BY(T.a, T.b) adds two entries.
        self._order.extend((c, direction) for c in columns)
        return self

    def GROUP_BY(self, *columns: SQLRenderable) -> SelectBuilder:
        # Bare SELECT(db) + GROUP_BY is rejected because executor would try to
        # map "table.*" rows to dataclasses, but GROUP BY changes the result
        # shape unpredictably.  Requiring explicit columns forces the user to
        # think about what the aggregated result looks like.
        if not self._columns:
            raise ValueError(
                "GROUP_BY requires explicit column selection — "
                "use SELECT(db, col1, col2, ...) rather than SELECT(db)"
            )
        self._group.extend(columns)
        return self

    def LIMIT(self, n: int) -> SelectBuilder:
        # PostgreSQL rejects negative LIMIT, but the error surfaces late
        # and confusingly (server-side parse error).  Catch it client-side
        # so the traceback points at the offending call site.
        if n < 0:
            raise ValueError(f"LIMIT must be non-negative, got {n}")
        self._limit = n
        return self

    def OFFSET(self, n: int) -> SelectBuilder:  # noqa: N802
        # Same client-side rejection as LIMIT.  OFFSET 0 is a no-op
        # (allowed) but negative is an error.
        if n < 0:
            raise ValueError(f"OFFSET must be non-negative, got {n}")
        self._offset = n
        return self

    def FOR_UPDATE(  # noqa: N802
        self,
        *,
        of: TableSource | tuple[TableSource, ...] | list[TableSource] = (),
        nowait: bool = False,
        skip_locked: bool = False,
        no_key: bool = False,
    ) -> SelectBuilder:
        """`FOR UPDATE [OF …] [NOWAIT | SKIP LOCKED]` — exclusive row lock.

        Use when the caller intends to UPDATE / DELETE the selected rows
        in the same transaction; PG holds the lock until commit, blocking
        concurrent writes (and concurrent FOR UPDATE / FOR SHARE).

        ``of`` restricts the lock to specific tables in a join — leave
        unset to lock every table the SELECT touches.  ``no_key=True``
        downgrades to ``FOR NO KEY UPDATE``, the weaker variant that
        doesn't block FK-referencing inserts in other transactions
        (useful when the SELECT-then-UPDATE doesn't change the PK).

        ``nowait`` and ``skip_locked`` are mutually exclusive (PG would
        reject both at parse time; we reject client-side for a clearer
        error).  Both are off by default — the lock waits.
        """
        mode = "NO KEY UPDATE" if no_key else "UPDATE"
        return self._set_lock(mode=mode, of=of, nowait=nowait, skip_locked=skip_locked)

    def FOR_SHARE(  # noqa: N802
        self,
        *,
        of: TableSource | tuple[TableSource, ...] | list[TableSource] = (),
        nowait: bool = False,
        skip_locked: bool = False,
        key: bool = False,
    ) -> SelectBuilder:
        """`FOR SHARE [OF …] [NOWAIT | SKIP LOCKED]` — shared row lock.

        Use when the caller wants to read rows under the assumption that
        they won't change before the transaction commits, but doesn't
        intend to update them.  Multiple FOR SHARE locks coexist on the
        same row; FOR UPDATE blocks them.

        ``key=True`` downgrades to ``FOR KEY SHARE``, the weakest lock
        mode — blocks only FOR UPDATE / FOR NO KEY UPDATE on the same
        row.  Useful for FK integrity reads.

        See FOR_UPDATE for ``of`` / ``nowait`` / ``skip_locked`` semantics.
        """
        mode = "KEY SHARE" if key else "SHARE"
        return self._set_lock(mode=mode, of=of, nowait=nowait, skip_locked=skip_locked)

    def _set_lock(
        self,
        *,
        mode: str,
        of: TableSource | tuple[TableSource, ...] | list[TableSource],
        nowait: bool,
        skip_locked: bool,
    ) -> SelectBuilder:
        # Centralised validation + dataclass construction.  Keeping it on
        # _set_lock instead of repeating in FOR_UPDATE / FOR_SHARE means
        # the rules (mutual exclusion, second-call rejection, of-tuple
        # normalisation) only live in one place.
        if nowait and skip_locked:
            raise ValueError(
                "FOR_UPDATE / FOR_SHARE: nowait and skip_locked are mutually exclusive"
            )
        # Second-call rejection: a single _lock slot means re-calling
        # FOR_UPDATE / FOR_SHARE would silently clobber the prior lock.
        # Raising forces the caller to be explicit about what they meant.
        if self._lock is not None:
            raise ValueError(
                "FOR_UPDATE / FOR_SHARE called twice on the same SELECT — "
                "PG accepts multiple lock clauses but Cygnet only stores one slot; "
                "if you genuinely need this, raise an issue with the use case"
            )
        # Normalise `of`: accept a single table, a tuple, or a list.  The
        # internal _LockClause stores a tuple for hashability / immutability.
        # Dispatch on tuple/list explicitly — `tuple(of)` on a bare
        # dataclass class would raise an opaque "type is not iterable"
        # before the `_check_table_source` helpful error could fire.
        if isinstance(of, tuple | list):
            of_tuple: tuple[Any, ...] = tuple(of)
        else:
            of_tuple = (of,)
        for t in of_tuple:
            _check_table_source(t, "FOR_UPDATE/FOR_SHARE OF")
        self._lock = _LockClause(
            mode=mode, of=of_tuple, nowait=nowait, skip_locked=skip_locked
        )
        return self

    def HAVING(self, predicate: SQLRenderable) -> SelectBuilder:  # noqa: N802
        # HAVING filters post-aggregation, so it can only be meaningful in
        # combination with GROUP_BY.  We don't enforce that here — emitting
        # HAVING without GROUP BY is occasionally useful (e.g. with window
        # functions) and the SQL planner will reject anything truly invalid.
        # cygnet.all isn't accepted: "all aggregate groups" isn't a thing
        # the way "all rows" is for WHERE.  S3 (2026-05-22): the isinstance
        # check below makes the docstring's promise enforceable; before
        # this guard the sentinel would silently render through to SQL.
        if isinstance(predicate, _All):
            raise ValueError(
                "HAVING does not accept cygnet.all — HAVING is for "
                "aggregate-group filters, not 'all groups'"
            )
        self._having.append(predicate)
        return self

    def DISTINCT(self) -> SelectBuilder:  # noqa: N802
        # Plain DISTINCT (deduplicate every row).  See DISTINCT_ON for
        # the column-restricted PG variant.
        if self._distinct_on:
            raise ValueError("DISTINCT and DISTINCT_ON are mutually exclusive")
        self._distinct = True
        return self

    def DISTINCT_ON(self, *columns: SQLRenderable) -> SelectBuilder:  # noqa: N802
        """`SELECT DISTINCT ON (col, …) …` — keep one row per distinct
        value of the listed columns.

        For deterministic results, pair with `.ORDER_BY(...)` whose
        leading columns match the DISTINCT ON columns; PG picks the
        first row per group as ordered.  Cygnet doesn't enforce the
        ORDER BY relationship — that's the user's responsibility.

        Mutually exclusive with plain `.DISTINCT()`.  An empty
        `DISTINCT_ON()` call is rejected: PG syntax requires at least
        one column.
        """
        if not columns:
            raise ValueError("DISTINCT_ON requires at least one column")
        if self._distinct:
            raise ValueError("DISTINCT and DISTINCT_ON are mutually exclusive")
        self._distinct_on = columns
        return self

    # ── Set operations ────────────────────────────────────────────────────
    # Each method appends (keyword, other) to _set_ops.  The executor
    # emits the left SELECT, then each set op + the other SELECT, in
    # the order they were chained.  ORDER BY / LIMIT / OFFSET on the
    # left builder apply to the COMPOUND (PG syntax: trailing ORDER BY
    # binds to the whole set-op chain, not the last operand).

    def UNION(self, other: SelectBuilder) -> SelectBuilder:  # noqa: N802
        self._set_ops.append(("UNION", other))
        return self

    def UNION_ALL(self, other: SelectBuilder) -> SelectBuilder:  # noqa: N802
        self._set_ops.append(("UNION ALL", other))
        return self

    def INTERSECT(self, other: SelectBuilder) -> SelectBuilder:  # noqa: N802
        self._set_ops.append(("INTERSECT", other))
        return self

    def INTERSECT_ALL(self, other: SelectBuilder) -> SelectBuilder:  # noqa: N802
        self._set_ops.append(("INTERSECT ALL", other))
        return self

    def EXCEPT_(self, other: SelectBuilder) -> SelectBuilder:  # noqa: N802
        # Trailing underscore because `except` is a Python keyword.
        self._set_ops.append(("EXCEPT", other))
        return self

    def EXCEPT_ALL(self, other: SelectBuilder) -> SelectBuilder:  # noqa: N802
        self._set_ops.append(("EXCEPT ALL", other))
        return self

    def sql(self) -> tuple[str, list[Any]]:
        # render_select shares rendering logic with run_select and applies
        # the same validation (no execution-only checks exist for SELECT).
        # Each call creates a fresh params list, so sql() is idempotent.
        return Executor(self._db).render_select(self)

    def render_sql(self, params: list[Any]) -> str:
        """Render as an inline subquery `(SELECT …)`.

        Makes SelectBuilder satisfy the SQLRenderable protocol so it can
        appear in any expression position: scalar subquery in a SELECT
        list, ``WHERE col IN (subq)``, ``WHERE EXISTS (subq)``, etc.  The
        surrounding parens are part of every subquery context, so we add
        them here once rather than at each consumer.

        Inner-query $N parameters are appended to the shared params list
        at the textual position the subquery appears in the outer SQL,
        keeping numbering monotonic across the whole statement (same
        trick the LATERAL render path uses).
        """
        # _render_select is the rendering primitive (versus the public
        # render_select which always allocates a fresh params list).
        # Calling the underscore method here is the internal API; the
        # alternative — promoting it to a public name — would just rename
        # the same coupling.
        inner = Executor(self._db)._render_select(self, params)
        return f"({inner})"

    def __await__(self) -> Generator[Any, None, list[Any]]:
        # Delegating __await__ to an async coroutine is what makes
        # `await builder` syntactically valid without a trailing .fetch().
        # The same two-method pattern (__await__ → _execute) repeats in
        # InsertBuilder / UpdateBuilder / DeleteBuilder; _execute is split
        # out so subclasses can `await` the same flow internally without
        # re-implementing __await__'s generator wiring.
        return self._execute().__await__()

    async def _execute(self) -> list[Any]:
        # Fresh Executor per await: keeps the builder stateless w.r.t. the
        # last execution, so a builder can be safely awaited more than
        # once (each call re-renders SQL and re-numbers params from $1).
        return await Executor(self._db).run_select(self)

    def stream(self) -> AsyncIterator[Any]:
        """Yield results one at a time without materialising the full list.

        Use as `async for row in builder.stream()`.  Requires the db
        adapter to implement an async `stream(sql, params)` method
        (psycopg's portal-cursor approach is the reference).  PG portal
        cursors must run inside a transaction, so typical usage wraps
        this in `async with cygnet.transaction(db)`.
        """
        return Executor(self._db).stream_select(self)


# ── ON CONFLICT spec ─────────────────────────────────────────────────────
# Collapses the five state fields the InsertBuilder used to carry
# individually (target, constraint, action, set_kwargs, excluded_cols)
# into one validated value object.  Centralising the structural
# invariants in __post_init__ removes the cross-method validation
# duplication and gives the executor a single struct to read rather
# than five sibling attributes.  Closes S5 and the S7 typing
# tightening (tuple[ColumnProxy[Any], ...] vs. tuple[Any, ...]) in
# one refactor.


@dataclass(frozen=True)
class _OnConflictSpec:
    """Validated PG ``ON CONFLICT`` specification.

    Five orthogonal axes — only certain combinations are legal:
      ``target`` / ``constraint``   conflict target (mutually exclusive)
      ``action``                    "nothing" or "update"
      ``set_kwargs``                ``DO UPDATE SET col = literal``
      ``excluded_cols``             ``DO UPDATE SET col = EXCLUDED.col``

    Invariants enforced in ``__post_init__``:
      - ``target`` and ``constraint`` are mutually exclusive
      - ``action="update"`` requires a target (column or constraint) AND
        exactly one of ``set_kwargs`` / ``excluded_cols``
      - ``action="nothing"`` is legal with any target shape, including
        none (that's the ``ON_CONFLICT_DO_NOTHING()`` shorthand path)

    Builder methods on ``InsertBuilder`` construct/update specs via
    ``dataclasses.replace`` — atomic from the spec's point of view, so
    multi-field updates (e.g. action+set_kwargs in one call) run
    ``__post_init__`` once with the final shape.
    """

    target: tuple[ColumnProxy[Any], ...] | None = None
    constraint: str | None = None
    action: TypeLiteral["nothing", "update"] | None = None
    set_kwargs: dict[str, Any] | None = None
    excluded_cols: tuple[ColumnProxy[Any], ...] | None = None

    def __post_init__(self) -> None:
        if self.target is not None and self.constraint is not None:
            raise ValueError(
                "ON_CONFLICT and ON_CONFLICT_CONSTRAINT are mutually exclusive"
            )
        if self.action == "update":
            if self.target is None and self.constraint is None:
                # Same wording the old method-level guard used so the
                # existing tests' match= substrings still hit.
                raise ValueError(
                    "DO_UPDATE requires a preceding ON_CONFLICT (column "
                    "target) or ON_CONFLICT_CONSTRAINT — PG can't perform "
                    "DO UPDATE without knowing which constraint to look at"
                )
            if self.set_kwargs is None and self.excluded_cols is None:
                # Reachable only if a programmer constructs a spec
                # directly; the builder methods always supply one or
                # the other.  Surface it anyway so the spec is honest
                # standalone.
                raise ValueError(
                    "DO_UPDATE / DO_UPDATE_FROM_EXCLUDED requires SET values"
                )
            if self.set_kwargs is not None and self.excluded_cols is not None:
                raise ValueError(
                    "DO_UPDATE and DO_UPDATE_FROM_EXCLUDED are mutually exclusive"
                )


class InsertBuilder(_Builder):
    # Four mutually-exclusive value sources are tracked by separate
    # attributes; the methods that set them (VALUES, BULK_VALUES, SELECT)
    # each guard against the other three being populated, so the state
    # machine is enforced at the call site rather than at render time.
    # The four states:
    #   1. single object        — _obj is set (kwargs empty, _bulk_objs None)
    #   2. column-kwargs        — _kwargs populated (_obj None, _bulk_objs None)
    #   3. BULK_VALUES list     — _bulk_objs is a non-empty list
    #   4. INSERT…SELECT        — _select_source is set
    def __init__(self, db: Any) -> None:
        super().__init__(db)
        self._table: TableProxy[Any] | None = None
        self._obj: Any = None
        self._kwargs: dict[str, Any] = {}
        # Bulk path: a list of objects to insert in one round-trip.  Mutually
        # exclusive with _obj/_kwargs; the executor branches on whichever is
        # populated.  Kept None (not []) so "empty bulk" can raise rather
        # than silently emit no SQL.
        self._bulk_objs: list[Any] | None = None
        # INSERT … SELECT path: a SelectBuilder whose result feeds the
        # target table.  Mutually exclusive with the VALUES / BULK_VALUES
        # paths; only one of the four can populate at a time.
        self._select_source: SelectBuilder | None = None
        self._select_columns: list[str] | None = None
        # ON CONFLICT clause state.  Replaced five sibling attributes with
        # one validated spec (S5): structural invariants live in
        # _OnConflictSpec.__post_init__ and the executor reads a single
        # struct.  None means "no ON CONFLICT clause".
        self._on_conflict: _OnConflictSpec | None = None

    def INTO(self, table: TableProxy[Any]) -> InsertBuilder:
        """Set the INSERT target table.

        Calling INTO a second time replaces the prior target outright
        (the underlying builder holds a single _table slot — there's no
        "merge two targets" semantics in INSERT SQL, so clobber is the
        honest behaviour rather than an ambiguous merge).
        """
        _check_table_source(table, "INSERT … INTO")
        _reject_aliased_dml(table, "INSERT … INTO")
        self._table = table
        return self

    def VALUES(self, obj: Any = None, **kwargs: Any) -> InsertBuilder:
        # Either an object or kwargs, not both.  The previous behavior
        # (obj wins, kwargs silently dropped) made calls like
        # VALUES(account, status="override") look like an override pattern
        # while quietly inserting the object's status.  Forcing the caller
        # to pick one keeps intent unambiguous.
        if obj is not None and kwargs:
            raise ValueError("VALUES accepts either an object or kwargs, not both")
        if self._bulk_objs is not None:
            raise ValueError("INSERT cannot combine VALUES with BULK_VALUES")
        # S36: complete the four-way mutual exclusion.  BULK_VALUES and SELECT
        # already guard their directions; without this, .SELECT(src).VALUES(obj)
        # set _obj alongside _select_source and the executor (which checks
        # _select_source first) silently dropped the VALUES object.
        if self._select_source is not None:
            raise ValueError("INSERT cannot combine VALUES with SELECT")
        self._obj = obj
        self._kwargs = kwargs
        return self

    def BULK_VALUES(self, objs: list[Any]) -> InsertBuilder:  # noqa: N802
        """Insert many objects in one statement: VALUES (…), (…), ….

        All objects must be of the same model class.  The column list is
        determined by the *first* object: DBKey=None on the first object
        excludes the PK column for every row (PG generates each one).
        Mixing PK-set and PK-None objects is not supported — split into
        two calls if you need that.

        After awaiting, each object's DBKey gets populated from RETURNING
        in input order, mirroring single-row VALUES behaviour.

        Empty list raises ValueError; PG doesn't accept empty VALUES().
        """
        if not objs:
            raise ValueError("BULK_VALUES requires at least one object")
        if self._obj is not None or self._kwargs:
            raise ValueError("INSERT cannot combine VALUES with BULK_VALUES")
        if self._select_source is not None:
            raise ValueError("INSERT cannot combine BULK_VALUES with SELECT")
        # Materialise once so callers can pass any iterable; the executor
        # iterates this list multiple times (column extraction + per-row
        # render + per-row PK assignment).
        self._bulk_objs = list(objs)
        return self

    # ── ON CONFLICT clause ────────────────────────────────────────────
    # PG's `INSERT ... ON CONFLICT [target] [action]` family.  Three
    # methods set the target — ON_CONFLICT(*cols), ON_CONFLICT_CONSTRAINT
    # (name), and the all-in-one ON_CONFLICT_DO_NOTHING() which sets
    # both target (none) and action (nothing) — and two set the action:
    # DO_NOTHING() and DO_UPDATE(**fields) / DO_UPDATE_FROM_EXCLUDED(*cols).
    #
    # Target & action are accumulated separately on the builder so the
    # method ordering is rigid: target first, then action.  The render
    # path validates the combination at SQL-emission time.
    #
    # Currently scoped to the single-row VALUES path.  BULK_VALUES +
    # ON_CONFLICT and INSERT…SELECT + ON_CONFLICT raise: PG accepts
    # them but Cygnet's RETURNING-row-count and PK-population logic
    # would need extra care to handle skipped rows correctly.  Add
    # later if a real use case appears.

    def ON_CONFLICT(self, *cols: Any) -> InsertBuilder:  # noqa: N802
        """Set the conflict target by column.

        Pair with .DO_NOTHING() or .DO_UPDATE(...) / .DO_UPDATE_FROM_EXCLUDED(...).
        For the any-conflict skip case (no target), use the shorthand
        .ON_CONFLICT_DO_NOTHING() instead.
        """
        # The "not empty" check is method-specific input validation; the
        # mutual-exclusion-with-constraint and all action invariants are
        # enforced by the spec's __post_init__ via replace().
        if not cols:
            raise ValueError(
                "ON_CONFLICT requires at least one column; "
                "use ON_CONFLICT_DO_NOTHING() for the any-conflict shorthand"
            )
        current = self._on_conflict or _OnConflictSpec()
        self._on_conflict = replace(current, target=cols)
        return self

    def ON_CONFLICT_CONSTRAINT(self, name: str) -> InsertBuilder:  # noqa: N802
        """Set the conflict target by named constraint."""
        current = self._on_conflict or _OnConflictSpec()
        self._on_conflict = replace(current, constraint=name)
        return self

    def _require_no_action(self) -> None:
        # S35: the conflict action is terminal — DO_NOTHING / DO_UPDATE /
        # DO_UPDATE_FROM_EXCLUDED each set it, and chaining a second one
        # previously clobbered the first silently (last-call-wins, with the
        # stale set_kwargs left inert in the spec).  Reject re-setting it,
        # mirroring _set_lock's deliberate second-call rejection.  ON_CONFLICT
        # / ON_CONFLICT_CONSTRAINT set only the target, never the action, so
        # they remain freely chainable before the single action call.
        if self._on_conflict is not None and self._on_conflict.action is not None:
            raise ValueError(
                "ON CONFLICT action already set — DO_NOTHING / DO_UPDATE / "
                "DO_UPDATE_FROM_EXCLUDED is terminal; chain only one"
            )

    def ON_CONFLICT_DO_NOTHING(self) -> InsertBuilder:  # noqa: N802
        """`ON CONFLICT DO NOTHING` with no target — silently skip any
        conflict on any unique index or exclusion constraint."""
        self._require_no_action()
        current = self._on_conflict or _OnConflictSpec()
        self._on_conflict = replace(current, action="nothing")
        return self

    def DO_NOTHING(self) -> InsertBuilder:  # noqa: N802
        """Pair with a preceding ON_CONFLICT(*cols) or ON_CONFLICT_CONSTRAINT."""
        self._require_no_action()
        # Chain-time invariant: this is the post-target form, so a
        # preceding ON_CONFLICT or ON_CONFLICT_CONSTRAINT must already be
        # in the spec.  The spec's __post_init__ legitimately allows
        # action="nothing" with no target (that's the shorthand path),
        # so the check has to live here — only the call-site knows
        # which spelling the user wrote.
        if self._on_conflict is None or (
            self._on_conflict.target is None and self._on_conflict.constraint is None
        ):
            raise ValueError(
                "DO_NOTHING requires a preceding ON_CONFLICT or "
                "ON_CONFLICT_CONSTRAINT; use ON_CONFLICT_DO_NOTHING() for the "
                "any-conflict shorthand"
            )
        self._on_conflict = replace(self._on_conflict, action="nothing")
        return self

    def DO_UPDATE(self, **fields: Any) -> InsertBuilder:  # noqa: N802
        """`DO UPDATE SET col = value, …` with literal values from kwargs.

        For "use the value the new row tried to insert" semantics
        (i.e. SET col = EXCLUDED.col), use DO_UPDATE_FROM_EXCLUDED.
        """
        self._require_no_action()
        # "Not empty" is method-specific input validation; the
        # action+target coherence checks live in _OnConflictSpec.
        if not fields:
            raise ValueError("DO_UPDATE requires at least one field to set")
        current = self._on_conflict or _OnConflictSpec()
        self._on_conflict = replace(current, action="update", set_kwargs=fields)
        return self

    def DO_UPDATE_FROM_EXCLUDED(self, *cols: Any) -> InsertBuilder:  # noqa: N802
        """`DO UPDATE SET col1 = EXCLUDED.col1, …` for the listed columns.

        EXCLUDED is PG's pseudo-table referring to the row the INSERT
        attempted to add; this mirrors save()'s upsert semantics where
        every non-PK field gets clobbered with the new value.
        """
        self._require_no_action()
        if not cols:
            raise ValueError("DO_UPDATE_FROM_EXCLUDED requires at least one column")
        current = self._on_conflict or _OnConflictSpec()
        self._on_conflict = replace(current, action="update", excluded_cols=cols)
        return self

    def SELECT(  # noqa: N802
        self,
        source: SelectBuilder,
        columns: list[str] | None = None,
    ) -> InsertBuilder:
        """Insert rows from a SELECT: ``INSERT INTO t (cols) SELECT ...``.

        ``columns`` are target DB column names that align positionally
        with the source SELECT's projection.  When omitted, they're
        inferred from the source's explicit ColumnProxy list (each
        column's DB ``column_name``); the source must use ColumnProxy
        refs for inference to work.  For sources that project opaque
        expressions (cygnet.fn / cygnet.lit / op), pass ``columns=[…]``
        explicitly.

        After awaiting, returns the list of generated PKs (DBKey models)
        or None (AppKey / no-PK targets) — INSERT…SELECT generally
        produces a row per source row, and there's no input list to
        mutate the way single-row / BULK_VALUES does.
        """
        # No _check_table_source here: source is a SelectBuilder, not a
        # TableSource.  The executor's render path passes it through
        # SelectBuilder.render_sql to inline it as the row source.
        if self._obj is not None or self._kwargs or self._bulk_objs is not None:
            raise ValueError("INSERT cannot combine SELECT with VALUES / BULK_VALUES")
        self._select_source = source
        self._select_columns = list(columns) if columns is not None else None
        return self

    def sql(self) -> tuple[str, list[Any]]:
        return Executor(self._db).render_insert(self)

    def __await__(self) -> Generator[Any, None, Any]:
        return self._execute().__await__()

    async def _execute(self) -> Any:
        return await Executor(self._db).run_insert(self)


class UpdateBuilder(_Builder):
    # UPDATE requires an explicit WHERE clause (enforced in the executor).
    # Callers who want to affect every row must opt in with WHERE(cygnet.all).
    # This is a safety rail against accidental mass mutation — one of the
    # most common catastrophic SQL mistakes.
    def __init__(self, db: Any) -> None:
        super().__init__(db)
        self._table: TableProxy[Any] | None = None
        self._obj: Any = None
        self._kwargs: dict[str, Any] = {}
        # When set, the executor emits RETURNING and the awaited result
        # becomes a list of tuples (one per affected row) rather than None.
        self._returning: tuple[SQLRenderable, ...] | None = None
        # Other tables to read SET values from (UPDATE … FROM other …).
        # Multiple FROM tables append; the WHERE clause carries the
        # join condition.  PG-specific extension to standard SQL.
        self._from_tables: list[TableSource] = []

    def WHERE(self, predicate: SQLRenderable | _All) -> UpdateBuilder:
        self._predicates.append(predicate)
        return self

    def SET(
        self, table: TableProxy[Any], obj: Any = None, **kwargs: Any
    ) -> UpdateBuilder:
        # SET serves double duty: it specifies which table to update AND
        # what values to set.  This keeps the fluent API concise:
        #   UPDATE(db).SET(T, name="x").WHERE(...)
        # rather than requiring a separate .TABLE(T).SET(name="x") chain.
        # kwargs values are flexible: literals become $N parameters, but
        # SQLRenderable values (T.col + 1, cygnet.fn(...), Other.col)
        # render in place — that's how `count = count + 1` and
        # cross-table UPDATE FROM joins work.
        #
        # Re-calling SET clobbers _table / _obj / _kwargs entirely.  There's
        # no .SET(...).SET(...) accumulation pattern by design — UPDATE has
        # a single SET clause in SQL, and merging kwargs across calls would
        # invite confusion about which call's values win.
        _check_table_source(table, "UPDATE … SET")
        _reject_aliased_dml(table, "UPDATE … SET")
        self._table = table
        self._obj = obj
        self._kwargs = kwargs
        return self

    def FROM(self, *tables: TableSource) -> UpdateBuilder:
        """`UPDATE … FROM other …` — read SET values from other tables.

        Variadic so multiple sources can be added in one call:
            .FROM(OtherA, OtherB)
        Each subsequent .FROM() append cumulates.  The join condition
        goes in the WHERE clause (PG's convention for UPDATE FROM,
        unlike SELECT's separate JOIN/ON syntax).
        """
        for t in tables:
            _check_table_source(t, "UPDATE … FROM")
        self._from_tables.extend(tables)
        return self

    def RETURNING(self, *columns: SQLRenderable) -> UpdateBuilder:  # noqa: N802
        # Non-empty: an empty RETURNING() call is meaningless and would
        # produce invalid SQL ("RETURNING ").  Reject up front.
        if not columns:
            raise ValueError("RETURNING requires at least one column")
        self._returning = columns
        return self

    def sql(self) -> tuple[str, list[Any]]:
        # Applies the same WHERE-required validation as run_update —
        # .sql() is NOT a shortcut around the safety rail.
        return Executor(self._db).render_update(self)

    def __await__(self) -> Generator[Any, None, Any]:
        return self._execute().__await__()

    async def _execute(self) -> Any:
        return await Executor(self._db).run_update(self)


class DeleteBuilder(_Builder):
    # DELETE requires an explicit WHERE clause (enforced in the executor).
    # Same rationale as UpdateBuilder: prevent DELETE FROM table mishaps.
    # Use WHERE(cygnet.all) to truncate intentionally.
    def __init__(self, db: Any) -> None:
        super().__init__(db)
        self._table: TableProxy[Any] | None = None
        self._returning: tuple[SQLRenderable, ...] | None = None
        # Other tables referenced in WHERE for cross-table deletes
        # (DELETE … USING other …).  PG's syntactic mirror of UPDATE FROM
        # — same convention: WHERE carries the join condition.
        self._using_tables: list[TableSource] = []

    def FROM(self, table: TableProxy[Any]) -> DeleteBuilder:
        """Set the DELETE target table.

        Calling FROM a second time replaces the prior target outright
        (same rationale as InsertBuilder.INTO: SQL has a single DELETE
        target slot, so clobber is the honest behaviour).
        """
        _check_table_source(table, "DELETE FROM")
        _reject_aliased_dml(table, "DELETE FROM")
        self._table = table
        return self

    def USING(self, *tables: TableSource) -> DeleteBuilder:
        """`DELETE … USING other …` — reference other tables in WHERE
        for cross-table deletes (e.g., DELETE rows from t1 that have
        no matching row in t2).  PG-specific extension; the join
        condition goes in WHERE, the same pattern UPDATE FROM uses.
        """
        for t in tables:
            _check_table_source(t, "DELETE … USING")
        self._using_tables.extend(tables)
        return self

    def WHERE(self, predicate: SQLRenderable | _All) -> DeleteBuilder:
        self._predicates.append(predicate)
        return self

    def RETURNING(self, *columns: SQLRenderable) -> DeleteBuilder:  # noqa: N802
        if not columns:
            raise ValueError("RETURNING requires at least one column")
        self._returning = columns
        return self

    def sql(self) -> tuple[str, list[Any]]:
        # Re-validates WHERE presence; mirrors render_update's contract.
        return Executor(self._db).render_delete(self)

    def __await__(self) -> Generator[Any, None, Any]:
        return self._execute().__await__()

    async def _execute(self) -> Any:
        return await Executor(self._db).run_delete(self)
