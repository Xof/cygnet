# Comment Pass — Cygnet (cygnet-orm)

Date: 2026-05-22
Operator: Claude Code (7 parallel agents, one per file group)
Scope: every `.py` file under `cygnet/`
Prior comment pass: 2026-04-22 (consolidated into REVIEW.md, all items closed by 2026-04-25)
Prior fresh-eyes review: `docs/reviews/review-20260429-175335.md`

---

## What this pass did

A pure comment-only sweep across the package. Rules of the road:

- ADD comments only — never delete or rewrite existing ones.
- No refactoring, reordering, reformatting, or behavior changes.
- File-level header at the top of every file explaining its role in the
  system.
- Inside-file comments concentrated on *choices, tradeoffs, constraints,
  invariants, non-obvious side effects, and ordering dependencies* — not
  signature restatements.
- Anything discovered along the way that looks like a real bug, stale
  doc, or behavioural gap gets memorialized here (this file) instead of
  silently fixed.

### Files touched and approximate volume

| File | Blocks added | Lines (approx) |
|---|---|---|
| `cygnet/annotations.py` | 3 | ~6 |
| `cygnet/meta.py` | 3 | ~10 |
| `cygnet/expression.py` | 4 | ~12 |
| `cygnet/proxy.py` | 4 | ~12 |
| `cygnet/predicate.py` | 3 | ~14 |
| `cygnet/builders.py` | 14 | ~57 |
| `cygnet/executor.py` | 13 | ~75 net (+132/−1 rewrap) |
| `cygnet/cte.py` | 9 | ~35 |
| `cygnet/__init__.py` | 8 | ~50 |
| `cygnet/functions.py` | 1 | ~5 |
| `cygnet/jsonb.py` | 3 | ~16 |
| `cygnet/arrays.py` | 1 (large header) | ~12 |
| `cygnet/fts.py` | 2 | ~21 |
| `cygnet/stubs.py` | 3 | ~24 |
| `cygnet/psycopg_db.py` | 0 | 0 |
| **Total** | **~71 blocks** | **~349 lines** |

`psycopg_db.py` was intentionally left alone — its existing inline
documentation already matches the code accurately (the agent assigned
to it verified line-by-line and found no gaps).

### Verification

After the sweep:

- `just lint` (`ruff check cygnet tests`) — clean.
- `just fmt-check` (`ruff format --check`) — clean.
- `just typecheck` (`mypy --strict cygnet`) — clean.
- `just test` (full unit suite) — 396 / 396 pass, 90% coverage unchanged.

No behavior changed. Nothing imports differently. All four checks land
in the same state they did before the pass.

---

## Findings memorialized for follow-up

Severity scale matches `docs/reviews/review-20260429-175335.md`:
**BUG** (real defect), **SMELL** (latent / context-dependent),
**DRIFT** (doc disagrees with code), **NOTE** (observation only).

### 1. **BUG** — `cygnet/stubs.py` `_format_type` loses generic parameters

`_format_type` uses `getattr(t, "__name__", None)` to short-circuit on
"simple" types. But parameterised generics still carry `__name__`:
`list[str].__name__ == "list"`, `dict[str, int].__name__ == "dict"`.
The result is that `python -m cygnet.stubs <module>` emits
`ColumnProxy[list]` for an `Annotated[list[str], …]` column instead of
`ColumnProxy[list[str]]`.

Union types (`int | None`) are unaffected — `UnionType` has no
`__name__` and falls through to `str()`.

The function's docstring implies `list[str]` falls through to
`str()`, so the docstring matches the *intent* but the implementation
diverges. Surfaces the moment anyone declares a list/dict/set column
and regenerates stubs. **Not fixed in this pass.**

### 2. **BUG** — `cygnet/executor.py` `run_save` ignores DEFAULT-aware omission

