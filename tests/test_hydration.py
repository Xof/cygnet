# test_hydration.py — Row→object construction strategy (meta.row_builder)
# and end-to-end hydration parity through the executor.
import dataclasses
from typing import Annotated

import cygnet
from cygnet.annotations import DBKey
from tests.conftest import Account, AccountTable, FakeDB, TaggedTable


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


async def test_row_to_obj_delegates_to_row_builder(monkeypatch):
    # White-box: hydration must go through meta.row_builder, not a private
    # kwargs path.  Swapping the builder for a sentinel proves the delegation.
    # AccountTable._meta is a shared singleton; monkeypatch's function scope
    # reverts the swap after this test (so other tests see the real builder).
    sentinel = object()
    monkeypatch.setattr(
        AccountTable._meta, "row_builder", lambda row: sentinel
    )
    db = FakeDB(rows=[(1, "a", "a@example.com")])
    result = await cygnet.SELECT(db).FROM(AccountTable)
    assert result == [sentinel]
