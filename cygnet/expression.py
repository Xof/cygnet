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

from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
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


@dataclass(frozen=True, eq=False)
class RowValue(_InfixOps):
    """A SQL row-value constructor: ``(a, b)``.

    Renders as a parenthesised, comma-joined list of elements, each one
    either a SQLRenderable (rendered in place) or a plain Python value
    (parameterised as ``$N``) — the same duck-typing every other operand
    position uses.  That single shape covers three distinct SQL uses:

      - the left side of a comparison — ``(t.a, t.b)`` (columns);
      - the right side          — ``($1, $2)`` (values);
      - an ``IN`` list          — ``(($1, $2), ($3, $4))`` (rows of values).

    They are the same construct syntactically, so one renderer serves all
    three; ``in_`` builds the third by nesting RowValues.

    The comparison operators are overridden (rather than inherited
    unchanged from ``_InfixOps``) for one load-bearing reason: a plain
    tuple on the right would otherwise be bound as a *single* parameter
    (``(t.a, t.b) = $1``), which is not what the caller means.  Wrapping
    it in another RowValue produces ``(t.a, t.b) = ($1, $2)``.  Arity is
    checked as the comparison is built — not when ``row()`` constructs
    the operands, and not at execute time — so a mismatch fails in
    Python with the two lengths named, rather than as a PG "unequal
    number of entries in row expressions" error from the server.

    Two behaviours worth knowing:

      - ``row(a, b) == None`` hits ``Predicate``'s None → IS NULL rewrite
        and renders ``(a, b) IS NULL``.  Valid PG, but it means "both
        members are null", NOT "the row is null".  Deliberately not
        special-cased — the rewrite is uniform across every expression
        type, and carving out an exception here would be the surprise.
      - No ``&`` / ``|`` / ``~``: a bare row value is not a boolean
        expression.  Compare it first, then compose the resulting
        Predicate.

    Inherits ``__hash__ = None`` from ``_InfixOps`` (``eq=False`` keeps
    the mixin's operators rather than dataclass-generated ones), so a
    RowValue is unhashable — consistent with ColumnProxy and
    FunctionCall.
    """

    items: tuple[Any, ...]

    def __post_init__(self) -> None:
        # `row()` guards this too, but RowValue is a documented public
        # symbol (ARCHITECTURE module map) and `()` is a PG syntax error,
        # so the invariant belongs on the class, not only on the factory.
        if not self.items:
            raise TypeError("a row value requires at least one element")

    @staticmethod
    def _is_param_free(value: Any) -> bool:
        """True if ``value`` renders with no bound parameters at all.

        Used to police nesting: PG can resolve a nested row comparison only
        when nothing inside it needs a type inferred (see ``_coerce``).
        """
        if isinstance(value, RowValue):
            return all(RowValue._is_param_free(item) for item in value.items)
        return bool(hasattr(value, "render_sql"))

    @staticmethod
    def _contains_none(value: Any) -> bool:
        """True if ``value`` has a ``None`` leaf anywhere inside it."""
        if isinstance(value, RowValue):
            return any(RowValue._contains_none(item) for item in value.items)
        return value is None

    def render_sql(self, params: list[Any]) -> str:
        # Left-to-right, same contract as FunctionCall.render_sql: each
        # element either renders itself (appending its own params) or is
        # appended here, so $N indexes match the emitted text order.
        rendered: list[str] = []
        for item in self.items:
            if hasattr(item, "render_sql"):
                rendered.append(item.render_sql(params))
            else:
                params.append(item)
                rendered.append(f"${len(params)}")
        return f"({', '.join(rendered)})"

    def _coerce(self, other: Any) -> Any:
        """Normalise a comparison's right operand and check arity.

        The accepted shapes, in the order they are tested:

          - a RowValue, or a tuple/list coerced to one — compared
            element-wise, with arity checked here and, for a nested
            row, recursively;
          - ``None`` or anything renderable (a ColumnProxy, a function
            call, a subquery) — passed through untouched for
            ``Predicate`` to handle, including the None → IS NULL
            rewrite documented above.  PG checks the arity of those;
          - a scalar (a number, ``str``, ``bytes``, …) — bound as a
            single ``$N``, which is only meaningful against a
            one-element row, so the arity check below rejects it for
            any wider row.

        Every *other* iterable — set, dict, range, generator — is
        refused outright.  Those are the "iterable but wrong" shapes
        that ``in_`` already rejects for the same reason: a set and a
        dict have no dependable element order, and a generator would be
        consumed here.  Binding one whole as a single parameter (what
        the earlier catch-all did) hid the mistake until PG rejected
        the query, or bound it in an arbitrary order.
        """
        if isinstance(other, RowValue):
            other_len = len(other.items)
        elif isinstance(other, tuple | list):
            other_len = len(other)
            other = RowValue(tuple(other))
        elif other is None or hasattr(other, "render_sql"):
            return other
        elif isinstance(other, str | bytes):
            # Deliberately ahead of the Iterable arm: a string is a
            # scalar operand here, not a sequence of characters.
            other_len = 1
        elif isinstance(other, Iterable):
            raise TypeError(
                f"row value cannot be compared against "
                f"{type(other).__name__} — pass a tuple or list of "
                f"{len(self.items)} elements (an unordered or "
                f"lazily-consumed iterable has no dependable order)"
            )
        else:
            other_len = 1
        if other_len != len(self.items):
            raise ValueError(
                f"row value has {len(self.items)} elements but the compared "
                f"value has {other_len} — arity must match"
            )
        # Nested rows: PG cannot infer the type of a parameter sitting
        # inside a nested row constructor.  `((a,b),c) = (($1,$2),$3)`
        # fails to prepare with "could not determine data type of
        # parameter $1", because the outer comparison resolves its members
        # as `record` vs `record`, which carries no per-member type
        # information — while the *flat* `(a,b) = ($1,$2)` prepares with no
        # declared types at all.  psycopg dumps str/None as oid 0
        # (UNKNOWN) and asyncpg sends no parameter OIDs whatsoever, so
        # neither driver can rescue it.  Refuse the shape here rather than
        # render SQL no server will run.  A nested row compared against
        # *columns* binds nothing and is fine, so it stays legal.
        if isinstance(other, RowValue):
            coerced: list[Any] = []
            for mine, theirs in zip(self.items, other.items, strict=True):
                if isinstance(mine, RowValue):
                    if not self._is_param_free(theirs):
                        raise TypeError(
                            "cannot compare a nested row value against bound "
                            "values — PostgreSQL cannot infer a parameter's "
                            "type inside a nested row constructor "
                            "(((a, b), c) = (($1, $2), $3) fails to prepare). "
                            "Compare the columns in one flat row, or against "
                            "another row of columns."
                        )
                    # Recursing is also what checks the nested arity: the
                    # outer count is satisfied by the nested row as a
                    # single element.
                    coerced.append(mine._coerce(theirs))
                else:
                    coerced.append(theirs)
            other = RowValue(tuple(coerced))
        return other

    # The six comparisons mirror _InfixOps' but coerce the right operand
    # first.  __eq__/__ne__ need the override-ignore for the same reason
    # the mixin does: object.__eq__ returns bool, these return Predicate.
    def __eq__(self, other: object) -> Predicate:  # type: ignore[override]
        return Predicate(self, "=", self._coerce(other))

    def __ne__(self, other: object) -> Predicate:  # type: ignore[override]
        return Predicate(self, "!=", self._coerce(other))

    def __lt__(self, other: object) -> Predicate:
        return Predicate(self, "<", self._coerce(other))

    def __gt__(self, other: object) -> Predicate:
        return Predicate(self, ">", self._coerce(other))

    def __le__(self, other: object) -> Predicate:
        return Predicate(self, "<=", self._coerce(other))

    def __ge__(self, other: object) -> Predicate:
        return Predicate(self, ">=", self._coerce(other))

    # Arithmetic is inherited from _InfixOps but meaningless on a row:
    # `(a, b) + $1` is not valid PostgreSQL in any context.  Emitting it
    # silently is the same class of defect as binding a tuple as one
    # parameter, so the whole arithmetic menu — forward and reflected —
    # fails at the seam instead.  Comparison and composition are the only
    # things a row value does.
    def _no_arithmetic(self, *_: object) -> Predicate:
        raise TypeError(
            "a row value only supports comparison (=, !=, <, <=, >, >=) "
            "and IN; arithmetic on a row constructor is not valid SQL"
        )

    __add__ = _no_arithmetic
    __radd__ = _no_arithmetic
    __sub__ = _no_arithmetic
    __rsub__ = _no_arithmetic
    __mul__ = _no_arithmetic
    __rmul__ = _no_arithmetic
    __truediv__ = _no_arithmetic
    __rtruediv__ = _no_arithmetic
    __mod__ = _no_arithmetic
    __rmod__ = _no_arithmetic


