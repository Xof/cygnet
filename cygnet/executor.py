# executor.py — SQL rendering and execution engine.
#
# This module is where the abstract builder state becomes concrete SQL.
# Each public method pair (render_X / run_X) follows the same pattern:
# render builds the SQL string + params list; run calls render, executes
# the query via the db adapter, and optionally maps results back to
# dataclass instances.
#
# This is the ONLY place in Cygnet where SQL strings get assembled —
# every other layer (builders, predicates, proxies, JSON/array/FTS
# helpers, CTEs) either holds state or contributes a render_sql()
# fragment, but never produces a complete statement.  Concentrating
# string assembly here means the $N renumbering invariant, the
# clause-ordering invariant, and the DEFAULT-aware INSERT codegen
# all have exactly one place to live and be reasoned about.
#
# Row mapping (the inverse direction — rows back to objects) also lives
# here, in _map_row / _row_to_obj.  Keeping both directions in one file
# means the "column order matches meta.fields order" invariant — set up
# by the SELECT renderer and consumed by the row mapper — never has to
# cross a module boundary.
#
# The Executor is stateless (aside from the db handle) and short-lived —
# builders create one per .sql() or .__await__() call.  This keeps the
# executor free of accumulated state between queries.
#
# Rendering pipeline invariants:
#   - Params are appended to a single shared list in the exact order they
#     appear in the final SQL string.  Every render_* helper takes that
#     list and passes it down so $N numbering stays monotonic across all
#     clauses (SELECT list, JOIN ON, WHERE, GROUP BY, ORDER BY, SET, etc.).
#     Any caller that constructs placeholders independently (see INSERT's
#     `$1..$N` loop) must do so *after* every param has been appended, or
#     the numbering will skew.
#   - Clause emission order is fixed by the _render_* methods, not by the
#     builder's method-call order.  Builders just collect state.
#   - Transactions are NOT this module's concern.  cygnet.transaction() in
#     __init__.py flips db._in_transaction and issues BEGIN/SAVEPOINT; the
#     Executor only calls db.execute / db.execute_one.  The one exception
#     is stream_select: PG portal cursors require a transaction, but the
#     check is on the *adapter's* side (psycopg raises), not here —
#     stream_select just passes the call through.

from __future__ import annotations

import weakref
from typing import Any

from .annotations import DBKey
from .predicate import _All
from .proxy import TableProxy


