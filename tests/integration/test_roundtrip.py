# test_roundtrip.py — End-to-end integration tests against a real PostgreSQL.
#
# These tests INSERT, SELECT, UPDATE, save (upsert), and transact against
# a live database to verify that Cygnet's generated SQL actually works.
# Requires CYGNET_TEST_DSN env var; `just test-all` handles the Docker lifecycle.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import DBKey
from cygnet.psycopg_db import PsycopgDB

pytestmark = pytest.mark.integration


@dataclasses.dataclass
class Widget:
    id: Annotated[int, DBKey]
    name: str
    weight: float


WidgetTable = cygnet.Table(Widget)


@pytest.fixture(scope="module")
async def db(conn):
    await conn.execute("""
        CREATE TEMP TABLE widgets (
            id     SERIAL PRIMARY KEY,
            name   TEXT    NOT NULL,
            weight FLOAT   NOT NULL
        )
    """)
    yield PsycopgDB(conn)


class TestRoundtrip:
    async def test_insert_and_select(self, db):
        w = Widget(id=None, name="Sprocket", weight=1.5)
        await cygnet.INSERT(db).INTO(WidgetTable).VALUES(w)
        assert w.id is not None

        results = (
            await cygnet.SELECT(db).FROM(WidgetTable).WHERE(WidgetTable.id == w.id)
        )
        assert len(results) == 1
        assert results[0].name == "Sprocket"

    async def test_save_upsert(self, db):
        w = Widget(id=None, name="Cog", weight=0.8)
        await cygnet.save(db, w)
        assert w.id is not None

        w.name = "Big Cog"
        await cygnet.save(db, w)

        fetched = await cygnet.get(db, WidgetTable, id=w.id)
        assert fetched is not None
        assert fetched.name == "Big Cog"

    async def test_update(self, db):
        w = Widget(id=None, name="Bolt", weight=0.1)
        await cygnet.save(db, w)
        await (
            cygnet.UPDATE(db).SET(WidgetTable, weight=0.2).WHERE(WidgetTable.id == w.id)
        )
        fetched = await cygnet.get(db, WidgetTable, id=w.id)
        assert fetched is not None
        assert fetched.weight == pytest.approx(0.2)

    async def test_order_by_and_limit(self, db):
        for name, weight in [("A", 3.0), ("B", 1.0), ("C", 2.0)]:
            await cygnet.save(db, Widget(id=None, name=name, weight=weight))

        results = await (
            cygnet.SELECT(db).FROM(WidgetTable).ORDER_BY(WidgetTable.weight).LIMIT(2)
        )
        assert len(results) == 2
        assert results[0].weight <= results[1].weight

    async def test_transaction_commit(self, db):
        async with cygnet.transaction(db) as tx:
            w = Widget(id=None, name="Nut", weight=0.05)
            await cygnet.INSERT(tx).INTO(WidgetTable).VALUES(w)
        fetched = await cygnet.get(db, WidgetTable, id=w.id)
        assert fetched is not None

    async def test_transaction_rollback(self, db):
        initial = await cygnet.SELECT(db).FROM(WidgetTable)
        initial_count = len(initial)
        try:
            async with cygnet.transaction(db) as tx:
                await cygnet.save(tx, Widget(id=None, name="Phantom", weight=99.0))
                raise ValueError("force rollback")
        except ValueError:
            pass
        after = await cygnet.SELECT(db).FROM(WidgetTable)
        assert len(after) == initial_count


@dataclasses.dataclass
class Author:
    id: Annotated[int, DBKey]
    name: str


@dataclasses.dataclass
class Book:
    id: Annotated[int, DBKey]
    author_id: Annotated[int, cygnet.ForeignKey(Author)]
    title: str


AuthorTable = cygnet.Table(Author)
BookTable = cygnet.Table(Book)