`run_create` and the bare `INSERT` path both go through
`_extract_insert_fields`, which (when the adapter implements
`column_defaults`) omits None-valued fields whose column has a non-NULL
DEFAULT — letting the schema default fire. `run_save` does *not* take
this path: it emits every column with its current in-memory value,
including explicit `None` for DEFAULT-bearing columns.

Concretely: `save()` on an object with `created_at=None` against a
`created_at DEFAULT now()` column writes `NULL` (overriding the
default) and, on conflict, UPDATEs to `NULL` too. The fresh-INSERT
branch refreshes from RETURNING; the UPDATE branch does not, so the
in-memory `obj` and the DB diverge.

This is a known divergence from `run_create` semantics, but it is not
documented anywhere and the README's "`save()` always sends the full
object" line under-specifies the behaviour. Related to **prior review
item #5** (save() upsert RETURNING) which is still open. **Not fixed.**

### 3. **SMELL** — `HAVING` docstring promises a check it doesn't enforce

`builders.py` documents that `HAVING` rejects `cygnet.all`, but there's
no `isinstance(predicate, _All): raise` guard in the method body. A
caller can pass `cygnet.all` and the predicate will render through
whatever the executor does with the `_All` sentinel — most likely
`TRUE`, harmless in practice but not what the docs say. Either tighten
the implementation or relax the docstring. **Not fixed.**

### 4. **SMELL** — `executor._row_to_obj` zip-truncation is silent

`_row_to_obj` uses `zip(meta.fields, row)`, which silently drops the
longer of the two if they disagree. The renderer always emits exactly
`len(meta.fields)` columns in the implicit-columns case, so this only
bites with `cygnet.lit()` or hand-written column lists where the user
adds extras. Schema drift (DBA adds a column, model doesn't) would
also produce silently-wrong objects on `SELECT *` paths. Worth either
an assertion in dev mode or a docstring note. **Not fixed.**

### 5. **SMELL** — `run_insert` AppKey + omitted-default + row=None path

When `_obj is not None`, has omitted DEFAULT columns, and is using the
AppKey path, `execute_one` is called for RETURNING. If `row is None`
(an anomaly here — no ON CONFLICT in this branch), the AppKey path
silently `setattr`-loops over zero rows and returns `None`. The DBKey
branch raises "driver bug or row not inserted" in the same situation.
Asymmetric error handling; almost certainly benign in practice
(successful AppKey INSERTs return a row) but the absence of the
defensive raise is intentional vs. oversight is unclear. **Not fixed.**

### 6. **DRIFT** — `CLAUDE.md` lists JOIN variants that don't exist

`CLAUDE.md` and the public docs cite `RIGHT_JOIN` and `FULL_JOIN` as
part of the JOIN family. Neither is implemented in `builders.py` or
`executor.py`. Either restore them or trim the docs. The same comment
in this commenting pass conservatively listed only `INNER`/`LEFT` as
the live join kinds. **Doc fix recommended.**

### 7. **DRIFT** — `cte.py` header says "Recursive CTEs out of scope"

The original top-of-file header for `cte.py` claimed recursive CTEs
were "deliberately out of scope for this initial pass" — but
`RecursiveCTE` and `recursive_cte()` are implemented in the same file
(lines ~153–235). The commenting agent appended an `(Update: …)`
clarification per the no-deletion rule. Worth a real edit when convenient.

### 8. **DRIFT** — `psycopg_db.stream()` overstates portal-cursor requirement

The existing comment on `stream()` says "PG requires the connection to
be in a transaction (or autocommit off) for portal cursors". In
psycopg3, `cursor.stream()` actually starts an implicit transaction if
none is active. The recommendation to wrap in `cygnet.transaction(db)`
remains correct (deterministic lifetime, explicit cleanup), but the
strict-requirement framing is slightly wrong. **Doc tweak only.**

### 9. **NOTE** — `proxy.py` aliased-proxy DML claim is unverified

The aliased-proxy docstring says: "INSERT / UPDATE / DELETE on an
aliased proxy work, but PG doesn't allow `AS` in DML target position;
if a real need ever surfaces, the executor would need to drop the
alias for those verbs." The commenting agent did not audit whether
the executor's `_render_insert` / `_render_update` / `_render_delete`
strip the alias. Reads as aspirational. Worth a quick verification +
test.

### 10. **NOTE** — `meta.TableMeta.__new__` caches before introspection

`TableMeta.__new__` inserts the new instance into `_cache` before
`_introspect` runs. If introspection raises (a model annotation error,
say), the half-built instance is left in the cache. The next
`TableMeta(cls)` short-circuits via the `_initialised` guard and
returns a `TableMeta` with `fields=[]` and `pk=None`. The existing
comment on this guard says the opposite — that the cache won't end up
holding a "valid-looking empty TableMeta" — but it's about the
happy-path-with-no-fields case, not the failed-introspection case.
Reasonable people could argue introspection failures are configuration
bugs and we don't owe a retry contract; flagging anyway.

### 11. **NOTE** — `transaction._savepoint` is mutable across `async with` reuse

The `transaction(db)` async context manager's docstring suggests an
instance can be reused across sequential `async with` blocks.
`__aenter__` resets `self._savepoint = None`, but `_savepoint` is
instance state — concurrent re-entry from two tasks (already
documented as unsupported) would leak savepoint names. Single-task
sequential reuse is fine. The behaviour matches the contract, but the
implicit-mutable-state pattern is worth a comment beyond what's there.

### 12. **NOTE** — `Predicate.__invert__` return-type annotation drifts from siblings

`Predicate.__invert__` is annotated `-> Any`. `ColumnProxy.__invert__`,
`FunctionCall.__invert__`, `WindowExpression.__invert__`, and
`SuffixOp.__invert__` all return `-> PrefixOp` explicitly. The
inconsistency is probably to dodge a lazy-import forward reference,
but worth either harmonising or adding a comment explaining the
asymmetry.

### 13. **NOTE** — Undocumented re-call clobber on `INSERT.INTO` / `DELETE.FROM`

Calling either method twice silently swaps the table. Consistent with
`UpdateBuilder.SET`'s clobber behaviour (which is now documented by
this pass), but neither `INTO` nor `FROM` has a corresponding note.
Either harmless API design or a footgun, depending on user
expectations.

