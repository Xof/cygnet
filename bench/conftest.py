# conftest.py — Shared fixtures for the Cygnet benchmark suite.
#
# Three layers of benchmarks share this conftest:
#   - test_render.py    : pure SQL generation, no DB at all
#   - test_overhead.py  : full op via FakeDB (Cygnet's path, no PG)
#   - test_e2e.py       : real PG via PsycopgDB (skipped without DSN)
#   - test_comparison.py: cross-ORM comparison (Cygnet/Django/SA)
#
# pytest-benchmark wraps a *synchronous* callable, but Cygnet is
# async-first.  The `loop` fixture below provides a single event loop
# reused across rounds of a benchmark — wrapping every call in
# `asyncio.run()` would create and tear down a loop per iteration and
# add hundreds of microseconds of noise that swamp the actual measure.

from __future__ import annotations

import asyncio
import dataclasses
import os
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import pytest

import cygnet
from cygnet.annotations import DBKey
from tests.conftest import FakeDB

# ── Shared models ─────────────────────────────────────────────────────────
# Defined once for all benchmark layers.  The cross-ORM comparison file
# defines parallel Django and SQLAlchemy models that mirror this shape
# so the side-by-side numbers measure equivalent work.


@dataclasses.dataclass
class Account:
    id: Annotated[int, DBKey]
    name: str
    email: str


@dataclasses.dataclass
class Post:
    id: Annotated[int, DBKey]
    account_id: Annotated[int, cygnet.ForeignKey(Account)]
    title: str
    body: str


AccountTable = cygnet.Table(Account)
PostTable = cygnet.Table(Post)


# ── Sync wrapper for async benchmarks ─────────────────────────────────────


@pytest.fixture(scope="session")
def loop() -> Any:
    """A single event loop reused across all benchmark rounds.

    Yielding a fresh loop per session — not per test — keeps the loop's
    own setup cost out of the per-iteration measurement.  Tests pass
    coroutine factories into `benchmark()`, and the body resolves them
    via `loop.run_until_complete`.
    """
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def run_async[T](loop: Any, factory: Callable[[], Awaitable[T]]) -> T:
    """Resolve a fresh coroutine on the shared loop.

    `factory` must be a zero-arg callable returning a coroutine — NOT
    a pre-awaited coroutine — because each benchmark round creates a
    new coroutine instance.  pytest-benchmark calls the wrapped
    function many times; reusing a single coroutine would StopIteration
    after the first run.
    """
    return loop.run_until_complete(factory())


# ── FakeDB fixtures (overhead benchmarks) ─────────────────────────────────


@pytest.fixture
def fake_db() -> FakeDB:
    """Empty FakeDB; rendering benchmarks don't need preloaded rows."""
    return FakeDB()


@pytest.fixture
def fake_db_populated() -> FakeDB:
    """FakeDB with 100 rows preloaded for SELECT-mapping benchmarks.

    The Cygnet path through FakeDB exercises render → execute → row-to-
    object mapping.  Without preloaded rows the mapping branch is
    skipped, which would understate the per-row hydration cost.
    """
    rows = [(i, f"User {i}", f"user{i}@example.com") for i in range(1, 101)]
    return FakeDB(rows=rows)


# ── Real-PG fixtures (e2e benchmarks) ─────────────────────────────────────

DSN = os.environ.get("CYGNET_TEST_DSN", "")


@pytest.fixture(scope="session")
def dsn() -> str:
    if not DSN:
        pytest.skip("CYGNET_TEST_DSN not set; e2e benchmarks skipped")
    return DSN


@pytest.fixture(scope="module")
def conn(loop: Any, dsn: str) -> Any:
    """One psycopg connection per module, opened on the bench loop.

    Sync fixture (not `async def`) so pytest-asyncio doesn't try to
    manage its own event loop alongside the one we hand to
    pytest-benchmark.  Mixing the two reliably hangs every connection
    attempt when bench tests run; using a single loop end-to-end keeps
    the connection bound to the same scheduler that drives the benchmark
    body.
    """
    import psycopg

    conn = loop.run_until_complete(
        psycopg.AsyncConnection.connect(dsn, autocommit=True)
    )
    yield conn
    loop.run_until_complete(conn.close())


@pytest.fixture(scope="module")
def populated_db(loop: Any, conn: Any) -> Any:
    """A PsycopgDB with bench tables created and seeded.

    Module-scoped so seeding only happens once per benchmark file.
    Schema:  100 accounts × 10 posts each = 1000 posts total — big
    enough that SELECT-all benchmarks measure realistic mapping cost,
    small enough that the seed step finishes in well under a second.
    """
    from cygnet.psycopg_db import PsycopgDB

    db = PsycopgDB(conn)

    async def setup() -> None:
        await conn.execute("DROP TABLE IF EXISTS posts")
        await conn.execute("DROP TABLE IF EXISTS accounts")
        await conn.execute("""
            CREATE TABLE accounts (
                id    SERIAL PRIMARY KEY,
                name  TEXT NOT NULL,
                email TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TABLE posts (
                id         SERIAL PRIMARY KEY,
                account_id INT REFERENCES accounts(id),
                title      TEXT NOT NULL,
                body       TEXT NOT NULL
            )
        """)
        accounts = [
            Account(id=None, name=f"User {i}", email=f"user{i}@example.com")
            for i in range(100)
        ]
        await cygnet.INSERT(db).INTO(AccountTable).BULK_VALUES(accounts)
        posts = [
            Post(
                id=None,
                account_id=a.id,
                title=f"Post {j} by {a.name}",
                body="Lorem ipsum dolor sit amet.",
            )
            for a in accounts
            for j in range(10)
        ]
        await cygnet.INSERT(db).INTO(PostTable).BULK_VALUES(posts)

    async def teardown() -> None:
        await conn.execute("DROP TABLE IF EXISTS posts")
        await conn.execute("DROP TABLE IF EXISTS accounts")

    loop.run_until_complete(setup())
    yield db
    loop.run_until_complete(teardown())
