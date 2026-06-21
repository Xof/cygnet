# expression.py — The SQLRenderable protocol and extended operator classes.
#
# This module defines the structural contract (SQLRenderable) that unifies
# all SQL-emitting types: ColumnProxy, Predicate, Literal, PrefixOp, and
# SuffixOp.  Any object with a render_sql(params) method can appear wherever
# Cygnet expects a SQL fragment — SELECT columns, WHERE clauses, ORDER BY,
# GROUP BY.  This is duck-typed in predicate.py (_render_operand checks
# hasattr(value, "render_sql")), but the Protocol here gives mypy a
# structural type to check against.
#
# PrefixOp and SuffixOp extend Cygnet's built-in comparison operators to
# cover SQL constructs that don't map to Python's __eq__/__lt__/etc.
# (e.g., ILIKE, IS NULL, NOT).  The factory functions op(), ops(), is_null(),
# and is_not_null() are the public API for creating these.

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, overload, runtime_checkable

from .predicate import Predicate, _InfixOps


# The render_sql contract: mutate `params` in place AND return the SQL
# fragment, in a single pass.  This is deliberate — a two-pass design
# (first collect params, then render SQL) would require traversing the
# expression tree twice and keeping parallel bookkeeping.  Because $N
# indexes are assigned via `len(params) + 1` at render time, an
# expression cannot know its own placeholder index ahead of time; the
# tree must therefore be rendered in the final execution order.  The
# shared `params` list is what lets independent subtrees (e.g., separate
# WHERE predicates, SELECT expressions, ORDER BY keys) agree on
# monotonic, non-overlapping parameter numbers.
class SQLRenderable(Protocol):
    def render_sql(self, params: list[Any]) -> str: ...


# ── Duck-type Protocols for the table / field surfaces ──────────────
#
# TableProxy and the CTE family share enough structural surface that the
# executor and ColumnProxy treat them interchangeably — but because CTE
# isn't a TableProxy subclass (deliberate, see cte.py header for the
# rationale), code that took `TableProxy[Any]` had to carry
# `# type: ignore[arg-type]` at every CTE site.  These Protocols name
# the shared shape so the type system can express "anything with these
# attributes" without requiring inheritance.  Closes S8.
#
# Read-only Protocols: every attribute is consumed but never assigned
# by the executor / ColumnProxy, so mypy will accept properties OR
# bare attributes on the conforming side (TableMeta uses attributes,
# CTE uses @property).


class FieldLike(Protocol):
    """The minimum field-meta surface ColumnProxy and the executor read.

    ``FieldMeta`` (meta.py) and ``_PseudoField`` (cte.py) both conform
    structurally — no explicit inheritance needed.  ``primary_key`` and
    ``foreign_key`` are typed ``Any`` here because the concrete types
    (``_PrimaryKey`` / ``_ForeignKey`` from annotations.py) would
    create an import cycle if expression.py reached up to annotations.
    The consumers (executor) only check ``is None`` / ``== DBKey``,
    which works fine through ``Any``.

    Property-style declarations (rather than bare attributes) make the
    Protocol read-only, which is what lets ``_PseudoField`` (a frozen
    dataclass with read-only attrs) conform alongside ``FieldMeta``
    (regular dataclass with settable attrs).  Mypy treats settable
    attrs as satisfying read-only property requirements.
    """

    @property
    def attr_name(self) -> str: ...

    @property
    def column_name(self) -> str: ...

    @property
    def primary_key(self) -> Any: ...

    @property
    def foreign_key(self) -> Any: ...


