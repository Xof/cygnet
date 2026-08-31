---
id: 0016
title: P1. `$N` placeholders (libpq-native), not `%s`
date: 2026-04-29
status: Accepted
summary: Rendered SQL uses libpq-native $N placeholders, translated to %s only at the psycopg adapter edge.
---

# 0016. P1. `$N` placeholders (libpq-native), not `%s`

All rendered SQL uses positional `$1, $2, ...` placeholders. The reference psycopg adapter translates them to psycopg's native `%s` at the edge via regex.

**Why:** `$N` reads more naturally in logs (numbered, distinguishable from `%`-formatted Python strings) and matches what `EXPLAIN` and other PG tooling show. The single-place translation in the adapter is cheap; the readability win in dev/debug is permanent.

**Cost:** A regex-based translator. The translation is documented as position-insensitive (psycopg consumes params in list order, the same order Cygnet appends them); a hypothetical out-of-order `$2 before $1` would break it, but the executor's left-to-right render guarantees that can't happen.

**Footgun (documented in review):** `cygnet.lit("'$1 some text'")` gets translated too, because the regex doesn't know SQL syntax. Use `lit()` carefully.
