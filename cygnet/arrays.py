# arrays.py — Curated helpers for PostgreSQL array operators and functions.
#
# Mix of operator helpers (built on cygnet.op) and function helpers
# (built on cygnet.fn).  ANY/ALL deserve special mention: they're not
# standalone predicates — they appear on the right side of a comparison,
# as in `value = ANY(arr_col)`.  The any() / all() helpers below return
# FunctionCall expressions; pair them with `==` / `!=` / etc. or wrap
# with cygnet.op for non-standard comparisons.
#
# Usage:
#   import cygnet.arrays as arr
#   .WHERE(arr.contains(T.tags, ["python", "sql"]))
#   .WHERE(T.id == arr.any(other_table.allowed_ids))
#   .WHERE(arr.length(T.items) > 0)

from __future__ import annotations

from typing import Any

from .expression import FunctionCall, fn, op
from .predicate import Predicate


def contains(a: Any, b: Any) -> Predicate:
    """`a @> b` — does array `a` contain every element of array `b`?"""
    return op(a, "@>", b)


def contained_by(a: Any, b: Any) -> Predicate:
    """`a <@ b` — is array `a` contained by array `b`?"""
    return op(a, "<@", b)


def overlaps(a: Any, b: Any) -> Predicate:
    """`a && b` — do the arrays share any element?"""
    return op(a, "&&", b)


def concat(a: Any, b: Any) -> Predicate:
    """`a || b` — array concatenation."""
    return op(a, "||", b)


def any(col: Any) -> FunctionCall:  # noqa: A001 — shadows builtin intentionally
    """`ANY(col)` — for use on the right side of a comparison.

    Example: `WHERE T.id = arr.any(other_col)` renders as
    `T.id = ANY(other_col)`.  Returns a FunctionCall, not a Predicate;
    pair with `==`, `!=`, or another comparison to produce a WHERE-able
    expression.
    """
    return fn("ANY")(col)


def all(col: Any) -> FunctionCall:  # noqa: A001 — shadows builtin intentionally
    """`ALL(col)` — like any(), but every element must satisfy the comparison.

    Example: `WHERE T.score > arr.all(thresholds)` renders as
    `T.score > ALL(thresholds)`.
    """
    return fn("ALL")(col)


def length(col: Any, dim: int = 1) -> FunctionCall:
    """`array_length(col, dim)` — length along the given dimension (1-based).

    Default dim=1 covers the common case of a 1-D array.  Returns NULL
    if the array is empty or the dimension doesn't exist — that's PG's
    semantics, not Cygnet's.
    """
    return fn("array_length")(col, dim)


def cardinality(col: Any) -> FunctionCall:
    """`cardinality(col)` — total element count regardless of dimensions.

    Unlike length(), returns 0 (not NULL) for empty arrays.
    """
    return fn("cardinality")(col)


def unnest(col: Any) -> FunctionCall:
    """`unnest(col)` — expand an array into rows.

    Mostly useful in FROM (lateral) or SELECT contexts; less common in
    WHERE.  Returns a FunctionCall that renders as `unnest(col)`.
    """
    return fn("unnest")(col)


def array_agg(col: Any) -> FunctionCall:
    """`array_agg(col)` — collect into an array (aggregate).

    Re-exported here from cygnet.functions for discoverability alongside
    the other array helpers.
    """
    return fn("array_agg")(col)
