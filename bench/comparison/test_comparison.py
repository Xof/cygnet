# test_comparison.py — Side-by-side benchmarks across Cygnet, SQLAlchemy 2,
# and Django.  Same operations, same database, same connection style
# (single connection per run, no pooling) so the deltas measure ORM
# overhead and not connection management.
#
# Cygnet and SQLAlchemy each run on BOTH drivers (psycopg and asyncpg) — the
# *_asyncpg columns — so the matrix isolates two axes at once: ORM overhead
# (Cygnet vs SA vs Django) and driver overhead (psycopg vs asyncpg) within an
# ORM.  Django is psycopg/sync only (one column).
#
# Async strategy:
#   - Cygnet:     async-native; benchmark wraps loop.run_until_complete
#   - SA:         async session via create_async_engine; same wrapping
#   - Django:     sync API (its primary mode), called directly under
#                 pytest-benchmark.  Numbers reflect what real Django
#                 users see; we don't artificially async-wrap them.
#
# Fairness disclosures — we measure each ORM in its native, idiomatic mode and
# DISCLOSE the asymmetries rather than engineering artificial parity.  These
# are costs/behaviours a real application actually encounters, so correcting
# for them would put a thumb on the scale.  Read the ratios with these in mind:
#   - ASYNC TAX: Cygnet and SA pay one loop.run_until_complete PER op() call
#     (an event-loop entry per measured op); Django's sync path pays none.  A
#     real async app amortises one loop across many awaits, so this per-call
#     cost inflates the async ORMs' ABSOLUTE numbers relative to Django — most
#     visibly on cheap ops (e.g. insert-one, where Django can read as faster
#     despite issuing the same INSERT … RETURNING).
#   - SA IDENTITY MAP on reads: sa_session is reused across rounds and the read
#     ops never commit/expire, so Session.get(pk) serves from the identity map
#     WITHOUT SQL after round 1, and execute(select(...)) skips re-hydration of
#     already-loaded rows.  See the per-test notes on TestSelectByPk /
#     TestSelectAll.  This is genuine SQLAlchemy behaviour (the identity map is
#     a feature real apps benefit from); Cygnet has no identity map by design.
#   - SA WRITES additionally pay AsyncSession construction + a unit-of-work
#     flush + BEGIN/COMMIT (the "S41" note on TestInsertOne) vs Cygnet's single
#     autocommit statement.
#
# Skipped automatically when CYGNET_TEST_DSN is unset, or when Django /
# SQLAlchemy aren't installed (pytest.importorskip at module load).
#
# NOTE: this file deliberately does NOT use `from __future__ import
# annotations`.  SQLAlchemy 2's DeclarativeBase resolves Mapped[…]
# annotations at class-body evaluation time, and PEP 563's deferred
# string evaluation breaks that resolution path with a
# MappedAnnotationError.  Keeping annotations evaluated eagerly here
# is the simplest fix.

import os
from typing import Any
from urllib.parse import urlparse

import pytest

# Skip the whole module if optional [bench] deps aren't installed.
django = pytest.importorskip("django")
sa = pytest.importorskip("sqlalchemy")
asyncpg = pytest.importorskip("asyncpg")

from sqlalchemy import ForeignKey, String, Text  # noqa: E402
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column  # noqa: E402

import cygnet  # noqa: E402
from cygnet.asyncpg_db import AsyncpgDB  # noqa: E402

from ..conftest import (  # noqa: E402
    Account,
    AccountTable,
    run_async,
)

pytestmark = pytest.mark.bench


# ── SQLAlchemy models ────────────────────────────────────────────────────
# Defined at module scope (not inside a fixture) so SA's annotation
# introspection sees Mapped/int/str in the module's globals.  Defining
# these inside a function body fails with MappedAnnotationError because
# typing.get_type_hints can't resolve names from a vanished frame.


class SABase(DeclarativeBase):
    pass


class SAAccount(SABase):
    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(100))


class SAPost(SABase):
    __tablename__ = "posts"
    id: Mapped[int] = mapped_column(primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)


# ── Shared DSN / parsing ─────────────────────────────────────────────────


DSN = os.environ.get("CYGNET_TEST_DSN", "")


@pytest.fixture(scope="session")
def parsed_dsn() -> Any:
    """Parsed CYGNET_TEST_DSN for Django + SA setup.

    Both Django's DATABASES dict and SA's URL want the components
    separately; parse once at session scope.
    """
    if not DSN:
        pytest.skip("CYGNET_TEST_DSN not set; comparison benchmarks skipped")
    return urlparse(DSN)


