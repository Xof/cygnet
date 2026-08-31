---
id: 0018
title: D1. Duck-typed db protocol (no driver class)
date: 2026-04-29
status: Accepted
summary: The db object is a duck-typed four-method protocol; Cygnet core never imports a driver.
---

# 0018. D1. Duck-typed db protocol (no driver class)

Cygnet never imports a specific database driver in its core. The `db` object passed to `SELECT/INSERT/UPDATE/DELETE` must satisfy a four-method protocol:

```python
async def execute(sql: str, params: list) -> list[tuple]
async def execute_one(sql: str, params: list) -> tuple | None
async def stream(sql: str, params: list) -> AsyncIterator[tuple]   # optional
_in_transaction: bool                                              # required
```

`tests/conftest.py:FakeDB` is the reference unit-test implementation (captures SQL + params, returns preloaded rows). `cygnet/psycopg_db.py:PsycopgDB` is the reference production adapter (wraps `psycopg.AsyncConnection`).

**Why:** The driver is the user's choice. asyncpg, an HTTP-based DB gateway, a sharded multi-connection wrapper, a fake for tests — any of these can satisfy the protocol. Cygnet is unopinionated about connection management, pooling, retry, and so on, because those concerns vary by deployment and are not the ORM's problem.
