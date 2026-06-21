# test_advanced_queries.py — Real-PG coverage for the high-risk constructs that
# were previously validated only by FakeDB SQL-string assertions (S39): LATERAL,
# correlated EXISTS / IN subqueries, set-op scoping (incl. an operand carrying
# its own ORDER BY / LIMIT — the B7 regression), INTERSECT / EXCEPT / UNION ALL,
# ON CONFLICT ON CONSTRAINT / DO_UPDATE_FROM_EXCLUDED, UPDATE/DELETE RETURNING,
# and row locking.  A plausible-but-invalid emission passes a substring check
# but fails here against a live server — which is the whole point.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import DBKey
from cygnet.psycopg_db import PsycopgDB

pytestmark = pytest.mark.integration


@dataclasses.dataclass
@cygnet.table("acc")
class Acc:
    id: Annotated[int, DBKey]
    name: str
    email: str


@dataclasses.dataclass
@cygnet.table("ev")
class Ev:
    id: Annotated[int, DBKey]
    acc_id: Annotated[int, cygnet.ForeignKey(Acc)]
    body: str


AccTable = cygnet.Table(Acc)
EvTable = cygnet.Table(Ev)


@pytest.fixture(scope="module")
async def db(conn):
    await conn.execute("""
        CREATE TEMP TABLE acc (
            id    SERIAL PRIMARY KEY,
            name  TEXT NOT NULL,
            email TEXT NOT NULL,
            CONSTRAINT uq_acc_email UNIQUE (email)
        )
    """)
    await conn.execute("""
        CREATE TEMP TABLE ev (
            id     SERIAL PRIMARY KEY,
            acc_id INTEGER NOT NULL REFERENCES acc(id),
            body   TEXT NOT NULL
        )
    """)
    yield PsycopgDB(conn)


@pytest.fixture
async def seeded(db):
    """Reset to a deterministic dataset before each test.

    RESTART IDENTITY makes the serial ids predictable (acc 1=Fred, 2=Lonely,
    3=Barney), so set-op tests that reason about id ranges stay stable
    regardless of test order.  Fred has two events; Lonely and Barney have none.
    """
    await db.execute("TRUNCATE acc, ev RESTART IDENTITY CASCADE", [])
    fred = Acc(id=None, name="Fred", email="fred@x.com")
    lonely = Acc(id=None, name="Lonely", email="lonely@x.com")
    barney = Acc(id=None, name="Barney", email="barney@x.com")
    for a in (fred, lonely, barney):
        await cygnet.create(db, a)
    for body in ("first", "second"):
        await cygnet.create(db, Ev(id=None, acc_id=fred.id, body=body))
    return db


class TestLateral:
    async def test_left_join_lateral_top_one_per_group(self, seeded):
        """Most-recent event per account; accounts with no events get NULL."""
        recent = (
            cygnet.SELECT(seeded, EvTable.body)
            .FROM(EvTable)
            .WHERE(EvTable.acc_id == AccTable.id)
            .ORDER_BY(EvTable.id, DESC=True)
            .LIMIT(1)
        )
        recent_lat = cygnet.lateral("recent", recent, columns=["body"])
        rows = await (
            cygnet.SELECT(seeded, AccTable.name, recent_lat.body)
            .FROM(AccTable)
            .LEFT_JOIN_LATERAL(recent_lat)
            .ORDER_BY(AccTable.name)
        )
        assert rows == [
            ("Barney", None),
            ("Fred", "second"),
            ("Lonely", None),
        ]


class TestSubqueryPredicates:
    async def test_correlated_exists_and_not_exists(self, seeded):
        any_ev = (
            cygnet.SELECT(seeded, cygnet.lit("1"))
            .FROM(EvTable)
            .WHERE(EvTable.acc_id == AccTable.id)
        )
        with_events = await (
            cygnet.SELECT(seeded)
            .FROM(AccTable)
            .WHERE(cygnet.exists(any_ev))
            .ORDER_BY(AccTable.name)
        )
        assert [a.name for a in with_events] == ["Fred"]

        without = await (
            cygnet.SELECT(seeded)
            .FROM(AccTable)
            .WHERE(cygnet.not_exists(any_ev))
            .ORDER_BY(AccTable.name)
        )
        assert [a.name for a in without] == ["Barney", "Lonely"]

    async def test_in_subquery(self, seeded):
        fred_ids = (
            cygnet.SELECT(seeded, AccTable.id)
            .FROM(AccTable)
            .WHERE(AccTable.name == "Fred")
        )
        fred_events = await (
            cygnet.SELECT(seeded)
            .FROM(EvTable)
            .WHERE(cygnet.op(EvTable.acc_id, "IN", fred_ids))
            .ORDER_BY(EvTable.body)
        )
        assert [e.body for e in fred_events] == ["first", "second"]