class MetaProtocol(Protocol):
    """The minimum table-meta surface the executor reads.

    Satisfied by ``TableMeta`` and by CTE/RecursiveCTE (which return
    themselves from ``_meta``, then expose ``table_name`` / ``fields``
    / ``pk`` / ``cls`` directly).

    ``fields`` is ``Sequence[FieldLike]`` (not ``list``) so the variance
    matches — ``list`` is invariant in its element type, but ``Sequence``
    is covariant.  A ``list[FieldMeta]`` (TableMeta's actual type)
    therefore conforms even though ``list[FieldLike]`` would not.
    Same idiomatic move as ``collections.abc.Sequence`` provides
    everywhere mypy needs to accept "any read-only sequence of an
    interface".
    """

    @property
    def table_name(self) -> str: ...

    @property
    def fields(self) -> Sequence[FieldLike]: ...

    @property
    def pk(self) -> FieldLike | None: ...

    @property
    def cls(self) -> type: ...


class TableSourceProtocol(Protocol):
    """The minimum table-source surface ColumnProxy and the executor read.

    ``TableProxy``, ``CTE``, ``RecursiveCTE``, and ``Lateral`` all
    conform.  Used to retype ColumnProxy.__init__ so the
    duck-typed CTE → ColumnProxy stamping no longer needs
    ``# type: ignore[arg-type]``.
    """

    @property
    def _sql_name(self) -> str: ...

    @property
    def _meta(self) -> MetaProtocol: ...

    @property
    def _alias(self) -> str | None: ...


# ── DB adapter contract ─────────────────────────────────────────────
#
# Cygnet's ``db`` parameter accepts anything that satisfies this
# Protocol structurally.  PsycopgDB (the reference adapter in
# cygnet/psycopg_db.py) and FakeDB (the test fixture in
# tests/conftest.py) both conform without inheriting from it.
# Custom adapters — say, an asyncpg wrapper or a tracing proxy —
# only need to expose these members to be usable.
#
# Optional methods (``stream`` and ``column_defaults``) are
# deliberately NOT on the Protocol: they're probed via ``hasattr``
# at the consumer sites.  Adapters that don't implement them get
# the historical behaviour (no streaming, no DEFAULT-aware INSERT
# codegen).  If a third optional method ever shows up, the
# capability-set pattern (S4 alt) becomes worth considering — for
# now, two optionals stays under the documentation budget.


@runtime_checkable
class DBAdapter(Protocol):
    """Contract for objects that can be passed as ``db`` to Cygnet builders.

    Required members:

    - ``_in_transaction`` — ``bool``, ``False`` on a fresh adapter;
      flipped by ``cygnet.transaction`` at outermost BEGIN/COMMIT.
      Drives nesting detection (savepoints) and the task-locality guard.
    - ``_transaction_task`` — Cygnet-managed: ``cygnet.transaction``
      stashes ``asyncio.current_task()`` here at outermost entry and
      clears it at outermost exit.  Adapters should initialise this
      to ``None`` (matches the S10 task-locality guard contract).
      Typed ``Any`` to spare adapter authors an asyncio import; the
      runtime value is ``asyncio.Task[Any] | None``.
    - ``execute(sql, params)`` — issue a statement and return the result
      rows as a list of tuples.  Statements that don't return rows
      (DDL / INSERT-without-RETURNING / UPDATE / DELETE) return ``[]``.
    - ``execute_one(sql, params)`` — issue a statement expected to
      produce at most one row.  Returns ``None`` if no row.  Used for
      ``INSERT … RETURNING`` and ``cygnet.get``.

    Optional members (duck-typed via ``hasattr`` — not in the Protocol):

    - ``async stream(sql, params) -> AsyncIterator[tuple]`` — yields
      rows incrementally for memory-efficient large reads.  Without
      it, ``SelectBuilder.stream()`` raises TypeError.
    - ``async column_defaults(table_name) -> set[str]`` — return the
      set of columns on ``table_name`` carrying a non-NULL DEFAULT.
      Without it, Cygnet falls back to the historical "emit every
      field including NULL" INSERT codegen.

    ``@runtime_checkable`` means ``isinstance(my_adapter, DBAdapter)``
    works for "is my adapter shaped right?" checks — useful for
    adapter authors verifying conformance before shipping.
    """

    _in_transaction: bool
    _transaction_task: Any

    async def execute(
        self, sql: str, params: list[Any] | None = None
    ) -> list[tuple[Any, ...]]: ...

    async def execute_one(
        self, sql: str, params: list[Any] | None = None
    ) -> tuple[Any, ...] | None: ...


