# test_row_values.py — Real-PG coverage for cygnet.row / cygnet.in_.
#
# The unit tests assert the emitted SQL *string*; this file asserts that a
# live server parses it and returns the rows the construct is supposed to
# mean.  Three claims here can only be settled against real PG:
#
#   1. `(a, b) IN ((…), (…))` with per-element $N parameters parses and
#      binds — the shape the composite-key batch lookup depends on.
#   2. Row ordering is *lexicographic*, not element-wise AND.  The
#      keyset-pagination idiom is only correct if PG agrees, and the seed
#      data below contains the row that distinguishes the two readings.
#   3. `(a, b) IS NULL` means "every member is null", not "the row is
#      null" — the documented caveat of the None → IS NULL rewrite.  A
#      row with one null member must NOT match.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import DBKey
from cygnet.psycopg_db import PsycopgDB

pytestmark = pytest.mark.integration


@dataclasses.dataclass
@cygnet.table("pair")
class Pair:
    id: Annotated[int, DBKey]
    a: int | None
    b: str | None


PairTable = cygnet.Table(Pair)


@pytest.fixture(scope="module")
async def db(conn):
    await conn.execute("""
        CREATE TEMP TABLE pair (
            id SERIAL PRIMARY KEY,
            a  INTEGER,
            b  TEXT
        )
    """)
    yield PsycopgDB(conn)


@pytest.fixture
async def seeded(db):
    """Reset to a fixed dataset before each test.

    ids 1-4 are the (a, b) grid {1,2} × {'x','y'}; id 5 is both-null and
    id 6 is half-null, which is what separates "row IS NULL" from "member
    IS NULL".  Row (2, 'x') is the lexicographic discriminator: it sorts
    after (1, 'y') even though 'x' < 'y'.
    """
    await db.execute("TRUNCATE pair RESTART IDENTITY")
    await db.execute(
        "INSERT INTO pair (a, b) VALUES "
        "(1, 'x'), (1, 'y'), (2, 'x'), (2, 'y'), (NULL, NULL), (NULL, 'z')"
    )
    return db


async def test_row_equality_matches_one_row(seeded):
    rows = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(cygnet.row(PairTable.a, PairTable.b) == (1, "x"))
    )
    assert [r.id for r in rows] == [1]


async def test_composite_in_list_matches_exactly_those_pairs(seeded):
    rows = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(
            cygnet.in_(
                cygnet.row(PairTable.a, PairTable.b),
                [(1, "y"), (2, "x")],
            )
        )
        .ORDER_BY(PairTable.id)
    )
    assert [r.id for r in rows] == [2, 3]


async def test_composite_in_list_with_one_pair(seeded):
    rows = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(cygnet.in_(cygnet.row(PairTable.a, PairTable.b), [(2, "y")]))
    )
    assert [r.id for r in rows] == [4]


async def test_row_ordering_is_lexicographic_not_elementwise(seeded):
    """`(a, b) > (1, 'y')` must include (2, 'x') — the whole point of the
    construct.  An element-wise `a > 1 AND b > 'y'` would exclude it."""
    rows = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(cygnet.row(PairTable.a, PairTable.b) > (1, "y"))
        .ORDER_BY(PairTable.a, PairTable.b)
    )
    assert [r.id for r in rows] == [3, 4]


async def test_keyset_pagination_walks_the_whole_ordering(seeded):
    """The pagination idiom end to end: page by (a, b), resuming from the
    last row of the previous page."""
    page_one = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(cygnet.is_not_null(PairTable.a))
        .ORDER_BY(PairTable.a, PairTable.b)
        .LIMIT(2)
    )
    assert [r.id for r in page_one] == [1, 2]

    last = page_one[-1]
    page_two = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(cygnet.is_not_null(PairTable.a))
        .WHERE(cygnet.row(PairTable.a, PairTable.b) > (last.a, last.b))
        .ORDER_BY(PairTable.a, PairTable.b)
        .LIMIT(2)
    )
    assert [r.id for r in page_two] == [3, 4]


async def test_row_is_null_means_every_member_is_null(seeded):
    """Grounds the documented caveat: id 6 has a null `a` but a non-null
    `b`, and must NOT match."""
    rows = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(cygnet.row(PairTable.a, PairTable.b) == None)  # noqa: E711
    )
    assert [r.id for r in rows] == [5]


async def test_single_element_row_degenerates_to_the_column(seeded):
    rows = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(cygnet.in_(cygnet.row(PairTable.a), [(1,)]))
        .ORDER_BY(PairTable.id)
    )
    assert [r.id for r in rows] == [1, 2]


async def test_scalar_in_list(seeded):
    rows = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(cygnet.in_(PairTable.id, [2, 4]))
        .ORDER_BY(PairTable.id)
    )
    assert [r.id for r in rows] == [2, 4]


async def test_composite_in_composes_with_other_predicates(seeded):
    rows = await (
        cygnet.SELECT(seeded)
        .FROM(PairTable)
        .WHERE(PairTable.b == "x")
        .WHERE(
            cygnet.in_(
                cygnet.row(PairTable.a, PairTable.b),
                [(1, "x"), (1, "y"), (2, "y")],
            )
        )
    )
    assert [r.id for r in rows] == [1]
