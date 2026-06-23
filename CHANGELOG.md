# Cygnet Change Log

## Release 1.1, 2026-06-23: "Drying out"

## New Features:

* Native asyncpg adapter (`AsyncpgDB`), available via the new `[asyncpg]` optional extra.
* Faster SELECT hydration: row-to-object construction is now positional and chosen once per table.

## Bug Fixes:

* `ClassVar`, `InitVar`, and `KW_ONLY`-sentinel attributes are no longer mistaken for table columns.

## Release 1.0, 2026-06-21: "Newly hatched"

Initial release.
