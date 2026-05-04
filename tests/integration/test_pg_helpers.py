# test_pg_helpers.py — Integration tests for cygnet.jsonb, .arrays, .fts.
#
# Unit tests in tests/test_pg_helpers.py verify the SQL string shape
# against FakeDB; this file verifies the SQL actually runs against real
# PostgreSQL and returns the rows we expect.  The two suites overlap
# deliberately: the unit tests catch generation regressions, the
# integration tests catch operator-precedence and type-adaptation
# mistakes that only surface in PG.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
import cygnet.arrays as arr
import cygnet.fts as fts
import cygnet.jsonb as jb
from cygnet.annotations import DBKey
from cygnet.psycopg_db import PsycopgDB

pytestmark = pytest.mark.integration


@dataclasses.dataclass
class Item:
    id: Annotated[int, DBKey]
    title: str
    tags: list[str]
    payload: dict


ItemTable = cygnet.Table(Item)


class TestJsonbRoundtrip:
    @pytest.fixture(autouse=True)
    async def setup_table(self, conn):
        # JSONB column: PG stores arbitrary JSON, the helpers exercise the
        # native operators against it.
        await conn.execute("""
            CREATE TEMP TABLE items (
                id      SERIAL PRIMARY KEY,
                title   TEXT NOT NULL,
                tags    TEXT[] NOT NULL DEFAULT '{}',
                payload JSONB NOT NULL DEFAULT '{}'::jsonb
            )
        """)
        yield PsycopgDB(conn)
        await conn.execute("DROP TABLE IF EXISTS items")

    async def test_contains(self, setup_table):
        db = setup_table
        await cygnet.create(
            db, Item(id=None, title="A", tags=[], payload={"role": "admin"})
        )
        await cygnet.create(
            db, Item(id=None, title="B", tags=[], payload={"role": "user"})
        )
        results = await (
            cygnet.SELECT(db)
            .FROM(ItemTable)
            .WHERE(jb.contains(ItemTable.payload, {"role": "admin"}))
        )
        assert len(results) == 1
        assert results[0].title == "A"

    async def test_get_text_compares(self, setup_table):
        db = setup_table
        await cygnet.create(
            db,
            Item(id=None, title="A", tags=[], payload={"name": "Fred"}),
        )
        await cygnet.create(
            db,
            Item(id=None, title="B", tags=[], payload={"name": "Wilma"}),
        )
        results = await (
            cygnet.SELECT(db)
            .FROM(ItemTable)
            .WHERE(jb.get_text(ItemTable.payload, "name") == "Fred")
        )
        assert len(results) == 1
        assert results[0].title == "A"

    async def test_has_key(self, setup_table):
        db = setup_table
        await cygnet.create(
            db, Item(id=None, title="A", tags=[], payload={"email": "x@y"})
        )
        await cygnet.create(
            db, Item(id=None, title="B", tags=[], payload={"name": "Fred"})
        )
        results = await (
            cygnet.SELECT(db)
            .FROM(ItemTable)
            .WHERE(jb.has_key(ItemTable.payload, "email"))
        )
        assert len(results) == 1
        assert results[0].title == "A"


class TestArrayRoundtrip:
    @pytest.fixture(autouse=True)
    async def setup_table(self, conn):
        await conn.execute("""
            CREATE TEMP TABLE items (
                id      SERIAL PRIMARY KEY,
                title   TEXT NOT NULL,
                tags    TEXT[] NOT NULL DEFAULT '{}',
                payload JSONB NOT NULL DEFAULT '{}'::jsonb
            )
        """)
        yield PsycopgDB(conn)
        await conn.execute("DROP TABLE IF EXISTS items")

    async def test_contains(self, setup_table):
        db = setup_table
        await cygnet.create(
            db, Item(id=None, title="A", tags=["py", "sql"], payload={})
        )
        await cygnet.create(db, Item(id=None, title="B", tags=["js"], payload={}))
        results = await (
            cygnet.SELECT(db)
            .FROM(ItemTable)
            .WHERE(arr.contains(ItemTable.tags, ["py"]))
        )
        assert len(results) == 1
        assert results[0].title == "A"

    async def test_overlaps(self, setup_table):
        db = setup_table
        await cygnet.create(
            db, Item(id=None, title="A", tags=["py", "sql"], payload={})
        )
        await cygnet.create(
            db, Item(id=None, title="B", tags=["go", "rust"], payload={})
        )
        results = await (
            cygnet.SELECT(db)
            .FROM(ItemTable)
            .WHERE(arr.overlaps(ItemTable.tags, ["py", "go"]))
            .ORDER_BY(ItemTable.id)
        )
        assert {r.title for r in results} == {"A", "B"}

    async def test_length(self, setup_table):
        db = setup_table
        await cygnet.create(
            db, Item(id=None, title="A", tags=["a", "b", "c"], payload={})
        )
        await cygnet.create(db, Item(id=None, title="B", tags=["a"], payload={}))
        results = await (
            cygnet.SELECT(db).FROM(ItemTable).WHERE(arr.length(ItemTable.tags) > 1)
        )
        assert len(results) == 1
        assert results[0].title == "A"


