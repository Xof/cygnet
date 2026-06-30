# Cygnet Change Log

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