# ── Django setup ─────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def django_app(parsed_dsn: Any) -> Any:
    """Configure Django settings + run setup() once per session.

    Idempotent on the `settings.configured` check so re-running the
    suite in the same interpreter (e.g. pytest --pdb retries) doesn't
    redefine settings.  Returns the `Account` model class so callers
    don't need to know about the import path.
    """
    from django.conf import settings

    if not settings.configured:
        settings.configure(
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.postgresql",
                    "NAME": parsed_dsn.path.lstrip("/"),
                    "USER": parsed_dsn.username,
                    "PASSWORD": parsed_dsn.password,
                    # Tolerate socket DSNs (postgresql:///db): urlparse yields
                    # hostname=None/port=None there, and str(None) -> "None"
                    # made psycopg reject the port. Fall back to "" so Django
                    # uses libpq defaults (local socket / default port).
                    "HOST": parsed_dsn.hostname or "",
                    "PORT": str(parsed_dsn.port) if parsed_dsn.port else "",
                }
            },
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "bench.comparison",
            ],
            DEFAULT_AUTO_FIELD="django.db.models.AutoField",
            USE_TZ=False,
        )
        django.setup()

    from .models import DjangoAccount

    return DjangoAccount


# ── SQLAlchemy setup ─────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def sa_engine(parsed_dsn: Any) -> Any:
    """One async SA engine for the whole session.

    `pool_size=1, max_overflow=0` to mirror the single-connection setup
    of Cygnet's PsycopgDB and Django's per-request connection.  Without
    this clamp, SA's default pool would warm up extra connections that
    skew comparisons against the others' single-connection mode.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    # SA wants postgresql+psycopg://... for psycopg3 async.
    sa_dsn = DSN.replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(sa_dsn, pool_size=1, max_overflow=0)
    yield engine
    # Engine teardown is async; fire it through an isolated event loop
    # so we don't depend on session-scoped fixtures still being alive.
    import asyncio

    asyncio.new_event_loop().run_until_complete(engine.dispose())


@pytest.fixture
def sa_session(loop: Any, sa_engine: Any) -> Any:
    """Per-test async session — fresh transaction per benchmark."""
    from sqlalchemy.ext.asyncio import AsyncSession

    session = AsyncSession(sa_engine, expire_on_commit=False)
    yield session
    loop.run_until_complete(session.close())


@pytest.fixture(scope="module")
def ag_conn(loop: Any, parsed_dsn: Any) -> Any:
    """One asyncpg connection for the module, opened on the bench loop.

    Opened/closed via loop.run_until_complete so the connection is bound to the
    shared bench loop (asyncpg binds to the running loop at connect time).
    parsed_dsn is accepted only as a skip-guard (it pytest.skip()s when
    CYGNET_TEST_DSN is unset); asyncpg.connect() takes the raw DSN directly.
    """
    conn = loop.run_until_complete(asyncpg.connect(DSN))
    yield conn
    loop.run_until_complete(conn.close())


@pytest.fixture
def cygnet_asyncpg_db(populated_db: Any, ag_conn: Any) -> Any:
    """Cygnet driven by asyncpg, reading populated_db's seeded tables.
    Depends on populated_db so the psycopg-side seeding has run."""
    return AsyncpgDB(ag_conn)


@pytest.fixture(scope="session")
def sa_engine_asyncpg(parsed_dsn: Any) -> Any:
    """SQLAlchemy async engine on the asyncpg driver (vs sa_engine's psycopg).

    pool_size=1, max_overflow=0 mirrors sa_engine's single-connection clamp so
    SA isn't given extra warmed connections the other ORMs don't get.
    parsed_dsn is accepted only as a skip-guard; the engine is built from the
    raw DSN, rewritten to the +asyncpg dialect.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    sa_dsn = DSN.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(sa_dsn, pool_size=1, max_overflow=0)
    yield engine
    import asyncio

    asyncio.new_event_loop().run_until_complete(engine.dispose())


@pytest.fixture
def sa_session_asyncpg(loop: Any, sa_engine_asyncpg: Any) -> Any:
    from sqlalchemy.ext.asyncio import AsyncSession

    session = AsyncSession(sa_engine_asyncpg, expire_on_commit=False)
    yield session
    loop.run_until_complete(session.close())


# ── Schema fixture: ensure the populated_db tables exist ─────────────────
# The cross-ORM benchmarks all read from the schema Cygnet's
# `populated_db` fixture creates, but populated_db is module-scoped and
# we want it loaded once for this whole comparison module too.  Reuse
# the same fixture name from the parent conftest by accepting it.


@pytest.fixture(scope="module", autouse=True)
def shared_schema(populated_db: Any) -> Any:
    """Bring the bench schema (accounts + posts, pre-seeded) into scope
    for every test in this module.  populated_db is defined in
    bench/conftest.py and creates 100 accounts × 10 posts each."""
    return populated_db


# ── Cygnet operations ────────────────────────────────────────────────────


class TestSelectByPk:
    def test_cygnet(self, benchmark, loop, populated_db: Any):
        def op() -> Any:
            async def go() -> Any:
                return await cygnet.get(populated_db, AccountTable, id=42)

            return run_async(loop, go)

        result = benchmark(op)
        assert result is not None

    def test_sqlalchemy(self, benchmark, loop, sa_session: Any) -> None:
        # DISCLOSURE (not corrected — see module header): sa_session is reused
        # across all rounds and op never commits/expires, so after round 1
        # Session.get(42) returns from the identity map WITHOUT issuing SQL.
        # Rounds 2..N (the measured median) are cache hits, not round-trips,
        # whereas Cygnet and Django round-trip every call. Genuine SQLAlchemy
        # behaviour a real app benefits from; read the SA select-by-pk ratio
        # knowing it is not a like-for-like round-trip measurement.
        def op() -> Any:
            async def go() -> Any:
                return await sa_session.get(SAAccount, 42)

            return run_async(loop, go)

        result = benchmark(op)
        assert result is not None

    def test_cygnet_asyncpg(self, benchmark, loop, cygnet_asyncpg_db: Any) -> None:
        def op() -> Any:
            async def go() -> Any:
                return await cygnet.get(cygnet_asyncpg_db, AccountTable, id=42)

            return run_async(loop, go)

        assert benchmark(op) is not None

    def test_sqlalchemy_asyncpg(self, benchmark, loop, sa_session_asyncpg: Any) -> None:
        # DISCLOSURE: same identity-map cache as test_sqlalchemy above —
        # sa_session_asyncpg is reused across rounds, so after round 1
        # Session.get(42) is served from the identity map without SQL. Not a
        # round-trip, so the asyncpg-vs-psycopg driver swap is not even
        # exercised on the cached rounds. See the module header.
        def op() -> Any:
            async def go() -> Any:
                return await sa_session_asyncpg.get(SAAccount, 42)

            return run_async(loop, go)

        assert benchmark(op) is not None

    def test_django(self, benchmark, django_app: Any) -> None:
        Account_ = django_app

        def op() -> Any:
            return Account_.objects.get(pk=42)

        result = benchmark(op)
        assert result is not None


class TestSelectAll:
    """Materialise 100 rows as model objects."""

    def test_cygnet(self, benchmark, loop, populated_db: Any) -> None:
        def op() -> list:
            async def go() -> list:
                return await cygnet.SELECT(populated_db).FROM(AccountTable)

            return run_async(loop, go)

        rows = benchmark(op)
        assert len(rows) >= 100

    def test_sqlalchemy(self, benchmark, loop, sa_session: Any) -> None:
        from sqlalchemy import select

        # DISCLOSURE (not corrected — see module header): the reused sa_session
        # issues the SELECT every round, but returns already-loaded identity-map
        # instances for known PKs, skipping the per-row hydration Cygnet performs
        # on all 100 rows each round. So this compares Cygnet's full hydration
        # against SA's near-zero re-hydration — native SQLAlchemy behaviour,
        # disclosed rather than forced to re-materialise.
        def op() -> list:
            async def go() -> list:
                result = await sa_session.execute(select(SAAccount))
                return list(result.scalars())

            return run_async(loop, go)

        rows = benchmark(op)
        assert len(rows) >= 100

    def test_cygnet_asyncpg(self, benchmark, loop, cygnet_asyncpg_db: Any) -> None:
        def op() -> list:
            async def go() -> list:
                return await cygnet.SELECT(cygnet_asyncpg_db).FROM(AccountTable)

            return run_async(loop, go)

        assert len(benchmark(op)) >= 100

    def test_sqlalchemy_asyncpg(self, benchmark, loop, sa_session_asyncpg: Any) -> None:
        # DISCLOSURE: same as test_sqlalchemy above — the reused
        # sa_session_asyncpg issues the SELECT each round but returns cached
        # identity-map instances for known PKs, skipping per-row re-hydration.
        # See the module header.
        from sqlalchemy import select

        def op() -> list:
            async def go() -> list:
                result = await sa_session_asyncpg.execute(select(SAAccount))
                return list(result.scalars())

            return run_async(loop, go)

        assert len(benchmark(op)) >= 100

    def test_django(self, benchmark, django_app: Any) -> None:
        Account_ = django_app

        def op() -> list:
            return list(Account_.objects.all())

        rows = benchmark(op)
        assert len(rows) >= 100


class TestInsertOne:
    """Single-row INSERT with PK populated back onto the in-Python obj."""

    def test_cygnet(self, benchmark, loop, populated_db: Any) -> None:
        def op() -> Account:
            acc = Account(id=None, name="Bench User", email="bench@example.com")

            async def go() -> Account:
                await cygnet.INSERT(populated_db).INTO(AccountTable).VALUES(acc)
                return acc

            return run_async(loop, go)

        result = benchmark(op)
        assert result.id is not None

    def test_sqlalchemy(self, benchmark, loop, sa_engine: Any) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        def op() -> Any:
            async def go() -> Any:
                # Fresh session per call: this measures SA's session-per-write
                # idiom, which additionally pays AsyncSession construction + a
                # unit-of-work flush that Cygnet's reused autocommit connection
                # does not (S41).  The libpq commit itself is comparable —
                # Cygnet autocommits too — but the session/UoW overhead is not,
                # so this is not a like-for-like single-statement equivalence.
                async with AsyncSession(sa_engine, expire_on_commit=False) as s:
                    acc = SAAccount(name="Bench User", email="bench@example.com")
                    s.add(acc)
                    await s.commit()
                    return acc.id

            return run_async(loop, go)

        result = benchmark(op)
        assert result is not None

    def test_cygnet_asyncpg(self, benchmark, loop, cygnet_asyncpg_db: Any) -> None:
        def op() -> Account:
            acc = Account(id=None, name="Bench User", email="bench@example.com")

            async def go() -> Account:
                await cygnet.INSERT(cygnet_asyncpg_db).INTO(AccountTable).VALUES(acc)
                return acc

            return run_async(loop, go)

        assert benchmark(op).id is not None

    def test_sqlalchemy_asyncpg(self, benchmark, loop, sa_engine_asyncpg: Any) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        def op() -> Any:
            async def go() -> Any:
                async with AsyncSession(sa_engine_asyncpg, expire_on_commit=False) as s:
                    acc = SAAccount(name="Bench User", email="bench@example.com")
                    s.add(acc)
                    await s.commit()
                    return acc.id

            return run_async(loop, go)

        assert benchmark(op) is not None

    def test_django(self, benchmark, django_app: Any) -> None:
        Account_ = django_app

        def op() -> Any:
            return Account_.objects.create(name="Bench User", email="bench@example.com")

        result = benchmark(op)
        assert result.pk is not None


class TestBulkInsert:
    """Insert 50 fresh rows in one statement (Cygnet/Django) or one
    transaction (SA's add_all + commit)."""

    def test_cygnet(self, benchmark, loop, populated_db: Any) -> None:
        def op() -> list:
            accs = [
                Account(id=None, name=f"Bulk {i}", email=f"b{i}@example.com")
                for i in range(50)
            ]

            async def go() -> list:
                return await (
                    cygnet.INSERT(populated_db).INTO(AccountTable).BULK_VALUES(accs)
                )

            return run_async(loop, go)

        result = benchmark(op)
        assert len(result) == 50

    def test_sqlalchemy(self, benchmark, loop, sa_engine: Any) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        def op() -> int:
            objs = [
                SAAccount(name=f"Bulk {i}", email=f"b{i}@example.com")
                for i in range(50)
            ]

            async def go() -> int:
                async with AsyncSession(sa_engine, expire_on_commit=False) as s:
                    s.add_all(objs)
                    await s.commit()
                    return len(objs)

            return run_async(loop, go)

        n = benchmark(op)
        assert n == 50

    def test_cygnet_asyncpg(self, benchmark, loop, cygnet_asyncpg_db: Any) -> None:
        def op() -> list:
            accs = [
                Account(id=None, name=f"Bulk {i}", email=f"b{i}@example.com")
                for i in range(50)
            ]

            async def go() -> list:
                return await (
                    cygnet.INSERT(cygnet_asyncpg_db).INTO(AccountTable).BULK_VALUES(accs)
                )

            return run_async(loop, go)

        assert len(benchmark(op)) == 50

    def test_sqlalchemy_asyncpg(self, benchmark, loop, sa_engine_asyncpg: Any) -> None:
        from sqlalchemy.ext.asyncio import AsyncSession

        def op() -> int:
            objs = [
                SAAccount(name=f"Bulk {i}", email=f"b{i}@example.com")
                for i in range(50)
            ]

            async def go() -> int:
                async with AsyncSession(sa_engine_asyncpg, expire_on_commit=False) as s:
                    s.add_all(objs)
                    await s.commit()
                    return len(objs)

            return run_async(loop, go)

        assert benchmark(op) == 50

    def test_django(self, benchmark, django_app: Any) -> None:
        Account_ = django_app

        def op() -> int:
            objs = [
                Account_(name=f"Bulk {i}", email=f"b{i}@example.com") for i in range(50)
            ]
            created = Account_.objects.bulk_create(objs)
            return len(created)

        n = benchmark(op)
        assert n == 50