### 14. **NOTE** — `_extract_insert_fields` returns a tuple component nobody reads

The function returns `(columns, values, omitted)`. Every caller
unpacks all three slots, but the `values` accumulator is never *used*
downstream — the actual params come from the rendered placeholders.
Documented as part of the return tuple, so removing would be a public
shape change. Worth either trimming or noting why it stays.

---

## Cross-reference to REVIEW.md

`REVIEW.md` remains the single open-issues tracker per project memory.
Items in this file that rise to "behavioural defect" rather than
"comment-pass observation" — items 1 (`stubs._format_type`), 2
(`run_save` DEFAULT divergence), 6 (RIGHT_JOIN/FULL_JOIN doc drift) —
could reasonably be promoted to `REVIEW.md` entries. The remaining
items are either minor doc tweaks or "worth looking at once" notes
that probably don't earn an open-issue slot.

The 2026-04-29 fresh-eyes review's open question OQ1 (save() upsert
RETURNING) overlaps with finding #2 above. They're the same
underlying issue, viewed from different angles: the prior review
flagged it from the "no RETURNING on UPDATE" side; this pass found it
from the "no DEFAULT-aware omission on save" side. Resolving either
half of OQ1 should explicitly consider both.

## Things that were NOT done

- No code edits beyond comments.
- No reformatting.
- No `REVIEW.md` updates (left to a human decision per project policy
  on what gets tracked there).
- No follow-up spawn-tasks (the bugs noted here would benefit from
  dedicated work but the task-spawn channel is reserved for items the
  user is likely to want as separate sessions; these are listed here
  for batch triage instead).