class TestFollowRoundtrip:
    @pytest.fixture(autouse=True)
    async def setup_tables(self, conn):
        await conn.execute("""
            CREATE TEMP TABLE authors (
                id   SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TEMP TABLE books (
                id        SERIAL PRIMARY KEY,
                author_id INT REFERENCES authors(id),
                title     TEXT NOT NULL
            )
        """)
        yield PsycopgDB(conn)
        await conn.execute("DROP TABLE IF EXISTS books")
        await conn.execute("DROP TABLE IF EXISTS authors")

    async def test_follow_loads_related_object(self, setup_tables):
        db = setup_tables
        author = Author(id=None, name="Ursula K. Le Guin")
        await cygnet.create(db, author)

        book = Book(id=None, author_id=author.id, title="The Left Hand of Darkness")
        await cygnet.create(db, book)

        loaded_author = await cygnet.follow(db, book, BookTable.author_id)
        assert loaded_author is not None
        assert loaded_author.name == "Ursula K. Le Guin"
        assert loaded_author.id == author.id

    async def test_follow_builder_join(self, setup_tables):
        db = setup_tables
        author = Author(id=None, name="Octavia Butler")
        await cygnet.create(db, author)

        book = Book(id=None, author_id=author.id, title="Kindred")
        await cygnet.create(db, book)

        results = await cygnet.SELECT(db).FROM(BookTable).FOLLOW(BookTable.author_id)
        assert len(results) == 1
        loaded_book, loaded_author = results[0]
        assert loaded_book.title == "Kindred"
        assert loaded_author.name == "Octavia Butler"

    async def test_left_follow_with_null_fk(self, setup_tables):
        db = setup_tables
        author = Author(id=None, name="Samuel Delany")
        await cygnet.create(db, author)

        book_with = Book(id=None, author_id=author.id, title="Dhalgren")
        await cygnet.create(db, book_with)

        book_without = Book(id=None, author_id=None, title="Anonymous")
        await cygnet.create(db, book_without)

        results = await (
            cygnet.SELECT(db)
            .FROM(BookTable)
            .LEFT_FOLLOW(BookTable.author_id)
            .ORDER_BY(BookTable.id)
        )
        assert len(results) == 2

        book1, author1 = results[0]
        assert book1.title == "Dhalgren"
        assert author1 is not None
        assert author1.name == "Samuel Delany"

        book2, author2 = results[1]
        assert book2.title == "Anonymous"
        assert author2 is None


# Self-join model: a book can list co-authors via a junction.  We use the
# books table itself with two pseudo-roles ("primary" / "co") via aliases
# rather than introducing a junction table — the goal is to exercise the
# alias machinery against real PG, not to model anything sophisticated.
@dataclasses.dataclass
class Pairing:
    id: Annotated[int, DBKey]
    a_book_id: int
    b_book_id: int


PairingTable = cygnet.Table(Pairing)


