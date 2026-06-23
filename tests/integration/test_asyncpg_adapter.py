# test_asyncpg_adapter.py — Integration tests for the asyncpg adapter.
#
# Proves AsyncpgDB satisfies Cygnet's adapter protocol against a real
# PostgreSQL the same way PsycopgDB does: SELECT round-trip + hydration,
# INSERT ... RETURNING PK populate, and cygnet.transaction() commit/rollback.
# Requires CYGNET_TEST_DSN; uses a TEMP table so nothing persists.
from __future__ import annotations

import dataclasses
import os
from typing import Annotated

import pytest
import pytest_asyncio

import cygnet
from cygnet.annotations import DBKey

asyncpg = pytest.importorskip("asyncpg")
from cygnet.asyncpg_db import AsyncpgDB  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    # asyncpg binds its connection to the event loop at connect time.
    # module loop scope ensures the fixture and all tests share one loop.
    pytest.mark.asyncio(loop_scope="module"),
]

DSN = os.environ.get("CYGNET_TEST_DSN", "")


@dataclasses.dataclass
class Gizmo:
    id: Annotated[int, DBKey]
    name: str
    qty: int


GizmoTable = cygnet.Table(Gizmo)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def db():
    # One asyncpg connection + TEMP table shared across the module's tests.
    # Tests are order-independent: each filters by a distinct name/id, so rows
    # left behind (e.g. "Committed") don't affect the others — preserve that if
    # adding tests (no COUNT(*)-style assertions on whole-table contents).
    if not DSN:
        pytest.skip("CYGNET_TEST_DSN not set")
    conn = await asyncpg.connect(DSN)
    await conn.execute(
        "CREATE TEMP TABLE gizmos "
        "(id SERIAL PRIMARY KEY, name TEXT NOT NULL, qty INT NOT NULL)"
    )
    yield AsyncpgDB(conn)
    await conn.close()


class TestAsyncpgAdapter:
    async def test_insert_returning_and_select(self, db):
        g = Gizmo(id=None, name="Widget", qty=3)
        await cygnet.INSERT(db).INTO(GizmoTable).VALUES(g)
        assert g.id is not None  # RETURNING populated the PK

        rows = await cygnet.SELECT(db).FROM(GizmoTable).WHERE(GizmoTable.id == g.id)
        assert len(rows) == 1
        assert rows[0] == Gizmo(id=g.id, name="Widget", qty=3)
        assert isinstance(rows[0], Gizmo)  # hydrated to a dataclass, not a Record

    async def test_transaction_commit(self, db):
        async with cygnet.transaction(db):
            g = Gizmo(id=None, name="Committed", qty=1)
            await cygnet.INSERT(db).INTO(GizmoTable).VALUES(g)
        found = await cygnet.get(db, GizmoTable, id=g.id)
        assert found is not None and found.name == "Committed"

    async def test_transaction_rollback(self, db):
        g = Gizmo(id=None, name="RolledBack", qty=9)
        with pytest.raises(RuntimeError, match="boom"):
            async with cygnet.transaction(db):
                await cygnet.INSERT(db).INTO(GizmoTable).VALUES(g)
                raise RuntimeError("boom")
        # The row must not have persisted.
        matches = await cygnet.SELECT(db).FROM(GizmoTable).WHERE(
            GizmoTable.name == "RolledBack"
        )
        assert matches == []
