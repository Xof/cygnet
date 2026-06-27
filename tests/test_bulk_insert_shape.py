# test_bulk_insert_shape.py — Characterization net for the BULK_VALUES render
# path (Executor._render_bulk_insert).
#
# These pin the *exact* SQL, parameter ordering, and error semantics that a
# bulk INSERT must produce, so the per-row hot-loop optimization (hoisting the
# row-invariant column derivation out of the per-row loop) can be proven
# byte-identical.  In particular they pin the two behaviours most at risk from
# that change:
#   - the per-row AppKey-None check still fires (a row whose AppKey is None
#     raises, rather than silently emitting NULL), and
#   - cross-row column-shape divergence is still rejected, in BOTH directions
#     (a later row gaining or losing a column relative to the first).
#
# The looser "skip per-row validation" optimization would turn the AppKey and
# shape tests red — which is exactly why they're here.

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import AppKey
from tests.conftest import Account, AccountTable, Event, EventTable, FakeDB


class TestBulkInsertShape:
    async def test_default_pk_params_in_row_major_order(self):
        """DBKey=None is omitted; params accumulate row-major in column order."""
        db = FakeDB(rows=[(1,), (2,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .BULK_VALUES([Account(None, "Fred", "f@x"), Account(None, "Wilma", "w@x")])
        )
        assert db.last_sql == (
            "INSERT INTO accounts (name, email) VALUES ($1, $2), ($3, $4) RETURNING id"
        )
        assert db.last_params == ["Fred", "f@x", "Wilma", "w@x"]

    async def test_explicit_pk_includes_id_column(self):
        """A DBKey supplied with a non-None value on every row emits the id
        column (and still RETURNING id), params interleaved per row."""
        db = FakeDB(rows=[(1,), (2,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .BULK_VALUES([Account(10, "Fred", "f@x"), Account(20, "Wilma", "w@x")])
        )
        assert db.last_sql == (
            "INSERT INTO accounts (id, name, email) "
            "VALUES ($1, $2, $3), ($4, $5, $6) RETURNING id"
        )
        assert db.last_params == [10, "Fred", "f@x", 20, "Wilma", "w@x"]

    async def test_shape_mismatch_extra_column_raises(self):
        """Row 0 omits the DBKey (None); a later row supplies it → that row
        would emit a column the first didn't.  Rejected."""
        db = FakeDB(rows=[(1,), (2,)])
        with pytest.raises(ValueError, match="consistent column shape across rows"):
            await (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .BULK_VALUES([Account(None, "Fred", "f@x"), Account(5, "Wilma", "w@x")])
            )

    async def test_shape_mismatch_missing_column_raises(self):
        """Row 0 supplies the DBKey; a later row omits it (None) → that row
        would drop a column the first had.  Rejected (the other direction)."""
        db = FakeDB(rows=[(1,), (2,)])
        with pytest.raises(ValueError, match="consistent column shape across rows"):
            await (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .BULK_VALUES([Account(5, "Fred", "f@x"), Account(None, "Wilma", "w@x")])
            )

    async def test_appkey_none_in_later_row_raises(self):
        """An AppKey that is None on row 1 must raise — not silently emit NULL.
        Row 0 is valid, so the failure is specifically a per-row check."""
        db = FakeDB()
        with pytest.raises(ValueError, match="is AppKey but value is None"):
            await (
                cygnet.INSERT(db)
                .INTO(EventTable)
                .BULK_VALUES([Event("e1", "Launch"), Event(None, "Liftoff")])
            )

    async def test_single_object_bulk(self):
        """A one-element BULK_VALUES exercises only the row-0 path (objs[1:]
        empty) — the hoist's per-row loop must be a no-op here."""
        db = FakeDB(rows=[(1,)])
        await (
            cygnet.INSERT(db)
            .INTO(AccountTable)
            .BULK_VALUES([Account(None, "Solo", "s@x")])
        )
        assert db.last_sql == (
            "INSERT INTO accounts (name, email) VALUES ($1, $2) RETURNING id"
        )
        assert db.last_params == ["Solo", "s@x"]

    async def test_appkey_consistent_rows_emit_all(self):
        """Sanity: well-formed multi-row AppKey bulk emits every row, no
        RETURNING (AppKey PKs aren't DB-generated)."""
        db = FakeDB()
        await (
            cygnet.INSERT(db)
            .INTO(EventTable)
            .BULK_VALUES(
                [Event("e1", "Launch"), Event("e2", "Liftoff"), Event("e3", "Orbit")]
            )
        )
        assert db.last_sql == (
            "INSERT INTO events (id, name) VALUES ($1, $2), ($3, $4), ($5, $6)"
        )
        assert db.last_params == ["e1", "Launch", "e2", "Liftoff", "e3", "Orbit"]
        assert "RETURNING" not in db.last_sql

    async def test_three_column_no_pk_model(self):
        """A model whose only key is application-supplied still interleaves
        params correctly across rows (guards the per-row append order)."""

        @dataclasses.dataclass
        class Widget:
            sku: Annotated[str, AppKey]
            name: str
            qty: int

        WidgetTable = cygnet.Table(Widget)
        db = FakeDB()
        await (
            cygnet.INSERT(db)
            .INTO(WidgetTable)
            .BULK_VALUES([Widget("a", "Anvil", 3), Widget("b", "Bolt", 7)])
        )
        assert db.last_params == ["a", "Anvil", 3, "b", "Bolt", 7]
