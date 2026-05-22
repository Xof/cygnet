# predicate.py — Recursive SQL expression trees and supporting sentinels.
#
# A Predicate is a binary tree where leaves are either ColumnProxy references
# (which render as "table.column") or plain Python values (which become $N
# parameters).  Interior nodes carry a SQL operator (=, >, AND, OR, etc.).
#
# The tree is built implicitly via Python operator overloads on ColumnProxy
# (proxy.py) and composed with & (AND) and | (OR).  render_sql() walks the
# tree depth-first, appending parameter values to a shared list and emitting
# positional $N placeholders.  The shared list is what makes parameter
# numbering work correctly across multiple predicates in the same query.
#
# Note: chained .WHERE() calls on a builder are stored as a list and ANDed
# together externally by executor._render_where, NOT by composing a giant
# AND tree inside this module.  So a Predicate's & operator is only invoked
# when the user explicitly writes `(a == 1) & (b == 2)`.  Two semantically
# equivalent forms (one chained .WHERE() per term vs. one .WHERE() with
# & between terms) render to subtly different — but functionally identical —
# SQL.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(eq=False)
class Predicate:
    # `left` and `right` are deliberately typed as Any because they are
    # heterogeneous: either SQLRenderable objects (ColumnProxy, Literal,
    # nested Predicate, PrefixOp, SuffixOp) that render themselves, or
    # plain Python values that become $N bind parameters in
    # _render_operand.  `op` is inlined into the SQL verbatim — no
    # validation, no escaping — so callers constructing Predicates
    # directly (cygnet.op(...)) are trusted to supply a safe operator
    # string.
    #
    # eq=False suppresses the dataclass-generated __eq__, because we
    # override __eq__ below to build a SQL comparison rather than test
    # value equality.  Tests that need to compare Predicate values can
    # use dataclasses.astuple() or compare render_sql() output instead.
    left: Any
    op: str
    right: Any

    # Overloading & and | (not `and`/`or`, which can't be overloaded in Python).
    # Users must parenthesise: (T.a == 1) & (T.b == 2), because & binds
    # tighter than == in Python's operator precedence.  Forgetting the
    # parens — `T.a == 1 & T.b == 2` — parses as `T.a == (1 & T.b) == 2`,
    # which is a chained comparison Python evaluates as boolean `and`,
    # NOT the SQL AND we want.  See the file header for the corresponding
    # `0 < T.col < 10` trap on the ColumnProxy side.
    def __and__(self, other: Predicate) -> Predicate:
        return Predicate(self, "AND", other)

    def __or__(self, other: Predicate) -> Predicate:
        return Predicate(self, "OR", other)

    def __invert__(self) -> Any:
        # ~ is Python's bitwise NOT — overloaded here for SQL negation, the
        # same way & / | stand in for AND / OR.  Returns a PrefixOp which
        # already participates in & / |, so ~(T.x == 1) & (T.y == 2) works.
        # Imported lazily to avoid a circular import (expression.py imports
        # Predicate from this module).
        from .expression import PrefixOp

        return PrefixOp(op="NOT", operand=self)

    # Comparison overloads mirror ColumnProxy / FunctionCall: the result
    # of any infix expression — including non-boolean ones like `data ->>
    # 'key'` — can be compared against a value to form another Predicate.
    # This is what makes (jb.get_text(T.data, 'name') == 'Fred') chainable
    # into WHERE clauses without intermediate variables.
    def __eq__(self, other: object) -> Predicate:  # type: ignore[override]
        return Predicate(self, "=", other)

    def __ne__(self, other: object) -> Predicate:  # type: ignore[override]
        return Predicate(self, "!=", other)

    def __lt__(self, other: object) -> Predicate:
        return Predicate(self, "<", other)

    def __gt__(self, other: object) -> Predicate:
        return Predicate(self, ">", other)

    def __le__(self, other: object) -> Predicate:
        return Predicate(self, "<=", other)

    def __ge__(self, other: object) -> Predicate:
        return Predicate(self, ">=", other)

    # Arithmetic infix.  Predicate is really "any infix expression",
    # not just boolean predicates — chaining `(c.n + 1) * 2` is a
    # common pattern in recursive CTEs and computed SELECT columns.
    def __add__(self, other: object) -> Predicate:
        return Predicate(self, "+", other)

    def __radd__(self, other: object) -> Predicate:
        return Predicate(other, "+", self)

    def __sub__(self, other: object) -> Predicate:
        return Predicate(self, "-", other)

    def __rsub__(self, other: object) -> Predicate:
        return Predicate(other, "-", self)

    def __mul__(self, other: object) -> Predicate:
        return Predicate(self, "*", other)

    def __rmul__(self, other: object) -> Predicate:
        return Predicate(other, "*", self)

    def __truediv__(self, other: object) -> Predicate:
        return Predicate(self, "/", other)

    def __rtruediv__(self, other: object) -> Predicate:
        return Predicate(other, "/", self)

    def __mod__(self, other: object) -> Predicate:
        return Predicate(self, "%", other)

    def __rmod__(self, other: object) -> Predicate:
        return Predicate(other, "%", self)

    # Same dodge ColumnProxy uses: __eq__ no longer returns bool, so
    # the auto-hash would be inconsistent with our equality semantics.
    __hash__ = None  # type: ignore[assignment]

    def render_sql(self, params: list[Any]) -> str:
        # Depth-first, left-before-right walk: the left subtree is fully
        # rendered (and its parameters appended) before the right subtree
        # begins.  Callers that render multiple top-level expressions into
        # the same params list must call them in the order the fragments
        # appear in the final SQL — otherwise $N indexes won't align with
        # the values in params.  executor.py relies on this when stitching
        # SELECT list + WHERE + GROUP BY + ORDER BY together.
        # Logical connectives (AND/OR) wrap their children in parens to
        # preserve grouping.  Comparison operators don't — the operands
        # are either column refs or $N placeholders, which need no parens.
        if self.op in ("AND", "OR"):
            left_sql = f"({self.left.render_sql(params)})"
            right_sql = f"({self.right.render_sql(params)})"
        else:
            left_sql = self._render_operand(self.left, params)
            right_sql = self._render_operand(self.right, params)

        return f"{left_sql} {self.op} {right_sql}"

    @staticmethod
    def _render_operand(value: Any, params: list[Any]) -> str:
        # Duck-typing: anything with render_sql() (ColumnProxy, Literal,
        # PrefixOp, SuffixOp, nested Predicate) renders itself.  Plain
        # Python values become positional parameters.  This is what makes
        # T.id == T2.fk render as "t.id = t2.fk" (no params) while
        # T.id == 42 renders as "t.id = $1" (42 appended to params).
        # Side-effect ordering: params.append() runs AFTER any nested
        # render_sql() calls (which may themselves append).  The returned
        # placeholder index therefore always matches the position of THIS
        # value in the final list, even when sibling subtrees contribute
        # parameters in between.
        if hasattr(value, "render_sql"):
            result: str = value.render_sql(params)
            return result
        params.append(value)
        return f"${len(params)}"


class _All:
    """Sentinel: pass to WHERE() to explicitly allow unrestricted DELETE/UPDATE.

    executor.py checks for this in _check_predicates().  If present alone,
    the WHERE clause is omitted entirely.  If mixed with real predicates,
    a ValueError is raised — combining "all rows" with a filter is
    contradictory and almost certainly a bug.
    """

    pass


# Lowercase `all` to read naturally: WHERE(cygnet.all).
all = _All()


@dataclass(frozen=True)
class Literal:
    """Raw SQL fragment for any expression position. No parameter substitution.

    Intentionally does not touch the params list — the SQL string is emitted
    verbatim.  This is the escape hatch for SQL that Cygnet's expression API
    doesn't cover (e.g., function calls, casts, sub-selects).
    """

    sql: str

    # ORDER_BY opt-out: a Literal's SQL string may already contain ASC/DESC
    # (e.g., cygnet.lit("created_at DESC")), so the executor must not append
    # another direction.  Other renderables — ColumnProxy, op(...) results,
    # SuffixOp — get the direction appended.
    _renders_own_direction = True

    def render_sql(self, params: list[Any]) -> str:
        return self.sql
