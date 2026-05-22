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

### S1. `$N` → `%s` regex blind to string literals  *[2026-04-29 #2]*

`cygnet/psycopg_db.py:50-60`. The translation regex rewrites every
`$\d+` substring, including those embedded inside `cygnet.lit("'$1 prefix'")`.
The escape hatch is documented as trusted, but the literal-blindness
isn't called out anywhere.

**Direction of fix**: Either narrow the regex to skip string-literal
contexts (invasive — proper SQL tokenization), or document the
limitation on `lit()`'s docstring and `psycopg_db.py`'s header. The
library's stance ("lit is trusted") makes the second acceptable.

### S2. `_row_to_obj` zip-truncation is silent  *[2026-05-22-deepdive smell; comment-run #4]*

`cygnet/executor.py:1267-1286`. `zip(meta.fields, row)` silently
truncates the longer of the two. Docstring documents this; the
failure mode (silently-wrong objects) is severe relative to the
warning surface.

The renderer guarantees `len(row) == len(meta.fields)` in the
implicit-columns case, so this only bites with `cygnet.lit()` columns
that smuggle in extra positions, or hand-written `SELECT *` paths.

**Direction of fix**: `zip(meta.fields, row, strict=True)`.

### S3. `HAVING` docstring promises a check it doesn't enforce  *[2026-05-22-deepdive smell; comment-run #3]*

`cygnet/builders.py` (HAVING method). The docstring claims `cygnet.all`
is rejected, but no `isinstance(predicate, _All): raise` guard exists.
A caller can pass `cygnet.all` and the sentinel renders through to
SQL — probably harmless but inconsistent with WHERE's stricter rule.

**Direction of fix**: Add the explicit isinstance check, or relax the
docstring.

### S4. `column_defaults` as optional-via-hasattr fragments the adapter protocol  *[2026-05-22-deepdive API design]*

`cygnet/executor.py:_get_defaulted_columns` (`hasattr` probe) +
`cygnet/psycopg_db.py:column_defaults`. The duck-typed db contract
was three methods (`execute`, `execute_one`, `stream`); the May 17
commit added a fourth as *optional*, probed via `hasattr`. With one
optional method this is fine; with two it becomes a documentation
problem. Today's pattern doesn't scale to "does this adapter
support X? does it support Y?" matrix.

**Direction of fix**: Formalize with `typing.Protocol` (decorated
`runtime_checkable`), document optionality explicitly. Or expose
capabilities via `adapter_capabilities() -> set[str]` to keep
detection in one place. See **OQ2**.

### S5. `InsertBuilder` ON CONFLICT cluster — 6 methods, 5 state fields  *[2026-04-29 #3]*

`cygnet/builders.py:586-720`. Six methods sharing five `_on_conflict_*`
state fields with cross-validation duplicated across method bodies.
Works and is well-tested. Adding any sixth axis (e.g., `ON CONFLICT …
DO UPDATE … WHERE …`, which PG supports) forces another bit-flag dance
across method bodies.

**Direction of fix**: A single `_OnConflictSpec` frozen dataclass with
`target / action / set_kwargs / excluded_cols`, validated in
`__post_init__`. One state object, one place to validate.

### S6. Executor function-local imports of `cte`/`proxy`  *[2026-04-29 #4]*

`cygnet/executor.py:117/181/419/481`. Inline `from .cte import …` and
`from .proxy import ColumnProxy` calls "to avoid the circular dep".
Verified that no cycle exists today: `cte.py` does its `proxy` import
lazily inside its constructor, so `executor → cte → proxy` is acyclic
at module-load time. Same dead-weight pattern that `builders.py`
already cleaned up.

**Direction of fix**: Lift to module level. Leave the
`from .builders import InsertBuilder` on line ~1221 in place — that
one IS a real cycle (builders → executor at module level).

### S7. Broad `Any` in builder state  *[2026-04-29 typing]*

`cygnet/builders.py` has 53 `Any` occurrences. Several are necessary
(`db: Any` for duck typing). Speculative ones include
`_on_conflict_target: tuple[Any, ...] | None` and `_on_conflict_excluded:
tuple[Any, ...] | None` — both could be
`tuple[ColumnProxy[Any], ...] | None` since the executor immediately
isinstance-checks for `ColumnProxy`.

**Direction of fix**: Tighten the on_conflict tuple types. Defer the
broader `_obj: Any` story until a `DataclassWithTable` Protocol exists.

### S8. `_PseudoField` and CTE's TableMeta-shaped surface need a Protocol  *[2026-04-29 typing]*

