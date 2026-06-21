# Architecture

Cygnet is a small, async, **PostgreSQL-only** ORM that maps plain dataclasses to
tables and keeps SQL visible. The one fact to hold first: there is no query DSL
behind which SQL hides — every query verb builds an AST of **`SQLRenderable`**
nodes that render, in a single left-to-right pass over one shared params list, to
parameterised PG SQL (`$1, $2, …`). Almost everything else falls out of that.

For *why* any of this is shaped the way it is, see [THEORY.md](THEORY.md). For
install/build/test/usage, see [README.md](README.md). Open issues and deferred
features are tracked in [ISSUES.md](ISSUES.md).

## Component map

Authoritative module enumeration. Dependency direction is strictly downward:
`__init__` → `builders` → `executor` → `proxy`/`cte`/`predicate`/`expression` →
`meta` → `annotations`. No cycles. The single deferred import is
`from .builders import …` inside `executor.run_save` (breaks an otherwise-circular
save→builder edge).

| Module | Responsibility | Key symbols |
|---|---|---|
| `__init__.py` | Public API surface; query-verb factories | `Table`, `SELECT`/`INSERT`/`UPDATE`/`DELETE`/`TRUNCATE`, `get`/`save`/`create`/`follow`, `lit`/`op`/`ops`/`exists`, `transaction`, `cte`/`recursive_cte`/`lateral` |
| `annotations.py` | Passive metadata markers, introspected by `meta` | `DBKey`, `AppKey`, `Column`, `ForeignKey`, `@table` |
| `meta.py` | Dataclass → `TableMeta`/`FieldMeta` introspection | `TableMeta` (WeakValueDict-cached per class) |
| `proxy.py` | Attribute access → predicate AST | `TableProxy[T]`, `ColumnProxy[FT]` (per-class singletons; `.AS()` bypasses cache) |
| `predicate.py` | Comparison/predicate AST + the operator-overload menu | `Predicate`, `Literal`, `_All`, **`_InfixOps`** mixin |
| `expression.py` | `SQLRenderable` protocol + expression nodes | `SQLRenderable`, `op`/`ops`, `PrefixOp`/`SuffixOp`, `FunctionCall`, `WindowExpression`, `_Exists`, `exists`/`not_exists` |
| `builders.py` | Fluent, awaitable query builders | `SelectBuilder`, `InsertBuilder`, `UpdateBuilder`, `DeleteBuilder`, `_LockClause` |
| `executor.py` | Render + execute each verb; row→object mapping | `render_*`/`run_*` per verb, hydration |
| `cte.py` | WITH-clause / lateral sources that duck-type `TableProxy` | `CTE`, `RecursiveCTE`, `Lateral` |
| `functions.py` | Curated PG function wrappers | `count`, `sum`, `coalesce`, `row_number`, `lag`/`lead`, … + `fn(name)` escape hatch |
| `jsonb.py` / `arrays.py` / `fts.py` | Operator/function helpers for JSONB, arrays, full-text | `->`/`->>`/`@>`, `&&`/`ANY`/`ALL`, `to_tsvector`/`@@`/`ts_rank` |
| `psycopg_db.py` | Reference psycopg3 adapter | `PsycopgDB` (the **only** module that imports psycopg; gated behind `[psycopg]` extra) |
| `stubs.py` | `python -m cygnet.stubs` codegen for IDE autocomplete | — |
| `py.typed` | PEP 561 typed-library marker | — |

Tests: `tests/` unit suite against `tests/conftest.py:FakeDB` (no DB);
`tests/integration/` real-PG round-trips (CI matrix PG 14–18); `bench/` advisory
pytest-benchmark + cross-ORM comparison.

## Invariants

Load-bearing rules. THEORY.md §"Invariants, and why they hold" supplies the *why*;
this is the enumeration.

