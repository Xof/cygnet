# Cygnet — Open Issues & Investigations

This file tracks outstanding work on Cygnet.

The 2026-04-22 fresh-eyes review and 2026-04-22 `/comment-run` findings
have all landed:

- **Phases 1–8** shipped 2026-04-24 (commits `9264b04` … `468c427`).
- **Item 5.4** (generic ColumnProxy) and **Item 8.1** (self-join test
  via aliasing) closed 2026-04-25.

The full original review is preserved in git history at commit
`1c55ce9` if the long-form rationale is ever needed.

---

## Decisions (preserved)

These four answers shaped the implementation; recorded so future
maintainers don't re-litigate them.

1. **Empty `UPDATE.SET(T)` raises** — the silent no-op was a bug.
2. **OFFSET / HAVING / DISTINCT / RETURNING-on-UPDATE-DELETE are in
   scope and built**, not out-of-scope.
3. **`cygnet.all` mixed with real predicates raises everywhere**,
   including SELECT — same rule UPDATE/DELETE enforce.
4. **REVIEW.md is the single open-issues tracker.**

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

The unit-level `test_multi_join_mapping` is left as-is: it tests row-
to-object mapping behaviour with FakeDB, which is its actual purpose.

---

## Out of scope (preserved for context)

- `psycopg[binary]>=3.1` lower bound with no upper bound. Trade-off,
  not clearly wrong.
- Action versions in `.github/workflows/ci.yml` not audited for
  supply-chain concerns. Low priority for an alpha-stage library.
- Node 20 deprecation in GitHub-hosted runners (cutoff September 2026).
  Action version bumps; track separately.

---

*Originally a 2026-04-22 fresh-eyes review at `main` @ `4ef08a2`.
Consolidated 2026-04-24 with `/comment-run` findings (2026-04-22) and
the deferred enhancement plan (2026-04-06). Phases 1–8 implemented
2026-04-24 (commits `9264b04` … `468c427`). Items 5.4 and 8.1 closed
2026-04-25.*