@dataclass(frozen=True)
class PrefixOp:
    """Prefix operator: renders as 'OP (expr)', e.g., NOT (accounts.active = $1).

    The operand is wrapped in parens to avoid precedence surprises — this
    means NOT x = 1 renders as NOT (x = $1), not NOT x = $1.
    """

    op: str
    operand: Any

    def render_sql(self, params: list[Any]) -> str:
        return f"{self.op} ({self.operand.render_sql(params)})"

    # __and__ / __or__ let PrefixOp participate in compound expressions:
    # cygnet.op("NOT", T.active == True) & (T.name == "x")
    # Without these, the & operator would fail because Predicate.__rand__
    # doesn't exist (& dispatches to the left operand first).
    def __and__(self, other: Any) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Any) -> Predicate:
        return Predicate(self, "OR", other)

    def __invert__(self) -> PrefixOp:
        # ~PrefixOp wraps in another NOT.  Double-negation is left explicit
        # rather than simplified — surprising-but-honest beats clever-but-
        # mismatching-the-source.
        return PrefixOp(op="NOT", operand=self)


@dataclass(frozen=True)
class SuffixOp:
    """Suffix operator: renders as 'expr OP', e.g., accounts.email IS NULL.

    Unlike PrefixOp, no parens are added — suffix SQL operators (IS NULL,
    IS NOT NULL) bind tightly enough that parens would be unusual.
    """

    operand: Any
    op: str

    def render_sql(self, params: list[Any]) -> str:
        return f"{self.operand.render_sql(params)} {self.op}"

    def __and__(self, other: Any) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Any) -> Predicate:
        return Predicate(self, "OR", other)

    def __invert__(self) -> PrefixOp:
        # ~is_null(col) -> NOT (col IS NULL).  Users who want IS NOT NULL
        # specifically should reach for cygnet.is_not_null; ~ produces the
        # general NOT wrapping, which is the Pythonic-looking alternative.
        return PrefixOp(op="NOT", operand=self)


# Overloads narrow op()'s return type by arity: the 3-arg infix form
# always yields a Predicate, the 2-arg prefix form a PrefixOp, and the
# 1-arg "factory factory" form a Callable that returns Predicate.
# Without these, callers (e.g. cygnet.jsonb's helpers that always pass
# three args) would see Predicate | PrefixOp | Any and fail mypy.
@overload
def op(operator: str, /) -> Callable[[Any, Any], Predicate]: ...
@overload
def op(operator: str, operand: Any, /) -> PrefixOp: ...
@overload
def op(left: Any, operator: str, right: Any, /) -> Predicate: ...


def op(*args: Any) -> Any:
    """Create an operator expression.

    - 3 args: op(left, 'ILIKE', right) -> infix Predicate
    - 2 args: op('NOT', expr) -> PrefixOp
    - 1 arg:  op('ILIKE') -> reusable callable returning Predicate

    The 1-arg form is a factory-factory: it captures the operator string
    and returns a callable that creates Predicates.  This is useful when
    the same non-standard operator is used repeatedly:
        ILIKE = cygnet.op('ILIKE')
        q.WHERE(ILIKE(T.name, '%pattern%'))

    Security: the operator string is interpolated into the rendered SQL
    verbatim — no escaping, no parameterisation.  Treat it as trusted
    input.  A naive `cygnet.op(col, user_input, val)` is a SQL-injection
    vector.  Operands (left/right values) ARE parameterised; only the
    operator itself is trusted.
    """
    # Arity dispatch is positional, with no keyword arguments accepted.
    # The dispatch order matters here: a stray 0-arg or 4+-arg call falls
    # through to the explicit TypeError at the bottom, rather than being
    # silently bound to one of the overload arms.
    if len(args) == 3:
        return Predicate(args[0], args[1], args[2])
    if len(args) == 2:
        return PrefixOp(op=args[0], operand=args[1])
    if len(args) == 1:
        operator = args[0]

        def _precreated(left: Any, right: Any) -> Predicate:
            return Predicate(left, operator, right)

        return _precreated
    raise TypeError(f"cygnet.op() requires 1, 2, or 3 arguments, got {len(args)}")


