---
id: 0004
title: F4. Async only
date: 2026-04-29
status: Accepted
summary: Every verb is awaitable and there is no sync API, halving both the API surface and the test matrix.
---

# 0004. F4. Async only

Every query verb returns an awaitable. There is no sync API. `cygnet.transaction(db)` is an `async with` context manager. `SelectBuilder.stream()` returns an async iterator.

**Why:** Modern Python web frameworks (FastAPI, Starlette, Litestar) are async by default. Building sync-and-async dual APIs (SQLAlchemy's path) doubles the surface and the test matrix. Single-mode keeps the codebase small.

**Cost:** Users in sync codebases must adapt (`asyncio.run`, `anyio.from_thread`). Acceptable for the alpha-stage scope.
