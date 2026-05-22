# CLAUDE.md — Cygnet ORM

## What is this?

Cygnet is a minimal async Python ORM for PostgreSQL. It maps dataclasses to tables using `Annotated` type hints — no base classes, no schema DSL. Queries read like SQL: `await cygnet.SELECT(db).FROM(T).WHERE(T.col == val)`.

## Commands

```bash
just bootstrap          # Create venv + install dev deps
just test               # Unit tests (no database needed)
just test-integration   # Integration tests (needs CYGNET_TEST_DSN)
just test-all           # Spins up Docker PG, runs everything, tears down
just check              # fmt-check + lint + typecheck + unit tests (pre-push gate)
just lint               # ruff check cygnet tests
just fmt                # ruff format cygnet tests
just typecheck          # mypy cygnet (strict mode)
```

## Architecture

```
cygnet/
  expression.py — SQLRenderable protocol: the contract for any SQL expression
  __init__.py    — Public API: Table(), SELECT/INSERT/UPDATE(), get(), save(), transaction
  annotations.py — DBKey, AppKey, Column(), @table decorator
  meta.py        — TableMeta introspects dataclass → FieldMeta list (WeakValueDict-cached)
  proxy.py       — TableProxy / ColumnProxy: attribute access builds Predicates
  predicate.py   — Predicate tree: renders to parameterised SQL ($1, $2, ...)
  builders.py    — SelectBuilder, InsertBuilder, UpdateBuilder, DeleteBuilder (fluent, awaitable)
  executor.py    — Executor: renders SQL from builders, maps rows back to dataclass instances
  functions.py   — Curated wrappers around common PG functions (count, sum, coalesce, …)
  jsonb.py       — JSONB operator helpers (->, ->>, @>, ?, …)
  arrays.py      — Array operator/function helpers (@>, &&, ANY, ALL, array_length, …)
  fts.py         — Full-text search helpers (@@, to_tsvector, websearch_to_tsquery, ts_rank, …)
  cte.py         — CTE class for WITH-clause subqueries; duck-types TableProxy
  psycopg_db.py  — Reference psycopg3 adapter; the only place Cygnet itself imports psycopg
  stubs.py       — `python -m cygnet.stubs` codegen for IDE autocomplete on TableProxy attrs
```

## Key patterns