class TestSetOps:
    async def test_intersect_and_except(self, seeded):
        low = (
            cygnet.SELECT(seeded, AccTable.name).FROM(AccTable).WHERE(AccTable.id <= 2)
        )
        high = (
            cygnet.SELECT(seeded, AccTable.name).FROM(AccTable).WHERE(AccTable.id >= 2)
        )
        # ids 1=Fred,2=Lonely,3=Barney → low={Fred,Lonely}, high={Lonely,Barney}
        inter = await (
            cygnet.SELECT(seeded, AccTable.name)
            .FROM(AccTable)
            .WHERE(AccTable.id <= 2)
            .INTERSECT(high)
        )
        assert [r[0] for r in inter] == ["Lonely"]

        diff = await (
            cygnet.SELECT(seeded, AccTable.name)
            .FROM(AccTable)
            .WHERE(AccTable.id <= 2)
            .EXCEPT_(
                cygnet.SELECT(seeded, AccTable.name)
                .FROM(AccTable)
                .WHERE(AccTable.id >= 2)
            )
        )
        assert [r[0] for r in diff] == ["Fred"]
        assert low is not None  # (low built for symmetry / readability)

    async def test_compound_order_by_limit_binds_to_whole(self, seeded):
        """Trailing ORDER BY / LIMIT on the left builder scope the whole
        compound — not the last operand."""
        res = await (
            cygnet.SELECT(seeded, AccTable.name)
            .FROM(AccTable)
            .WHERE(AccTable.id <= 2)
            .UNION(
                cygnet.SELECT(seeded, AccTable.name)
                .FROM(AccTable)
                .WHERE(AccTable.id >= 2)
            )
            .ORDER_BY(cygnet.lit("name"))
            .LIMIT(2)
        )
        # union {Fred,Lonely} ∪ {Lonely,Barney} = {Barney,Fred,Lonely}; sorted,
        # limit 2 → Barney, Fred (all capitalised, so ASCII order is stable).
        assert [r[0] for r in res] == ["Barney", "Fred"]

    async def test_operand_with_own_order_by_limit(self, seeded):
        """B7 end-to-end: a set-op operand carrying its OWN ORDER BY / LIMIT is
        parenthesised and runs against real PG.  Pre-B7 this rendered as invalid
        SQL (the operand's clauses leaked into the compound)."""
        res = await (
            cygnet.SELECT(seeded, AccTable.name)
            .FROM(AccTable)
            .WHERE(AccTable.id == 1)  # Fred
            .UNION(
                cygnet.SELECT(seeded, AccTable.name)
                .FROM(AccTable)
                .ORDER_BY(AccTable.name)
                .LIMIT(1)  # top-1 by name = Barney
            )
        )
        assert sorted(r[0] for r in res) == ["Barney", "Fred"]


class TestOnConflict:
    async def test_do_update_from_excluded(self, seeded):
        """Conflict on the unique email rewrites the existing row's name with
        the value the new row tried to insert."""
        dup = Acc(id=None, name="Frederick", email="fred@x.com")
        await (
            cygnet.INSERT(seeded)
            .INTO(AccTable)
            .VALUES(dup)
            .ON_CONFLICT(AccTable.email)
            .DO_UPDATE_FROM_EXCLUDED(AccTable.name)
        )
        [row] = await (
            cygnet.SELECT(seeded).FROM(AccTable).WHERE(AccTable.email == "fred@x.com")
        )
        assert row.name == "Frederick"

    async def test_on_constraint_do_nothing_skips(self, seeded):
        before = await (
            cygnet.SELECT(seeded).FROM(AccTable).WHERE(AccTable.email == "fred@x.com")
        )
        dup = Acc(id=None, name="Ignored", email="fred@x.com")
        result = await (
            cygnet.INSERT(seeded)
            .INTO(AccTable)
            .VALUES(dup)
            .ON_CONFLICT_CONSTRAINT("uq_acc_email")
            .DO_NOTHING()
        )
        assert result is None  # row skipped; PK left unset
        after = await (
            cygnet.SELECT(seeded).FROM(AccTable).WHERE(AccTable.email == "fred@x.com")
        )
        # The existing row is untouched (still "Fred", not "Ignored").
        assert len(after) == len(before) == 1
        assert after[0].name == "Fred"


class TestReturning:
    async def test_update_returning(self, seeded):
        returned = await (
            cygnet.UPDATE(seeded)
            .SET(AccTable, name="Renamed")
            .WHERE(AccTable.id == 3)
            .RETURNING(AccTable.id, AccTable.name)
        )
        assert returned == [(3, "Renamed")]

    async def test_delete_returning(self, seeded):
        returned = await (
            cygnet.DELETE(seeded)
            .FROM(EvTable)
            .WHERE(EvTable.acc_id == 1)
            .RETURNING(EvTable.body)
        )
        assert sorted(r[0] for r in returned) == ["first", "second"]


class TestRowLocking:
    async def test_for_update_executes(self, seeded):
        locked = await (
            cygnet.SELECT(seeded).FROM(AccTable).WHERE(AccTable.id == 1).FOR_UPDATE()
        )
        assert len(locked) == 1
        assert locked[0].name == "Fred"

    async def test_for_update_skip_locked_executes(self, seeded):
        batch = await (
            cygnet.SELECT(seeded)
            .FROM(AccTable)
            .ORDER_BY(AccTable.id)
            .LIMIT(5)
            .FOR_UPDATE(skip_locked=True)
        )
        assert [a.name for a in batch] == ["Fred", "Lonely", "Barney"]