def ops(operand: Any, operator: str) -> SuffixOp:
    """Create a suffix operator: ops(col, 'IS NULL') -> col IS NULL.

    Named `ops` (operator-suffix) to distinguish from `op` (operator-infix/prefix).

    Security: like `op()`, the operator string is interpolated verbatim
    into the rendered SQL.  Treat it as trusted; never pass unsanitised
    user input as the operator.
    """
    return SuffixOp(operand=operand, op=operator)


def is_null(operand: Any) -> SuffixOp:
    """Convenience: is_null(col) -> col IS NULL."""
    return SuffixOp(operand=operand, op="IS NULL")


def is_not_null(operand: Any) -> SuffixOp:
    """Convenience: is_not_null(col) -> col IS NOT NULL."""
    return SuffixOp(operand=operand, op="IS NOT NULL")


@dataclass(frozen=True)
class _Exists:
    """`EXISTS (subquery)` / `NOT EXISTS (subquery)` predicate.

    Distinct from PrefixOp because PrefixOp wraps its operand in parens
    (``OP (operand)``) and a SelectBuilder operand already wraps itself
    in parens via its own render_sql — the combination would emit
    ``EXISTS ((SELECT …))`` (valid PG, ugly).  This class assumes the
    subquery operand provides its own parens, so the rendered shape is
    a clean ``EXISTS (SELECT …)``.

    Participates in & / | / ~ the same way other predicate-like classes
    do, so EXISTS can compose freely with column predicates::

        .WHERE(cygnet.exists(inner) & (T.active == True))
    """

    op: str  # "EXISTS" or "NOT EXISTS"
    subquery: Any  # SelectBuilder, but typed Any to avoid an import cycle.

    def render_sql(self, params: list[Any]) -> str:
        # subquery.render_sql must produce its own parens.  SelectBuilder's
        # render_sql does this; if a caller passes a different renderable
        # without self-parens, the generated SQL will be malformed —
        # acceptable since exists() validates the type at construction.
        return f"{self.op} {self.subquery.render_sql(params)}"

    def __and__(self, other: Any) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Any) -> Predicate:
        return Predicate(self, "OR", other)

    def __invert__(self) -> _Exists:
        # Toggle EXISTS ↔ NOT EXISTS rather than wrapping in another NOT.
        # Double negation `~~exists(b)` collapses back to plain EXISTS,
        # which matches what users typically want from ~ on this specific
        # operator (versus the general PrefixOp NOT-wrapping behaviour).
        flipped = "NOT EXISTS" if self.op == "EXISTS" else "EXISTS"
        return _Exists(op=flipped, subquery=self.subquery)


def exists(subquery: Any) -> _Exists:
    """`EXISTS (subquery)` — true iff the subquery returns at least one row.

    The subquery's column list doesn't matter (EXISTS only checks for
    row presence), so any SELECT shape works.  Correlated subqueries
    referencing outer-query columns are the most common use::

        any_post = (
            cygnet.SELECT(db, cygnet.lit("1"))
            .FROM(PostTable)
            .WHERE(PostTable.account_id == AccountTable.id)
        )
        active_authors = (
            cygnet.SELECT(db).FROM(AccountTable)
            .WHERE(cygnet.exists(any_post))
        )

    Only SelectBuilder is accepted; the type check fires immediately so
    a wrong argument doesn't render broken SQL silently.
    """
    # Lazy import to avoid the cycle expression → builders → executor → …
    from .builders import SelectBuilder

    if not isinstance(subquery, SelectBuilder):
        raise TypeError(
            f"cygnet.exists() expects a SelectBuilder, got {type(subquery).__name__}"
        )
    return _Exists(op="EXISTS", subquery=subquery)