class TestFtsRoundtrip:
    @pytest.fixture(autouse=True)
    async def setup_table(self, conn):
        await conn.execute("""
            CREATE TEMP TABLE articles (
                id   SERIAL PRIMARY KEY,
                body TEXT NOT NULL
            )
        """)
        yield PsycopgDB(conn)
        await conn.execute("DROP TABLE IF EXISTS articles")

    async def test_web_query_match(self, setup_table):
        db = setup_table
        # We don't have a Cygnet model for `articles` because we want the
        # body column without registering a full dataclass.  Use raw SQL
        # via execute() for setup; queries below go through the builder
        # against an inline-declared model.
        for body in [
            "Cygnet is a fierce small ORM",
            "PostgreSQL is the world's most advanced open source database",
            "ORMs come in many flavors",
        ]:
            await db.execute("INSERT INTO articles (body) VALUES ($1)", [body])

        @dataclasses.dataclass
        class Article:
            id: Annotated[int, DBKey]
            body: str

        ArticleTable = cygnet.Table(Article)

        # WHERE body @@ websearch_to_tsquery('english', 'fierce ORM')
        results = await (
            cygnet.SELECT(db)
            .FROM(ArticleTable)
            .WHERE(
                fts.matches(
                    fts.to_tsvector(ArticleTable.body),
                    fts.web_query("fierce ORM"),
                )
            )
        )
        assert len(results) == 1
        assert "fierce" in results[0].body

    async def test_rank_orders_results(self, setup_table):
        db = setup_table
        for body in [
            "small ORM tools are useful",
            "Cygnet, a small but fierce ORM",
            "ORMs",
        ]:
            await db.execute("INSERT INTO articles (body) VALUES ($1)", [body])

        @dataclasses.dataclass
        class Article:
            id: Annotated[int, DBKey]
            body: str

        ArticleTable = cygnet.Table(Article)

        results = await (
            cygnet.SELECT(db)
            .FROM(ArticleTable)
            .WHERE(
                fts.matches(
                    fts.to_tsvector(ArticleTable.body),
                    fts.web_query("fierce ORM"),
                )
            )
            .ORDER_BY(
                fts.rank(
                    fts.to_tsvector(ArticleTable.body),
                    fts.web_query("fierce ORM"),
                ),
                DESC=True,
            )
        )
        assert len(results) >= 1
        # The "Cygnet, a small but fierce ORM" article should rank highest:
        # both query terms are present, with "fierce" giving it an edge.
        assert "fierce" in results[0].body


# ── Window functions ──────────────────────────────────────────────────


@dataclasses.dataclass
class Employee:
    id: Annotated[int, DBKey]
    name: str
    dept: str
    salary: int


EmployeeTable = cygnet.Table(Employee)


class TestWindowsRoundtrip:
    @pytest.fixture(autouse=True)
    async def setup_table(self, conn):
        await conn.execute("""
            CREATE TEMP TABLE employees (
                id     SERIAL PRIMARY KEY,
                name   TEXT NOT NULL,
                dept   TEXT NOT NULL,
                salary INT  NOT NULL
            )
        """)
        yield PsycopgDB(conn)
        await conn.execute("DROP TABLE IF EXISTS employees")

    async def test_row_number_per_partition(self, setup_table):
        """ROW_NUMBER OVER (PARTITION BY dept ORDER BY salary DESC):
        each department's top earner is row 1, second is row 2, etc."""
        from cygnet import functions as f

        db = setup_table
        for name, dept, salary in [
            ("Alice", "eng", 120),
            ("Bob", "eng", 100),
            ("Carla", "eng", 150),
            ("Dave", "sales", 80),
            ("Eva", "sales", 90),
        ]:
            await cygnet.create(
                db, Employee(id=None, name=name, dept=dept, salary=salary)
            )

        rn = f.row_number().OVER(
            partition_by=[EmployeeTable.dept],
            order_by=[(EmployeeTable.salary, "DESC")],
        )
        rows = await (
            cygnet.SELECT(db, EmployeeTable.name, EmployeeTable.dept, rn)
            .FROM(EmployeeTable)
            .ORDER_BY(EmployeeTable.dept, EmployeeTable.id)
        )
        # Expect: Alice / Bob / Carla in eng (interleaved by id but ranked
        # by salary DESC), Dave / Eva in sales.  We assert the rank
        # numbers match the salary order within each dept.
        eng = [(name, rank) for name, dept, rank in rows if dept == "eng"]
        sales = [(name, rank) for name, dept, rank in rows if dept == "sales"]
        # Carla 150 is rank 1, Alice 120 is rank 2, Bob 100 is rank 3.
        assert dict(eng) == {"Alice": 2, "Bob": 3, "Carla": 1}
        # Eva 90 is rank 1, Dave 80 is rank 2.
        assert dict(sales) == {"Dave": 2, "Eva": 1}

    async def test_lag_compares_to_previous_row(self, setup_table):
        """LAG(salary) OVER (ORDER BY id) yields each row's predecessor."""
        from cygnet import functions as f

        db = setup_table
        for name, dept, salary in [
            ("A", "x", 10),
            ("B", "x", 20),
            ("C", "x", 30),
        ]:
            await cygnet.create(
                db, Employee(id=None, name=name, dept=dept, salary=salary)
            )

        lag_expr = f.lag(EmployeeTable.salary, 1).OVER(order_by=[EmployeeTable.id])
        rows = await (
            cygnet.SELECT(db, EmployeeTable.name, lag_expr)
            .FROM(EmployeeTable)
            .ORDER_BY(EmployeeTable.id)
        )
        # First row: no predecessor → NULL.  Then each row's lag is the
        # previous row's salary.
        assert rows == [("A", None), ("B", 10), ("C", 20)]