class TestSelfJoinRoundtrip:
    """Closes ISSUES.md item 8.1 (formerly REVIEW.md): a real-PG self-join via aliasing.

    The unit-level test_multi_join_mapping uses identical ON clauses with
    the same table twice — that SQL would be rejected by PG's parser as
    ambiguous.  The fix is the aliasing API (TableProxy.AS), and this
    test exercises it end-to-end against a live PG.
    """

    @pytest.fixture(autouse=True)
    async def setup_tables(self, conn):
        # Reuse the books table from TestFollowRoundtrip's pattern, plus
        # a tiny pairings junction.  Both tables are TEMP so they're
        # cleaned up automatically between sessions.
        await conn.execute("""
            CREATE TEMP TABLE authors (
                id   SERIAL PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TEMP TABLE books (
                id        SERIAL PRIMARY KEY,
                author_id INT REFERENCES authors(id),
                title     TEXT NOT NULL
            )
        """)
        await conn.execute("""
            CREATE TEMP TABLE pairings (
                id         SERIAL PRIMARY KEY,
                a_book_id  INT NOT NULL REFERENCES books(id),
                b_book_id  INT NOT NULL REFERENCES books(id)
            )
        """)
        yield PsycopgDB(conn)
        await conn.execute("DROP TABLE IF EXISTS pairings")
        await conn.execute("DROP TABLE IF EXISTS books")
        await conn.execute("DROP TABLE IF EXISTS authors")

    async def test_bulk_values_inserts_many_rows_one_round_trip(self, setup_tables):
        """BULK_VALUES against real PG: one statement, N inserted rows,
        each object's DBKey populated in input order."""
        db = setup_tables
        author = Author(id=None, name="Octavia Butler")
        await cygnet.create(db, author)

        books = [
            Book(id=None, author_id=author.id, title="Kindred"),
            Book(id=None, author_id=author.id, title="Wild Seed"),
            Book(id=None, author_id=author.id, title="Parable of the Sower"),
        ]
        result = await cygnet.INSERT(db).INTO(BookTable).BULK_VALUES(books)
        assert all(b.id is not None for b in books)
        assert result == [b.id for b in books]
        # Verify the rows actually landed in PG.
        loaded = await (
            cygnet.SELECT(db)
            .FROM(BookTable)
            .WHERE(BookTable.author_id == author.id)
            .ORDER_BY(BookTable.id)
        )
        assert {b.title for b in loaded} == {
            "Kindred",
            "Wild Seed",
            "Parable of the Sower",
        }

    async def test_cte_filters_in_with(self, setup_tables):
        """CTE end-to-end: WITH active AS (...) SELECT … FROM active …"""
        db = setup_tables
        a = Author(id=None, name="Active Author")
        b = Author(id=None, name="Inactive Author")
        await cygnet.create(db, a)
        await cygnet.create(db, b)
        await cygnet.create(db, Book(id=None, author_id=a.id, title="Live"))
        await cygnet.create(db, Book(id=None, author_id=b.id, title="Old"))

        active = cygnet.cte(
            "active",
            cygnet.SELECT(db, AuthorTable.id, AuthorTable.name)
            .FROM(AuthorTable)
            .WHERE(AuthorTable.name == "Active Author"),
        )
        results = await (
            cygnet.SELECT(db, active.name, BookTable.title)
            .WITH(active)
            .FROM(active)
            .JOIN(BookTable, ON=active.id == BookTable.author_id)
        )
        assert results == [("Active Author", "Live")]

    async def test_stream_yields_rows_in_a_transaction(self, setup_tables):
        """SELECT.stream() against real PG via a portal cursor.

        Portal cursors require a transaction context; the typical wrapper
        is `async with cygnet.transaction(db)`.  Streaming should yield
        dataclass instances one at a time and reach the same total as
        awaiting the same query would.
        """
        db = setup_tables
        author = Author(id=None, name="Streaming Author")
        await cygnet.create(db, author)
        for i in range(20):
            await cygnet.create(
                db, Book(id=None, author_id=author.id, title=f"Book {i}")
            )

        # Materialise via await for the comparison baseline.
        materialised = await (
            cygnet.SELECT(db)
            .FROM(BookTable)
            .WHERE(BookTable.author_id == author.id)
            .ORDER_BY(BookTable.id)
        )

        # Stream via async-for.  PG requires a transaction for portal
        # cursors — `async with cygnet.transaction(db)` provides it.
        streamed: list[Book] = []
        async with cygnet.transaction(db) as tx:
            async for book in (
                cygnet.SELECT(tx)
                .FROM(BookTable)
                .WHERE(BookTable.author_id == author.id)
                .ORDER_BY(BookTable.id)
                .stream()
            ):
                streamed.append(book)

        assert len(streamed) == len(materialised) == 20
        assert [b.title for b in streamed] == [b.title for b in materialised]

    async def test_insert_select_clones_rows(self, setup_tables):
        """INSERT INTO books (author_id, title) SELECT author_id, title
        FROM books WHERE …  — bulk row-cloning in one statement."""
        db = setup_tables
        author = Author(id=None, name="Cloning Author")
        await cygnet.create(db, author)
        for title in ["Original 1", "Original 2", "Original 3"]:
            await cygnet.create(db, Book(id=None, author_id=author.id, title=title))

        # Clone the three originals back into the same table.  In real
        # life you'd target a different table, but reusing one keeps the
        # test setup simple.
        source = (
            cygnet.SELECT(db, BookTable.author_id, BookTable.title)
            .FROM(BookTable)
            .WHERE(BookTable.author_id == author.id)
        )
        new_ids = await cygnet.INSERT(db).INTO(BookTable).SELECT(source)
        assert len(new_ids) == 3
        # Now there should be 6 books for this author: 3 originals + 3 clones.
        all_books = await (
            cygnet.SELECT(db).FROM(BookTable).WHERE(BookTable.author_id == author.id)
        )
        assert len(all_books) == 6
        # Every clone's title matches one of the originals.
        titles = [b.title for b in all_books]
        assert titles.count("Original 1") == 2
        assert titles.count("Original 2") == 2
        assert titles.count("Original 3") == 2

    async def test_recursive_cte_count_up_against_real_pg(self, setup_tables):
        """Classic recursive CTE: count from 1 to 5 via anchor + step.

        Doesn't need any of the test tables — it's a pure-SQL recursion
        — but the conn fixture is shared with the suite, so we attach
        to TestSelfJoinRoundtrip's setup for convenience.
        """
        db = setup_tables
        c = cygnet.recursive_cte("counter", columns=["n"])
        c.anchor = cygnet.SELECT(db, cygnet.lit("1"))
        c.step = cygnet.SELECT(db, c.n + 1).FROM(c).WHERE(c.n < 5)
        rows = await cygnet.SELECT(db, c.n).WITH(c).FROM(c).ORDER_BY(c.n)
        # Five rows: 1, 2, 3, 4, 5.
        assert rows == [(1,), (2,), (3,), (4,), (5,)]

    async def test_distinct_on_picks_one_per_group(self, setup_tables):
        """DISTINCT ON (author_id) ORDER BY author_id, title — keep the
        first book (alphabetically) per author."""
        db = setup_tables
        a1 = Author(id=None, name="A")
        a2 = Author(id=None, name="B")
        await cygnet.create(db, a1)
        await cygnet.create(db, a2)
        for author, title in [
            (a1, "Z by A"),
            (a1, "M by A"),
            (a2, "K by B"),
            (a2, "X by B"),
        ]:
            await cygnet.create(db, Book(id=None, author_id=author.id, title=title))

        rows = await (
            cygnet.SELECT(db, BookTable.author_id, BookTable.title)
            .DISTINCT_ON(BookTable.author_id)
            .FROM(BookTable)
            .ORDER_BY(BookTable.author_id, BookTable.title)
        )
        # One row per author, picked alphabetically by title.
        # M < Z for author 1, K < X for author 2.
        assert sorted(rows) == [(a1.id, "M by A"), (a2.id, "K by B")]

    async def test_union_combines_two_selects(self, setup_tables):
        """UNION dedupes; UNION ALL preserves duplicates."""
        db = setup_tables
        author = Author(id=None, name="Solo")
        await cygnet.create(db, author)
        for title in ["A", "B", "B"]:
            await cygnet.create(db, Book(id=None, author_id=author.id, title=title))

        # UNION dedupes -> 2 unique titles.
        deduped = await (
            cygnet.SELECT(db, BookTable.title)
            .FROM(BookTable)
            .WHERE(BookTable.author_id == author.id)
            .UNION(
                cygnet.SELECT(db, BookTable.title)
                .FROM(BookTable)
                .WHERE(BookTable.author_id == author.id)
            )
        )
        assert sorted(deduped) == [("A",), ("B",)]

        # UNION ALL preserves -> 6 rows (3 + 3).
        all_rows = await (
            cygnet.SELECT(db, BookTable.title)
            .FROM(BookTable)
            .WHERE(BookTable.author_id == author.id)
            .UNION_ALL(
                cygnet.SELECT(db, BookTable.title)
                .FROM(BookTable)
                .WHERE(BookTable.author_id == author.id)
            )
        )
        assert len(all_rows) == 6

    async def test_on_conflict_do_nothing_skips_duplicate(self, setup_tables):
        """ON CONFLICT (col) DO NOTHING on a real PG: duplicate INSERT
        returns None; the original row is untouched."""
        db = setup_tables
        # Add a UNIQUE constraint so we have something to conflict with.
        await db.execute(
            "ALTER TABLE authors ADD CONSTRAINT uq_authors_name UNIQUE (name)",
            [],
        )
        try:
            original = Author(id=None, name="Conflict Author")
            await cygnet.create(db, original)

            # Re-insert a row with the same name — should be skipped.
            duplicate = Author(id=None, name="Conflict Author")
            result = await (
                cygnet.INSERT(db)
                .INTO(AuthorTable)
                .VALUES(duplicate)
                .ON_CONFLICT(AuthorTable.name)
                .DO_NOTHING()
            )
            assert result is None
            assert duplicate.id is None  # PK left unset, signaling skip
            # Only the original row exists.
            rows = await cygnet.SELECT(db).FROM(AuthorTable)
            assert len(rows) == 1
            assert rows[0].id == original.id
        finally:
            await db.execute("ALTER TABLE authors DROP CONSTRAINT uq_authors_name", [])

    async def test_on_conflict_do_update_writes_kwargs(self, setup_tables):
        """ON CONFLICT (id) DO UPDATE SET name = $N exercises Cygnet's
        DO_UPDATE(**fields) path against real PG.  Conflicts on the PK
        and rewrites the existing row's name from a literal kwarg."""
        db = setup_tables
        original = Author(id=None, name="Original")
        await cygnet.create(db, original)

        # Re-INSERT with the same id to trigger PK conflict, with a
        # different name; DO_UPDATE rewrites the existing row's name
        # from the kwarg literal (NOT from EXCLUDED — we don't read
        # the inserting row's name, we substitute our own).
        replay = Author(id=original.id, name="Ignored")
        await (
            cygnet.INSERT(db)
            .INTO(AuthorTable)
            .VALUES(replay)
            .ON_CONFLICT(AuthorTable.id)
            .DO_UPDATE(name="From Kwargs")
        )

        # The existing row should now have the kwargs-supplied name.
        fetched = await cygnet.get(db, AuthorTable, id=original.id)
        assert fetched is not None
        assert fetched.name == "From Kwargs"

    async def test_self_join_via_aliases(self, setup_tables):
        db = setup_tables
        author = Author(id=None, name="N. K. Jemisin")
        await cygnet.create(db, author)

        b1 = Book(id=None, author_id=author.id, title="The Fifth Season")
        b2 = Book(id=None, author_id=author.id, title="The Obelisk Gate")
        await cygnet.create(db, b1)
        await cygnet.create(db, b2)

        pairing = Pairing(id=None, a_book_id=b1.id, b_book_id=b2.id)
        await cygnet.create(db, pairing)

        # Self-join: pair each pairing's two book references back to the
        # books table.  Without aliasing, this would fail at the parser
        # stage with "table name 'books' specified more than once".
        BA = BookTable.AS("ba")
        BB = BookTable.AS("bb")
        results = await (
            cygnet.SELECT(db, BA.title, BB.title)
            .FROM(PairingTable)
            .JOIN(BA, ON=PairingTable.a_book_id == BA.id)
            .JOIN(BB, ON=PairingTable.b_book_id == BB.id)
        )
        assert results == [("The Fifth Season", "The Obelisk Gate")]