def row(*items: Any) -> RowValue:
    """Build a SQL row-value constructor: ``cygnet.row(T.a, T.b)`` → ``(t.a, t.b)``.

    Compare it against a tuple, or against another row::

        cygnet.row(T.tenant_id, T.order_id) == (7, 42)   # (a, b) = ($1, $2)
        cygnet.row(T.a, T.b) == cygnet.row(T2.a, T2.b)   # (a, b) = (c, d)

    Ordering comparisons are the keyset-pagination idiom — one predicate
    that PG can drive from a multi-column index, instead of the
    ``(a > $1) OR (a = $1 AND b > $2)`` expansion::

        .WHERE(cygnet.row(T.created_at, T.id) > (last_ts, last_id))
        .ORDER_BY(T.created_at, T.id)

    Elements may be columns, other expressions, or plain values (which
    become ``$N`` parameters).  A one-element row is allowed — ``(a)`` is
    just ``a`` to PG — so code that builds a row from a key of unknown
    length needs no special case.
    """
    if not items:
        raise TypeError("cygnet.row() requires at least one element")
    return RowValue(items=items)


def in_(left: Any, values: Any) -> Predicate:
    """``left IN (v1, v2, …)`` — membership against an explicit value list.

    The row-value form is the reason this exists: a composite key can't be
    batched with ``= ANY($1)`` (that binds one array), so it needs a
    genuine IN-list::

        cygnet.in_(cygnet.row(T.tenant_id, T.order_id), [(1, 7), (1, 9)])
        # (t.tenant_id, t.order_id) IN (($1, $2), ($3, $4))

    For a *scalar* column, prefer ``T.id == cygnet.arrays.any([...])``:
    it binds the whole list as one array parameter, so the SQL text stays
    constant regardless of batch size and there's no IN-list length
    limit.  ``in_`` is offered for the scalar case too, for when the
    literal IN shape is what you want.

    Trade-off to know for the row form: the rendered SQL text varies with
    the number of values, so PG (and asyncpg's statement cache) re-plans
    once per distinct batch size.  The array-of-composite alternative
    would keep the text constant but needs per-column *SQL* type names,
    which Cygnet doesn't track — it introspects Python types only.

    ``IN (subquery)`` is a different construct (no parameterisation
    needed) and stays with ``cygnet.op(col, "IN", subquery)``.

    Raises TypeError if ``values`` isn't a sequence of values, ValueError
    if it's empty (``IN ()`` is a syntax error in PG) or if any element's
    arity doesn't match a row-value ``left``.
    """
    # Order matters: reject the two shapes that are *iterable but wrong*
    # before materialising, so each gets its own pointed message rather
    # than a confusing downstream failure.
    if hasattr(values, "render_sql"):
        raise TypeError(
            "cygnet.in_() takes a sequence of values, not a subquery or "
            'expression — use cygnet.op(col, "IN", subquery) for IN (SELECT …)'
        )
    if isinstance(values, str | bytes):
        raise TypeError(
            f"cygnet.in_() requires a sequence of values, got "
            f"{type(values).__name__} (which would expand per-character)"
        )
    if not isinstance(values, Sequence):
        # An ordered sequence is required, not merely an iterable.  A dict
        # iterates its KEYS (so `in_(col, {"a": 1})` would silently bind
        # "a" instead of 1), a set has no dependable order (so the emitted
        # SQL text — and therefore PG's plan cache entry — would vary run
        # to run), and a generator would be consumed here.  str/bytes are
        # Sequences but were rejected above.
        if isinstance(values, Iterable):
            raise TypeError(
                f"cygnet.in_() requires an ordered sequence of values, got "
                f"{type(values).__name__} — a set, dict, or generator has no "
                f"dependable order (and a dict would bind its keys); pass a "
                f"list or tuple"
            )
        raise TypeError(
            f"cygnet.in_() requires a sequence of values, got {type(values).__name__}"
        )
    items = list(values)
    if not items:
        # PG has no empty IN-list; emitting a constant FALSE instead would
        # silently turn "I have no keys to look up" into a valid query,
        # which is the class of thing Cygnet fails loudly on.  Callers
        # batching a possibly-empty set should short-circuit themselves.
        raise ValueError("cygnet.in_() requires a non-empty sequence of values")

    if isinstance(left, tuple | list | Mapping | AbstractSet):
        # The natural slip, because `row(T.a, T.b) == (1, 2)` teaches that
        # a bare tuple is an acceptable row.  It is not acceptable *here*:
        # without a RowValue there is nothing to render the columns, so
        # they would be bound as one opaque parameter and the driver would
        # fail far from the cause ("cannot adapt type 'ColumnProxy'").
        raise TypeError(
            f"cygnet.in_(): the left operand is a {type(left).__name__} — "
            f"wrap it in cygnet.row(...) to compare a composite key"
        )

    elements: list[Any] = []
    for i, item in enumerate(items):
        if isinstance(left, RowValue):
            # Reuse the comparison path's coercion rather than re-deriving
            # it: that is what keeps `in_(row, [pair])` and `row == pair`
            # agreeing on identical operands, and it inherits the arity
            # check, the nested-row refusal, and the "unordered iterable"
            # rejection for free.
            try:
                item = left._coerce(item)
            except (TypeError, ValueError) as exc:
                raise type(exc)(f"cygnet.in_(): value at index {i}: {exc}") from None
        if RowValue._contains_none(item):
            # `IN` compares with `=`, and nothing equals NULL: PG returns
            # zero rows for `(a, b) IN ((NULL, 'z'))` even when exactly
            # that row exists.  This is B6/OQ7 on a new surface — the trap
            # the `== None` -> IS NULL rewrite exists to prevent — and
            # there is no rewrite available inside an IN-list, so refuse.
            raise ValueError(
                f"cygnet.in_(): value at index {i} contains None — IN never "
                f"matches NULL, so this would silently match nothing; filter "
                f"the None out, or test for it separately with is_null()"
            )
        elements.append(item)

    # The IN-list is itself a parenthesised comma-joined list, i.e. the
    # same rendering RowValue provides — reuse it rather than adding a
    # second class that renders identically.
    return Predicate(left, "IN", RowValue(items=tuple(elements)))


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