- **`UPDATE` and `DELETE` must call `.WHERE()`.** Breaks: raises `ValueError` at render *and* `.sql()` time. Opt out of the rail with `WHERE(cygnet.all)`; mixing `cygnet.all` with a real predicate also raises.
- **`ColumnProxy.__eq__` (and `__ne__`/`__lt__`/arithmetic) return a `Predicate`, not a `bool`; `__hash__` is `None`.** Breaks: a proxy comparison used in a boolean/`if` context is always truthy (it's an AST node) — a silent logic bug, not an error. Proxies are unhashable on purpose (can't go in a set/dict key).
- **One shared `params: list` threads through the whole render, numbered monotonically in document order (`$N = len(params)` after append).** Breaks: every renderable must append in left-to-right textual order; any out-of-order or two-pass rendering corrupts `$N` numbering and the adapter's `$N`→`%s` translation.
- **Anything in a WHERE/SELECT-list/ORDER BY/HAVING/SET-RHS position must implement `render_sql(self, params) -> str`.** Breaks: non-renderables don't compose; this protocol is the only contract the executor relies on.
- **`TableProxy`/`TableMeta` are per-class singletons (WeakValueDictionary); code compares them by identity (`b._table is X`).** Breaks: a non-singleton proxy fails identity checks in the executor. `.AS(alias)` deliberately returns a *fresh* proxy (self-joins need two); the canonical `Table(cls)` stays singleton.
- **Exactly one `DBKey` or `AppKey` field per model.** Breaks: `meta._introspect` raises; composite PKs are unsupported by design.
- **`DBKey` + `frozen=True` is rejected at introspection time.** Breaks: post-INSERT the executor `setattr`s the generated PK; a frozen instance would raise `FrozenInstanceError` deep in insert — caught early so the error names the model.
- **`AppKey` + `None` at INSERT raises; empty `UPDATE … SET` raises; unknown `SET`/INSERT/`DO UPDATE` kwargs raise.** Breaks: these are anti-silent-no-op rails — a typo'd field name must fail loudly, not emit a no-op.
- **The `db` object satisfies a duck-typed protocol — `execute`, `execute_one`, optional `stream`, and a `_in_transaction: bool` flag — and is per-task.** Breaks: `_in_transaction` is per-`db`-instance, not task-local; sharing one connection across `asyncio` tasks corrupts SAVEPOINT nesting.
- **Core never imports a driver; only `psycopg_db.py` imports psycopg.** Breaks: pulling psycopg into core contradicts the bring-your-own-adapter design and the driver-free core install.

## Landmines

Non-obvious things a cold worker gets wrong. Each prevents a specific wrong action.

- **`cygnet.lit(sql)` is raw, trusted, and *still* passes through the adapter's `$N`→`%s` regex.** A literal containing `$1` (or a `%`) gets rewritten. No parameter substitution happens inside `lit()` — it is a SQL-injection surface; never build it from untrusted input.
- **Awaiting a builder twice re-renders and re-executes it.** Builders hold no rendered SQL/params between calls — intentional, so `.sql()`-then-`await` is safe, but a reused builder is a fresh query each time.
- **`save()` does NOT refresh non-PK columns on the upsert branch.** `INSERT … ON CONFLICT DO UPDATE` emits no `RETURNING`; the in-memory object diverges from the row if the table has triggers / generated columns / `DEFAULT` on update. The *fresh-INSERT* branch does refresh. (Deferred tradeoff; OQ1 in the ADR.)
- **`GROUP_BY` requires explicit columns: `SELECT(db, col1, …)`.** Bare `SELECT(db)` + `GROUP_BY` raises `ValueError`.
- **Window-frame strings are interpolated verbatim** (e.g. `ROWS BETWEEN …`) — trusted, not parameterised. Another injection surface.
- **`CTE`/`Lateral` duck-type `TableProxy`** by stamping `ColumnProxy` attrs on themselves with `# type: ignore`; they are not yet a formal `TableSourceProtocol` (OQ4). Treat `_sql_name`/`_meta`/`_alias` as the surface the executor reads.
- **`_Exists` is dedicated, not `PrefixOp("EXISTS", …)`** — because `SelectBuilder.render_sql` already wraps itself in parens, reusing `PrefixOp` would emit `EXISTS ((SELECT …))`. `~exists(b)` toggles `EXISTS ↔ NOT EXISTS` rather than wrapping in `NOT (…)`.

## Flow

`cygnet.SELECT(db, …)` returns a `SelectBuilder`; fluent methods (`FROM`, `WHERE`,
`JOIN`, `ORDER_BY`, …) mutate it and return `self`. The terminal action is
`await builder` → `__await__` → `_execute()` → `Executor.render_<verb>(b, params)`
(single pass, document order) → `db.execute(sql, params)` → row tuples →
`Executor` maps rows back to dataclass instances (single-source) or
`(left, right, …)` tuples (multi-source JOIN; `None` on a side signals an
outer-join miss). `.sql()` renders without executing; `.stream()` async-iterates a
server-side portal cursor instead of buffering. A `SelectBuilder` is itself a
`SQLRenderable`, so it drops into any expression position as a subquery with no
wrapper.

## Where to change X

- **Add a curated PG function** → `functions.py` (or reach `fn("name")` at call site).
- **Add a JSONB / array / FTS operator** → `jsonb.py` / `arrays.py` / `fts.py`.
- **Add or alter a SQL verb / clause** → fluent method in `builders.py` + render branch in `executor.py`.
- **Change row→object hydration** → `executor.py` mapping path.
- **Add a new expression node** → implement `render_sql(self, params)`; it composes everywhere automatically (the operator-overload menu is the `_InfixOps` mixin in `predicate.py`).
- **Add PK/FK/column semantics** → marker in `annotations.py` + introspection in `meta.py`.
- **Add a `db` adapter** → satisfy the four-method protocol; `psycopg_db.py` is the reference.

---

For the reasoning behind all of the above, see [THEORY.md](THEORY.md).
