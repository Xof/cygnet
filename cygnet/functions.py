# functions.py — Curated wrappers around common PostgreSQL functions.
#
# Built on top of cygnet.fn(name) (expression.py).  Each name here is a
# thin convenience around a fn() call; the only "logic" is COUNT's special
# empty-args form (COUNT(*) rather than COUNT()).  The set is hand-picked,
# not auto-generated from pg_proc — a maintained subset of the most common
# functions, accepting that a few names (sum, min, max) collide with
# Python builtins for downstream code that does
# `from cygnet.functions import min, max`.
#
# Usage:
#   import cygnet.functions as f
#   f.count()                # COUNT(*)
#   f.count(T.id)            # COUNT(accounts.id)
#   f.sum(T.amount)          # SUM(orders.amount)
#   f.coalesce(T.email, T.name)  # COALESCE(...)
#
# Anything not in this module is still reachable via cygnet.fn('name').

from __future__ import annotations

from typing import Any

from .expression import FunctionCall, fn
from .predicate import Literal

# Reusable Literal('*') for COUNT(*).  Kept module-private — users who need
# `*` elsewhere should construct cygnet.lit('*') explicitly.
_STAR = Literal(sql="*")


def count(*args: Any) -> FunctionCall:
    """COUNT(...) — empty args produces COUNT(*).

    The empty-args special case matches the canonical SQL idiom and saves
    callers from spelling cygnet.lit('*') every time they want a row count.
    All other functions in this module use the generic fn(name) factory
    directly, so their behaviour follows fn's defaults (no args -> empty
    parens).
    """
    if not args:
        return fn("count")(_STAR)
    return fn("count")(*args)


# Single-argument aggregates.  These shadow Python builtins (sum, min, max)
# when star-imported; that's a deliberate trade-off documented in the
# original enhancement plan.  Use `import cygnet.functions as f` to avoid
# the clash.
sum = fn("sum")
avg = fn("avg")
min = fn("min")
max = fn("max")

# Variadic / null-handling
coalesce = fn("coalesce")
nullif = fn("nullif")

# Time / now
now = fn("now")
current_timestamp = fn("current_timestamp")

# Array / aggregation
array_agg = fn("array_agg")
string_agg = fn("string_agg")
json_agg = fn("json_agg")
jsonb_agg = fn("jsonb_agg")

# String
lower = fn("lower")
upper = fn("upper")
length = fn("length")
trim = fn("trim")

# Math
abs = fn("abs")
round = fn("round")
ceil = fn("ceil")
floor = fn("floor")

# Window functions.  These are ordinary FunctionCall factories — the OVER
# clause is added via FunctionCall.OVER(...).  Listed here for
# discoverability alongside the aggregates; pair any of them with .OVER()
# to get a real window expression:
#     row_number().OVER(partition_by=[T.dept], order_by=[T.salary])
row_number = fn("row_number")
rank = fn("rank")
dense_rank = fn("dense_rank")
percent_rank = fn("percent_rank")
cume_dist = fn("cume_dist")
ntile = fn("ntile")
lag = fn("lag")
lead = fn("lead")
first_value = fn("first_value")
last_value = fn("last_value")
nth_value = fn("nth_value")