# DEFAULT-aware INSERT: when a column has a schema-side DEFAULT and the
# dataclass field is None, Cygnet must omit the column from the INSERT
# (so the DEFAULT fires) and patch the DB-generated value back onto the
# in-memory object via RETURNING.  Before the fix, every non-DBKey-None
# field was emitted as an explicit NULL parameter, which suppressed
# DEFAULT firing (PG only fires DEFAULT when the column is *absent* from
# the column list, not when it's NULL).
@dataclasses.dataclass
class TimestampedThing:
    """Two DEFAULT-having columns to exercise the multi-column RETURNING
    path: ``created_at TIMESTAMPTZ DEFAULT now()`` and
    ``status TEXT DEFAULT 'pending'``.  When both are None on input, both
    should land with their DEFAULT values in the DB and on the in-memory
    object after INSERT."""

    id: Annotated[int, DBKey]
    name: str
    created_at: object | None = None  # TIMESTAMPTZ; opaque to keep import surface small
    status: str | None = None


TimestampedThingTable = cygnet.Table(TimestampedThing)


class TestDefaultAwareInsertRoundtrip:
    """Regression coverage for the DEFAULT-aware INSERT fix.  Validates
    end-to-end against real PG: omitted columns get DEFAULT'd server-side,
    RETURNING populates the object, the DB row matches what the object
    sees post-INSERT.  See executor._extract_insert_fields for the fix."""

    @pytest.fixture(autouse=True)
    async def setup_table(self, conn):
        await conn.execute("""
            CREATE TEMP TABLE timestampedthings (
                id          SERIAL PRIMARY KEY,
                name        TEXT NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT now(),
                status      TEXT DEFAULT 'pending'
            )
        """)
        yield PsycopgDB(conn)
        await conn.execute("DROP TABLE IF EXISTS timestampedthings")

    async def test_default_columns_fire_server_side(self, setup_table):
        """The schema's DEFAULTs fire when the dataclass fields are None,
        and the in-memory object is populated with the server-generated
        values via RETURNING — so application code sees what PG stored."""
        db = setup_table
        t = TimestampedThing(id=None, name="alpha")
        # Pre-condition: both DEFAULT-having fields are None.
        assert t.created_at is None
        assert t.status is None

        await cygnet.INSERT(db).INTO(TimestampedThingTable).VALUES(t)

        # Post-INSERT: PK populated as always, and the two DEFAULT-having
        # fields are populated from RETURNING with the DB-generated values.
        assert t.id is not None
        assert t.created_at is not None  # DEFAULT now() fired
        assert t.status == "pending"  # DEFAULT 'pending' fired

        # The DB row matches what's on the in-memory object — no drift.
        loaded = await cygnet.get(db, TimestampedThingTable, id=t.id)
        assert loaded is not None
        assert loaded.created_at == t.created_at
        assert loaded.status == t.status

    async def test_explicit_value_overrides_default(self, setup_table):
        """A non-None value on a DEFAULTed field is emitted explicitly:
        the application is overriding the DEFAULT, which is the
        historical behaviour we must preserve."""
        db = setup_table
        t = TimestampedThing(id=None, name="beta", status="custom")
        await cygnet.INSERT(db).INTO(TimestampedThingTable).VALUES(t)

        # status kept the caller-supplied value, NOT 'pending'.
        loaded = await cygnet.get(db, TimestampedThingTable, id=t.id)
        assert loaded is not None
        assert loaded.status == "custom"
        # created_at was None -> DEFAULT now() still fired.
        assert loaded.created_at is not None
        assert t.created_at == loaded.created_at

    async def test_create_path_picks_up_defaults(self, setup_table):
        """cygnet.create(db, obj) — the no-ON-CONFLICT path used directly
        by callers like Magenta's bootstrap — also benefits from the
        DEFAULT-aware codegen."""
        db = setup_table
        t = TimestampedThing(id=None, name="gamma")
        await cygnet.create(db, t)
        assert t.id is not None
        assert t.created_at is not None
        assert t.status == "pending"

    async def test_save_path_picks_up_defaults_on_new_row(self, setup_table):
        """cygnet.save(db, obj) on a DBKey=None object delegates to
        run_insert internally, so DEFAULTs fire and the obj is populated.
        This branch hasn't changed in B3 but anchors the contract."""
        db = setup_table
        t = TimestampedThing(id=None, name="delta")
        await cygnet.save(db, t)
        assert t.id is not None
        assert t.created_at is not None
        assert t.status == "pending"

    async def test_save_existing_row_preserves_default_column(self, setup_table):
        """B3 fix end-to-end.  An upsert where a None field has a schema
        DEFAULT must NOT clobber the existing row's value to NULL — the
        column is omitted from both the INSERT list and the SET clause,
        and RETURNING refreshes the in-memory object to match the
        preserved DB value.
        """
        db = setup_table

        # First persist the row so created_at gets a real timestamp.
        t = TimestampedThing(id=None, name="epsilon", status="active")
        await cygnet.save(db, t)
        original_created_at = t.created_at
        original_id = t.id
        assert original_created_at is not None

        # Re-save with created_at=None and status=None.  Under the bug
        # (pre-fix), both would be clobbered to NULL.  Post-fix, both
        # are omitted from INSERT and SET; RETURNING refreshes the obj
        # with the DB's preserved values.
        t.created_at = None
        t.status = None
        t.name = "epsilon-updated"
        await cygnet.save(db, t)

        # In-memory: created_at refreshed from RETURNING with the
        # preserved value; status likewise; name reflects the new value.
        assert t.id == original_id
        assert t.created_at == original_created_at, (
            "B3: existing DEFAULT value was clobbered to NULL"
        )
        assert t.status == "active", "B3: existing 'active' was overwritten"
        assert t.name == "epsilon-updated"

        # DB row matches: a fresh SELECT sees the same values.
        loaded = await cygnet.get(db, TimestampedThingTable, id=original_id)
        assert loaded is not None
        assert loaded.created_at == original_created_at
        assert loaded.status == "active"
        assert loaded.name == "epsilon-updated"

    async def test_save_existing_row_with_explicit_override_writes_it(
        self, setup_table
    ):
        """The negative companion: a non-None value on a DEFAULTed column
        IS written through the upsert (the app is overriding the
        default).  Preserves the historical contract for explicit values."""
        db = setup_table
        t = TimestampedThing(id=None, name="zeta", status="active")
        await cygnet.save(db, t)
        original_id = t.id

        # Explicitly override status (DEFAULT-eligible but caller wants
        # a value): it must be written.
        t.status = "archived"
        await cygnet.save(db, t)

        loaded = await cygnet.get(db, TimestampedThingTable, id=original_id)
        assert loaded is not None
        assert loaded.status == "archived"
