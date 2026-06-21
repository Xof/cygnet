# Theory of Operation

This document is for an engineer picking up Cygnet to change it. It explains how
the pieces fit and *why* they are shaped this way — the reasoning that the code
itself does not record. For the bare module map and the enumerated invariants, see
[ARCHITECTURE.md](ARCHITECTURE.md); this file cross-references that enumeration
rather than repeating it.

This document synthesises the project's decision record and cites each decision by
the id used in that record — the foundation/API/safety/parameter/DB/caching/
composition/testing entries `F1`–`T4`, the "Trade-offs explicitly deferred" list,
and the open questions `OQ1`–`OQ4` — so the narrative stays self-contained. (That
record, the project's Architecture Decision Record, is maintained as a local
internal artifact and is not checked into the repo; nothing here depends on having
it.) Per-feature design notes *are* in the repo at
[`docs/superpowers/specs/`](docs/superpowers/specs/), and the running issue list is
[ISSUES.md](ISSUES.md).

## Orientation

Cygnet calls itself "the littlest ORM." Its design is deliberately *adjacent* to
SQLAlchemy and Django rather than competitive: where those hide SQL behind a query
DSL or a manager API, Cygnet keeps SQL on the surface. The intended reader of a
Cygnet call site already knows the SQL they want; the library's job is the
mechanical glue — typed column references, parameter binding, row-to-object
hydration, transactions, `ON CONFLICT`, `RETURNING` — and nothing more. It is not a
query optimiser, a migration framework, a multi-dialect abstraction, a lazy-loading
proxy, or a unit-of-work session. That scope *narrowness is the product*, and most
of the surprising-looking choices below are it being honest about that (ADR
"Context").

Two big ideas carry the whole codebase, and if you internalise only these you can
place almost any new code correctly:

1. **A query is an AST of renderable nodes, not a string being concatenated.**
   `T.id == 1` does not evaluate to a bool — it builds a `Predicate`. Every node
   (column refs, predicates, function calls, window expressions, even a whole
   `SelectBuilder`) implements one method, `render_sql(params) -> str`, and renders
   itself by *appending* its values to a single shared `params` list and returning
   the SQL fragment with `$N` placeholders. Composition is free: anything that
   renders drops into any clause position with no wrapper.

2. **The model is a plain dataclass; Cygnet reaches into it, never the reverse.**
   There is no base class, no metaclass, no `objects` manager. Metadata (PK, FK,
   column-name overrides) rides inside `Annotated[...]` hints, and `meta.py`
   introspects it. Using Cygnet is opt-in *per call site*, not per model.

Everything else — the safety rails, the duck-typed `db`, the singleton proxies, the
PG-only stance — hangs off the consequences of those two ideas.

## How it's structured

The module enumeration and the strict downward dependency direction are in
ARCHITECTURE.md's component map; here is how the layers actually relate.

At the bottom, **`annotations.py`** defines passive marker objects (`DBKey`,
`AppKey`, `Column`, `ForeignKey`) that do nothing but sit inside `Annotated[]`.
**`meta.py`** is the one place that reads them, turning a dataclass into a
`TableMeta` (a list of `FieldMeta`). Everything upward treats `TableMeta` as the
description of a table.

