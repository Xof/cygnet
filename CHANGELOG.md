# Cygnet Change Log

## Unreleased

## New Features:

* `cygnet.row(*items)` — SQL row-value constructors. `cygnet.row(T.a, T.b) == (1, "x")` renders `(t.a, t.b) = ($1, $2)`; rows compare against tuples, lists, or other rows with `=`, `!=`, and the ordering operators. Row ordering is lexicographic, which makes `cygnet.row(T.a, T.b) > (last_a, last_b)` the correct keyset-pagination predicate over a multi-column sort. A `None` *inside* the compared value is refused on all six operators, matching `cygnet.in_`: nothing compares equal to NULL, so `(a, b) = ($1, $2)` with a NULL bound is NULL rather than a match for exactly the row you wanted. The ordering operators and `!=` are included because they're subtler rather than safer — a row comparison stops at the first decisive pair, so `(3,'x') > (2,NULL)` is true and `(1,'x') > (2,NULL)` is false, and a rule that holds only when the data decides early isn't one to rely on. (A whole-operand `None` — `row(a, b) == None` — still takes the usual `IS NULL` rewrite.)
* `cygnet.in_(left, values)` — membership against an explicit value list. With a row-value left operand it emits `(t.a, t.b) IN (($1, $2), ($3, $4))`, the shape a composite key needs for a batched lookup; with a scalar left operand it emits a plain `IN` list. Deliberately strict about its value list, because each rejected shape would otherwise yield a silently wrong query: it refuses an empty sequence, a bare string, a set/dict/generator (no dependable order — a dict would bind its *keys*), any value whose arity doesn't match the row, and any value containing `None` (`IN` compares with `=`, so `(a, b) IN ((NULL, 'z'))` matches zero rows even when that row exists). For a single column, `T.id == cygnet.arrays.any([...])` remains preferable (constant SQL text, no list-length limit); for `IN (SELECT …)` use `cygnet.op(col, "IN", subquery)`.

## Release 1.2, 2026-06-29: "Taking to water"

## New Features:

* `cygnet.follow_many(db, objs, fk_column)` — batched foreign-key navigation. Resolves the FK target for a whole collection in a single `WHERE pk = ANY($1)` round-trip instead of one query per object (the classic N+1), returning the targets aligned to the inputs (`None` for a NULL FK or a missing row).
* Faster bulk INSERT: the row-invariant column derivation is hoisted out of the per-row render loop, cutting `_render_bulk_insert` to ~500 ns/row (~1.64× faster bulk-INSERT rendering for a 100-row batch). Output is byte-identical.

## Release 1.1, 2026-06-23: "Drying out"

## New Features:

* Native asyncpg adapter (`AsyncpgDB`), available via the new `[asyncpg]` optional extra.
* Faster SELECT hydration: row-to-object construction is now positional and chosen once per table.

## Bug Fixes:

* `ClassVar`, `InitVar`, and `KW_ONLY`-sentinel attributes are no longer mistaken for table columns.

## Release 1.0, 2026-06-21: "Newly hatched"

Initial release.
