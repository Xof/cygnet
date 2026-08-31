---
id: 0026
title: PF1. Bulk INSERT render hoists the row-invariant column derivation
date: 2026-06-27
status: Accepted
summary: Bulk INSERT hoists row-invariant column derivation out of the per-row loop (~855 to ~500 ns/row) while preserving the per-row AppKey-None check.
---

# 0026. PF1. Bulk INSERT render hoists the row-invariant column derivation

`_render_bulk_insert` determines the emitted column shape once from the first object, then for every subsequent row replays `_extract_insert_fields`' field-order classification inline — a precomputed `(attr_name, column_name, is_dbkey, is_pk)` plan plus direct `getattr` — instead of calling `_extract_insert_fields` per row. The per-row call rebuilt a throwaway kwargs dict plus two attribute sets on every row; profiling (2026-06-27, via `bench/profile_hotspots.py`) showed it was the single largest CPU cost in Cygnet — ~855 ns/row, roughly 15× any other per-row cost. The hoist cuts that to ~500 ns/row (render bulk-100: 87 µs → 53 µs, ~1.64×; full path: 106 µs → 67 µs).

**Why this shape and not a faster one.** A looser variant that validated only the omitted-column set and blind-appended the rest measured ~2.1× — but it silently dropped the per-row AppKey-None check (emitting `NULL` instead of raising per S2), a correctness regression. The shipped version keeps the full field-order pass so an `AppKey`-None still short-circuits (raising) *before* any cross-row column-shape mismatch is reported — byte-identical to the per-row `_extract_insert_fields` call it replaces. Verified by an exhaustive old-vs-new differential (198 DBKey/AppKey scenarios), the `tests/test_bulk_insert_shape.py` characterization net, the full unit suite, and adversarial review.

**`_extract_insert_fields` is unchanged** — it is shared with the single-row INSERT path, so only the bulk per-row loop was touched. The first row still goes through it, leaving first-row error semantics intact.
