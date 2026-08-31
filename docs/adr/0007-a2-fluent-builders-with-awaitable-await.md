---
id: 0007
title: A2. Fluent builders with awaitable `__await__`
date: 2026-04-29
status: Accepted
summary: Builders mutate and return self, `await` is the terminal action, and re-awaiting re-renders from scratch.
---

# 0007. A2. Fluent builders with awaitable `__await__`

`cygnet.SELECT(db)` returns a `SelectBuilder`. Subsequent calls (`FROM`, `WHERE`, `JOIN`, …) mutate the builder and return `self`. The terminal step is `await builder` — there is no `.fetch()` / `.execute()` / `.run()` / `.all()`.

**Why:** `await` is already the terminal-action marker in async Python; reusing it removes one method name from the API surface. `SelectBuilder.__await__` delegates to an internal `_execute()` coroutine that calls the executor. The same builder also exposes `.sql()` for inspection without execution and `.stream()` for async-iterator output.

**Subtle point:** awaiting the same builder twice re-renders and re-executes. Builders never hold rendered SQL or params between calls. This is intentional — it means a partially-built query can be debugged with `.sql()` and then run with `await` without state corruption.