**`proxy.py`** is the bridge from Python syntax to the AST. `Table(MyModel)` returns
a `TableProxy`; attribute access (`T.col`) returns a `ColumnProxy`. The proxies are
where the operator-overloading trick lives: comparing or doing arithmetic on a
`ColumnProxy` produces a node in **`predicate.py`** (`Predicate`) or
**`expression.py`** (`FunctionCall`, `WindowExpression`, …). Those overloads are
not duplicated per class — they are collected in the **`_InfixOps`** mixin
(`predicate.py`) and mixed into `ColumnProxy`, `Predicate`, `FunctionCall`, and
`WindowExpression`, so the same `col + 1`, `a == b`, `x >= y` work uniformly
wherever they appear in the tree. (`_InfixOps` was extracted from the previously
scattered per-class overloads; the consolidation is recent — commit "extract shared
infix-operator menu into `_InfixOps` mixin".)

**`expression.py`** owns the `SQLRenderable` protocol itself and the open-ended
expression nodes; **`functions.py`**, **`jsonb.py`**, **`arrays.py`**, and
**`fts.py`** are thin, curated catalogues built on top of it (each helper just
constructs a renderable). They are separate files purely for discoverability — none
holds special executor knowledge.

**`builders.py`** is the fluent layer the user actually touches: `SelectBuilder` and
friends accumulate clause state and return `self`. They are intentionally dumb about
SQL — they hold *intent* (which tables, which predicates, which columns), and hand
that to **`executor.py`**, which is the only component that knows how each verb
renders and how a result row maps back to objects. The split matters: a builder can
be inspected (`.sql()`), re-rendered, or embedded as a subquery without ever
executing, because rendering is a separate, stateless pass owned by the executor.

**`cte.py`** is the odd one out: `CTE`, `RecursiveCTE`, and `Lateral` are not
`TableProxy` subclasses but *duck-type* enough of its surface
(`_sql_name`, `_meta`, `_alias`, plus stamped `ColumnProxy` attributes) that the
executor renders them through the same `FROM`/`JOIN` paths as a real table.

**`psycopg_db.py`** sits outside the core entirely — it is the reference adapter and
the sole importer of psycopg, kept behind an optional extra so the core install
pulls no driver.

## Core ideas

**The AST + single shared params list.** This is the spine. Two-pass rendering
(collect params, then format) would force every node to know its eventual `$N`
position in advance. Instead there is one pass and one list: each `render_sql`
appends its values and reads `$N = len(params)` locally, and correct global
numbering falls out *for free* as long as everyone renders in document order (ADR
`A5`, `P2`). This is why "render left-to-right" is an invariant and not a style
preference — a subquery in the SELECT list and another in the WHERE clause get
coherent numbering only because their params enter the list in textual order.

**Operator overloading to build predicates.** `T.id == 1` reading as a SQL equality
is the entire ergonomic premise, so `__eq__` (and the rest of the comparison and
arithmetic operators) are overridden to return AST nodes (ADR `A4`). The unavoidable
cost is that `ColumnProxy.__hash__` must be set to `None`: Python derives `__hash__`
from `__eq__`, and an `__eq__` that returns a `Predicate` no longer satisfies the
hash/equality contract. Making proxies unhashable is the safe failure — it turns
"proxy used as a dict key or set member" into an immediate `TypeError` instead of
nonsense. The same overload set, via `_InfixOps`, covers the rest of the expression
tree.

**The builder is itself renderable.** `SelectBuilder.render_sql` delegates to the
executor and wraps the result in parentheses (ADR `A6`). That single decision is why
there is no `subquery()` wrapper, no special-case verbs for `EXISTS`/`IN`/scalar
subqueries: once a builder renders like any other node, every expression context
"just works". The parens-on-self rule is what keeps downstream contexts from having
to parenthesise — and it is exactly why `_Exists` is a dedicated node rather than a
reuse of `PrefixOp` (which would add a *second* pair of parens; ADR `CO2`).

**The duck-typed `db`.** Cygnet's core never imports a driver. The `db` argument
just has to provide `execute`, `execute_one`, an optional `stream`, and an
`_in_transaction` flag (ADR `D1`). Connection management, pooling, and retry are the
caller's concern because they vary by deployment and are not the ORM's problem. This
is also what makes the unit suite possible: `FakeDB` is a full, legitimate adapter
that happens to record SQL instead of talking to Postgres.

**Singleton proxies with identity semantics.** `TableMeta` and `TableProxy` are
cached per class in a `WeakValueDictionary` so that `Table(M)` is `Table(M)` — the
executor relies on `b._table is X` identity in places (ADR `C1`). The cache is
*weak* so dynamically generated model classes (codegen, tests) don't pin memory.
`.AS()` is the deliberate exception: self-joins need two distinct proxies for one
table, so aliased proxies are constructed fresh and bypass the cache, leaving the
canonical singleton untouched (ADR `C2`).

## Design decisions and tradeoffs

The full record is the ADR; this is the synthesis a new maintainer needs, with ids
to chase for detail.

- **Plain dataclasses, no base class (`F1`).** Subclassing a framework type couples
  the model to Cygnet at import and breaks its use in serialisation/validation/pure
  tests. The accepted cost: a model can't carry `db`-aware helper methods — that
  logic lives in the user's service layer, considered a feature.
- **`Annotated[]` for metadata (`F2`).** Uses the type system's official escape
  hatch instead of inventing subscriptable generics; the cost is verbosity
  (`Annotated[int, DBKey]` rather than `DBKey[int]`), accepted to avoid custom magic.
- **PostgreSQL only, forever (`F3`).** Not a "MySQL later" placeholder. Multi-dialect
  layers force lowest-common-denominator features; the target user chose Postgres
  *for* `RETURNING`, `ON CONFLICT`, `DISTINCT ON`, `LATERAL`, JSONB. Hiding them
  would defeat the purpose.
- **Async only (`F4`).** Dual sync/async APIs double the surface and test matrix
  (SQLAlchemy's path). Single-mode keeps the codebase small; sync users adapt via
  `asyncio.run`/`anyio`.
- **`$N` placeholders, not `%s` (`P1`).** Rendered SQL uses libpq-native `$1, $2`;
  the psycopg adapter translates to `%s` at the edge with a regex. Chosen for
  readability in logs and parity with `EXPLAIN`. The cost is a real footgun — see
  "Where the bodies are buried".
- **Safety rails that fail loudly (`S1`–`S4`).** `UPDATE`/`DELETE` without `WHERE`
  raises; `AppKey`+`None` raises; empty `SET` raises; unknown kwargs raise. The
  unifying principle: a *silent no-op is the dangerous kind of safety rail* because
  it masks bugs. Every rail here converts a likely mistake into an exception. The
  `WHERE(cygnet.all)` opt-out exists so "all rows" is a deliberate two-word act.
- **psycopg is an optional extra (`D2`).** Since core never imports psycopg outside
  `psycopg_db.py`, forcing it into the base install would contradict `D1`. The extra
  keeps the core driver-free.
- **Transactions are an async context manager, not a session object (`D3`).**
  `async with transaction(db)` issues `BEGIN`/`COMMIT`; nested uses promote to
  `SAVEPOINT`/`RELEASE` by checking `_in_transaction` at runtime. No new "unit of
  work" type is introduced. The limitation this buys is real — see below.
- **Testing strategy (`T1`–`T4`).** Unit tests assert on generated SQL + params via
  `FakeDB` (test Cygnet, not Postgres); integration tests run real round-trips
  across the PG 14–18 matrix (ON CONFLICT semantics, RETURNING shape, jsonb
  adapters, lock contention can only be checked against a real planner); benchmarks
  are advisory and never block merge (runner noise would create false positives);
  cross-ORM comparison exists so "fast" has a referent.

Decisions consciously *deferred* (not bugs — ADR "Trade-offs explicitly deferred"):
per-field IDE autocomplete on `T.col` (needs a mypy plugin; `python -m cygnet.stubs`
is the stopgap), composite primary keys, per-branch set-op `ORDER BY`, and the
`save()` upsert-refresh gap. The live versions of these are tracked as `OQ1`–`OQ4`
and in [ISSUES.md](ISSUES.md).

## Invariants, and why they hold

The enumerated list with "what breaks" clauses is in
[ARCHITECTURE.md](ARCHITECTURE.md) §Invariants. The reasoning behind the
non-obvious ones:

- **Document-order rendering.** Holds because `$N` is computed as `len(params)`
  *after* appending, with one list shared by every node. Nothing assigns positions
  ahead of time, so the only way numbering stays correct is if every node renders in
  textual order — which the executor guarantees by walking clauses left to right. A
  change that renders a later clause's params before an earlier one's silently
  produces wrong-but-valid SQL.
- **`__hash__ = None` on proxies.** Holds because the operator overloads broke the
  hash/equality contract; unhashability is the *chosen* consequence, not an
  oversight. Re-enabling `__hash__` to "fix" a `TypeError` would reintroduce the
  silent-misbehaviour class it exists to prevent.
- **`.AS()` bypasses the singleton cache.** Holds because identity comparison
  (`is`) is load-bearing elsewhere, so the canonical proxy must stay unique — but a
  self-join needs two proxies for one table, so aliases *must* be fresh. The two
  requirements are reconciled by making exactly the aliased path non-cached.
- **`_in_transaction` is per-`db`, not task-local.** This is why sharing one
  connection across `asyncio` tasks corrupts SAVEPOINT nesting: the flag is a single
  instance attribute, so concurrent tasks racing on it mis-detect nesting depth.
  One-connection-per-task is the only safe pattern (and is required by psycopg
  connections regardless). The invariant holds only under that usage (ADR `D3`).

## Where the bodies are buried

The honest section — sharp edges, intentional-looking-bugs, and real debt.

- **`lit()` is a triple hazard.** It is raw (no parameterisation), trusted (never
  build it from user input — SQL injection), *and* its text still flows through the
  adapter's `$N`→`%s` regex, so a literal containing `$1` or `%` gets rewritten
  underneath you (ADR `P1` footgun; `OQ3`). Window-frame strings share the
  "interpolated verbatim, trusted" property.
- **`save()` doesn't refresh on upsert.** The `ON CONFLICT DO UPDATE` path emits no
  `RETURNING`, so triggers / generated columns / `DEFAULT` on update leave the
  in-memory object stale — while the fresh-INSERT path *does* refresh. This
  inconsistency is deliberate-for-now, not a bug you should "fix" without reading
  `OQ1` / ISSUES.md first; someone may be relying on the cheaper upsert.
- **The boolean-context proxy trap.** `if T.a == T.b:` does not compare anything —
  it's a truthy `Predicate`. There is no way to make this raise without losing the
  overloading premise, so it is a permanent sharp edge to watch for in review.
- **CTE/Lateral duck-typing is unfinished.** `cte.py` stamps `ColumnProxy`
  attributes onto itself with `# type: ignore`. The intended end state is a real
  `TableSourceProtocol` (`OQ4`); until then the `type: ignore`s are the tell that
  the surface is informal, and changing what the executor reads from a table source
  means changing both `proxy.py` and `cte.py` in lockstep.
- **The `$N`→`%s` translation assumes ordered consumption.** psycopg consumes params
  in list order, which matches the order Cygnet appends them; a hypothetical
  out-of-order render (`$2` before `$1`) would break it. The left-to-right invariant
  is what keeps this safe — the translator has no SQL awareness of its own.

## Making common changes

Grounded in the real structure (signatures are in the code — this is the *shape*):

- **Add a curated PG function.** Add a wrapper in `functions.py` that returns a
  `FunctionCall` (or use `fn("name")` ad hoc). It composes everywhere automatically
  because `FunctionCall` is a `SQLRenderable` and mixes in `_InfixOps`.
- **Add a JSONB / array / FTS operator.** Same pattern in `jsonb.py` / `arrays.py` /
  `fts.py` — construct an `op(...)`/`ops(...)` renderable; no executor change needed.
- **Add a new expression node.** Implement `render_sql(self, params)` (append values,
  return the fragment with `$N`). That is the *entire* contract — it then works in
  every clause position. Mix in `_InfixOps` if it should support `==`/arithmetic.
- **Add or change a SQL verb or clause.** Two edits in lockstep: a fluent method on
  the relevant builder in `builders.py` (store intent, return `self`) and a render
  branch in `executor.py`. Keep the builder ignorant of SQL; put the SQL in the
  executor.
- **Change row→object hydration.** `executor.py` mapping path only. Remember the
  multi-source JOIN contract: a tuple per row, `None` on a side meaning an
  outer-join miss (detected via the PK column, falling back to all-NULL).
- **Add a `db` adapter.** Satisfy the four-method protocol (ARCHITECTURE §Invariants);
  copy `psycopg_db.py` as the template. If you support `stream`, return an async
  iterator over a server-side cursor.
- **Add PK/FK/column semantics.** A passive marker in `annotations.py` plus the
  `isinstance` branch that reads it in `meta._introspect`. Keep markers passive —
  they must not mutate the dataclass.

---

When this document and the code disagree, the code is authoritative and this file is
stale — fix it. When this document and the decision record (the local ADR) disagree,
the ADR is the older artifact: reconcile deliberately rather than assuming either is
right.