`cygnet/cte.py:21-37` (`_PseudoField`) uses `primary_key: Any = None`
and `foreign_key: Any = None` defaults. Lines 122-134 expose CTE's
`table_name / fields / pk / cls` to mimic TableMeta. The
`# type: ignore[arg-type]` on the `setattr(self, col, ColumnProxy(…))`
line is the tell: CTE is structurally TableProxy-like but isn't typed
as one.

**Direction of fix**: Extract a `TableSourceProtocol` (Protocol with the
four properties + a `FieldLike` Protocol for `_PseudoField`). Use as
the union member in `TableSource = …` and as the return type of
`_meta`.

### S9. `psycopg.ProgrammingError` swallow in `execute`  *[2026-04-29 exception hygiene]*

`cygnet/psycopg_db.py:71-74`:
```python
try:
    return await cur.fetchall()
except psycopg.ProgrammingError:
    return []
```
ProgrammingError is broad. Intent is "no result set" (DML without
RETURNING, DDL, etc.); psycopg uses ProgrammingError there. A future
psycopg version could narrow or change this. `cur.description is None`
is a deterministic test for "no result set".

**Direction of fix**: `if cur.description is None: return []` plus a
`return await cur.fetchall()` else-branch.

### S10. `transaction(db)` offers no task-locality guard  *[2026-04-29 concurrency]*

`cygnet/__init__.py:230-308`. The class docstring and the README's
Concurrency Caveat both call out that `_in_transaction` is not
task-local. Strong documentation, no runtime guard. A user who reuses
one PsycopgDB across two `asyncio` tasks silently corrupts nesting.

**Direction of fix**: Optional `__aenter__` fingerprint via
`asyncio.current_task()`; raise loudly if it diverges from the task
that flipped `_in_transaction = True`. Cost: one
`asyncio.current_task()` per nesting boundary. Benefit: the failure
mode is hard to debug, so a loud check pays for itself.

### S11. `lit()` doesn't document the `$N`-rewrite trap  *[2026-04-29 docs]*

`cygnet/__init__.py:122-129`. `lit()`'s docstring says "the SQL is
emitted verbatim — no escaping, no parameter substitution. The
string is trusted." True at the Cygnet layer. But combined with
`psycopg_db.py`'s `$N`→`%s` regex (**S1**), a `lit()` containing
`$1` substring gets rewritten downstream. Docstring should mention
that adapters may translate placeholders.

**Direction of fix**: Add a sentence to `lit()`'s docstring noting
that the reference psycopg adapter rewrites `$\d+` patterns and that
`lit()` consumers should avoid those substrings.

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

### S16. FakeDB has no `column_defaults` template  *[2026-05-22-deepdive tests]*

`tests/conftest.py:55-92`. By design — preserves existing tests. But
that means `_DefaultedFakeDB` in `test_builders.py` is the only
non-PG fixture covering the DEFAULT-aware path. A custom adapter
that mis-shapes `column_defaults` (returns a list of tuples, returns
None, returns whitespace in names) has no fixture to validate
against.

**Direction of fix**: Expose `_DefaultedFakeDB` as a fixture (or add
a `make_fake_defaulted_db` factory) so consumers writing custom
adapters can borrow the contract.

### S17. CI no scheduled audit run  *[2026-05-22-deepdive CI]*

`.github/workflows/ci.yml`. No `schedule:` trigger. CVEs that fire
between PRs surface only on the next push. Weekly cron on the audit
job is the standard mitigation.

**Direction of fix**: Add `schedule: - cron: "0 4 * * 1"` (Monday
04:00 UTC) gating the audit job.

### S18. `uv.lock` vs pip-based workflow  *[2026-05-22-deepdive packaging]*

