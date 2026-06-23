# test_hydration.py — Row→object construction strategy (meta.row_builder)
# and end-to-end hydration parity through the executor.
import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import DBKey
from tests.conftest import (
    Account,
    AccountTable,
    Doc,
    DocTable,
    Event,
    EventTable,
    FakeDB,
    LogEntry,
    LogTable,
    TaggedAccount,
    TaggedTable,
)


def test_standard_dataclass_uses_positional_builder():
    assert AccountTable._meta.row_builder.__name__ == "_build_positional"


def test_column_rename_stays_positional():
    # Column("tag_name") changes column_name, not attr order — still positional.
    assert TaggedTable._meta.row_builder.__name__ == "_build_positional"


def test_kw_only_field_falls_back_to_kwargs():
    @dataclasses.dataclass
    class KwOnly:
        id: Annotated[int, DBKey]
        label: str = dataclasses.field(kw_only=True)

    assert cygnet.Table(KwOnly)._meta.row_builder.__name__ == "_build_kwargs"


def test_init_false_field_falls_back_to_kwargs():
    @dataclasses.dataclass
    class HasInitFalse:
        id: Annotated[int, DBKey]
        name: str
        computed: str = dataclasses.field(default="x", init=False)

    # Not constructible on either path; we only assert the gate picks fallback.
    assert cygnet.Table(HasInitFalse)._meta.row_builder.__name__ == "_build_kwargs"


@pytest.mark.parametrize(
    "table, row, expected",
    [
        (AccountTable, (1, "Ann", "a@e.co"), Account(1, "Ann", "a@e.co")),
        (LogTable, (2, 1, "msg"), LogEntry(2, 1, "msg")),
        (EventTable, ("evt-1", "launch"), Event("evt-1", "launch")),
        (TaggedTable, (5, "vip"), TaggedAccount(5, "vip")),
        (DocTable, (3, ["a", "b"], {"x": 1}), Doc(3, ["a", "b"], {"x": 1})),
    ],
)
def test_row_builder_parity_across_conftest_models(table, row, expected):
    # Golden parity for every shipped test model (the spec's full-model list):
    # the chosen builder round-trips a positional row to an object equal to
    # direct construction — for both positional and rename (TaggedAccount) shapes.
    assert table._meta.row_builder(row) == expected


def test_positional_builder_constructs_correctly():
    obj = AccountTable._meta.row_builder((1, "Ann", "ann@example.com"))
    assert obj == Account(id=1, name="Ann", email="ann@example.com")


def test_kwargs_fallback_constructs_correctly():
    @dataclasses.dataclass
    class KwOnly:
        id: Annotated[int, DBKey]
        label: str = dataclasses.field(kw_only=True)

    obj = cygnet.Table(KwOnly)._meta.row_builder((1, "hi"))
    assert obj == KwOnly(id=1, label="hi")


@pytest.mark.parametrize("row", [(1,), (1, "hi", "extra")])
def test_kwargs_fallback_rejects_arity_mismatch(row):
    # Pins the zip(..., strict=True) guard in _build_kwargs: a row whose length
    # doesn't match the field count (too short OR too long) must raise
    # ValueError at the build seam rather than silently truncating to the
    # shorter side.  KwOnly has exactly two fields, so neither a 1- nor a
    # 3-element row aligns.  If strict=True were removed, zip would truncate
    # and cls(**...) would build a partial/wrong object without raising —
    # this test fails in that case.
    @dataclasses.dataclass
    class KwOnly:
        id: Annotated[int, DBKey]
        label: str = dataclasses.field(kw_only=True)

    builder = cygnet.Table(KwOnly)._meta.row_builder
    assert builder.__name__ == "_build_kwargs"
    with pytest.raises(ValueError):
        builder(row)


async def test_row_to_obj_delegates_to_row_builder(monkeypatch):
    # White-box: hydration must go through meta.row_builder, not a private
    # kwargs path.  Swapping the builder for a sentinel proves the delegation.
    # AccountTable._meta is a shared singleton; monkeypatch's function scope
    # reverts the swap after this test (so other tests see the real builder).
    sentinel = object()
    monkeypatch.setattr(AccountTable._meta, "row_builder", lambda row: sentinel)
    db = FakeDB(rows=[(1, "a", "a@example.com")])
    result = await cygnet.SELECT(db).FROM(AccountTable)
    assert result == [sentinel]


async def test_right_join_left_side_miss_yields_none():
    # RIGHT JOIN: accounts.* is NULL for an orphan log row → left object None,
    # right object present.  Locks the hoisted left_can_miss decision.
    rows = [(None, None, None, 5, 99, "orphan log")]
    db = FakeDB(rows=rows)
    result = await (
        cygnet.SELECT(db)
        .FROM(AccountTable)
        .RIGHT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
    )
    ((acct, log),) = result
    assert acct is None
    assert log == LogEntry(id=5, account_id=99, message="orphan log")


async def test_left_join_right_side_miss_yields_none():
    # LEFT JOIN: the log (right) side is NULL when an account has no matching
    # log → right object None, left object present.  Locks the per-join cm=True
    # (LEFT/FULL) branch the row mapper restructured into its plan.
    rows = [(1, "Ann", "ann@example.com", None, None, None)]
    db = FakeDB(rows=rows)
    result = await (
        cygnet.SELECT(db)
        .FROM(AccountTable)
        .LEFT_JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
    )
    ((acct, log),) = result
    assert acct == Account(id=1, name="Ann", email="ann@example.com")
    assert log is None


async def test_inner_join_maps_both_sides():
    rows = [(1, "Ann", "ann@example.com", 7, 1, "hello")]
    db = FakeDB(rows=rows)
    result = await (
        cygnet.SELECT(db)
        .FROM(AccountTable)
        .JOIN(LogTable, ON=AccountTable.id == LogTable.account_id)
    )
    ((acct, log),) = result
    assert acct == Account(id=1, name="Ann", email="ann@example.com")
    assert log == LogEntry(id=7, account_id=1, message="hello")


async def test_columnar_returns_raw_tuples():
    db = FakeDB(rows=[(1, "Ann")])
    result = await cygnet.SELECT(db, AccountTable.id, AccountTable.name).FROM(
        AccountTable
    )
    assert result == [(1, "Ann")]


async def test_stream_matches_enbloc():
    rows = [(i, f"U{i}", f"u{i}@example.com") for i in range(5)]
    enbloc = await cygnet.SELECT(FakeDB(rows=rows)).FROM(AccountTable)
    streamed = [
        obj
        async for obj in cygnet.SELECT(FakeDB(rows=rows)).FROM(AccountTable).stream()
    ]
    assert streamed == enbloc
