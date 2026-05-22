# Cygnet — Open Issues & Investigations

This file tracks outstanding work on Cygnet. (Renamed from `REVIEW.md`
on 2026-05-22; same role, friendlier name.)

The 2026-04-22 fresh-eyes review and 2026-04-22 `/comment-run` findings
have all landed:

- **Phases 1–8** shipped 2026-04-24 (commits `9264b04` … `468c427`).
- **Item 5.4** (generic ColumnProxy) and **Item 8.1** (self-join test
  via aliasing) closed 2026-04-25.

The full original review is preserved in git history at commit
`1c55ce9` if the long-form rationale is ever needed.

Two further rounds of review landed 2026-05-22 and contributed the
findings below:

- `docs/reviews/review-20260429-175335.md` — fresh-eyes deep-dive
  (5 executive findings + 4 open questions).
- `docs/reviews/comment-run-20260522.md` — comment-pass observations
  (14 findings discovered while annotating).
- `docs/reviews/review-20260522-084756.md` — second fresh-eyes
  deep-dive (5 executive findings + 4 open questions), incorporating
  context from both prior passes.

Each entry below cross-references its source review in `[brackets]`.

---

## Decisions (preserved)

These four answers shaped the implementation; recorded so future
maintainers don't re-litigate them.

1. **Empty `UPDATE.SET(T)` raises** — the silent no-op was a bug.
2. **OFFSET / HAVING / DISTINCT / RETURNING-on-UPDATE-DELETE are in
   scope and built**, not out-of-scope.
3. **`cygnet.all` mixed with real predicates raises everywhere**,
   including SELECT — same rule UPDATE/DELETE enforce.
4. **`ISSUES.md` is the single open-issues tracker** (renamed from
   REVIEW.md 2026-05-22).

---

## Resolved trade-offs (closed without further work)

### 5.4 Per-field IDE autocomplete on `T.col` — needs a mypy plugin

`TableProxy` is generic on the model class (Phase 5.3) and `ColumnProxy`
is now generic on the column's value type (`ColumnProxy[FT]`, 2026-04-25).
Both pieces type-check correctly when the user spells the type
explicitly:

```python
name_col: ColumnProxy[str] = T.name  # checked
```

What's still missing — and what would require a mypy plugin (or stub
codegen) — is **automatic per-field type inference**: making `T.name`
resolve as `ColumnProxy[str]` without the explicit annotation, with the
field name verified against the model. Python's static type system
doesn't have the primitives to project a generic-parameter class's
fields into typed proxy attributes; SQLAlchemy 2 ships a mypy plugin
for the same reason.

**Decision (2026-04-25):** This is out of scope for an alpha-stage
library. The runtime API is stable, the generic typing carries through
`get`/`save`/`follow`/`create`, and the gap is only visible at the
query-construction call site. Revisit if Cygnet sees enough adoption to
justify a plugin, or if Python's type system grows the necessary
primitives.

### 8.1 Self-join integration test — closed via aliasing API

The original test (`test_multi_join_mapping` in `tests/test_mapping.py`)
joined the same table twice with identical ON, which would fail PG's
parser as ambiguous. The fix landed 2026-04-25:

- **`TableProxy.AS("alias")`** — returns an aliased proxy view that
  renders as `tablename AS alias` in FROM/JOIN, with column refs
  scoped via `alias.col`. Aliased proxies bypass the singleton cache;
  unaliased `Table(cls)` lookups remain canonical.
- **`tests/integration/test_roundtrip.py::TestSelfJoinRoundtrip`** —
  exercises a real-PG self-join via `BookTable.AS("ba")` /
  `BookTable.AS("bb")`. Passes against PG 14–18.

The unit-level `test_multi_join_mapping` is left as-is: it tells row-
to-object mapping behaviour with FakeDB, which is its actual purpose.

### Apr 29 #1 — Stale README path to PsycopgDB

The streaming section in README cited `tests/integration/conftest.py:PsycopgDB`,
which had become a one-line re-export. Closed: README now points at
`cygnet/psycopg_db.py`, and the conftest re-export was removed.

### Apr 29 OQ3 — psycopg dependency placement

Settled: psycopg moved from required to optional. `pyproject.toml` now
has `dependencies = []` and `[project.optional-dependencies] psycopg`.

### Apr 29 OQ4 — `tests/integration/conftest.py` re-export

Settled: the re-export was a back-compat hangover from when PsycopgDB
moved into the package. Removed; integration tests import from
`cygnet.psycopg_db` directly.

### Apr 29 #5 (partial, 2026-05-17 in commit `a2156bf`) — DEFAULT-aware INSERT