def not_exists(subquery: Any) -> _Exists:
    """`NOT EXISTS (subquery)` — true iff the subquery returns zero rows.

    Equivalent to ``~cygnet.exists(subq)``; provided as a separate verb
    because anti-join queries read more clearly with the explicit name
    than with a tilde.
    """
    from .builders import SelectBuilder

    if not isinstance(subquery, SelectBuilder):
        raise TypeError(
            f"cygnet.not_exists() expects a SelectBuilder, "
            f"got {type(subquery).__name__}"
        )
    return _Exists(op="NOT EXISTS", subquery=subquery)


@dataclass(frozen=True, eq=False)
class FunctionCall(_InfixOps):
    """A SQL function call: `NAME(arg1, arg2, ...)`.

    Each arg is either a SQLRenderable (rendered in place) or a plain
    Python value (becomes a `$N` parameter, mirroring how Predicate
    operands work).  The function name is interpolated verbatim — same
    trust model as op()/ops()/lit().

    FunctionCall participates in comparisons (returns Predicate) and in
    boolean composition (& / | / ~), so a function call can appear
    anywhere a column can: WHERE / HAVING / ORDER BY / SELECT lists.
    """

    name: str
    args: tuple[Any, ...]

    def render_sql(self, params: list[Any]) -> str:
        # Left-to-right traversal of self.args is load-bearing: $N indexes
        # are assigned by mutation of `params`, and they must match the
        # order in which the rendered SQL fragments reference them.
        # hasattr(a, "render_sql") is the duck-typed SQLRenderable check;
        # anything else becomes a $N parameter.  Note that this duck-typing
        # mirrors predicate._render_operand — keep the two in step.
        rendered: list[str] = []
        for a in self.args:
            if hasattr(a, "render_sql"):
                rendered.append(a.render_sql(params))
            else:
                params.append(a)
                rendered.append(f"${len(params)}")
        return f"{self.name}({', '.join(rendered)})"

    # Comparison + arithmetic operators and __hash__ = None come from the
    # shared _InfixOps mixin (see predicate.py).  eq=False on the dataclass is
    # load-bearing: it stops @dataclass generating a field-based __eq__ that
    # would shadow the mixin's Predicate-returning one, and (with eq false)
    # leaves the mixin's __hash__ = None in force so FunctionCall stays
    # unhashable.  The logical connectives below stay local — a bare function
    # call gets them too, but __invert__ can reference PrefixOp directly here.

    def __and__(self, other: Any) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Any) -> Predicate:
        return Predicate(self, "OR", other)

    def __invert__(self) -> PrefixOp:
        return PrefixOp(op="NOT", operand=self)

    def OVER(  # noqa: N802
        self,
        *,
        partition_by: tuple[Any, ...] | list[Any] = (),
        order_by: tuple[Any, ...] | list[Any] = (),
        frame: str | None = None,
    ) -> WindowExpression:
        """Wrap this function call in an OVER clause: `func(...) OVER (...)`.

        partition_by accepts any iterable of SQLRenderables; order_by
        accepts either bare renderables (default ASC) or
        ``(col, "DESC")`` / ``(col, "ASC")`` tuples for explicit
        direction.  ``frame`` is a raw SQL string for the rare case
        you need an explicit ``ROWS BETWEEN ...`` / ``RANGE BETWEEN ...``
        — interpolated verbatim, so treat it as trusted.

        Returns a WindowExpression that participates in SELECT lists,
        ORDER BY, and (rarely) WHERE, the same way an ordinary
        FunctionCall does.
        """
        # Normalise order_by entries to (renderable, direction) tuples.
        # A bare ColumnProxy / FunctionCall / Literal entry implies ASC.
        normalised_order: list[tuple[Any, str]] = []
        for entry in order_by:
            if isinstance(entry, tuple):
                col, direction = entry
                normalised_order.append((col, direction))
            else:
                normalised_order.append((entry, "ASC"))
        return WindowExpression(
            function=self,
            spec=WindowSpec(
                partition_by=tuple(partition_by),
                order_by=tuple(normalised_order),
                frame=frame,
            ),
        )