- **Models are plain dataclasses.** Annotate PK with `Annotated[int, DBKey]` (DB-assigned) or `Annotated[str, AppKey]` (app-assigned). Override column name with `Column("col_name")`. Override table name with `@cygnet.table("name")`.
- **TableProxy / ColumnProxy.** `T = cygnet.Table(MyModel)` creates a proxy; `T.col == val` returns a `Predicate`, not a bool (`__eq__` is overridden). Proxies are WeakValueDict-cached by class. `TableProxy[T]` and `ColumnProxy[FT]` are generic for explicit annotations, but `T.col` resolves as `ColumnProxy` without per-field FT inference — full IDE autocomplete on `T.col` would need a mypy plugin (deferred).
- **Aliasing — `T.AS("alias")`.** Returns an aliased view of the table proxy. Renders as `tablename AS alias` in FROM/JOIN, and column refs use `alias.col`. Required for self-joins (same table referenced twice in one query). Aliased proxies bypass the singleton cache so each `.AS()` call yields a fresh proxy; the canonical `Table(cls)` proxy is unaffected.
- **Builders are awaitable.** `__await__` delegates to `_execute()` which calls `Executor`. No need for `.fetch()`.
- **`.sql()` on builders.** `builder.sql()` returns `(sql_string, params_list)` without executing the query. Available on all four builders (`SELECT`, `INSERT`, `UPDATE`, `DELETE`). Enforces the same validation as execution (e.g., UPDATE/DELETE require WHERE).
- **`$N` parameter style** — SQL uses positional `$1, $2, ...` placeholders (psycopg / libpq native).
- **Predicate requirement.** `DELETE` and `UPDATE` must have a `.WHERE()` clause. Use `WHERE(cygnet.all)` to explicitly affect all rows. `SELECT` has no such requirement.
- **`cygnet.lit(sql)`** — raw SQL literal for any expression position (SELECT, WHERE, GROUP BY, ORDER BY). No parameter substitution. Use when Cygnet's predicate API doesn't cover your case.
- **Expression protocol.** Any type with a `render_sql(self, params: list[Any]) -> str` method can appear where a `ColumnProxy` goes (SELECT columns, WHERE, GROUP BY, ORDER BY). `ColumnProxy`, `Literal`, and `Predicate` all satisfy `SQLRenderable` (defined in `expression.py`).
- **`cygnet.op()`** — custom operators. 3-arg infix: `op(T.col, 'ILIKE', val)`. 2-arg prefix: `op('NOT', expr)`. 1-arg pre-created: `ILIKE = op('ILIKE')` returns a reusable callable.
- **`cygnet.ops()`** — suffix operators: `ops(T.col, 'IS NULL')`.
- **`cygnet.is_null(col)` / `cygnet.is_not_null(col)`** — convenience for IS NULL / IS NOT NULL.
- **`ForeignKey(TargetClass)`** — annotation declaring a foreign key. Always targets the PK of the referenced class. Validated at introspection time: target must be a dataclass with a PK, types must match, field can't be both PK and FK.
- **`cygnet.follow(db, obj, T.fk_col)`** — loads the related object a FK points to. Returns `None` if FK value is `None` or no matching row exists.
- **`FOLLOW(T.fk_col)` / `LEFT_FOLLOW(T.fk_col)`** — builder methods on `SelectBuilder`. Syntactic sugar for `JOIN` / `LEFT_JOIN` with auto-generated ON condition from FK metadata. Returns tuples like manual JOINs.
- **JOIN family.** `JOIN` (INNER), `LEFT_JOIN`, `RIGHT_JOIN`, `FULL_JOIN` on `SelectBuilder`. Row mapping returns `(left_obj_or_None, right_obj_or_None, …)` tuples — `None` on a side signals an outer-join miss (LEFT misses on the right, RIGHT misses on the left, FULL misses on either; miss detection looks at the PK column when present, falls back to all-NULL otherwise). INNER never produces `None`.
- **`cygnet.create(db, obj)`** — INSERT without ON CONFLICT. Duplicate keys raise from the database. Returns the object with PK populated.
- **`cygnet.TRUNCATE(db, *tables, cascade=False)`** — truncates one or more tables. No builder needed.
- **DB adapter protocol** — Cygnet expects a `db` object with `async execute(sql, params) -> list[tuple]` and `async execute_one(sql, params) -> tuple | None`. See `tests/conftest.py:FakeDB` and `cygnet/psycopg_db.py:PsycopgDB`.
- **Transactions** — `cygnet.transaction(db)` is an async context manager. Nesting uses SAVEPOINTs. Relies on `db._in_transaction` flag.

## Commenting standards

Comments should explain choices, tradeoffs, higher-level algorithms, constraints, and invariants — not restate what the code does. Each file should have a brief header noting its role in the overall system. Emphasize non-obvious side effects, ordering dependencies, and intentional design decisions. The audience is a reader (human or AI) encountering this code for the first time.

## Testing

- **Unit tests** use `FakeDB` (captures SQL + params, returns preset rows). No database needed. Fixtures and shared models live in `tests/conftest.py`.
- **Integration tests** use a real PostgreSQL via `psycopg` async. Requires `CYGNET_TEST_DSN` env var. Marked with `@pytest.mark.integration`. `just test-all` handles Docker lifecycle.
- `asyncio_mode = "auto"` in pyproject.toml — all `async def test_*` methods run automatically, no `@pytest.mark.asyncio` needed.

## Gotchas

- `DBKey` fields on `frozen=True` dataclasses raise `TypeError` at introspection time — Cygnet can't set the PK after INSERT on a frozen object.
- `save()` with `AppKey` and `pk=None` raises `ValueError` — the app must supply the key.
- `GROUP_BY` requires explicit column selection in `SELECT(db, col1, col2)` — using bare `SELECT(db)` with `GROUP_BY` raises `ValueError`.
- `ColumnProxy.__hash__` is set to `None` to avoid conflicts with the overridden `__eq__`.
- `UPDATE` and `DELETE` without `.WHERE()` raise `ValueError` — use `WHERE(cygnet.all)` to affect all rows intentionally.

## Style

- Python 3.12+, ruff (line-length 88, rules E/F/I/UP), mypy strict
- Public API functions use UPPER_CASE names (`SELECT`, `INSERT`, `UPDATE`, `Table`) — suppressed via `# noqa: N802`
- Tests are async methods in classes (e.g., `class TestSelectSQL`), not standalone functions
