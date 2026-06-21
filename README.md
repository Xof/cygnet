# CYGNET

A small but fierce ORM. Bring your own objects, write real SQL.

CYGNET is a PostgreSQL-first ORM for Python 3.12+ that keeps SQL visible.
SQL keywords are uppercase method names. Python utilities are lowercase.
If you're comfortable with SQL and just want some help writing it, CYGNET
is for you.

It's small but full-featured: dataclass models, foreign keys with
`FOLLOW`/`LEFT_FOLLOW` joins, upsert via `ON CONFLICT DO UPDATE`,
LATERAL joins and EXISTS / IN subqueries, `FOR UPDATE` row locking,
savepoint-aware transactions, full mypy strictness, and an expression
protocol that lets `cygnet.op()` / `cygnet.ops()` / `cygnet.lit()`
extend the query API without touching internals.

## Installation

```
pip install cygnet-orm                # core only — bring your own db adapter
pip install 'cygnet-orm[psycopg]'     # + reference psycopg3 adapter
```

Cygnet itself doesn't depend on a particular database driver: the `db`
object is duck-typed (four methods — see [The db object](#the-db-object)
below).  If you want the bundled `PsycopgDB` reference adapter, install
the `[psycopg]` extra; otherwise the core install stays driver-free
and you supply your own `execute` / `execute_one` / `stream` methods.

Requires Python 3.12+ and PostgreSQL 14+.

## Quick start

```python
import dataclasses
from typing import Annotated
import cygnet

@dataclasses.dataclass
class Account:
    id: Annotated[int, cygnet.DBKey]   # database-assigned primary key
    name: str
    email: str

AccountTable = cygnet.Table(Account)
```

### SELECT

```python
# All rows
accounts = await cygnet.SELECT(db).FROM(AccountTable)

# With WHERE
accounts = await cygnet.SELECT(db).FROM(AccountTable).WHERE(
    AccountTable.name == "Fred"
)

# Compound predicates
accounts = await cygnet.SELECT(db).FROM(AccountTable).WHERE(
    (AccountTable.name == "Fred") & (AccountTable.id > 10)
)

# Specific columns — returns list of tuples
rows = await cygnet.SELECT(db, AccountTable.id, AccountTable.name).FROM(AccountTable)

# Pagination — ORDER BY + LIMIT + OFFSET
accounts = await (
    cygnet.SELECT(db)
    .FROM(AccountTable)
    .ORDER_BY(AccountTable.name)
    .LIMIT(20)
    .OFFSET(40)        # third page of 20
)

# ORDER BY descending
accounts = await (
    cygnet.SELECT(db)
    .FROM(AccountTable)
    .ORDER_BY(AccountTable.name, DESC=True)
)

# Mixed ASC/DESC: chain multiple ORDER_BY calls.  A single call applies
# one direction to all of its arguments.
accounts = await (
    cygnet.SELECT(db)
    .FROM(AccountTable)
    .ORDER_BY(AccountTable.name)                  # ASC
    .ORDER_BY(AccountTable.id, DESC=True)         # DESC
)

# GROUP BY (requires explicit columns)
rows = await (
    cygnet.SELECT(db, AccountTable.name)
    .FROM(AccountTable)
    .GROUP_BY(AccountTable.name)
)
```

### JOIN

```python
@dataclasses.dataclass
@cygnet.table("log_entries")       # override table name
class LogEntry:
    id: Annotated[int, cygnet.DBKey]
    account_id: int
    message: str

LogTable = cygnet.Table(LogEntry)

# INNER JOIN — returns list of (Account, LogEntry) tuples
rows = await (
    cygnet.SELECT(db)
    .FROM(AccountTable)
    .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
)

# LEFT JOIN — right side is None when there is no match
rows = await (
    cygnet.SELECT(db)
    .FROM(AccountTable)
    .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
)
for account, entry in rows:
    print(account.name, entry.message if entry else "(no log)")

# RIGHT JOIN — left (FROM-side) is None when there is no match
rows = await (
    cygnet.SELECT(db)
    .FROM(AccountTable)
    .RIGHT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
)
for account, entry in rows:
    print(account.name if account else "(orphan log)", entry.message)

# FULL JOIN — either side can be None (matched rows populate both)
rows = await (
    cygnet.SELECT(db)
    .FROM(AccountTable)
    .FULL_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
)
for account, entry in rows:
    a = account.name if account else "(no account)"
    e = entry.message if entry else "(no log)"
    print(a, e)

# Self-join via aliases — same table referenced twice in one query
A = AccountTable.AS("a")
B = AccountTable.AS("b")
pairs = await (
    cygnet.SELECT(db, A.name, B.name)
    .FROM(A)
    .JOIN(B, ON=A.id != B.id)
)
```

### Lateral joins

LATERAL subqueries reference columns from preceding `FROM` / `JOIN`
tables — the canonical "top-N per group" pattern, or any per-row
correlated subquery a regular `JOIN` can't express:

```python
# Most recent log entry per account
recent = (
    cygnet.SELECT(db, LogTable.message)
    .FROM(LogTable)
    .WHERE(LogTable.account_id == AccountTable.id)   # outer reference
    .ORDER_BY(LogTable.id, DESC=True)
    .LIMIT(1)
)
recent_lat = cygnet.lateral("recent", recent, columns=["message"])

rows = await (
    cygnet.SELECT(db, AccountTable.name, recent_lat.message)
    .FROM(AccountTable)
    .LEFT_JOIN_LATERAL(recent_lat)        # NULL for accounts with no logs
)
```

`JOIN_LATERAL` and `LEFT_JOIN_LATERAL` accept an optional `ON=`
predicate (defaults to `cygnet.lit("true")` since PG syntax requires
`ON` even when there's no extra filter to express). Column inference
follows the same rules as `cygnet.cte()`: explicit `ColumnProxy`
projections work without `columns=[…]`; opaque expressions need it.

### INSERT

```python
# From a dataclass instance — DBKey fields with None are omitted,
# and the generated key is written back onto the object via RETURNING
acc = Account(id=None, name="Fred", email="fred@example.com")
await cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)
print(acc.id)   # populated by PostgreSQL

# From keyword arguments
await cygnet.INSERT(db).INTO(AccountTable).VALUES(
    name="Wilma", email="wilma@example.com"
)

# Bulk INSERT — one statement, many rows, one round-trip.  Each
# object's DBKey is populated in input order from the RETURNING result.
accs = [
    Account(id=None, name="Fred", email="fred@example.com"),
    Account(id=None, name="Wilma", email="wilma@example.com"),
    Account(id=None, name="Barney", email="barney@example.com"),
]
ids = await cygnet.INSERT(db).INTO(AccountTable).BULK_VALUES(accs)
# ids == [acc.id for acc in accs]

# INSERT … SELECT — copy or transform rows in one statement.  Target
# columns are inferred from the source's ColumnProxy projection;
# pass columns=[…] explicitly when the source uses opaque expressions.
source = (
    cygnet.SELECT(db, AccountTable.name, AccountTable.email)
    .FROM(AccountTable)
    .WHERE(AccountTable.created_at > cutoff)
)
new_ids = await cygnet.INSERT(db).INTO(ArchiveTable).SELECT(source)
```

### UPDATE

```python
# Partial update via kwargs
await (
    cygnet.UPDATE(db)
    .SET(AccountTable, name="Frederick")
    .WHERE(AccountTable.id == 1)
)

# Full object update (pk excluded from SET clause)
acc.name = "Frederick"
await cygnet.UPDATE(db).SET(AccountTable, acc).WHERE(AccountTable.id == acc.id)

# Expressions on the right-hand side render in place rather than as
# parameters — that's how `count = count + 1` and any computed update
# work.  Any SQLRenderable (column ref, op(), fn(), lit(), …) is fine.
await (
    cygnet.UPDATE(db)
    .SET(AccountTable, name=cygnet.fn("upper")(AccountTable.name))
    .WHERE(AccountTable.id == 1)
)

# Cross-table UPDATE: pull values from another table.  PG joins via the
# WHERE clause (not a separate JOIN/ON), unlike SELECT.
await (
    cygnet.UPDATE(db)
    .SET(AccountTable, email=LogTable.message)
    .FROM(LogTable)
    .WHERE(AccountTable.id == LogTable.account_id)
)

# RETURNING gives back the updated rows (a list of tuples).
[(new_name,)] = await (
    cygnet.UPDATE(db)
    .SET(AccountTable, name="Updated")
    .WHERE(AccountTable.id == 1)
    .RETURNING(AccountTable.name)
)
```

`UPDATE` requires an explicit `.WHERE()` clause — a mass-mutation
safety rail.  Pass `cygnet.all` to opt in: `.WHERE(cygnet.all)`.

### DELETE

```python
# DELETE always requires WHERE (the same safety rail).
await cygnet.DELETE(db).FROM(LogTable).WHERE(LogTable.account_id == 42)

# To wipe every row, opt in with cygnet.all.
await cygnet.DELETE(db).FROM(LogTable).WHERE(cygnet.all)

# Cross-table DELETE: USING references other tables; the join condition
# lives in WHERE.  PG's syntactic mirror of UPDATE … FROM.
await (
    cygnet.DELETE(db)
    .FROM(LogTable)
    .USING(AccountTable)
    .WHERE(
        (LogTable.account_id == AccountTable.id)
        & (AccountTable.name == "Fred")
    )
)

# RETURNING gives back the deleted rows.
deleted = await (
    cygnet.DELETE(db)
    .FROM(LogTable)
    .WHERE(LogTable.account_id == 42)
    .RETURNING(LogTable.id, LogTable.message)
)
```

For "drop every row" workflows, `cygnet.TRUNCATE` is faster (acquires
a stronger lock and resets sequences):

```python
await cygnet.TRUNCATE(db, LogTable)
await cygnet.TRUNCATE(db, LogTable, AccountTable, cascade=True)
```

### ON CONFLICT — explicit conflict handling

For finer control than `save()`, the INSERT builder exposes PG's full
`ON CONFLICT` family.  Currently scoped to single-row `VALUES(obj)`;
bulk + INSERT…SELECT variants will land in a follow-up.

```python
# Skip the row if it conflicts with any unique constraint.
# Returns None on skip; the object's PK is left unset.
await (
    cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)
    .ON_CONFLICT_DO_NOTHING()
)

# Skip if conflict on a specific column / set of columns.
await (
    cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)
    .ON_CONFLICT(AccountTable.email).DO_NOTHING()
)

# Or skip if a named constraint fires.
await (
    cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)
    .ON_CONFLICT_CONSTRAINT("uq_accounts_email").DO_NOTHING()
)

# DO UPDATE: rewrite the existing row with literal kwarg values.
await (
    cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)
    .ON_CONFLICT(AccountTable.email)
    .DO_UPDATE(name="Updated", email="new@example.com")
)

# DO UPDATE FROM EXCLUDED: rewrite the existing row with the values
# the new row tried to insert.  This is what save() does internally.
await (
    cygnet.INSERT(db).INTO(AccountTable).VALUES(acc)
    .ON_CONFLICT(AccountTable.email)
    .DO_UPDATE_FROM_EXCLUDED(AccountTable.name, AccountTable.email)
)
```

When `DO NOTHING` skips a row, `INSERT … RETURNING` returns no rows
and the awaited builder yields `None`.  This is the *only* case where
an empty `RETURNING` is treated as a normal outcome — without
`ON_CONFLICT`, an empty `RETURNING` still raises (the same
silent-failure guard as before).

### save() — upsert

```python
# New object (DBKey + None) -> INSERT ... RETURNING, pk populated
acc = Account(id=None, name="Fred", email="fred@example.com")
await cygnet.save(db, acc)

# Existing object -> INSERT ... ON CONFLICT DO UPDATE
acc.name = "Frederick"
await cygnet.save(db, acc)
```

`save()` is DEFAULT-aware (matches `INSERT` and `create()` since 2026-05-22):
a field whose in-memory value is `None` and whose column has a schema
`DEFAULT` is omitted from both the `INSERT` column list and the `DO UPDATE
SET` clause, then refreshed via `RETURNING`.  Consequences:

- New row (no conflict): the schema `DEFAULT` fires (e.g. `now()` for
  `created_at`), and `obj.created_at` is patched with the populated value.
- Existing row (conflict): the `DEFAULT`ed column is *not* touched by the
  `UPDATE` — the existing value is preserved — and `RETURNING` still
  refreshes `obj` to match the DB row.
- Explicit override: a non-`None` value is always written through, so the
  app can override the `DEFAULT` when it wants to.

In other words, `obj.created_at = None` is now a signal to "leave the DB's
value alone"; use `UPDATE` if you need to write a literal NULL to a
`DEFAULT`ed column.  Adapters that don't implement the optional
`column_defaults` protocol method (e.g. `FakeDB`) see the historical
shape: every field emitted, no `RETURNING`.

For surgical updates of individual columns, use `UPDATE`.

### get() — fetch by primary key

```python
acc = await cygnet.get(db, AccountTable, id=42)  # Account | None
```

### create() — INSERT without ON CONFLICT

```python
# Equivalent to INSERT … RETURNING; duplicate-key violations propagate
# from the database rather than being silently upserted.  Use this when
# you want the database to tell you "this already existed" loudly.
acc = await cygnet.create(db, Account(id=None, name="Fred", email="f@x.com"))
```

### Foreign keys and FOLLOW

Annotate a field with `cygnet.ForeignKey(Target)` to declare a foreign
key.  Cygnet validates the reference at introspection time (target must
be a dataclass with a PK; types must match) and uses it to power
`FOLLOW` / `LEFT_FOLLOW` and `cygnet.follow`:

```python
@dataclasses.dataclass
@cygnet.table("log_entries")
class LogEntry:
    id: Annotated[int, cygnet.DBKey]
    account_id: Annotated[int, cygnet.ForeignKey(Account)]
    message: str

LogTable = cygnet.Table(LogEntry)

# FOLLOW is an INNER JOIN with the FK condition spelled out for you.
# Returns tuples of (LogEntry, Account):
rows = await cygnet.SELECT(db).FROM(LogTable).FOLLOW(LogTable.account_id)

# LEFT_FOLLOW for the outer-join variant; the right side is None when
# the FK is NULL or the target row is missing.
rows = await cygnet.SELECT(db).FROM(LogTable).LEFT_FOLLOW(LogTable.account_id)

# Load a single related object on demand (returns Account | None).
log = await cygnet.get(db, LogTable, id=1)
account = await cygnet.follow(db, log, LogTable.account_id)
```

`ForeignKey(target)` always references the target's primary key —
composite PKs aren't supported.

### Subquery predicates: EXISTS / IN

A `SelectBuilder` is itself an SQL expression — it renders as a
parenthesised inline subquery wherever an expression is expected.
That makes `EXISTS`, `IN (SELECT …)`, and scalar subqueries work
without any extra builder methods:

```python
# EXISTS — is there at least one matching row?  Correlated subqueries
# reference outer-query columns directly (no LATERAL needed).
any_log = (
    cygnet.SELECT(db, cygnet.lit("1"))
    .FROM(LogTable)
    .WHERE(LogTable.account_id == AccountTable.id)
)
authors = await (
    cygnet.SELECT(db).FROM(AccountTable).WHERE(cygnet.exists(any_log))
)

# NOT EXISTS — anti-join idiom.  ~cygnet.exists(b) works too;
# both render as `NOT EXISTS (…)` (not `NOT (EXISTS …)`).
silent = await (
    cygnet.SELECT(db).FROM(AccountTable).WHERE(cygnet.not_exists(any_log))
)

# IN (subquery) — uses cygnet.op since `in` isn't a Python overload.
active_ids = (
    cygnet.SELECT(db, AccountTable.id)
    .FROM(AccountTable)
    .WHERE(AccountTable.email == "fred@example.com")
)
fred_logs = await (
    cygnet.SELECT(db)
    .FROM(LogTable)
    .WHERE(cygnet.op(LogTable.account_id, "IN", active_ids))
)

# Scalar subquery in the SELECT list — the builder is rendered directly.
log_count = (
    cygnet.SELECT(db, cygnet.fn("count")(cygnet.lit("*")))
    .FROM(LogTable)
    .WHERE(LogTable.account_id == AccountTable.id)
)
rows = await cygnet.SELECT(db, AccountTable.name, log_count).FROM(AccountTable)
```

`$N` parameter numbering threads correctly through the inner-then-outer
pieces.

### Row-level locking

```python
# FOR UPDATE — exclusive lock; blocks other locks until commit.
acc = await (
    cygnet.SELECT(db).FROM(AccountTable).WHERE(AccountTable.id == 1)
    .FOR_UPDATE()
)

# Skip rows that someone else has locked — the queue-worker pattern.
batch = await (
    cygnet.SELECT(db).FROM(LogTable)
    .ORDER_BY(LogTable.id)
    .LIMIT(10)
    .FOR_UPDATE(skip_locked=True)
)

# Restrict the lock to specific tables in a join with `of=`.
rows = await (
    cygnet.SELECT(db).FROM(AccountTable)
    .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
    .FOR_UPDATE(of=AccountTable)            # lock accounts only
)

# FOR SHARE — shared lock; allows concurrent reads, blocks writes.
await cygnet.SELECT(db).FROM(AccountTable).WHERE(...).FOR_SHARE()
```

Both verbs accept `nowait=True` (fail immediately if any row is locked)
and `skip_locked=True` (silently skip locked rows; mutually exclusive
with `nowait`).  The rarer modes ride as flags:
`FOR_UPDATE(no_key=True)` → `FOR NO KEY UPDATE`,
`FOR_SHARE(key=True)` → `FOR KEY SHARE`.

### Inspecting SQL without executing

Every builder exposes `.sql()`, returning the rendered SQL plus the
parameter list — same validation as `await`, but no round trip:

```python
sql, params = (
    cygnet.SELECT(db, AccountTable.name)
    .FROM(AccountTable)
    .WHERE(AccountTable.id == 1)
    .sql()
)
# sql == "SELECT accounts.name FROM accounts WHERE (accounts.id = $1)"
# params == [1]
```

`UPDATE` / `DELETE` `.sql()` calls run the same WHERE-required
validation as execution, so the safety rail isn't bypassable through
inspection.

### Functions and operators

```python
import cygnet.functions as f

# Aggregates
total = await cygnet.SELECT(db, f.count(), f.sum(OrderTable.amount)).FROM(OrderTable)

# COUNT(*) is the empty-args form
rows = await cygnet.SELECT(db, f.count()).FROM(AccountTable)

# In WHERE / HAVING via comparison overloads
busy = await (
    cygnet.SELECT(db, AccountTable.name)
    .FROM(AccountTable)
    .GROUP_BY(AccountTable.name)
    .HAVING(f.count() > 1)
)

# Anything not curated is reachable via cygnet.fn(name)
await cygnet.SELECT(db, cygnet.fn("date_trunc")("day", OrderTable.created_at)).FROM(OrderTable)
```

For inline operators (`ILIKE`, `~~`, `@@`, etc.) use `cygnet.op` /
`cygnet.ops` / `cygnet.is_null` / `cygnet.is_not_null`. For raw SQL
fragments, `cygnet.lit("...")`. **Operator and function names are
trusted strings** — never pass user input as an operator or function
name.

Comparing a column to `None` is NULL-safe: `T.col == None` renders
`col IS NULL` and `T.col != None` renders `col IS NOT NULL` — so a value that
is `None` at runtime does the right thing instead of silently matching no rows.
`cygnet.is_null(col)` / `cygnet.is_not_null(col)` are the explicit equivalents.

`cygnet.op` has three arities:

```python
# 3-arg: one-shot infix predicate
.WHERE(cygnet.op(T.name, "ILIKE", "%fred%"))

# 2-arg: prefix operator (NOT, EXISTS-style)
.WHERE(cygnet.op("NOT", T.active == True))

# 1-arg: factory — bind the operator once, reuse the callable.
# Idiomatic when the same non-standard operator appears repeatedly:
ILIKE = cygnet.op("ILIKE")
.WHERE(ILIKE(T.name, "%fred%") | ILIKE(T.email, "%fred%"))
```

The 1-arg form is a closure capturing the operator string and
returning a `(left, right) -> Predicate` callable.  Operands are
still parameterised; only the operator string is interpolated
verbatim — same trusted-string rule as the other arities.

### JSONB, arrays, and full-text search

Three curated submodules wrap the most common PG-native operators
and functions. Each is a thin layer over `cygnet.op` / `cygnet.fn`,
so anything not curated is still reachable directly.

```python
import cygnet.jsonb as jb
import cygnet.arrays as arr
import cygnet.fts as fts

# JSONB — `data ->> 'name' = 'Fred'`, `data @> '{"active": true}'`, etc.
.WHERE(jb.get_text(T.payload, "name") == "Fred")
.WHERE(jb.contains(T.payload, {"active": True}))
.WHERE(jb.has_key(T.payload, "email"))

# Arrays — @>, <@, &&, ANY/ALL, length
.WHERE(arr.contains(T.tags, ["python", "sql"]))
.WHERE(arr.overlaps(T.tags, ["python", "go"]))
.WHERE(T.id == arr.any(other.allowed_ids))      # T.id = ANY(...)
.WHERE(arr.length(T.items) > 0)

# Full-text — to_tsvector / web_query / matches / rank
.WHERE(fts.matches(
    fts.to_tsvector(T.body),
    fts.web_query(user_input)
))
.ORDER_BY(
    fts.rank(fts.to_tsvector(T.body), fts.web_query(user_input)),
    DESC=True,
)
```

For dict-to-JSONB autoadaptation in psycopg, register the dumper
once at startup:

```python
import psycopg
from psycopg.types.json import JsonbDumper
psycopg.adapters.register_dumper(dict, JsonbDumper)
```

### DISTINCT and DISTINCT ON

```python
# Plain DISTINCT — deduplicate every selected row.
await cygnet.SELECT(db, T.country).FROM(T).DISTINCT()

# DISTINCT ON (cols) — PG-specific: keep one row per distinct value of
# the listed columns, picked according to ORDER BY.
await (
    cygnet.SELECT(db, T.country, T.name)
    .DISTINCT_ON(T.country)
    .FROM(T)
    .ORDER_BY(T.country, T.name)   # determines which row wins per country
)
```

### Set operations

```python
# UNION dedupes; UNION_ALL preserves duplicates.
combined = await (
    cygnet.SELECT(db, A.name).FROM(A)
    .UNION(cygnet.SELECT(db, B.name).FROM(B))
    .ORDER_BY(cygnet.lit("name"))    # applies to the COMPOUND result
    .LIMIT(100)
)

# Other set ops: INTERSECT / INTERSECT_ALL / EXCEPT_ / EXCEPT_ALL.
# (EXCEPT_ has a trailing underscore because `except` is a Python keyword.)
diff = await (
    cygnet.SELECT(db, A.name).FROM(A)
    .EXCEPT_(cygnet.SELECT(db, B.name).FROM(B))
)
```

### Streaming large result sets

```python
# `async for ...` instead of `await ...`: rows arrive one at a time
# from a server-side portal cursor, so the full result set never lives
# in process memory.
async with cygnet.transaction(db) as tx:
    async for entry in (
        cygnet.SELECT(tx)
        .FROM(LogTable)
        .WHERE(LogTable.account_id == 42)
        .ORDER_BY(LogTable.id)
        .stream()
    ):
        process(entry)
```

PostgreSQL portal cursors require a transaction (or autocommit off),
so streaming is typically wrapped in `cygnet.transaction(db)`. The db
adapter must implement an async `stream(sql, params)` method;
psycopg's `cursor.stream()` is the reference implementation — see
`cygnet/psycopg_db.py`.

### Window functions

```python
import cygnet.functions as f

# `row_number() OVER (PARTITION BY dept ORDER BY salary DESC)`
rn = f.row_number().OVER(
    partition_by=[EmployeeTable.dept],
    order_by=[(EmployeeTable.salary, "DESC")],
)
rows = await (
    cygnet.SELECT(db, EmployeeTable.name, EmployeeTable.dept, rn)
    .FROM(EmployeeTable)
)

# Aggregates work as windows too
avg_salary = f.avg(EmployeeTable.salary).OVER(
    partition_by=[EmployeeTable.dept]
)

# LAG / LEAD pull from neighbouring rows
prev = f.lag(EmployeeTable.salary, 1).OVER(order_by=[EmployeeTable.id])

# Frames are passed as raw SQL strings (interpolated verbatim — trusted)
running_sum = f.sum(T.amount).OVER(
    order_by=[T.id],
    frame="ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW",
)
```

The available curated window functions: `row_number`, `rank`,
`dense_rank`, `percent_rank`, `cume_dist`, `ntile`, `lag`, `lead`,
`first_value`, `last_value`, `nth_value`. Anything else reaches via
`cygnet.fn("name")(...).OVER(...)`.

### CTEs (WITH clauses)

```python
# Build the inner SELECT, wrap it in a CTE, then reference it like a table.
active = cygnet.cte(
    "active",
    cygnet.SELECT(db, AccountTable.id, AccountTable.name)
        .FROM(AccountTable)
        .WHERE(AccountTable.status == "active"),
)
results = await (
    cygnet.SELECT(db, active.name, LogTable.message)
    .WITH(active)
    .FROM(active)
    .LEFT_JOIN(LogTable, ON=active.id == LogTable.account_id)
)
```

CTE column names are inferred from the inner SELECT's `ColumnProxy`
arguments. For inner SELECTs that use opaque expressions
(`cygnet.fn`, `cygnet.lit`, …), pass `columns=[…]` explicitly:

```python
counts = cygnet.cte(
    "counts",
    cygnet.SELECT(db, AccountTable.id, cygnet.fn("count")(LogTable.id))
        .FROM(AccountTable)
        .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
        .GROUP_BY(AccountTable.id),
    columns=["id", "n"],
)
```

Multiple CTEs compose, either via repeated `.WITH()` or in one call:

```python
.WITH(active, recent)
.WITH(active).WITH(recent)
```

Recursive CTEs require explicit columns (so the recursive step can
reference them) and an anchor + step assigned after construction:

```python
counter = cygnet.recursive_cte("counter", columns=["n"])
counter.anchor = cygnet.SELECT(db, cygnet.lit("1"))
counter.step = (
    cygnet.SELECT(db, counter.n + 1)
    .FROM(counter)
    .WHERE(counter.n < 10)
)

rows = await cygnet.SELECT(db, counter.n).WITH(counter).FROM(counter)
# [(1,), (2,), …, (10,)]
```

When a recursive CTE appears in a `WITH(…)` list, the rendered SQL
uses `WITH RECURSIVE` for the whole list (PG's syntax requirement).

### Transactions

```python
async with cygnet.transaction(db) as tx:
    await cygnet.INSERT(tx).INTO(AccountTable).VALUES(acc)
    await cygnet.INSERT(tx).INTO(LogTable).VALUES(entry)
# commits on clean exit, rolls back on exception
```

Nested `transaction` blocks transparently use `SAVEPOINT`:

```python
async with cygnet.transaction(db) as tx:
    await cygnet.save(tx, acc)
    async with cygnet.transaction(tx) as tx2:   # SAVEPOINT
        await cygnet.save(tx2, risky_entry)      # RELEASE or ROLLBACK TO
```

> **Concurrency caveat.** `cygnet.transaction` toggles a `_in_transaction`
> flag on the db handle to detect nesting, and that flag is **not** task-local.
> A single db connection must not be shared across concurrent asyncio tasks.
> psycopg connections are themselves not task-safe, so the recommended
> pattern is one connection per task — typically by acquiring from a pool
> inside each task. Fresh connections must start with `_in_transaction = False`.
>
> Cygnet actively detects cross-task misuse: the outermost
> `__aenter__` records the owning `asyncio.current_task()` on the db,
> and a nested `__aenter__` from a different task raises `RuntimeError`
> rather than silently SAVEPOINTing inside the other task's
> transaction. The guard is best-effort — it only fires when the outer
> layer uses `cygnet.transaction` rather than an externally-managed
> `BEGIN`.

## Annotations

| Annotation | Meaning |
|---|---|
| `cygnet.DBKey` | Primary key assigned by the database (`SERIAL` / `IDENTITY`). Omitted on `INSERT` when `None`; populated via `RETURNING`. Incompatible with `frozen=True`. |
| `cygnet.AppKey` | Primary key assigned by the application (e.g. UUID). Must never be `None`. |
| `cygnet.Column("col_name")` | Override the column name for a field. |
| `cygnet.ForeignKey(Target)` | Mark a field as a foreign key referencing `Target`'s primary key. Enables `FOLLOW` / `LEFT_FOLLOW` join sugar and `cygnet.follow()`. Composite PKs are not supported. |
| `@cygnet.table("table_name")` | Override the table name for a dataclass (default: `classname.lower() + "s"`). |

## The db object

CYGNET does not manage connections. Pass any object that conforms to
`cygnet.DBAdapter` — a `@runtime_checkable` Protocol declared in
`cygnet/expression.py` and re-exported at the package root. Required
members:

```python
class DBAdapter(Protocol):
    _in_transaction: bool        # False on fresh adapter; toggled by cygnet.transaction
    _transaction_task: Any       # Cygnet-managed task-locality stash; init to None

    async def execute(self, sql: str, params: list | None = None) -> list[tuple]: ...
    async def execute_one(self, sql: str, params: list | None = None) -> tuple | None: ...
```

Optional methods (duck-typed via `hasattr`, not on the Protocol):

```python
# Only required for SelectBuilder.stream():
async def stream(self, sql: str, params: list | None = None) -> AsyncIterator[tuple]: ...

# Only required for DEFAULT-aware INSERT codegen (None-valued columns
# with a schema DEFAULT omitted from INSERT, refreshed via RETURNING):
async def column_defaults(self, table_name: str) -> set[str]: ...
```

Because `DBAdapter` is `runtime_checkable`, custom adapters can
verify conformance with a plain `isinstance(my_db, cygnet.DBAdapter)`
check before shipping. The reference `PsycopgDB` adapter implements
both required AND both optional methods.

### Reference psycopg3 adapter

Install with the `[psycopg]` extra (see [Installation](#installation)),
then:

```python
import psycopg
from cygnet.psycopg_db import PsycopgDB

conn = await psycopg.AsyncConnection.connect("postgresql://...")
db = PsycopgDB(conn)
accounts = await cygnet.SELECT(db).FROM(AccountTable)
```

`PsycopgDB` translates Cygnet's `$N` placeholders to psycopg's `%s`,
implements all four protocol methods (including `stream()` via portal
cursor), and tracks `_in_transaction` for `cygnet.transaction()`.

Importing `cygnet.psycopg_db` without the extra installed raises a
clear `ImportError` pointing at the install command — Cygnet's core
itself stays driver-free, so a project shipping a custom adapter
never pulls psycopg.

### Connection pooling

```python
from psycopg_pool import AsyncConnectionPool
from cygnet.psycopg_db import PsycopgDB

pool = AsyncConnectionPool("postgresql://...")
await pool.open()

async def handler(...):
    # One connection per task — never share a PsycopgDB across
    # concurrent tasks, since `_in_transaction` is per-instance and
    # not task-local.
    async with pool.connection() as conn:
        db = PsycopgDB(conn)
        async with cygnet.transaction(db):
            await cygnet.save(db, obj)
```

### Strict typing & IDE autocomplete

Out of the box, `cygnet.Table(Account)` returns `TableProxy[Account]`,
so `cygnet.get(db, AccountTable, id=1)` correctly types as
`Account | None`. What mypy *can't* infer without help is the per-field
shape: `AccountTable.name` resolves to `ColumnProxy` (effectively
`Any`), so a typo like `AccountTable.nmae` doesn't error at type-check
time and IDEs don't autocomplete attribute names.

For projects that want strict typing, `cygnet.stubs` generates a
hand-pasteable `TYPE_CHECKING` block:

```bash
$ python -m cygnet.stubs myapp.models
```

Output (paste into `myapp/models.py`, replacing the original
`XTable = cygnet.Table(X)` lines):

```python
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from cygnet.proxy import ColumnProxy, TableProxy

    class _AccountTable(TableProxy[Account]):
        id: ColumnProxy[int]
        name: ColumnProxy[str]
        email: ColumnProxy[str]

    AccountTable: _AccountTable
else:
    import cygnet
    AccountTable = cygnet.Table(Account)
```

Mypy now sees `AccountTable.name` as `ColumnProxy[str]`; runtime is
unchanged. Regenerate after schema changes.

### PG type adapters

psycopg3 handles most PG types natively (`uuid.UUID`, `decimal.Decimal`,
`datetime.datetime`/`date`/`time`, arrays of those, etc.). A few types
need one-line registration at app startup:

```python
import psycopg
from psycopg.types.json import JsonbDumper

# Plain Python dicts -> JSONB (Cygnet.jsonb helpers expect this).
psycopg.adapters.register_dumper(dict, JsonbDumper)
```

For ranges (`int4range`, `tstzrange`, …), psycopg provides `Range` and
`DateTimeTzRange` etc. as native Python types; no extra registration
needed. See psycopg3's docs for the full list.

## Design principles

- SQL keywords are uppercase method names: `.FROM()`, `.WHERE()`, `.JOIN()`.
- Python utilities are lowercase: `cygnet.get()`, `cygnet.save()`, `cygnet.table()`.
- No magic. No metaclasses. No implicit queries. You call it, it runs.
- PostgreSQL-specific features (`RETURNING`, `ON CONFLICT`) are used directly.
- Bring your own connection and transaction lifecycle.

## Development

Development requires [`uv`](https://docs.astral.sh/uv/) as the package
manager — install it via your platform's package manager (Homebrew /
apt / `pipx install uv`) before the steps below. `just bootstrap`
runs `uv sync --extra dev` which honours the checked-in `uv.lock` for
reproducible installs.

```
just bootstrap       # create .venv via uv sync, install dev dependencies (locked)
just check           # fmt + lint + typecheck + unit tests
just test-all        # full suite including integration (requires Docker)
just pg-psql         # open psql against the test container
```

## Benchmarks

The `bench/` suite tracks Cygnet's performance over time (advisory in
CI — never blocks merge) and gives a feel for ORM overhead vs total
wall time.

```
just bootstrap-bench    # one-time: install pytest-benchmark + Django + SQLAlchemy
just bench              # render + overhead benchmarks (no DB, ~5s)
just bench-e2e          # end-to-end against a fresh Docker PG (~30s)
just bench-all          # everything, JSON output for CI artifact
```

Three layers of measurement, each focused on a different cost:

- **`bench/test_render.py`** — pure SQL generation against `FakeDB`.
  Sub-microsecond per call; isolates the cost of building the AST and
  rendering it to a string.
- **`bench/test_overhead.py`** — full Cygnet path through `FakeDB`,
  including row-to-object hydration. Catches regressions in the
  executor and mapper.
- **`bench/test_e2e.py`** — real PG via `PsycopgDB`. Total wall time
  including round-trip.

CI runs all three on every push and PR, uploads the JSON as an
artifact, and posts a summary table in the job output. Regressions are
informational; the job is `continue-on-error: true` so a noisy
benchmark never wedges merging.

On `pull_request` events, the bench job additionally downloads main's
last successful `bench-results` artifact and renders a per-benchmark
delta table in the same step summary. Slowdowns greater than 15%
appear bold; runner-noise-sized changes (±10%) stay plain text. The
comparison gracefully no-ops when no baseline exists yet (first PR
after a fresh repo, expired retention, etc.).

### Cross-ORM comparison

`bench/comparison/test_comparison.py` runs the same operations
through Cygnet, SQLAlchemy 2 (async session), and Django (sync ORM)
against the same PG schema. Four operation classes × three ORMs =
twelve side-by-side benchmarks: SELECT-by-PK, SELECT-all-100, INSERT
one, bulk INSERT 50.

Each ORM is benchmarked in its idiomatic mode — Cygnet and SA in
async, Django in sync — so the numbers reflect what real applications
see. SA's connection pool is clamped to a single connection
(`pool_size=1, max_overflow=0`) to match Cygnet's PsycopgDB and
Django's per-request connection, so the deltas measure ORM overhead
rather than connection management.

Skipped automatically when `CYGNET_TEST_DSN` is unset; runs
inside `just bench-all` or directly via:

```bash
pytest bench/comparison/ --benchmark-only
```

## License

MIT