@dataclass(frozen=True)
class WindowSpec:
    """The OVER (...) part of a window expression.

    partition_by is a tuple of SQLRenderables; order_by is a tuple of
    (renderable, "ASC"|"DESC") pairs already normalised by
    FunctionCall.OVER.  frame is an optional raw SQL string (e.g.
    ``"ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW"``).

    Kept frozen so a WindowSpec can be reused across multiple
    FunctionCall.OVER calls without state-sharing surprises.
    """

    partition_by: tuple[Any, ...]
    order_by: tuple[tuple[Any, str], ...]
    frame: str | None

    def render_sql(self, params: list[Any]) -> str:
        parts: list[str] = []
        if self.partition_by:
            cols = ", ".join(c.render_sql(params) for c in self.partition_by)
            parts.append(f"PARTITION BY {cols}")
        if self.order_by:
            order_parts: list[str] = []
            for c, direction in self.order_by:
                rendered = c.render_sql(params)
                # Same opt-out path SelectBuilder.ORDER_BY uses: a
                # Literal that already contains its own direction
                # (e.g. cygnet.lit("created_at DESC")) shouldn't get
                # another suffix appended.
                if not getattr(c, "_renders_own_direction", False):
                    rendered += f" {direction}"
                order_parts.append(rendered)
            parts.append(f"ORDER BY {', '.join(order_parts)}")
        if self.frame:
            parts.append(self.frame)
        inside = " ".join(parts)
        # Empty OVER () is valid SQL and unambiguous: it requests the
        # full unpartitioned, unordered window.  We emit it explicitly
        # rather than skipping the OVER entirely, since the caller asked
        # for a window expression and silently dropping it would mask
        # programming errors at the SQL-shape level.
        return f"OVER ({inside})" if inside else "OVER ()"


@dataclass(frozen=True, eq=False)
class WindowExpression(_InfixOps):
    """`func(...) OVER (...)`: a function call with a window spec.

    Implements the SQLRenderable protocol and the same comparison /
    boolean-composition operators as FunctionCall, so a window
    expression is a drop-in for a column reference: usable in SELECT
    lists, ORDER BY, GROUP BY, even WHERE / HAVING when wrapped in a
    comparison.
    """

    function: FunctionCall
    spec: WindowSpec

    def render_sql(self, params: list[Any]) -> str:
        # render the function first so its $N parameters precede those
        # introduced by the OVER spec — keeps the params list aligned
        # with the SQL string left-to-right.
        fn_sql = self.function.render_sql(params)
        spec_sql = self.spec.render_sql(params)
        return f"{fn_sql} {spec_sql}"

    # Comparison + arithmetic operators and __hash__ = None come from the
    # shared _InfixOps mixin (see predicate.py); eq=False keeps that
    # __eq__/__hash__ rather than letting @dataclass generate field-based ones.

    def __and__(self, other: Any) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Any) -> Predicate:
        return Predicate(self, "OR", other)

    def __invert__(self) -> PrefixOp:
        return PrefixOp(op="NOT", operand=self)


def fn(name: str) -> Callable[..., FunctionCall]:
    """Create a factory for a SQL function call.

    Returns a callable that accepts 0+ arguments and produces a
    FunctionCall.  Plain Python values among the arguments are
    parameterised; SQLRenderable arguments (ColumnProxy, Literal,
    nested FunctionCall, etc.) render in place.

        count = cygnet.fn('count')
        count(T.id)             # COUNT(accounts.id)
        count(cygnet.lit('*'))  # COUNT(*)
        cygnet.fn('lower')(T.name) == 'fred'  # lower(...) = $1

    Security: the function name is interpolated verbatim — never pass
    untrusted input.  See cygnet.functions for a curated set of common
    PG functions (count, sum, avg, coalesce, now, array_agg, etc.).
    """

    def _factory(*args: Any) -> FunctionCall:
        return FunctionCall(name=name, args=args)

    return _factory