class Executor:
    # Process-wide cache of "which columns on table T carry a non-NULL DEFAULT
    # clause?".  Outer mapping is a WeakKeyDictionary keyed by the db
    # adapter instance — when the adapter is GC'd the entry evicts
    # automatically, avoiding the stale-entry hazard of an id()-based key
    # (memory addresses get reused).  Inner dict maps table_name -> set
    # of DEFAULT-having column names.
    #
    # The cache lives at class level (not instance level) because Executor
    # is short-lived — one per .sql() / .__await__() call — and we want
    # the introspection cost to amortise across calls.  The answer is
    # stable for the lifetime of the schema, so we cache it on first
    # INSERT and reuse on every subsequent INSERT against the same
    # connection + table.
    _defaults_cache: weakref.WeakKeyDictionary[Any, dict[str, frozenset[str]]] = (
        weakref.WeakKeyDictionary()
    )

    def __init__(self, db: Any) -> None:
        self._db = db

    async def _get_defaulted_columns(self, table_name: str) -> frozenset[str]:
        """Return the cached set of columns on ``table_name`` that carry a
        DEFAULT clause in the schema.  Returns an empty frozenset for db
        adapters that don't implement the optional ``column_defaults``
        protocol method (FakeDB and any consumer-supplied adapter that
        doesn't introspect).

        The optional-method probe (hasattr) is the opt-in mechanism for
        DEFAULT-aware INSERT codegen.  Adapters that implement
        column_defaults get the new behaviour (None-valued fields with a
        DEFAULT are omitted from INSERT so the DEFAULT fires, then
        repopulated from RETURNING); adapters that don't get the
        historical behaviour (every field emitted, NULL included).
        Crucially this keeps tests that use FakeDB working unchanged.
        """
        if not hasattr(self._db, "column_defaults"):
            return frozenset()
        # WeakKeyDictionary.get returns None for both "key missing" and
        # "key was GC'd"; we treat them the same — re-introspect and
        # repopulate.  Inner dict is created lazily on first miss so we
        # don't waste an empty dict for adapters that never INSERT.
        per_table = self._defaults_cache.get(self._db)
        if per_table is not None:
            cached = per_table.get(table_name)
            if cached is not None:
                return cached
        result = frozenset(await self._db.column_defaults(table_name))
        if per_table is None:
            per_table = {}
            try:
                self._defaults_cache[self._db] = per_table
            except TypeError:
                # The db adapter doesn't support weak refs (rare but
                # legal for custom adapters).  Skip caching for this
                # adapter — every INSERT will re-introspect, which is
                # the correctness-preserving fallback.  Return the
                # freshly-computed result so the call still works.
                return result
        per_table[table_name] = result
        return result

    # ── Predicate helpers ─────────────────────────────────────────────────────

    def _check_predicates(self, predicates: list[Any], verb: str) -> list[Any]:
        """Validate predicates for any verb.

        Returns the list of real predicates to render, or [] if cygnet.all
        was used or no predicates at all (SELECT only).  Raises ValueError
        if cygnet.all is mixed with real predicates, or if UPDATE/DELETE is
        missing a WHERE clause entirely.

        SELECT differs from UPDATE/DELETE in one place: an empty predicate
        list is allowed (reading all rows is normal).  Everywhere else the
        rules are the same — including the prohibition on mixing
        cygnet.all with real predicates, which used to be silently dropped
        in SELECT and is now consistently rejected.
        """
        # The returned list is the exact set of predicates the caller should
        # render.  Three return shapes the callers branch on:
        #   - []  → no WHERE clause should be emitted (either user wrote
        #           WHERE(cygnet.all) explicitly, or this is a SELECT with
        #           no predicates at all)
        #   - non-empty → emit WHERE with each predicate ANDed
        # The verb argument is used only in the error message; callers
        # MUST still gate WHERE emission on the returned list being
        # non-empty (see how _render_select, _render_update,
        # _render_delete all use `if checked:`).
        has_all = any(isinstance(p, _All) for p in predicates)
        real = [p for p in predicates if not isinstance(p, _All)]
        if not predicates:
            if verb == "SELECT":
                return []
            raise ValueError(
                f"{verb} requires a WHERE clause; "
                f"use WHERE(cygnet.all) to affect all rows"
            )
        if has_all and real:
            raise ValueError("cygnet.all cannot be combined with other predicates")
        if has_all:
            return []
        return real

    def _render_where(self, predicates: list[Any], params: list[Any]) -> str:
        """Render a WHERE clause from a list of renderable predicates.

        Multiple predicates (from chained .WHERE() calls) are ANDed together.
        Each predicate is wrapped in parens so compound predicates nest correctly:
        WHERE (a = $1) AND ((b > $2) OR (c < $3))
        """
        parts: list[str] = []
        for p in predicates:
            parts.append(f"({p.render_sql(params)})")
        return " AND ".join(parts)

    # ── SELECT ────────────────────────────────────────────────────────────────

    def render_select(self, b: Any) -> tuple[str, list[Any]]:
        # Public entry: own the params list here so the caller never has to
        # think about $N numbering.  _render_select mutates this list as
        # it walks the builder; on return its length == number of $N
        # placeholders in the SQL string.
        params: list[Any] = []
        sql = self._render_select(b, params)
        return sql, params

    async def run_select(self, b: Any) -> list[Any]:
        # No transaction handling here: SELECT is read-only and the adapter
        # decides whether it needs to be inside a transaction.  Contrast
        # with stream_select, which has a documented transaction
        # requirement on the PG-portal side.
        sql, params = self.render_select(b)
        rows = await self._db.execute(sql, params)
        return self._map_select(b, rows)

    def _render_select(self, b: Any, params: list[Any]) -> str:
        # FROM is normally required, but a SELECT with explicit columns
        # and no FROM is valid PG (`SELECT 1`, `SELECT now()`, recursive
        # CTE anchors, …).  The error path covers the more common
        # mistake — bare `await SELECT(db)` with neither FROM nor
        # columns — which would otherwise blow up later with a confusing
        # NoneType._meta failure.
        has_from = b._table is not None
        if not has_from and not b._columns:
            raise ValueError(
                "SELECT requires FROM or explicit columns; neither was supplied"
            )

        # WITH prefix: each CTE renders as `name AS (inner_sql)`, joined
        # by ", ".  Inner SQL is rendered through the same _render_select
        # path so $N numbering stays monotonic across the whole query —
        # CTE params come first, then the outer SELECT's params.
        # Recursive CTEs render as `name AS (anchor UNION ALL step)`; if
        # ANY of the WITH-list entries is recursive, PG requires the
        # WITH RECURSIVE keyword on the whole list (not per-CTE).
        #
        # Anchor and step are each rendered with the SAME params list,
        # in declaration order: anchor params come before step params.
        # This is a self-recursive call into _render_select, which is
        # safe — the CTE bodies are themselves SelectBuilders with no
        # shared state to corrupt.  Nested CTEs (CTE referencing another
        # CTE) work because the inner SELECTs reference CTE names as
        # plain table identifiers; the WITH-list itself is flat at
        # render time.
        cte_prefix = ""
        if b._ctes:
            from .cte import RecursiveCTE

            parts = []
            any_recursive = False
            for c in b._ctes:
                if isinstance(c, RecursiveCTE):
                    any_recursive = True
                    if c.anchor is None or c.step is None:
                        raise ValueError(
                            f"recursive CTE {c._name!r} is missing its anchor or step"
                        )
                    anchor_sql = self._render_select(c.anchor, params)
                    step_sql = self._render_select(c.step, params)
                    cols = ", ".join(c._cols)
                    parts.append(
                        f"{c._name}({cols}) AS ({anchor_sql} UNION ALL {step_sql})"
                    )
                else:
                    inner_sql = self._render_select(c._builder, params)
                    parts.append(f"{c._name} AS ({inner_sql})")
            with_kw = "WITH RECURSIVE" if any_recursive else "WITH"
            cte_prefix = f"{with_kw} {', '.join(parts)} "

        if b._columns:
            # Explicit column selection: render each SQLRenderable.
            cols = ", ".join(c.render_sql(params) for c in b._columns)
        else:
            # No columns specified: select all fields from the primary table
            # and all joined tables.  We use explicit column names (not
            # "table.*") so the result order matches meta.fields regardless
            # of the physical column order (attnum) in PostgreSQL.
            # has_from must be True here (caught above when both are missing).
            #
            # This is the load-bearing invariant for the row mapper:
            # _map_row / _row_to_obj rely on result[i] corresponding to
            # meta.fields[i].  Emitting columns in meta.fields order here
            # is what makes that work.  Joins extend the same convention:
            # left table's fields first (length = len(left.fields)), then
            # each right table's fields (length = len(jt.fields)) in
            # b._joins order — which is exactly what _map_row's offset
            # arithmetic assumes.
            assert b._table is not None
            table = b._table._meta
            from_name = b._table._sql_name
            cols = ", ".join(f"{from_name}.{f.column_name}" for f in table.fields)
            for _kind, jt, _on in b._joins:
                cols += ", " + ", ".join(
                    f"{jt._sql_name}.{f.column_name}" for f in jt._meta.fields
                )

        # DISTINCT goes immediately after SELECT, before the column list.
        # Three modes: plain DISTINCT, DISTINCT ON (cols), or neither.
        # The builder enforces mutual exclusion of the first two so we
        # don't have to here.
        if b._distinct:
            distinct = "DISTINCT "
        elif b._distinct_on:
            on_cols = ", ".join(c.render_sql(params) for c in b._distinct_on)
            distinct = f"DISTINCT ON ({on_cols}) "
        else:
            distinct = ""
        sql = f"{cte_prefix}SELECT {distinct}{cols}"

        # FROM / JOIN only emit when the user provided a table.  A no-FROM
        # SELECT with JOINs is meaningless; raise rather than emit broken
        # SQL.
        if has_from:
            assert b._table is not None
            from_clause = b._table._meta.table_name
            if b._table._alias:
                from_clause += f" AS {b._table._alias}"
            sql += f" FROM {from_clause}"

            from .cte import Lateral

            # Join order in the emitted SQL matches b._joins iteration
            # order, which matches the user's chained-method order on the
            # builder.  Mixing LATERAL with normal JOINs in one query is
            # legal — the isinstance(jt, Lateral) branch handles each
            # entry independently.  Both branches append on_pred params
            # AFTER any subquery-internal params for LATERAL, so $N
            # numbering keeps marching forward correctly.
            for kind, jt, on_pred in b._joins:
                if isinstance(jt, Lateral):
                    # JOIN LATERAL (inner_sql) alias ON cond.  The
                    # inner builder is rendered into the same params
                    # list (its $N indices come BEFORE the ON's), so
                    # numbering stays monotonic across the whole
                    # statement.
                    inner_sql = self._render_select(jt._builder, params)
                    on_sql = on_pred.render_sql(params)
                    sql += f" {kind} JOIN LATERAL ({inner_sql}) {jt._name} ON {on_sql}"
                else:
                    on_sql = on_pred.render_sql(params)
                    join_clause = jt._meta.table_name
                    if jt._alias:
                        join_clause += f" AS {jt._alias}"
                    sql += f" {kind} JOIN {join_clause} ON {on_sql}"
        elif b._joins:
            raise ValueError("JOIN requires FROM")

        # SELECT allows missing WHERE (no safety rail) and explicit
        # WHERE(cygnet.all), but rejects cygnet.all mixed with real
        # predicates — same rule UPDATE/DELETE enforce, just centralized.
        checked = self._check_predicates(b._predicates, "SELECT")
        if checked:
            where = self._render_where(checked, params)
            sql += f" WHERE {where}"

        if b._group:
            group_cols = ", ".join(c.render_sql(params) for c in b._group)
            sql += f" GROUP BY {group_cols}"

        # HAVING follows GROUP BY (PG syntax order).  Multiple .HAVING()
        # calls AND together, mirroring how chained .WHERE() calls work.
        if b._having:
            having_parts = [f"({h.render_sql(params)})" for h in b._having]
            sql += f" HAVING {' AND '.join(having_parts)}"

        # Set operations (UNION / INTERSECT / EXCEPT) emit after the
        # left SELECT's body and before any compound-level ORDER BY /
        # LIMIT / OFFSET.  Each operand is rendered into the same params
        # list so $N numbering stays monotonic across the chain.
        for op_kw, other in b._set_ops:
            other_sql = self._render_select(other, params)
            sql += f" {op_kw} {other_sql}"

        if b._order:
            order_parts: list[str] = []
            for c, d in b._order:
                rendered = c.render_sql(params)
                # Append ASC/DESC unless the renderable opts out.  Literal
                # opts out because its SQL may already include a direction
                # (e.g., cygnet.lit("created_at DESC")); ColumnProxy, op()
                # results, and SuffixOp all want the suffix.  The previous
                # rule (only ColumnProxy gets the suffix) silently dropped
                # DESC for op() expressions in ORDER BY.
                if not getattr(c, "_renders_own_direction", False):
                    rendered += f" {d}"
                order_parts.append(rendered)
            sql += f" ORDER BY {', '.join(order_parts)}"

        if b._limit is not None:
            sql += f" LIMIT {b._limit}"

        # OFFSET follows LIMIT in PG syntax.  Both LIMIT and OFFSET are
        # interpolated as integers — never user-controlled — and the
        # builder methods reject negatives, so this is safe to inline.
        if b._offset is not None:
            sql += f" OFFSET {b._offset}"

        # Row-level locking clause appears last in PG's SELECT pipeline
        # (after ORDER BY / LIMIT / OFFSET).  _LockClause renders the
        # full `FOR UPDATE [OF …] [NOWAIT|SKIP LOCKED]` fragment; the
        # params list is threaded for protocol uniformity even though
        # locking takes no parameters.
        if b._lock is not None:
            sql += " " + b._lock.render_sql(params)

        return sql

    def _map_row(self, b: Any, row: Any) -> Any:
        """Map a single row tuple to the appropriate result shape.

        Three modes, determined by query shape — same precedence as the
        en-bloc _map_select: explicit columns take precedence over joins,
        joins over the simple case.  Factored out so streaming
        (stream_select) can reuse the exact same mapping logic per row.
        """
        # 1. Explicit columns → raw tuple (no object mapping).
        # We can't reconstruct dataclasses here because the user may have
        # projected only some columns, computed expressions, or columns
        # from multiple tables — there's no general "fields N..M map to
        # dataclass X" rule that holds.  Caller deals with the tuple
        # shape they asked for.
        if b._columns:
            return tuple(row)

        # 2. JOINs → (left_obj, right_obj, ...).  Column slicing relies
        #    on the FieldMeta.fields list length matching the number of
        #    columns returned for that table in the SELECT list above —
        #    only holds because _render_select emits columns in
        #    meta.fields order for every joined table.
        if b._joins:
            left_meta = b._table._meta
            left_n = len(left_meta.fields)
            # Each iteration produces a fresh dataclass instance via
            # _row_to_obj.  No instance reuse across rows — distinct
            # rows produce distinct objects even when the row data
            # would compare equal.  The result list is never
            # de-duplicated; that's the caller's responsibility if
            # JOINs produce duplicate left-side rows.
            left_obj = self._row_to_obj(left_meta, row[:left_n])
            offset = left_n
            right_objs: list[Any] = []
            for kind, jt, _on in b._joins:
                n = len(jt._meta.fields)
                chunk = row[offset : offset + n]
                # LEFT JOIN miss-detection: prefer the right-side PK
                # column.  A real PG row never has a NULL primary key,
                # so PK=None is an unambiguous signal that LEFT JOIN
                # found no match.  Fall back to all-NULL only when the
                # right-side has no PK at all.
                is_miss = False
                if kind == "LEFT":
                    if jt._meta.pk is not None:
                        pk_idx = jt._meta.fields.index(jt._meta.pk)
                        is_miss = chunk[pk_idx] is None
                    else:
                        is_miss = all(v is None for v in chunk)
                if is_miss:
                    right_objs.append(None)
                else:
                    right_objs.append(self._row_to_obj(jt._meta, chunk))
                offset += n
            return (left_obj, *right_objs)

        # 3. Simple query → dataclass instance.
        meta = b._table._meta
        return self._row_to_obj(meta, row)

    def _map_select(self, b: Any, rows: list[Any]) -> list[Any]:
        return [self._map_row(b, row) for row in rows]

    async def stream_select(self, b: Any) -> Any:
        """Async generator yielding mapped objects one at a time.

        Avoids materialising the full result set in memory — useful for
        analytics-shaped queries that return many rows.  The underlying
        db adapter must implement `stream(sql, params) -> AsyncIterator[tuple]`;
        psycopg's portal-based cursor.stream() is the canonical
        implementation.  PG requires a transaction (or autocommit off)
        for portal cursors, so wrap the streaming code in
        `async with cygnet.transaction(db)` for typical use.
        """
        # The executor doesn't check db._in_transaction here on purpose:
        # the portal-vs-transaction requirement is a PG-side rule that
        # the adapter is responsible for surfacing (psycopg raises a
        # clear error if cursor.stream() is invoked in autocommit mode).
        # Checking it here would entangle the executor with adapter-
        # specific behaviour and would also reject legitimate use cases
        # like an adapter that streams via a server-side cursor in
        # autocommit.
        if not hasattr(self._db, "stream"):
            raise TypeError(
                f"{type(self._db).__name__} does not implement stream(); "
                "the db adapter must provide an async stream(sql, params) "
                "method to support streaming SELECTs"
            )
        sql, params = self.render_select(b)
        # Yield-per-row; mapping is done lazily so the row buffer never
        # has to materialise the whole result set.  Each yielded object
        # is a fresh dataclass instance (or tuple, per _map_row's mode).
        async for row in self._db.stream(sql, params):
            yield self._map_row(b, row)

    # ── INSERT ────────────────────────────────────────────────────────────────

    def render_insert(self, b: Any) -> tuple[str, list[Any]]:
        # Public render entry point used by InsertBuilder.sql() — preserves
        # the historical 2-tuple return type for backward compatibility.
        # Delegates to _render_insert which carries an extra
        # ``defaulted_columns`` parameter for the async run_insert path.
        # .sql() can't introspect (it's sync; introspection requires a db
        # round-trip), so it always passes an empty set — meaning a
        # caller inspecting the rendered SQL sees every field emitted,
        # exactly as before this fix.  This is correct for the inspection
        # use case: callers want to see what Cygnet WOULD have rendered
        # absent the DEFAULT-aware column omission.  run_insert sees the
        # real SQL via _render_insert_with_defaults.
        sql, params, _omitted = self._render_insert(b, frozenset())
        return sql, params

    def _render_insert(
        self, b: Any, defaulted_columns: frozenset[str]
    ) -> tuple[str, list[Any], list[Any]]:
        """Internal INSERT render that supports the DEFAULT-aware column-omit
        path.  Returns ``(sql, params, omitted_default_fields)`` — the third
        element is the list of FieldMeta entries that were omitted because
        the in-memory value was None and the schema column carries a
        DEFAULT.  ``run_insert`` extends the RETURNING clause to fetch
        those values and patches them back onto the in-memory object so
        the application's view matches what PG stored.
        """
        # Catch the missing-INTO mistake before it surfaces as a
        # confusing AttributeError on b._table._meta.  Same pattern
        # as render_select's missing-FROM guard.
        if b._table is None:
            raise ValueError(
                "INSERT requires INTO(table) before VALUES / BULK_VALUES / SELECT"
            )
        meta = b._table._meta
        # ON_CONFLICT is currently scoped to the single-row VALUES path.
        # BULK_VALUES + ON_CONFLICT and INSERT…SELECT + ON_CONFLICT
        # would need extra row-counting / PK-population care to handle
        # skipped rows; raise upfront rather than emit broken SQL.
        if b._on_conflict_action is not None and (
            b._select_source is not None or b._bulk_objs is not None
        ):
            raise ValueError(
                "ON_CONFLICT is not yet supported with BULK_VALUES or "
                "INSERT…SELECT; use single-row VALUES(obj) for now"
            )
        # INSERT … SELECT: target columns followed by an inner SELECT in
        # place of VALUES.  Routed first so the SELECT path doesn't fall
        # into _extract_insert_fields' kwargs validation.  The SELECT
        # source has no in-memory dataclass values to omit — every column
        # in the target list is supplied by the inner query — so the
        # DEFAULT-aware path doesn't apply here.
        if b._select_source is not None:
            sel_sql, sel_params = self._render_insert_select(b, meta)
            return sel_sql, sel_params, []
        # Bulk path: many objects -> a single VALUES (…), (…), … clause.
        # Routed before the single-row path so type checks and validation
        # don't double-fire.  Bulk DEFAULT-omission is not yet supported:
        # PG requires every row in a bulk VALUES to use the same column
        # list, and per-row DEFAULT-vs-explicit-NULL decisions would
        # break that invariant.  Bulk inserts therefore fall through to
        # the historical path (every field emitted), which is at most
        # losing a bit of DEFAULT-firing efficiency, not correctness.
        if b._bulk_objs is not None:
            bulk_sql, bulk_params = self._render_bulk_insert(b, meta)
            return bulk_sql, bulk_params, []

        params: list[Any] = []
        obj, kwargs = b._obj, b._kwargs

        # Object takes precedence over kwargs — if both are provided,
        # all fields are extracted from the object and kwargs are ignored.
        # This is intentional: VALUES(obj, key=val) would otherwise have
        # ambiguous semantics ("override one field on the obj"?  "merge
        # both"?), so we collapse to the simpler rule and document it.
        if obj is not None:
            # Type check: catch wrong-model VALUES() before getattr blows up
            # on a missing attribute.  Mirrors the same check in render_update.
            if not isinstance(obj, meta.cls):
                raise TypeError(
                    f"INSERT into {meta.cls.__name__} expects a "
                    f"{meta.cls.__name__} instance, got "
                    f"{type(obj).__name__}: {obj!r}"
                )
            kwargs = {f.attr_name: getattr(obj, f.attr_name) for f in meta.fields}

        columns, _values, omitted = self._extract_insert_fields(
            meta, kwargs, params, defaulted_columns
        )
        col_sql = ", ".join(columns)
        # Placeholders are $1..$N in the same order as `columns`, because
        # _extract_insert_fields appends to params in that same order.
        # INSERT never shares params with other clauses, so starting at $1
        # is always correct here.
        val_sql = ", ".join(f"${i + 1}" for i in range(len(columns)))
        sql = f"INSERT INTO {meta.table_name} ({col_sql}) VALUES ({val_sql})"

        # ON CONFLICT clause emits between VALUES and RETURNING.  The
        # action's own params (DO UPDATE SET kwarg values) are appended
        # after VALUES's params, keeping $N numbering monotonic.
        if b._on_conflict_action is not None:
            sql += " " + self._render_on_conflict(b, meta, params)

        # RETURNING: PK if DBKey, plus any columns we omitted in favour of
        # the schema DEFAULT so the caller can patch the in-memory object
        # with the DB-generated values.  Position matters — the run_insert
        # consumer unpacks the row positionally: PK first (when DBKey),
        # then omitted-default fields in meta.fields order (preserved by
        # _extract_insert_fields' iteration order).
        returning_cols: list[str] = []
        if meta.pk and meta.pk.primary_key == DBKey:
            returning_cols.append(meta.pk.column_name)
        returning_cols.extend(f.column_name for f in omitted)
        if returning_cols:
            sql += f" RETURNING {', '.join(returning_cols)}"

        return sql, params, omitted

    def _render_on_conflict(self, b: Any, meta: Any, params: list[Any]) -> str:
        """Render the `ON CONFLICT [target] [action]` clause.

        Target: either the explicit column list, the named constraint,
        or — for ON_CONFLICT_DO_NOTHING() with no preceding target —
        nothing at all.

        Action: `DO NOTHING` (always valid), or `DO UPDATE SET …`.  PG
        requires a target for DO UPDATE; the builder validates that
        invariant when DO_UPDATE/DO_UPDATE_FROM_EXCLUDED is called, but
        the no-target shorthand path (ON_CONFLICT_DO_NOTHING) skips
        target setup entirely so we don't need to re-check here.
        """
        from .proxy import ColumnProxy

        parts = ["ON CONFLICT"]
        # Target.
        if b._on_conflict_target is not None:
            target_cols: list[str] = []
            for c in b._on_conflict_target:
                if isinstance(c, ColumnProxy):
                    target_cols.append(c._field.column_name)
                else:
                    raise TypeError(
                        f"ON_CONFLICT target must be a ColumnProxy, "
                        f"got {type(c).__name__}"
                    )
            parts.append(f"({', '.join(target_cols)})")
        elif b._on_conflict_constraint is not None:
            parts.append(f"ON CONSTRAINT {b._on_conflict_constraint}")

        # Action.
        if b._on_conflict_action == "nothing":
            parts.append("DO NOTHING")
        elif b._on_conflict_action == "update":
            if b._on_conflict_excluded is not None:
                # SET col = EXCLUDED.col for each column
                set_parts = []
                for c in b._on_conflict_excluded:
                    if not isinstance(c, ColumnProxy):
                        raise TypeError(
                            f"DO_UPDATE_FROM_EXCLUDED target must be "
                            f"ColumnProxy, got {type(c).__name__}"
                        )
                    name = c._field.column_name
                    set_parts.append(f"{name} = EXCLUDED.{name}")
                parts.append(f"DO UPDATE SET {', '.join(set_parts)}")
            else:
                # DO_UPDATE(**kwargs): SET col = $N, with field-name
                # validation against the target table.
                fields = b._on_conflict_set or {}
                known = {f.attr_name for f in meta.fields}
                unknown = set(fields) - known
                if unknown:
                    raise ValueError(
                        f"Unknown field(s) for {meta.cls.__name__}: {sorted(unknown)}"
                    )
                set_parts = []
                # Iterate meta.fields for stable ordering (same convention
                # as render_update).
                for f in meta.fields:
                    if f.attr_name in fields:
                        params.append(fields[f.attr_name])
                        set_parts.append(f"{f.column_name} = ${len(params)}")
                parts.append(f"DO UPDATE SET {', '.join(set_parts)}")
        return " ".join(parts)

    def _render_insert_select(self, b: Any, meta: Any) -> tuple[str, list[Any]]:
        """Render `INSERT INTO target (cols) SELECT ...`.

        Target columns are either user-supplied (b._select_columns) or
        inferred from the source SelectBuilder's explicit ColumnProxy
        list.  Validates that every named column actually exists on the
        target so a typo doesn't reach PG as a confusing late error.
        """
        from .proxy import ColumnProxy

        source = b._select_source
        if b._select_columns is not None:
            columns = list(b._select_columns)
        else:
            if not source._columns:
                raise ValueError(
                    "INSERT…SELECT column inference requires the source "
                    "SELECT to use explicit ColumnProxy columns; pass "
                    "columns=[...] to .SELECT() if the source projects "
                    "opaque expressions"
                )
            columns = []
            for c in source._columns:
                if isinstance(c, ColumnProxy):
                    columns.append(c._field.column_name)
                else:
                    raise ValueError(
                        f"INSERT…SELECT can't infer a target column from "
                        f"{c!r}; pass columns=[...] explicitly"
                    )

        known = {f.column_name for f in meta.fields}
        unknown = set(columns) - known
        if unknown:
            raise ValueError(
                f"Unknown columns for {meta.cls.__name__}: {sorted(unknown)}"
            )

        # Render the source SELECT into the same params list, then prefix
        # with INSERT INTO target (cols).  The inner SELECT's $N indices
        # start at 1; that's still correct because the INSERT clause has
        # no params of its own.
        params: list[Any] = []
        inner_sql = self._render_select(source, params)
        col_sql = ", ".join(columns)
        sql = f"INSERT INTO {meta.table_name} ({col_sql}) {inner_sql}"
        if meta.pk and meta.pk.primary_key == DBKey:
            sql += f" RETURNING {meta.pk.column_name}"
        return sql, params

    def _render_bulk_insert(self, b: Any, meta: Any) -> tuple[str, list[Any]]:
        """Render a multi-row VALUES (…), (…), … INSERT.

        Column shape is determined by the first object: whichever fields
        appear (with non-None DBKey) become the column list, and every
        subsequent row must populate the same set.  Per-row params are
        appended in column order; placeholder numbering is monotonic
        across all rows so $N indices stay aligned with the params list.
        """
        objs = b._bulk_objs
        first = objs[0]
        # Type-check up front rather than at each setattr call.
        for o in objs:
            if not isinstance(o, meta.cls):
                raise TypeError(
                    f"BULK_VALUES expected {meta.cls.__name__}, got {type(o).__name__}"
                )

        # Determine columns from the first object: same rules as single-row
        # INSERT — DBKey=None excluded, AppKey=None raises.  No
        # defaulted_columns passed here: bulk INSERT requires every row
        # to use the same column list, so per-row DEFAULT-omit would
        # break that invariant.  (See _render_insert's bulk branch.)
        first_kwargs = {f.attr_name: getattr(first, f.attr_name) for f in meta.fields}
        params: list[Any] = []
        columns, _, _ = self._extract_insert_fields(meta, first_kwargs, params)

        # Reuse the column list for subsequent rows; collect each row's
        # values into params separately so we don't re-render the column
        # sentinel logic.  AppKey=None still raises here because
        # _extract_insert_fields runs per row.
        for o in objs[1:]:
            row_kwargs = {f.attr_name: getattr(o, f.attr_name) for f in meta.fields}
            row_cols, _, _ = self._extract_insert_fields(meta, row_kwargs, params)
            if row_cols != columns:
                raise ValueError(
                    "BULK_VALUES requires consistent column shape across rows; "
                    f"first row had {columns}, this row has {row_cols}"
                )

        # Render N row-tuples with $1..$M, $M+1..$2M, …  len(params) is the
        # total count after all rows have been appended; columns-per-row is
        # len(columns); rows is len(objs).
        per_row = len(columns)
        row_tuples = []
        for i in range(len(objs)):
            start = i * per_row
            row_tuples.append(
                "(" + ", ".join(f"${start + j + 1}" for j in range(per_row)) + ")"
            )
        col_sql = ", ".join(columns)
        sql = (
            f"INSERT INTO {meta.table_name} ({col_sql}) VALUES {', '.join(row_tuples)}"
        )
        if meta.pk and meta.pk.primary_key == DBKey:
            sql += f" RETURNING {meta.pk.column_name}"
        return sql, params

    async def run_insert(self, b: Any) -> Any:
        # Fetch DEFAULT-aware column metadata before rendering.  Only
        # applies to the single-row VALUES(obj) path: BULK_VALUES and
        # INSERT…SELECT bypass the DEFAULT-omit logic (see
        # _render_insert's bulk / select branches for why), so we can
        # skip the introspection round-trip entirely for those.
        # b._table can be None here (the missing-INTO mistake); defer the
        # _meta dereference until after _render_insert has the chance to
        # raise its more-informative ValueError.
        defaulted: frozenset[str] = frozenset()
        if b._table is not None and b._select_source is None and b._bulk_objs is None:
            defaulted = await self._get_defaulted_columns(b._table._meta.table_name)

        sql, params, omitted = self._render_insert(b, defaulted)
        meta = b._table._meta

        # INSERT … SELECT: no input objects to mutate, but for DBKey
        # targets we still want to surface the generated PKs so callers
        # can chain them into follow-up queries.
        if b._select_source is not None:
            if meta.pk and meta.pk.primary_key == DBKey:
                rows = await self._db.execute(sql, params)
                return [row[0] for row in rows]
            await self._db.execute(sql, params)
            return None

        # Bulk path: returns N rows for DBKey models, populates each obj's
        # PK in input order, returns the list of generated keys.  For
        # AppKey models, no RETURNING — just execute.
        if b._bulk_objs is not None:
            if meta.pk and meta.pk.primary_key == DBKey:
                rows = await self._db.execute(sql, params)
                if len(rows) != len(b._bulk_objs):
                    raise RuntimeError(
                        f"BULK_VALUES expected {len(b._bulk_objs)} RETURNING "
                        f"rows for {meta.cls.__name__}, got {len(rows)}"
                    )
                for o, row in zip(b._bulk_objs, rows, strict=True):
                    setattr(o, meta.pk.attr_name, row[0])
                return [row[0] for row in rows]
            await self._db.execute(sql, params)
            return None

        # Single-row path.  RETURNING shape is [pk?, *omitted_default_cols] —
        # so when there's a DBKey we always have at least the PK to
        # unpack, and when omitted is non-empty we additionally unpack
        # the DEFAULT-fired values back onto the in-memory object.  For
        # AppKey models with no omitted defaults, RETURNING is absent
        # and we fall through to the bare execute branch at the bottom.
        if meta.pk and meta.pk.primary_key == DBKey:
            # execute_one because RETURNING produces exactly one row.
            row = await self._db.execute_one(sql, params)
            if row is None:
                # Without ON CONFLICT, a None RETURNING is always a
                # problem — driver bug or row not inserted, and the
                # caller can't distinguish those cases from a
                # legitimately-NULL PK (PG never produces one).  With
                # ON CONFLICT DO NOTHING, an empty RETURNING is the
                # *normal* outcome of a skipped conflict, so we let
                # None bubble up rather than raising.
                if b._on_conflict_action is not None:
                    return None
                raise RuntimeError(
                    f"INSERT...RETURNING produced no row for "
                    f"{meta.cls.__name__} — driver bug or row not inserted"
                )
            # Mutate the original object in-place to populate its PK
            # and any DEFAULT-fired column values.  Position 0 is always
            # the PK (DBKey branch); positions 1..N correspond to the
            # omitted_default_fields in declaration order.  Without
            # b._obj (i.e. kwargs-only INSERT) we have nothing to mutate,
            # so the omitted-field values are simply discarded — the
            # PK is still returned to the caller as the sole return
            # value, matching the historical contract.
            if b._obj is not None:
                setattr(b._obj, meta.pk.attr_name, row[0])
                for i, f in enumerate(omitted, start=1):
                    setattr(b._obj, f.attr_name, row[i])
            return row[0]

        # AppKey path with omitted-default columns: still need to fetch
        # them via execute_one to patch the in-memory object.  Without
        # omitted defaults this collapses to the historical bare execute.
        if omitted and b._obj is not None:
            row = await self._db.execute_one(sql, params)
            if row is not None:
                for i, f in enumerate(omitted):
                    setattr(b._obj, f.attr_name, row[i])
            return None

        # Final fallthrough: AppKey + no obj, or AppKey + obj + no omitted
        # defaults.  No RETURNING was emitted upstream so no row to read;
        # we just dispatch the INSERT and return None.  Matches the
        # historical pre-DEFAULT-aware contract for AppKey inserts.
        await self._db.execute(sql, params)
        return None

    def _extract_insert_fields(
        self,
        meta: Any,
        kwargs: dict[str, Any],
        params: list[Any],
        defaulted_columns: frozenset[str] = frozenset(),
    ) -> tuple[list[str], list[Any], list[Any]]:
        """Build column and value lists for INSERT, skipping DBKey=None fields
        and (when ``defaulted_columns`` is supplied) any non-PK fields whose
        in-memory value is None and whose schema column carries a DEFAULT
        clause.

        Returns ``(columns, values, omitted_default_fields)`` where
        ``omitted_default_fields`` is the list of FieldMeta objects that
        were skipped because of a DEFAULT (NOT including the PK; the PK
        skip is unconditional and already handled by the RETURNING-id
        machinery).  The caller uses ``omitted_default_fields`` to extend
        the RETURNING clause and to patch the populated values back onto
        the in-memory object.

        Why the schema-side DEFAULT skip lives here rather than at the
        builder layer: PostgreSQL's ``DEFAULT`` clause only fires when the
        column is *absent* from the column list — sending an explicit
        ``NULL`` parameter suppresses the DEFAULT.  Before this fix, every
        non-DBKey-None field was always emitted, so columns like
        ``moved_at TIMESTAMPTZ DEFAULT now()`` always landed as NULL in
        the DB, with the schema default never firing.  Omitting the
        column when (field is None) AND (schema has a DEFAULT) is the
        narrowest change that makes the DEFAULT useful again while
        leaving columns without DEFAULTs unchanged (a NULL there is
        usually intentional — the field documents "this is nullable").
        """
        # Reject unknown kwargs upfront so a typo (.VALUES(nmae="x")) raises
        # immediately rather than silently being dropped by the meta.fields
        # iteration below.  When kwargs is built from an obj (render_insert
        # rebuilds it from the dataclass), this set is always empty.
        known_attrs = {f.attr_name for f in meta.fields}
        unknown = set(kwargs) - known_attrs
        if unknown:
            raise ValueError(
                f"Unknown field(s) for {meta.cls.__name__}: {sorted(unknown)}"
            )
        # Three parallel accumulators grow in lockstep:
        #   columns[i]  → column_name for the i'th emitted slot
        #   values[i]   → the in-memory value being inserted
        #   params      → SAME values (caller-supplied list, mutated)
        # The caller derives $N placeholders from len(columns), so columns
        # and params MUST stay aligned; appending to params without
        # appending to columns (or vice versa) would skew the numbering.
        # `values` is kept around mainly for the historical contract /
        # debug-friendly return shape; it duplicates `params`.
        columns: list[str] = []
        values: list[Any] = []
        omitted_default_fields: list[Any] = []
        # Iterate meta.fields (not kwargs) so column order in the emitted
        # INSERT matches the dataclass declaration order.  This also makes
        # the placeholder indices generated by the caller valid: columns,
        # values, and appended params grow in lockstep.
        for f in meta.fields:
            val = kwargs.get(f.attr_name)
            # DBKey with None → omit from INSERT; the DB generates the value.
            # Note: kwargs.get returns None both for "key present with None"
            # and "key missing entirely"; both cases are treated identically
            # here, which is intentional for the DBKey path.
            if f.primary_key == DBKey and val is None:
                continue
            # AppKey with None → application error; can't proceed.
            # Checking `primary_key is not None` rather than `== AppKey`
            # covers any future non-DBKey PK marker as well.
            if f.primary_key is not None and val is None:
                raise ValueError(
                    f"{meta.cls.__name__}.{f.attr_name} is AppKey but value "
                    f"is None — the application must supply this key"
                )
            # Non-PK field with None AND a schema DEFAULT → omit so the
            # DEFAULT fires.  Caller will add this to RETURNING and patch
            # the populated value back onto the in-memory object so the
            # application's view matches the DB row.  We deliberately
            # only skip when val is None: a caller-supplied non-None
            # value is treated as "the application is overriding the
            # default", which matches the historical contract.
            if (
                f.primary_key is None
                and val is None
                and f.column_name in defaulted_columns
            ):
                omitted_default_fields.append(f)
                continue
            columns.append(f.column_name)
            values.append(val)
            params.append(val)
        return columns, values, omitted_default_fields

    # ── CREATE (INSERT, no upsert) ─────────────────────────────────────────

    async def run_create(self, obj: Any) -> Any:
        """INSERT without ON CONFLICT. Returns the object with PK populated.

        Unlike run_insert (used by the INSERT builder), this bypasses the
        builder and works directly with the object.  The key difference
        from save() is that create() never generates ON CONFLICT — if the
        row already exists, the database raises a unique constraint violation.
        """
        # Path duplicates a fair bit of _render_insert intentionally:
        # run_create has no builder to consult (no ON CONFLICT, no bulk,
        # no INSERT…SELECT, no kwargs-only path), so reusing
        # _render_insert would require synthesising a builder shape just
        # to throw most of it away.  The simpler thing is to inline the
        # single-row VALUES path here and keep the two implementations
        # in sync by hand.  Drift risk: if _render_insert's RETURNING
        # logic changes, this code must change too.
        meta = TableProxy(type(obj))._meta
        params: list[Any] = []
        kwargs = {f.attr_name: getattr(obj, f.attr_name) for f in meta.fields}
        # DEFAULT-aware column omission — see _extract_insert_fields and
        # _get_defaulted_columns for the full rationale.  Mirrors the
        # run_insert path: introspect the schema once (cached), omit
        # None-valued fields whose column has a non-NULL DEFAULT, and
        # extend RETURNING to fetch the DB-generated values back onto
        # the in-memory object.
        defaulted = await self._get_defaulted_columns(meta.table_name)
        columns, _values, omitted = self._extract_insert_fields(
            meta, kwargs, params, defaulted
        )

        col_sql = ", ".join(columns)
        val_sql = ", ".join(f"${i + 1}" for i in range(len(columns)))
        sql = f"INSERT INTO {meta.table_name} ({col_sql}) VALUES ({val_sql})"

        # RETURNING shape mirrors run_insert: pk (when DBKey), then
        # omitted-default fields in declaration order.  AppKey + omitted
        # is also possible — when present, RETURNING fetches the defaults
        # but doesn't include the PK (the app supplied it on input).
        returning_cols: list[str] = []
        if meta.pk and meta.pk.primary_key == DBKey:
            returning_cols.append(meta.pk.column_name)
        returning_cols.extend(f.column_name for f in omitted)

        if returning_cols:
            sql += f" RETURNING {', '.join(returning_cols)}"
            row = await self._db.execute_one(sql, params)
            # See run_insert for the rationale on raising vs. silent None.
            # Both code paths share the same invariant: a DBKey INSERT
            # always produces exactly one RETURNING row.
            if row is None:
                raise RuntimeError(
                    f"INSERT...RETURNING produced no row for "
                    f"{meta.cls.__name__} — driver bug or row not inserted"
                )
            # Row layout: [pk?, *omitted_defaults].  `pos` tracks the
            # offset where omitted-default values begin.  For DBKey:
            # pos=1, row[0] is the PK, row[1..] are the omitted defaults.
            # For AppKey-with-omitted: pos=0, row[0..] are the omitted
            # defaults.  enumerate(start=pos) makes the index `i` line
            # up with the actual row tuple position.
            pos = 0
            if meta.pk and meta.pk.primary_key == DBKey:
                setattr(obj, meta.pk.attr_name, row[0])
                pos = 1
            for i, f in enumerate(omitted, start=pos):
                setattr(obj, f.attr_name, row[i])
        else:
            await self._db.execute(sql, params)

        # Returns the same object (mutated with PK if DBKey), not a copy.
        return obj

    # ── UPDATE ────────────────────────────────────────────────────────────────

    def render_update(self, b: Any) -> tuple[str, list[Any]]:
        # Final emitted clause order: UPDATE table SET ... [FROM ...]
        # WHERE ... [RETURNING ...].  This matches PG's documented
        # syntax and the order in which params are appended below;
        # any reordering would silently corrupt $N numbering.
        if b._table is None:
            raise ValueError("UPDATE requires SET(table, …) before WHERE / await")
        params: list[Any] = []
        meta = b._table._meta

        if b._obj is not None:
            # Type check: reject if the object's class doesn't match the table.
            if not isinstance(b._obj, meta.cls):
                raise TypeError(
                    f"Expected {meta.cls.__name__}, got {type(b._obj).__name__}"
                )
            # When updating from an object, SET all non-PK fields.
            # The PK is deliberately excluded — it's the identity, not a value
            # to update, and including it in SET would be a no-op at best.
            kwargs = {
                f.attr_name: getattr(b._obj, f.attr_name)
                for f in meta.fields
                if f.primary_key is None
            }
        else:
            kwargs = b._kwargs

        # Reject unknown kwargs upfront so a typo (.SET(T, nmae="x")) raises
        # immediately rather than silently producing an empty SET clause.
        # Object-derived kwargs are always a subset of meta.fields, so this
        # is effectively a no-op on the obj path.
        known_attrs = {f.attr_name for f in meta.fields}
        unknown = set(kwargs) - known_attrs
        if unknown:
            raise ValueError(
                f"Unknown field(s) for {meta.cls.__name__}: {sorted(unknown)}"
            )

        set_clauses: list[str] = []
        # Iterate meta.fields (not kwargs) so the SET clause order follows
        # the dataclass declaration order, producing deterministic SQL
        # regardless of kwargs insertion order.  Each value can be either
        # a literal (becomes a $N parameter) or a SQLRenderable
        # expression like T.count + 1 / cygnet.fn(...)/ OtherTable.col
        # (renders in place — needed for both `count = count + 1` self-
        # referential updates and UPDATE FROM joins where the SET value
        # references another table).  The `len(params)`-based numbering
        # naturally interleaves: a renderable that itself appends params
        # advances the counter for any literal kwargs that follow.
        for f in meta.fields:
            if f.attr_name in kwargs:
                value = kwargs[f.attr_name]
                if hasattr(value, "render_sql"):
                    rhs = value.render_sql(params)
                else:
                    params.append(value)
                    rhs = f"${len(params)}"
                set_clauses.append(f"{f.column_name} = {rhs}")

        # An empty SET is always a bug — UPDATE(db).SET(T) with no object
        # and no kwargs would emit invalid SQL, and silently no-opping was
        # the dangerous kind of safety rail (typos in field names also
        # landed here pre-validation).  Raising here makes the mistake loud.
        if not set_clauses:
            raise ValueError("UPDATE SET requires at least one field")

        sql = f"UPDATE {meta.table_name} SET {', '.join(set_clauses)}"

        # UPDATE … FROM other_tables: PG-specific extension for join-like
        # updates where the SET values reference columns from another
        # table.  Multiple FROM tables are comma-separated; the user's
        # WHERE clause supplies the join condition (this is PG's
        # convention, not a separate JOIN syntax).
        if b._from_tables:
            from_parts: list[str] = []
            for t in b._from_tables:
                tname = t._meta.table_name
                # Honor aliases the same way SelectBuilder does.
                if getattr(t, "_alias", None):
                    tname += f" AS {t._alias}"
                from_parts.append(tname)
            sql += f" FROM {', '.join(from_parts)}"

        # WHERE is mandatory for UPDATE (safety rail).
        # SET params are numbered before WHERE params, so $N numbering
        # continues correctly across the SET and WHERE clauses.
        checked = self._check_predicates(b._predicates, "UPDATE")
        if checked:
            where = self._render_where(checked, params)
            sql += f" WHERE {where}"

        # RETURNING runs last so its column-list params (rare, but possible
        # via cygnet.op() / cygnet.lit() expressions) are numbered after
        # SET and WHERE.
        if b._returning is not None:
            ret_cols = ", ".join(c.render_sql(params) for c in b._returning)
            sql += f" RETURNING {ret_cols}"

        return sql, params

    async def run_update(self, b: Any) -> Any:
        # render_update either returns a real SQL string or raises; there is
        # no longer a silent-no-op short-circuit.  See the empty-SET branch
        # in render_update for why.  Without RETURNING the awaited result is
        # None (matching the historical signature); with RETURNING it's the
        # list of affected rows as tuples.
        sql, params = self.render_update(b)
        if b._returning is not None:
            return await self._db.execute(sql, params)
        await self._db.execute(sql, params)
        return None

    # ── DELETE ────────────────────────────────────────────────────────────────

    def render_delete(self, b: Any) -> tuple[str, list[Any]]:
        if b._table is None:
            raise ValueError("DELETE requires FROM(table) before WHERE / await")
        params: list[Any] = []
        meta = b._table._meta
        # WHERE is mandatory for DELETE (safety rail).
        checked = self._check_predicates(b._predicates, "DELETE")
        sql = f"DELETE FROM {meta.table_name}"
        # USING other_tables: PG-specific extension.  Emitted between
        # FROM and WHERE; the join condition lives in WHERE itself.
        if b._using_tables:
            using_parts: list[str] = []
            for t in b._using_tables:
                tname = t._meta.table_name
                if getattr(t, "_alias", None):
                    tname += f" AS {t._alias}"
                using_parts.append(tname)
            sql += f" USING {', '.join(using_parts)}"
        if checked:
            where = self._render_where(checked, params)
            sql += f" WHERE {where}"
        if b._returning is not None:
            ret_cols = ", ".join(c.render_sql(params) for c in b._returning)
            sql += f" RETURNING {ret_cols}"
        return sql, params

    async def run_delete(self, b: Any) -> Any:
        # See run_update: None when no RETURNING, list of tuples when set.
        sql, params = self.render_delete(b)
        if b._returning is not None:
            return await self._db.execute(sql, params)
        await self._db.execute(sql, params)
        return None

    # ── SAVE (upsert) ─────────────────────────────────────────────────────────

    async def run_save(self, obj: Any) -> None:
        """Upsert: INSERT ... ON CONFLICT (pk) DO UPDATE SET ...

        Behaviour depends on PK type and value:
          - DBKey + None   → plain INSERT with RETURNING (new row)
          - DBKey + value  → INSERT ... ON CONFLICT DO UPDATE (upsert)
          - AppKey + None  → ValueError (app must supply the key)
          - AppKey + value → INSERT ... ON CONFLICT DO UPDATE (upsert)

        The DBKey + None case delegates to run_insert rather than
        duplicating INSERT logic, because the object needs its PK
        populated via RETURNING.
        """
        # Note: the explicit-upsert path below does NOT participate in
        # the DEFAULT-aware column-omission machinery — every column is
        # emitted with its current in-memory value, including None for
        # columns with a DEFAULT.  An upsert where a None-valued column
        # has a DEFAULT will INSERT NULL and on conflict will UPDATE to
        # NULL, regardless of the schema default.  This is consistent
        # with save() semantics ("persist the object's current state")
        # but means save() and create() differ in their DEFAULT
        # handling — see run_create for the contrast.
        meta = TableProxy(type(obj))._meta

        if meta.pk is None:
            raise TypeError(
                f"{type(obj).__name__} has no primary key — "
                f"cannot use save(), use INSERT or UPDATE directly"
            )

        pk_val = getattr(obj, meta.pk.attr_name)

        # DBKey with no value → first insert, delegate to the INSERT path
        # which handles RETURNING and in-place PK mutation.
        if meta.pk.primary_key == DBKey and pk_val is None:
            from .builders import InsertBuilder

            b = InsertBuilder(self._db)
            b._table = TableProxy(type(obj))
            b._obj = obj
            await self.run_insert(b)
            return

        if pk_val is None:
            raise ValueError(
                f"{type(obj).__name__}.{meta.pk.attr_name} is AppKey but value is None"
            )

        # Build the upsert.  All fields (including PK) appear in the INSERT
        # portion; only non-PK fields appear in the ON CONFLICT UPDATE SET.
        # Unlike render_insert, this path does NOT call _extract_insert_fields:
        # we're in a known-PK-present state, so the DBKey=None skip and
        # AppKey=None check don't apply, and we want the PK column emitted
        # explicitly for the ON CONFLICT target.
        params: list[Any] = []
        all_fields = meta.fields
        non_pk = [f for f in all_fields if f.primary_key is None]

        columns = [f.column_name for f in all_fields]
        placeholders: list[str] = []
        for f in all_fields:
            params.append(getattr(obj, f.attr_name))
            placeholders.append(f"${len(params)}")

        # EXCLUDED is PostgreSQL's name for the row that would have been
        # inserted.  This ensures the UPDATE uses the new values, not the
        # existing ones.
        set_clauses = [f"{f.column_name} = EXCLUDED.{f.column_name}" for f in non_pk]

        sql = (
            f"INSERT INTO {meta.table_name} "
            f"({', '.join(columns)}) "
            f"VALUES ({', '.join(placeholders)}) "
            f"ON CONFLICT ({meta.pk.column_name}) DO UPDATE SET "
            f"{', '.join(set_clauses)}"
        )

        await self._db.execute(sql, params)

    # ── Utilities ─────────────────────────────────────────────────────────────

    def _row_to_obj(self, meta: Any, row: Any) -> Any:
        """Map a database row (tuple) to a dataclass instance.

        Relies on positional correspondence: the Nth element of the row
        maps to the Nth field in meta.fields.  This works because
        _render_select emits explicit column names in meta.fields order,
        so the result columns always align with the dataclass fields
        regardless of the physical column order (attnum) in PostgreSQL.

        zip() silently truncates on length mismatch: if the row is shorter
        than meta.fields (schema drift, hand-written SQL via lit(), etc.),
        the dataclass constructor raises TypeError for the missing fields.
        A row longer than meta.fields silently drops the trailing columns.

        Construction uses meta.cls(**kwargs) with positional→name mapping,
        which means the dataclass must accept every field by keyword.  A
        dataclass that declares init=False on any field, or uses __init__
        with positional-only parameters, will not be hydratable this way.
        """
        # Every row produces a NEW dataclass instance — no caching, no
        # identity map, no de-duplication.  Two SELECTs that return the
        # same DB row produce two distinct Python objects.  This is a
        # deliberate simplification compared to richer ORMs (no session,
        # no unit-of-work) and is the reason "stale instance" hazards
        # don't exist in Cygnet.
        kwargs = {f.attr_name: val for f, val in zip(meta.fields, row)}
        return meta.cls(**kwargs)
