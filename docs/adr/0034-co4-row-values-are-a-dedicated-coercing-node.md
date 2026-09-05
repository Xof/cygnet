---
id: 0034
title: CO4. Row values are a dedicated node whose comparisons coerce the right operand
date: 2026-09-05
status: Accepted
summary: cygnet.row builds a RowValue node that overrides the six comparisons to wrap a tuple right-hand side into another RowValue, and the same node renders the IN-list for cygnet.in_.
---

# 0034. CO4. Row values are a dedicated node whose comparisons coerce the right operand

## Context

Cygnet had no way to spell SQL's row constructor, `(a, b)`. That blocks two
things. The near-term one is the composite-primary-key work
(`docs/specs/2026-08-31-composite-primary-keys-design.md`, phase 2 of 4):
a batched lookup on a composite key needs `WHERE (a, b) IN ((…), (…))`,
because the scalar batching idiom `= ANY($1)` binds one array and has no
composite equivalent Cygnet can type. The standalone one is keyset
pagination, where the correct predicate over a multi-column sort is a
lexicographic row comparison — `(a, b) > ($1, $2)` — and the intuitive
element-wise rewrite `(a > $1) AND (b > $2)` is silently wrong (verified
against PG 16: over rows `(1,'x') (1,'y') (2,'x') (2,'y')`, the row form
matches `(2,'x')` and `(2,'y')`; the AND form matches nothing).

The forcing constraint is that `_InfixOps.__eq__` — the shared comparison
menu every expression type inherits — sends its right operand through
`Predicate._render_operand`, which binds any non-renderable value as a
single `$N`. So a naive row node would render `(t.a, t.b) = $1` with a
Python tuple bound as one parameter: accepted by psycopg's tuple adapter
in some shapes, rejected outright in others, and never the row comparison
the caller wrote. Somewhere in the stack the tuple has to be taken apart.

## Decision

Add `RowValue` (`cygnet/expression.py`), a frozen `_InfixOps` dataclass
that renders its items as a parenthesised comma-joined list, each item
either rendering itself or becoming a `$N` — the same duck-typing every
other operand position uses. It overrides the six comparison operators to
run the right operand through `_coerce` first: a `tuple`/`list` becomes
another `RowValue` (so it renders `($1, $2)`), an existing `RowValue`
passes through, and anything else is left for `Predicate` to handle.
`_coerce` checks arity and raises with both lengths named.

The same node renders the `IN` list: `cygnet.in_(left, values)` returns
`Predicate(left, "IN", RowValue(...))`, whose elements are themselves
`RowValue`s when `left` is one. `in_` validates fail-loud — empty
sequence, `str`/`bytes`, non-iterable, a subquery (pointing at
`cygnet.op(col, "IN", subquery)` instead), and per-element arity, naming
the offending index.

Nothing in `executor.py`, `builders.py`, or `predicate.py` changes.

## Alternatives considered

- **Teach `Predicate._render_operand` to expand tuples** — rejected. It
  puts row-awareness in the one function every predicate in the library
  routes through, and it would change the meaning of a bare
  `T.col == (1, 2)` for anyone relying on the driver's tuple adaptation.
  The coercion belongs to the node that knows it is a row.
- **A separate `_InList` node for the `IN` right-hand side** — rejected.
  `(a, b)`, `($1, $2)`, and `(($1,$2), ($3,$4))` are one grammar
  production; a second class would render identically and drift.
- **Emit a constant `FALSE` for an empty `in_` list** — rejected. PG has
  no `IN ()`, and quietly turning "I have no keys to look up" into a
  valid query is the failure mode Cygnet fails loudly on everywhere else
  (S1–S4). Callers batching a possibly-empty set short-circuit themselves.
- **`IN (SELECT * FROM unnest($1::int[], $2::text[]))`** to keep the SQL
  text constant across batch sizes — rejected. It requires per-column
  *SQL* type names; Cygnet introspects Python types only and has nowhere
  to get them.