`run_insert` (single-row VALUES) and `run_create` now respect schema
DEFAULTs: when the adapter implements the optional `column_defaults`
method, None-valued fields with a non-NULL DEFAULT are omitted from
INSERT and refreshed via RETURNING. Bulk INSERT and INSERT…SELECT
are intentionally not covered (no in-memory object to patch).

The `run_save` upsert path was *not* updated and now carries a
documented divergence — see **B3** below.

---

## Open issues — bugs

### ~~B1. `PsycopgDB.column_defaults` lookup ignores schema~~  *[2026-05-22-deepdive #1] — CLOSED 2026-05-22*

Fixed by switching the query from `information_schema.columns` (no
schema filter possible without re-encoding search_path) to a direct
`pg_catalog.pg_attribute JOIN pg_attrdef WHERE attrelid = to_regclass($1)`.
`to_regclass()` reuses PG's own search_path resolution, so the lookup
now matches whatever PG would resolve an unqualified `FROM events` to —
correctly disambiguating `s1.events` from `s2.events` even when both
exist. Also handles the previously-incorrect edge case of a same-named
table in two search_path schemas (PG picks the first; the lookup now
matches that exact choice).

Regression test: `tests/integration/test_column_defaults.py::TestColumnDefaultsRespectsSearchPath`
sets up `s1.events` (default on `created_at`) and `s2.events` (default
on `archived_at`), flips `search_path` between them, and asserts the
returned set tracks the active schema without leakage. The third test
pins `to_regclass`'s NULL-on-missing semantics to "empty set" so the
contract matches the old information_schema behaviour for unknown
tables. Closes **S12** (the docstring now matches the code) and
**S13** (the misleading `pg_get_expr` comment is replaced with a
correct description of the pg_attrdef join).

### ~~B2. `_defaults_cache` has no schema-change invalidation~~  *[2026-05-22-deepdive #3] — CLOSED 2026-05-22*

Closed by adding `cygnet.flush_column_defaults(db=None)` as a public
API surface. Delegates to `Executor.flush_column_defaults`, a class-
method that either pops a specific adapter from the WeakKeyDictionary
or clears it entirely. Documented as the post-migration knob in
`cygnet/__init__.py` and in the cache-comment block at
`cygnet/executor.py:55-78`. Three unit tests cover the cases:
specific-adapter eviction triggers re-introspection on the next
INSERT, no-arg flush clears every adapter, flushing an uncached
adapter is a silent no-op. The class-level cache-clear utility was
preferred over an adapter-protocol method to keep the duck-typed
contract small.

### ~~B3. `run_save` ignores schema DEFAULTs and never refreshes~~  *[2026-04-29 #5 unresolved half; 2026-05-22-deepdive #2; comment-run #2] — CLOSED 2026-05-22*

Closed by routing the upsert path through `_extract_insert_fields`
with the same `defaulted_columns` set `run_insert` uses (OQ1 resolved
in favour of "fix it" rather than "document the divergence").
DEFAULT-omitted columns are excluded from BOTH the INSERT column list
AND the `DO UPDATE SET` clauses; on the new-row branch the schema
DEFAULT fires; on the conflict branch the existing value is preserved;
in both cases RETURNING refreshes the in-memory object so the caller's
view matches the DB row.

Adapters that don't implement `column_defaults` (FakeDB, custom
duck-typed adapters that opt out) see no behaviour change — when
nothing is DEFAULT-omitted, the upsert emits the historical
"no RETURNING, execute-not-execute_one" shape. Empty-SET edge case
(every non-PK field DEFAULT-omitted, or pure-PK model) falls back to
`SET pk = EXCLUDED.pk`, a syntactically valid no-op.

Coverage: 5 new unit tests in `tests/test_builders.py::TestSaveDefaultAwareness`
pin the SQL shape; 2 new integration tests in
`tests/integration/test_roundtrip.py::TestDefaultAwareInsertRoundtrip`
(`test_save_existing_row_preserves_default_column` and
`test_save_existing_row_with_explicit_override_writes_it`) exercise
the end-to-end behaviour against real PG.

Closes **S14** (README updated to describe the new save() semantics
explicitly) and resolves **OQ1**.

### ~~B4. `stubs._format_type` loses generic parameters~~  *[2026-05-22-deepdive #4; comment-run #1] — CLOSED 2026-05-22*

Fixed by replacing the `getattr(t, "__name__")` test with
`type(t) is type` — the precise discriminator between bare classes
(where `type(t) is type` holds) and parameterised forms (`list[str]`
is `types.GenericAlias`, `int | None` is `types.UnionType`; neither
matches `type` and both fall through to the readable `str(t)`).
Regression test added at `tests/test_stubs.py::test_parameterised_generics_keep_their_params`
with a `Doc` fixture in `tests/conftest.py` carrying `list[str]` and
`dict[str, int]` fields (closes **S15** too).

