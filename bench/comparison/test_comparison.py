# test_comparison.py — Side-by-side benchmarks across Cygnet, SQLAlchemy 2,
# and Django.  Same operations, same database, same connection style
# (single connection per run, no pooling) so the deltas measure ORM
# overhead and not connection management.
#
# Async strategy:
#   - Cygnet:     async-native; benchmark wraps loop.run_until_complete
#   - SA:         async session via create_async_engine; same wrapping
#   - Django:     sync API (its primary mode), called directly under
#                 pytest-benchmark.  Numbers reflect what real Django
#                 users see; we don't artificially async-wrap them.
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

from sqlalchemy import ForeignKey, String, Text  # noqa: E402
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column  # noqa: E402

import cygnet  # noqa: E402

from ..conftest import (  # noqa: E402
    Account,
    AccountTable,
    PostTable,
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
                    "HOST": parsed_dsn.hostname,
                    "PORT": str(parsed_dsn.port),
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
        def op() -> Any:
            async def go() -> Any:
                return await sa_session.get(SAAccount, 42)

            return run_async(loop, go)

        result = benchmark(op)
        assert result is not None

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

        def op() -> list:
            async def go() -> list:
                result = await sa_session.execute(select(SAAccount))
                return list(result.scalars())

            return run_async(loop, go)

        rows = benchmark(op)
        assert len(rows) >= 100

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


class TestJoinFollow:
    """Fetch every post joined to its account, both sides materialised as
    objects in one round-trip — ~1000 rows (100 accounts × 10 posts).

    Each ORM uses its idiomatic eager-join form, so the deltas reflect
    join + dual-object hydration cost, not a hand-rolled SQL string:
      - Cygnet: FOLLOW walks the declared FK and returns (Post, Account) tuples
      - SA:     explicit two-entity join, returns Row(Post, Account) — the
                closest like-for-like to FOLLOW without adding a relationship()
      - Django: select_related eager-loads .account in the same query
    """

    def test_cygnet(self, benchmark, loop, populated_db: Any) -> None:
        def op() -> list:
            async def go() -> list:
                return await (
                    cygnet.SELECT(populated_db)
                    .FROM(PostTable)
                    .FOLLOW(PostTable.account_id)
                )

            return run_async(loop, go)

        rows = benchmark(op)
        assert len(rows) >= 1000

    def test_sqlalchemy(self, benchmark, loop, sa_session: Any) -> None:
        from sqlalchemy import select

        def op() -> list:
            async def go() -> list:
                result = await sa_session.execute(
                    select(SAPost, SAAccount).join(
                        SAAccount, SAPost.account_id == SAAccount.id
                    )
                )
                return list(result.all())

            return run_async(loop, go)

        rows = benchmark(op)
        assert len(rows) >= 1000

    def test_django(self, benchmark, django_app: Any) -> None:
        # django_app param is taken for its side effect: it triggers Django
        # settings.configure()/setup() before we touch the ORM.  The model
        # itself comes from .models since this op is post-centric.
        from .models import DjangoPost

        def op() -> list:
            return [
                (p, p.account) for p in DjangoPost.objects.select_related("account")
            ]

        rows = benchmark(op)
        assert len(rows) >= 1000