- **Special-case `row(a, b) == None`** — rejected. `Predicate`'s
  `None` → `IS NULL` rewrite is uniform across every expression type;
  carving out an exception here would be the larger surprise. Documented
  instead (see Consequences).
- **Give `RowValue` `&` / `|` / `~`** — rejected. A bare row is not a
  boolean expression; compare it first and compose the `Predicate`.

## Consequences

- Composite-key batch lookups are now expressible, which is the
  precondition for phase 3 of the composite-PK design (composite foreign
  keys and a composite `follow_many`).
- Keyset pagination gets a first-class, index-drivable idiom instead of a
  hand-written OR-expansion.
- Because the node only implements `render_sql`, every context that
  accepts a renderable — `WHERE`, `HAVING`, `JOIN ON`, `SELECT` lists,
  `ORDER BY`, subqueries — accepts row values with no further work, and
  `$N` numbering stays correct by the existing left-to-right rule.
- **`(a, b) IS NULL` means "every member is null", not "the row is
  null".** A row with one null member does not match. Inherited from the
  uniform rewrite, documented in `THEORY.md` and `README.md`.
- **Plan-cache churn.** An `IN`-list's SQL text varies with the number of
  values, so PG (and asyncpg's statement cache) re-plans once per
  distinct batch size — unlike `= ANY($1)`, whose text is constant. This
  is why the scalar-column recommendation stays `arrays.any`. If a real
  workload hits it, the fix is chunking to a fixed set of batch sizes,
  not a new node.
- `RowValue` is unhashable (`_InfixOps.__hash__ = None`), consistent with
  `ColumnProxy` and `FunctionCall`, so a row in a set or dict key fails
  loudly rather than comparing nonsensically.

## Addendum (2026-09-05)

Adversarial review of the initial implementation found five defects, all in
operand discipline rather than in the design above. The record's decision
stands; these are the refinements it needed.

- **Nested rows are now refused against bound values.** PG cannot infer a
  parameter's type inside a nested row constructor — `((a,b),c) =
  (($1,$2),$3)` fails to prepare with "could not determine data type of
  parameter $1", while the flat `(a,b) = ($1,$2)` prepares with no declared
  types at all. psycopg dumps `str`/`None` as oid 0 (UNKNOWN) and asyncpg
  sends no parameter OIDs, so no driver rescues it. `_coerce` raises instead
  of rendering unexecutable SQL. Nesting against columns binds nothing and
  stays legal — but note it becomes a *composite-type* comparison, in which
  PG compares NULLs as equal, unlike the flat form on the same data.
- **`in_` routes each element through `left._coerce`** rather than
  re-deriving the wrap. The two entry points had disagreed on identical
  operands (`in_` bound a nested tuple as one parameter where `==` expanded
  it) and `in_` skipped the nested arity check entirely.
- **`in_` requires an ordered `Sequence`.** It previously accepted any
  iterable via `list(values)`, so a dict silently bound its **keys** and a
  set produced order-dependent SQL text.
- **`in_` rejects `None` anywhere in a value list.** `IN` compares with `=`,
  so `(a, b) IN ((NULL, 'z'))` matches zero rows even when that row exists —
  B6/OQ7 on a new surface, with no rewrite available inside an IN-list.
- **Arithmetic operators raise.** `RowValue` inherited `+ - * / %` from
  `_InfixOps` and emitted `(a, b) + $1`, which is not valid SQL anywhere.

One trap is documented rather than fixed, for consistency with the uniform
`None` rewrite: `row(a, b) != None` renders `IS NOT NULL`, which is *not* the
complement of `IS NULL` — a half-null row satisfies neither.

The equivalent NULL hazard on the comparison operators (`row(a, b) ==
(None, 'z')` renders `= ($1, $2)` and matches nothing) is **not** addressed
here. It is the same B6 trap on a surface the review did not cover, and
changing comparison semantics is a wider decision than this record made.