### ~~B5. `pip-audit --strict || true` swallows CVEs~~  *[2026-05-22-deepdive #5] — CLOSED 2026-05-22*

Closed by changing the audit step to `pip-audit --skip-editable`. The
investigation found the original `--strict || true` was contradictory
for a deeper reason than the review caught: `--strict` exits 1 on the
unauditable local editable install (cygnet-orm isn't on PyPI), so
`|| true` had been added not just to swallow CVEs but to swallow that
expected false-positive. `--skip-editable` skips the editable cleanly
without escalating it to a failure, and dropping `--strict` means the
job exits non-zero only when a real CVE shows up. OQ4 is resolved by
implication: strict-mode-blocking-on-CVE is now the default for the
non-editable portion of the dep tree.

---

## Open issues — smells

### ~~S1. `$N` → `%s` regex blind to string literals~~  *[2026-04-29 #2] — CLOSED 2026-05-22*

Closed by documenting the limitation in two places: a paragraph on
``lit()``'s docstring in ``cygnet/__init__.py`` calling out that adapters
which translate placeholders rewrite ``$\d+`` anywhere in the payload,
and a corresponding comment on ``cygnet/psycopg_db.py``'s file header
explaining the regex is string-literal-blind. The library's "lit is
trusted" stance makes the documented limitation acceptable; proper SQL
tokenisation was rejected as overkill for the escape hatch.

### ~~S2. `_row_to_obj` zip-truncation is silent~~  *[2026-05-22-deepdive smell; comment-run #4] — CLOSED 2026-05-22*

Closed by ``zip(meta.fields, row, strict=True)`` in
``Executor._row_to_obj``.  Length mismatches now raise ValueError at
the seam instead of producing either a TypeError-raising-dataclass-
constructor (too short) or a silent trailing-column drop (too long).
The implicit-column SELECTs the executor emits guarantee length
parity; only hand-written ``lit()`` projections trip the check.

### ~~S3. `HAVING` docstring promises a check it doesn't enforce~~  *[2026-05-22-deepdive smell; comment-run #3] — CLOSED 2026-05-22*

Closed by adding the explicit ``isinstance(predicate, _All)`` guard
to ``SelectBuilder.HAVING``: ``cygnet.all`` now raises ValueError with
a message explaining that HAVING is for aggregate-group filters, not
"all groups".  Regression test:
``TestSelectSQL::test_having_rejects_cygnet_all``.

### ~~S4. `column_defaults` as optional-via-hasattr fragments the adapter protocol~~  *[2026-05-22-deepdive API design] — CLOSED 2026-05-22*

Closed via **OQ2** (resolved to "formalise as Protocol"): added
``DBAdapter`` as a ``@runtime_checkable`` Protocol in
``cygnet/expression.py`` and re-exported at the package root
(``cygnet.DBAdapter``).

Required members in the Protocol: ``_in_transaction``,
``_transaction_task``, ``execute``, ``execute_one``.  Optional
methods (``stream`` and ``column_defaults``) stay duck-typed via
``hasattr`` at the consumer sites — explicitly documented in the
Protocol's docstring and in README's "The db object" section.  This
preserves the opt-in nature (adapters without these methods get the
historical behaviour) while making the documentation surface
discoverable.

``runtime_checkable`` lets ``isinstance(my_adapter, DBAdapter)``
work as a conformance smoke-test for adapter authors.  The public
entry points (``SELECT`` / ``INSERT`` / ``UPDATE`` / ``DELETE`` /
``TRUNCATE`` / ``get`` / ``follow`` / ``create`` / ``save`` /
``transaction`` / ``flush_column_defaults``) all carry ``db:
DBAdapter`` annotations now, replacing the previous ``db: Any``.
The capability-set alt (``adapter_capabilities() -> set[str]``) was
rejected as premature for two optionals.

### ~~S5. `InsertBuilder` ON CONFLICT cluster — 6 methods, 5 state fields~~  *[2026-04-29 #3] — CLOSED 2026-05-22*

Closed by introducing a ``_OnConflictSpec`` frozen dataclass (in
``cygnet/builders.py``, just above ``InsertBuilder``).  Five sibling
state attributes (``_on_conflict_target / _constraint / _action /
_set / _excluded``) collapsed into one ``_on_conflict:
_OnConflictSpec | None`` slot.  Structural invariants migrated to
``__post_init__``: target/constraint mutex, action="update" requires
target+exactly-one-of-set/excluded, action="nothing" valid with any
target shape (including none — preserves the ``ON_CONFLICT_DO_NOTHING``
shorthand).

Builder methods use ``dataclasses.replace`` to update the spec
atomically — multi-field updates (``action`` + ``set_kwargs`` in one
call) run ``__post_init__`` once with the final shape.  Only one
chain-time guard stayed at method level: ``DO_NOTHING`` requires a
preceding target because the spec legitimately allows
``action="nothing"`` with no target (the shorthand path is valid).
The executor's ``_render_on_conflict`` reads a single ``spec``
variable instead of five sibling attributes.  All 21 tests in
``tests/test_on_conflict.py`` pass unchanged (the same error-message
substrings remain).

### ~~S6. Executor function-local imports of `cte`/`proxy`~~  *[2026-04-29 #4] — CLOSED 2026-05-22*

Closed by lifting ``Lateral``, ``RecursiveCTE``, and ``ColumnProxy``
to module-scope imports in ``cygnet/executor.py``.  Verified no cycle
(``cte.py`` imports proxy lazily inside its constructor).  Module
docstring annotated to explain that the ``from .builders import
InsertBuilder`` inside ``run_save`` stays function-local — that one
IS a real module-level cycle.

### ~~S7. Broad `Any` in builder state~~  *[2026-04-29 typing] — CLOSED 2026-05-22*

Closed by **S5**'s refactor.  The two tuple slots that ISSUES.md
called out (``_on_conflict_target`` and ``_on_conflict_excluded``)
moved into ``_OnConflictSpec.target`` and
``_OnConflictSpec.excluded_cols``, both typed as
``tuple[ColumnProxy[Any], ...] | None`` — the static type now matches
the executor's runtime ``isinstance(c, ColumnProxy)`` check, which
stays as belt-and-suspenders against direct (non-builder) spec
construction.  The broader ``_obj: Any`` story is still deferred
until/unless a ``DataclassWithTable`` Protocol exists.

### ~~S8. `_PseudoField` and CTE's TableMeta-shaped surface need a Protocol~~  *[2026-04-29 typing] — CLOSED 2026-05-22*

Closed by adding three Protocols to ``cygnet/expression.py`` (next to
the existing ``SQLRenderable``):

- ``FieldLike`` — the minimum field-meta surface (``attr_name``,
  ``column_name``, ``primary_key``, ``foreign_key``).  Declared with
  ``@property`` so that both ``FieldMeta`` (regular dataclass with
  settable attrs) and ``_PseudoField`` (frozen dataclass with
  read-only attrs) conform structurally.
- ``MetaProtocol`` — the minimum table-meta surface (``table_name``,
  ``fields``, ``pk``, ``cls``).  ``fields`` is typed
  ``Sequence[FieldLike]`` rather than ``list[FieldLike]`` so
  ``list[FieldMeta]`` satisfies it under covariance (lists are
  invariant; Sequence is covariant).
- ``TableSourceProtocol`` — what ColumnProxy / executor actually
  consume off a "table source": ``_sql_name``, ``_meta``, ``_alias``.
  ``TableProxy`` / ``CTE`` / ``RecursiveCTE`` / ``Lateral`` all
  conform.

``ColumnProxy.__init__`` retyped from ``(TableProxy[Any], FieldMeta)``
to ``(TableSourceProtocol, FieldLike)``.  Two ``# type: ignore[arg-type]``
lines in ``cte.py`` removed.  The broader ``Any`` story on
``_PseudoField.primary_key / foreign_key`` is left as the Protocol
declares ``Any`` — the executor only checks ``is None`` / ``== DBKey``
through that field, so a tighter type adds no static safety.

Mypy: 0 errors across all 15 source files.  494 tests still green.

### ~~S9. `psycopg.ProgrammingError` swallow in `execute`~~  *[2026-04-29 exception hygiene] — CLOSED 2026-05-22*

Closed by replacing the ``try: fetchall except ProgrammingError``
swallow with ``if cur.description is None: return []`` —
the deterministic DB-API contract test for "no result set".
Insulates Cygnet from a future psycopg release narrowing or renaming
the exception class.  No test change needed: the existing DML-
without-RETURNING coverage (every UPDATE / DELETE / DDL test) is the
regression surface.

### ~~S10. `transaction(db)` offers no task-locality guard~~  *[2026-04-29 concurrency] — CLOSED 2026-05-22*

Closed by adding an ``asyncio.current_task()`` fingerprint at the
outermost ``transaction.__aenter__``: stored on
``db._transaction_task`` alongside the ``_in_transaction`` flag, then
checked at every nested ``__aenter__``.  Cross-task nesting now
raises ``RuntimeError`` with a clear message instead of silently
SAVEPOINTing inside another task's transaction.

Implementation notes:
- The guard is best-effort: a ``None`` current task (outside any task
  context) skips the check, and a ``None`` stored owner (older code
  that flipped ``_in_transaction`` without going through
  ``cygnet.transaction``) is treated as "no claim" — preserves
  backward compatibility for adapters that manage transactions
  externally.
- Both ``_in_transaction`` and ``_transaction_task`` are cleared in
  the outermost ``__aexit__``'s finally block, so sequential
  cross-task use (A's transaction commits before B starts) continues
  to work unchanged.

Coverage:
- ``test_cross_task_nesting_raises`` — deterministic interleave via
  ``asyncio.Event`` proving the concurrent case raises.
- ``test_sequential_cross_task_transactions_work`` — proves the
  finally-block cleanup keeps the sequential case working.

README's Concurrency Caveat updated to mention the runtime guard.

### ~~S11. `lit()` doesn't document the `$N`-rewrite trap~~  *[2026-04-29 docs] — CLOSED 2026-05-22*

Closed alongside **S1**. ``lit()``'s docstring now has a "Caveat for
adapters that translate placeholder syntax" paragraph naming
``PsycopgDB`` explicitly and giving the ``'$' || '1'`` workaround.

### ~~S12. `psycopg_db.column_defaults` docstring claims search_path lookup~~  *[2026-05-22-deepdive docs] — CLOSED 2026-05-22*

Closed by the B1 fix. The docstring is now accurate: the code does
honour search_path (via `to_regclass`), and the prose describes that
mechanism explicitly.

### ~~S13. `psycopg_db.column_defaults` comment mentions `pg_get_expr` it doesn't call~~  *[2026-05-22-deepdive docs] — CLOSED 2026-05-22*

Closed by the B1 fix. The new comment describes the actual SQL —
`pg_attribute JOIN pg_attrdef` keyed by `(adrelid, adnum)` — and
notes the system-column / dropped-column filters explicitly. The
`pg_get_expr` reference is gone (it was never in the query path).

### ~~S14. README save() doc doesn't mention DEFAULT suppression~~  *[2026-05-22-deepdive docs] — CLOSED 2026-05-22*

Closed alongside **B3**. The README's save() section now describes the
DEFAULT-aware behaviour directly: None-valued fields with a schema
DEFAULT are omitted from both INSERT cols and SET, then refreshed via
RETURNING, with the practical consequences spelled out
("created_at = None now means leave the DB's value alone").

### ~~S15. `tests/test_stubs.py` lacks parameterised-generic coverage~~  *[2026-05-22-deepdive tests] — CLOSED 2026-05-22*

Closed alongside **B4**. The new fixture (`Doc` in `tests/conftest.py`)
and test (`test_parameterised_generics_keep_their_params`) cover both
`list[str]` and `dict[str, int]` and assert the bug-symptom strings
(`ColumnProxy[list]`, `ColumnProxy[dict]`) are absent.

### ~~S16. FakeDB has no `column_defaults` template~~  *[2026-05-22-deepdive tests] — CLOSED 2026-05-22*

Closed by moving ``DefaultsFakeDB`` from ``tests/test_builders.py``
into ``tests/conftest.py`` alongside its parent ``FakeDB``.  The
docstring now describes its role as the protocol-shape reference for
the optional ``column_defaults(table_name) -> set[str]`` method:
custom-adapter authors can subclass or copy-paste it as the
known-good implementation to diff their own against.

### ~~S17. CI no scheduled audit run~~  *[2026-05-22-deepdive CI] — CLOSED 2026-05-22*

Closed by adding ``schedule: - cron: "0 4 * * 1"`` to the top-level
``on:`` block of ``.github/workflows/ci.yml``.  Non-audit jobs (unit,
integration, build, bench) are gated with
``if: github.event_name != 'schedule'`` so the cron only re-runs the
audit step — code hasn't changed since the last push, so re-running
unit/integration/build would be pure CI cost.  CVEs in dev deps now
surface within a week even when no PRs are pushing.

### ~~S18. `uv.lock` vs pip-based workflow~~  *[2026-05-22-deepdive packaging] — CLOSED 2026-05-22*

Closed via **OQ3** (resolved to "uv is canonical"):

- ``justfile``: ``bootstrap`` / ``install`` / ``bootstrap-bench`` now
  run ``uv sync --extra dev`` (and ``--extra bench`` where relevant).
  All ``ruff`` / ``mypy`` / ``pytest`` invocations go through
  ``uv run`` so they pick up lockfile-pinned versions.  ``hatch build``
  and ``hatch publish`` use ``uvx --from hatch`` for ephemeral isolation.
- ``.github/workflows/ci.yml``: every job's ``setup-python`` +
  ``pip install`` pair replaced with ``astral-sh/setup-uv@v6`` +
  ``uv sync --locked --extra dev`` (``--locked`` fails if uv.lock
  drifted from pyproject.toml, catching unintended graph changes in
  CI).  Tool calls go through ``uv run``.
- ``uv.lock`` regenerated to reflect current ``pyproject.toml``.
- README's "Development" section now documents ``uv`` as a prereq.

End-user install (``pip install cygnet-orm[psycopg]``) is unaffected:
the published package stays driver-agnostic and pip-installable.
Only the dev workflow migrated.

### ~~S19. CLAUDE.md JOIN family claims missing verbs~~  *[2026-05-22-deepdive docs; comment-run #6] — CLOSED 2026-05-22*

Closed by implementing both missing verbs rather than trimming the
docs.

- ``SelectBuilder.RIGHT_JOIN(table, *, ON)`` — appends a ``("RIGHT", …)``
  entry to ``_joins``; emitted as ``RIGHT JOIN tablename ON …``.
- ``SelectBuilder.FULL_JOIN(table, *, ON)`` — appends ``("FULL", …)``;
  emitted as ``FULL JOIN tablename ON …``.

Row-mapping extended symmetrically.  Extracted a
``_object_or_none_if_miss`` helper (consolidates the previous LEFT
JOIN PK-vs-all-NULL logic) and a per-join ``can_miss`` decision:
LEFT/FULL → right side can miss; RIGHT/FULL → left side can miss;
INNER → neither.  Result tuples are now
``(left_obj_or_None, right_obj_or_None, …)`` — INNER and pure-LEFT
queries preserve the historical contract (left never None).

Coverage: SQL-emission unit tests in
``TestSelectSQL::test_right_join`` and ``test_full_join``; row-mapping
unit tests in ``TestRowMapping`` covering the left-miss and both-can-
miss cases; integration tests in
``TestOuterJoinRoundtrip::test_right_join_preserves_unmatched_right``
and ``test_full_join_preserves_both_sides``.

CLAUDE.md and README updated with the new verbs and the row-mapping
contract.

### ~~S20. `cte.py` header says recursive CTEs are out of scope~~  *[comment-run #7] — CLOSED 2026-05-22*

Closed by rewriting the file header.  The lead paragraph now lists all
three shapes (CTE, RecursiveCTE, Lateral) and the obsolete "out of
scope" + "(Update:…)" two-step is gone.

### ~~S21. INSERT.INTO / DELETE.FROM re-call clobber undocumented~~  *[comment-run #13] — CLOSED 2026-05-22*

Closed by adding docstrings to ``InsertBuilder.INTO`` and
``DeleteBuilder.FROM`` that mirror ``UpdateBuilder.SET``'s clobber-
documentation pattern. SQL has a single target slot for each verb, so
clobber-on-recall is the honest behaviour; the docstrings make that
explicit.

### ~~S22. `Predicate.__invert__` return-type annotation diverges from siblings~~  *[comment-run #12] — CLOSED 2026-05-22*

Closed by harmonising the annotation to ``-> PrefixOp`` (matching the
``__invert__`` methods on ColumnProxy / FunctionCall / WindowExpression /
SuffixOp).  ``from __future__ import annotations`` was already in
effect; added a ``TYPE_CHECKING`` import for PrefixOp to keep ruff
happy without introducing the runtime cycle.

### ~~S23. `transaction._savepoint` mutable across `async with` reuse~~  *[comment-run #11] — CLOSED 2026-05-22*

Closed by beefing up the ``transaction`` docstring with an explicit
"Instance reuse" paragraph: sequential reuse is fine (``__aenter__``
resets ``_savepoint``), concurrent re-entry of the SAME instance is
unsupported (race on the field). The docstring now points users to
"construct a fresh ``transaction(db)`` per task" for parallel contexts
and reiterates the existing not-task-local caveat on the adapter.

### ~~S24. `_extract_insert_fields` returns an unused `values` accumulator~~  *[comment-run #14] — CLOSED 2026-05-22*

Closed by trimming the return tuple from
``(columns, values, omitted)`` to ``(columns, omitted)``.  Internal
refactor — no external API impact.  Updated all five call sites and
the function's docstring; values continue to flow through the
``params`` list as before.

### ~~S25. Aliased-proxy DML claim in `proxy.py` is unverified~~  *[comment-run #9] — CLOSED 2026-05-22*

Audit found the claim was worse than aspirational — actively wrong.
The executor strips the alias from the DML target (uses
``meta.table_name`` directly), but the ColumnProxies stamped onto an
aliased view still emit the alias on the left of the dot, so any
WHERE / SET RHS referencing the aliased proxy resolves to an
undefined identifier (PG: "missing FROM-clause entry"). Confirmed by
running ``UPDATE(db).SET(AT, name='x').WHERE(AT.id == 5)`` against a
``T.AS('a')`` proxy: the emitted SQL was ``UPDATE accounts SET name =
$1 WHERE (a.id = $2)`` — alias not in scope.

Closed by:
- Adding a ``_reject_aliased_dml`` helper in
  ``cygnet/builders.py`` that raises ``ValueError`` if a DML target
  proxy carries ``_alias``.
- Calling it from ``InsertBuilder.INTO``, ``UpdateBuilder.SET``, and
  ``DeleteBuilder.FROM``.
- Rewriting the AS() docstring in ``cygnet/proxy.py`` to describe the
  actual constraint ("SELECT-side only; DML raises ValueError").

Coverage: four new unit tests in
``tests/test_builders.py::TestAliasedDMLRejected`` — one per verb plus
a "unaliased still works" sanity test.

### ~~S26. `meta.TableMeta` caches before introspection runs~~  *[comment-run #10] — CLOSED 2026-05-22*

Closed by wrapping ``_introspect`` in ``TableMeta.__init__`` with a
``try/except`` that pops the half-built entry from ``_cache`` on any
exception.  Preserves the "every cached entry is fully initialised"
invariant against code that pokes ``_cache`` directly.  Regression
test: ``TestTableMeta::test_failed_introspection_evicts_cache``.

### ~~S27. `psycopg_db.stream()` docstring overstates portal-cursor requirement~~  *[comment-run #8] — CLOSED 2026-05-22*

Closed by softening the comment in ``PsycopgDB.stream()``. New wording
acknowledges that psycopg3 will start an implicit transaction if none
is active, but recommends an explicit ``cygnet.transaction(db)``
wrapper for deterministic lifetime — same practical advice, accurate
about the underlying mechanics.

### ~~S28. `run_insert` AppKey + omitted + row=None silent-skip~~  *[comment-run #5] — CLOSED 2026-05-22*

Closed by adding the symmetric defensive raise to the AppKey + omitted
branch in ``run_insert``: ``RuntimeError("INSERT...RETURNING produced
no row for X — driver bug or row not inserted")``, with the same
``b._on_conflict_action`` escape hatch the DBKey path already uses
for ON CONFLICT DO NOTHING.  Regression test:
``TestInsertDefaultColumnOmission::test_appkey_omitted_default_none_row_raises``.

### ~~S29. Cache-miss race in `_get_defaulted_columns`~~  *[2026-05-22-deepdive concurrency] — CLOSED 2026-05-22*

Closed alongside **B2**. The `_get_defaulted_columns` body now uses
`self._defaults_cache.setdefault(self._db, {})` — a single atomic
dictionary operation that returns the existing per-table dict if any
task has already installed one, eliminating the get-then-set window
where two tasks could each construct a fresh dict and clobber each
other's writes. Net code is also simpler (one early `TypeError`
branch covers the unhashable-adapter fallback).

---

## Open issues — nits

### ~~N1. `_columns` field assigned without annotation~~  *[2026-04-29 typing] — CLOSED 2026-05-22*

Closed: ``self._columns: tuple[SQLRenderable, ...] = columns`` in
``SelectBuilder.__init__``.  Matches the explicit-annotation pattern
of sibling fields.

### ~~N2. `EXCEPT_` trailing underscore~~  *[2026-04-29 NIT] — CLOSED 2026-05-22 (no fix needed)*

Resolved as a no-op: ``except`` is a Python reserved word, the
trailing-underscore convention is documented in README, and there's
no improvement available without renaming the SQL keyword.  Kept
in this list for traceability.

### ~~N3. Two `from .proxy import ColumnProxy` calls inside two methods~~  *[2026-04-29 nits] — CLOSED 2026-05-22*

Closed alongside **S6**: both function-local ``ColumnProxy`` imports
(and the two ``cte`` ones) lifted to module scope in ``executor.py``.

### ~~N4. `_render_select` iterates `_joins` twice~~  *[2026-04-29 perf] — CLOSED 2026-05-22 (acceptable)*

Resolved as acceptable: for typical join counts (1-3) the cost is
negligible, and the two passes do semantically different work
(column projection then join emission).  Folding them would couple
unrelated logic for no measurable benefit.

### ~~N5. Inline comment example `~~cygnet.exists(any_log)` would help~~  *[2026-04-29 tests] — CLOSED 2026-05-22*

Closed: inline comment added at
``tests/test_subquery.py::test_double_invert_collapses_to_exists``
explaining that the double tilde is intentional and what each
application of ``~`` does to a Predicate.

### ~~N6. First-INSERT-per-table introspection round-trip~~  *[2026-05-22-deepdive perf] — CLOSED 2026-05-22 (acceptable)*

Resolved as acceptable: the one-time round-trip per (adapter, table)
is the cost of correct DEFAULT-aware codegen, and it amortises away
after the first INSERT.  The cache invalidation knob
(``cygnet.flush_column_defaults``) makes it a knowable cost rather
than an opaque one.  No fix needed.

### ~~N7. Bare `except TypeError` in cache-write~~  *[2026-05-22-deepdive exception] — CLOSED 2026-05-22*

Closed alongside the **B2 + S29** cache refactor: the
``except TypeError`` in ``Executor._get_defaulted_columns`` now
carries an explanatory comment naming the unhashable-adapter case
(``WeakKeyDictionary`` refusing the key) and explaining the
skip-caching fallback.

---

## Open questions

### ~~OQ1. Is the `run_save` DEFAULT divergence intentional and permanent?~~  *— RESOLVED 2026-05-22*

Resolved in favour of "close the gap". `run_save`'s upsert path now
mirrors `run_insert` / `run_create` on DEFAULT handling. Closed via
**B3**.

### ~~OQ2. Should the duck-typed adapter contract become a formal `Protocol`?~~  *— RESOLVED 2026-05-22 (yes, formal Protocol)*

Resolved in favour of a formal ``@runtime_checkable`` Protocol.
Closed via **S4**: ``cygnet.DBAdapter`` now declares the required
adapter surface; optional methods (``stream`` / ``column_defaults``)
stay duck-typed via ``hasattr``.

### ~~OQ3. Is `uv.lock` authoritative, advisory, or accidental?~~  *— RESOLVED 2026-05-22 (uv canonical)*

Resolved: uv is canonical for the dev workflow.  Closed via **S18**
— justfile + CI both flow through ``uv sync --locked`` and
``uv run`` now.  End-user ``pip install cygnet-orm`` unchanged.

### ~~OQ4. Should the audit job's `--strict` block on CVEs?~~  *— RESOLVED 2026-05-22 (block on CVE)*

Resolved: the audit job blocks on real CVEs.  Closed via **B5** —
``pip-audit --skip-editable`` exits non-zero only when a real CVE
is found in dep tree (the contradictory ``--strict || true`` was
swallowing both the editable-skip false positive AND real CVEs).

### ~~OQ5. Is the `op()` 1-arg factory-factory pulling its weight?~~  *— RESOLVED 2026-05-22 (keep all three)*

Resolved in favour of keeping all three arities.  The 1-arg form is
genuinely useful for callers using the same non-standard operator
repeatedly (the canonical example: ``ILIKE = cygnet.op("ILIKE")``
followed by multiple ``ILIKE(col, val)`` invocations), and the
infrequent usage just means few existing tests need it — not that
the API is wrong.

Closure: README's operators section now carries an explicit
"three arities" block (`README.md`, after the inline-operators
paragraph) with a worked example of the factory form.  The
docstring in ``cygnet/expression.py`` already documented the use
case; the README addition makes it discoverable from the entry
point.

### ~~OQ6. Should the comment-run "extend in place" pattern be sanctioned?~~  *— RESOLVED 2026-05-22 (yes, EXTEND is sanctioned)*

Resolved: yes.  The ``/comment-run`` skill at
``~/.claude/commands/comment-run.md`` now lists three patterns
explicitly:

- **ADD** — no comment yet; write one.
- **LEAVE** — existing comment accurate and complete.
- **EXTEND** — existing comment correct as far as it goes but
  doesn't cover a tradeoff / constraint / invariant.  Append to it,
  preserving the original wording.

The unifying rule is preserved: never DELETE existing comment text.
EXTEND appends only.

Guardrail: if an existing comment is actively *wrong* (not just
incomplete), EXTEND is the wrong tool — memorialize the mismatch
for later decisions instead.  EXTEND is for honest gaps; mismatches
between comment and code should be surfaced, not papered over.

---

## Out of scope (preserved for context)

- `psycopg[binary]>=3.1` lower bound with no upper bound. Trade-off,
  not clearly wrong.
- Action versions in `.github/workflows/ci.yml` not audited for
  supply-chain concerns. Low priority for an alpha-stage library.
- Node 20 deprecation in GitHub-hosted runners (cutoff September 2026).
  Action version bumps; track separately. (CI uses
  `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: "true"` as the interim opt-in.)

---

*Originally a 2026-04-22 fresh-eyes review at `main` @ `4ef08a2`.
Consolidated 2026-04-24 with `/comment-run` findings (2026-04-22) and
the deferred enhancement plan (2026-04-06). Phases 1–8 implemented
2026-04-24 (commits `9264b04` … `468c427`). Items 5.4 and 8.1 closed
2026-04-25. DEFAULT-aware INSERT landed 2026-05-17 in `a2156bf`
(partial fix for Apr 29 #5). Renamed REVIEW.md → ISSUES.md on
2026-05-22 and merged findings from review-20260429-175335.md,
comment-run-20260522.md, and review-20260522-084756.md.*
