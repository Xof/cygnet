---
id: 0020
title: D3. Transactions are an async context manager, not a wrapper class
date: 2026-04-29
status: Accepted
summary: cygnet.transaction(db) is an async context manager that promotes nested use to SAVEPOINT via a per-adapter flag.
---

# 0020. D3. Transactions are an async context manager, not a wrapper class

`cygnet.transaction(db)` returns an async context manager. Outer use issues `BEGIN`/`COMMIT`. Nested use transparently promotes to `SAVEPOINT`/`RELEASE` based on the `db._in_transaction` flag.

**Why:** A "session" or "unit of work" object would add another layer. Async context manager covers BEGIN/COMMIT/ROLLBACK without inventing any new types. SAVEPOINT promotion is detected at runtime by checking the flag.

**Documented limitation:** `_in_transaction` is per-`db` instance, not task-local. Sharing a single db connection across `asyncio.tasks` corrupts nesting. One-connection-per-task is the recommended pattern (and the only safe one for psycopg connections anyway).