`uv.lock` is checked in; `pyproject.toml` and the `justfile` use pip
exclusively. Either uv is canonical (in which case `just bootstrap`
should `uv sync` and CI's install step should use uv) or pip is
canonical (in which case the lockfile should be gitignored).

**Direction of fix**: Pick one. See **OQ3**.

### S19. CLAUDE.md JOIN family claims missing verbs  *[2026-05-22-deepdive docs; comment-run #6]*

CLAUDE.md lists `RIGHT_JOIN` and `FULL_JOIN` as part of the JOIN
family. Neither exists in `cygnet/builders.py` or `cygnet/executor.py`.

**Direction of fix**: Trim CLAUDE.md, or implement them (one method
each in the JOIN cluster — straightforward addition).

### S20. `cte.py` header says recursive CTEs are out of scope  *[comment-run #7]*

`cygnet/cte.py` top-of-file header originally said "Recursive CTEs are
deliberately out of scope for this initial pass" — but `RecursiveCTE`
and `recursive_cte()` are implemented in the same file. The May 22
comment-run pass appended an `(Update: …)` clarification per the
no-deletion rule. The original misleading line still reads as the
top sentence.

**Direction of fix**: Rewrite the header so the recursive support is
described from line 1, with the "Update" note merged in or removed.

### S21. INSERT.INTO / DELETE.FROM re-call clobber undocumented  *[comment-run #13]*

`cygnet/builders.py` `INTO` and `FROM` (DeleteBuilder) silently swap
the target when called twice. Consistent with `UpdateBuilder.SET`'s
clobber behaviour (now documented in the May 22 comment pass), but
neither `INTO` nor `FROM` has a corresponding note.

**Direction of fix**: Either reject the second call (defensive) or
add a docstring sentence noting the clobber semantics.

### S22. `Predicate.__invert__` return-type annotation diverges from siblings  *[comment-run #12]*

`cygnet/predicate.py:Predicate.__invert__` is annotated `-> Any`.
`ColumnProxy.__invert__`, `FunctionCall.__invert__`,
`WindowExpression.__invert__`, and `SuffixOp.__invert__` all return
`-> PrefixOp` explicitly. Probably to dodge a lazy-import forward
reference.

**Direction of fix**: Either harmonize to `-> PrefixOp` (with the
forward reference as a string or `TYPE_CHECKING` import) or add a
code comment explaining why this one diverges.

### S23. `transaction._savepoint` mutable across `async with` reuse  *[comment-run #11]*

`cygnet/__init__.py` `transaction`. The class docstring suggests a
single instance can be reused across sequential `async with` blocks.
`__aenter__` resets `self._savepoint = None`, but `_savepoint` is
instance state, so concurrent re-entry (already documented
unsupported) would leak savepoint names. Sequential reuse is fine.
Behaviour matches contract; the implicit-mutable-state pattern is
worth a comment beyond what's there.

**Direction of fix**: Either change to a local-variable approach (no
mutable instance state across exits), or document the constraint more
loudly. Most defensible: keep the state, beef up the docstring.

### S24. `_extract_insert_fields` returns an unused `values` accumulator  *[comment-run #14]*

`cygnet/executor.py` returns `(columns, values, omitted)`. Every
caller unpacks all three slots, but the `values` accumulator's
contents never get used downstream — the actual params come from the
rendered placeholders. Documented as part of the return tuple.

**Direction of fix**: Trim to `(columns, omitted)`, or add a code
comment explaining why the unused field stays in the tuple. Removing
it would be a small internal refactor with no external API impact.

### S25. Aliased-proxy DML claim in `proxy.py` is unverified  *[comment-run #9]*

`cygnet/proxy.py` aliased-proxy docstring says: "INSERT / UPDATE /
DELETE on an aliased proxy work, but PG doesn't allow `AS` in DML
target position; if a real need ever surfaces, the executor would
need to drop the alias for those verbs." The commenting agent did
not audit whether `_render_insert / _render_update / _render_delete`
strip the alias. Reads as aspirational.

**Direction of fix**: Audit. If executor doesn't strip, either add the
strip + test, or rephrase the docstring to say "not currently
supported; pass `Table(cls)` (unaliased) for DML."

### S26. `meta.TableMeta` caches before introspection runs  *[comment-run #10]*

`cygnet/meta.py` `TableMeta.__new__` inserts the new instance into
`_cache` before `_introspect` runs. If introspection raises, the
half-built instance is left in the cache. The next `TableMeta(cls)`
short-circuits via the `_initialised` guard, returning a TableMeta
with `fields=[]` and `pk=None`. The existing in-file comment claims
the opposite ("doesn't leave a fully initialised empty TableMeta in
the cache") — but that's about happy-path-with-no-fields, not the
failed-introspection case.

**Direction of fix**: Either evict on `_introspect` failure (cache in
a `try/except` and `del self._cache[cls]` on raise), or argue that
introspection failures are configuration bugs not worth retry semantics
— and update the comment to match.

### S27. `psycopg_db.stream()` docstring overstates portal-cursor requirement  *[comment-run #8]*

`cygnet/psycopg_db.py:99-109`. Says "PG requires the connection to be
in a transaction (or autocommit off) for portal cursors." In psycopg3,
`cursor.stream()` starts an implicit transaction if none is active.
The recommendation to wrap in `cygnet.transaction(db)` remains correct
(deterministic lifetime, explicit cleanup), but the strict-requirement
framing is slightly wrong.

**Direction of fix**: Soften the docstring — replace "PG requires …"
with "PG portal cursors are most predictable inside an explicit
transaction; wrap in `cygnet.transaction(db)` for explicit lifetime
control."

### S28. `run_insert` AppKey + omitted + row=None silent-skip  *[comment-run #5]*

`cygnet/executor.py:853-858`. AppKey path with omitted DEFAULT columns
calls `execute_one`; if `row is None`, the AppKey path silently
`setattr`-loops over zero rows and returns `None`. The DBKey branch
raises "driver bug or row not inserted" in the same situation —
asymmetric error handling.

**Direction of fix**: Match the DBKey branch's defensive raise, or add
a code comment explaining why AppKey doesn't need one (e.g., "AppKey
INSERTs that succeed always return a row, so None here is impossible
unless the driver is misbehaving — same as DBKey, so should raise the
same way").

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

### N1. `_columns` field assigned without annotation  *[2026-04-29 typing]*

`cygnet/builders.py:142`. `self._columns = columns` relies on
`*columns: SQLRenderable` typing through. Inconsistent with sibling
fields that carry explicit annotations.

**Direction of fix**: `self._columns: tuple[SQLRenderable, ...] = columns`.

### N2. `EXCEPT_` trailing underscore  *[2026-04-29 NIT]*

`cygnet/builders.py:469-472`. `except` is reserved; trailing underscore
documented in README. UNION/INTERSECT don't have one. Cosmetic.

**Direction of fix**: None — the language constraint is real.

### N3. Two `from .proxy import ColumnProxy` calls inside two methods  *[2026-04-29 nits]*

`cygnet/executor.py:419` and `:481`. Same import in two methods.
Subsumed by **S6** (lift to module level).

### N4. `_render_select` iterates `_joins` twice  *[2026-04-29 perf]*

`cygnet/executor.py:_render_select` bare-FROM path iterates `b._joins`
twice (once for SELECT list, once for JOIN clause). For typical join
counts (1-3) the cost is negligible.

### N5. Inline comment example `~~cygnet.exists(any_log)` would help  *[2026-04-29 tests]*

`tests/test_subquery.py:74`. Double-invert collapse is intentional but
not obviously so. Docstring on the test explains it; a short inline
comment would help future readers grep faster.

### N6. First-INSERT-per-table introspection round-trip  *[2026-05-22-deepdive perf]*

`cygnet/executor.py:_get_defaulted_columns`. First INSERT to a
never-seen table pays one synchronous round-trip for the
`information_schema.columns` lookup. Cached thereafter. Acceptable;
documented because it's a behaviour change vs. the pre-May-17
contract.

### N7. Bare `except TypeError` in cache-write  *[2026-05-22-deepdive exception]*

`cygnet/executor.py:104-112`. Narrow today (single-statement try
block), worth a comment noting which TypeError is expected
(unhashable adapter from `WeakKeyDictionary.__setitem__`).

---

## Open questions

### ~~OQ1. Is the `run_save` DEFAULT divergence intentional and permanent?~~  *— RESOLVED 2026-05-22*

Resolved in favour of "close the gap". `run_save`'s upsert path now
mirrors `run_insert` / `run_create` on DEFAULT handling. Closed via
**B3**.

### OQ2. Should the duck-typed adapter contract become a formal `Protocol`?

**Source**: 2026-05-22-deepdive OQ2.

The May 17 introduction of `column_defaults` as an optional method
changes the protocol's shape. With one optional method, `hasattr`
probing is fine. With two, the pattern feels like a capability
bitmap. Decide while the surface is small.

### OQ3. Is `uv.lock` authoritative, advisory, or accidental?

**Source**: 2026-05-22-deepdive OQ3.

Presence of a lockfile without a uv-driven workflow is the canonical
tooling-ambiguity smell. See **S18**.

### OQ4. Should the audit job's `--strict` block on CVEs?

**Source**: 2026-05-22-deepdive OQ4.

Today the `--strict || true` combination is contradictory. Either is
defensible; the current combination isn't. See **B5**.

### OQ5. Is the `op()` 1-arg factory-factory pulling its weight?

**Source**: 2026-04-29 OQ2 (unresolved).

`cygnet/expression.py:102-110`. Three behaviours under one name (1-arg
factory, 2-arg prefix, 3-arg infix), distinguished by arity at
runtime. The `@overload` decorations type-check correctly, but the
1-arg form is rarely used in tests (grep finds one direct use).
Tighten the public API by removing it?

### OQ6. Should the comment-run "extend in place" pattern be sanctioned?

**Source**: 2026-05-22 comment-run summary.

The three `git diff` deletions in the May 22 comment pass were all
*extensions* of existing comments (original wording preserved,
new content appended). The current "do not delete or rewrite any
existing comment" rule is unambiguous but conservative — extending in
place might be a useful third option alongside ADD and LEAVE for
future comment passes.

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
