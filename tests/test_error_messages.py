# test_error_messages.py — Defensive guards that turn cryptic
# AttributeErrors / late PG-side errors into actionable Cygnet-level
# messages.  These complement the strict mypy types: anyone running
# mypy strict catches the misuse statically, but plenty of users
# don't, and the runtime error has historically been an unhelpful
# `'NoneType' object has no attribute '_meta'`.

from __future__ import annotations

import dataclasses
from typing import Annotated

import pytest

import cygnet
from cygnet.annotations import DBKey
from tests.conftest import Account, AccountTable, FakeDB, LogTable


@dataclasses.dataclass
class _UnregisteredAccount:
    """Same shape as Account but never wrapped in cygnet.Table — used
    to exercise the wrong-model VALUES() guard."""

    id: Annotated[int, DBKey]
    name: str
    email: str


class TestTableSourceTypeCheck:
    """Passing the dataclass class instead of cygnet.Table(C) is the
    most common FROM/INTO mistake.  The guard names the mistake and
    shows the fix."""

    async def test_select_from_dataclass_class_suggests_table_wrap(self):
        db = FakeDB()
        with pytest.raises(TypeError, match=r"cygnet\.Table\(Account\)"):
            cygnet.SELECT(db).FROM(Account)  # missed cygnet.Table()

    async def test_select_join_dataclass_class_suggests_table_wrap(self):
        db = FakeDB()
        with pytest.raises(TypeError, match=r"cygnet\.Table"):
            (
                cygnet.SELECT(db)
                .FROM(AccountTable)
                .JOIN(Account, ON=AccountTable.id == AccountTable.id)
            )

    async def test_insert_into_dataclass_class_suggests_table_wrap(self):
        db = FakeDB()
        with pytest.raises(TypeError, match=r"cygnet\.Table"):
            cygnet.INSERT(db).INTO(Account)

    async def test_update_set_dataclass_class_suggests_table_wrap(self):
        db = FakeDB()
        with pytest.raises(TypeError, match=r"cygnet\.Table"):
            cygnet.UPDATE(db).SET(Account, name="x")

    async def test_delete_from_dataclass_class_suggests_table_wrap(self):
        db = FakeDB()
        with pytest.raises(TypeError, match=r"cygnet\.Table"):
            cygnet.DELETE(db).FROM(Account)

    async def test_select_from_random_object_names_the_type(self):
        """Non-dataclass non-proxy gets a generic but actionable error."""
        db = FakeDB()
        with pytest.raises(TypeError, match=r"got str:"):
            cygnet.SELECT(db).FROM("accounts")  # type: ignore[arg-type]


class TestMissingTableGuard:
    """Awaiting a builder without setting the target table previously
    blew up with `NoneType._meta` 100 lines deep in the executor.
    Now each verb's render_* catches it with a clear message."""

    async def test_insert_without_into_raises_clearly(self):
        db = FakeDB()
        acc = Account(id=None, name="Fred", email="fred@example.com")
        with pytest.raises(ValueError, match="INSERT requires INTO"):
            await cygnet.INSERT(db).VALUES(acc)

    async def test_update_without_set_raises_clearly(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="UPDATE requires SET"):
            await cygnet.UPDATE(db).WHERE(AccountTable.id == 1)

    async def test_delete_without_from_raises_clearly(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="DELETE requires FROM"):
            await cygnet.DELETE(db).WHERE(AccountTable.id == 1)


class TestValuesTypeCheck:
    """VALUES(obj) with the wrong model class previously failed deep
    in getattr — the model fields don't exist on the unrelated obj.
    Now it fails fast at INSERT time with a message naming both the
    expected and supplied types."""

    async def test_values_with_wrong_class_raises_typeerror(self):
        db = FakeDB(rows=[(1,)])
        # Make a plain object that isn't an Account.
        wrong = _UnregisteredAccount(id=None, name="Fred", email="fred@example.com")
        with pytest.raises(TypeError, match="expects a Account instance"):
            await cygnet.INSERT(db).INTO(AccountTable).VALUES(wrong)

    async def test_values_with_dict_raises_typeerror(self):
        """A dict isn't an Account; should be rejected with the same
        clear message rather than failing on getattr later."""
        db = FakeDB(rows=[(1,)])
        with pytest.raises(TypeError, match="expects a Account instance"):
            await (
                cygnet.INSERT(db)
                .INTO(AccountTable)
                .VALUES(
                    {"name": "Fred", "email": "x"}  # type: ignore[arg-type]
                )
            )


class TestExistingGuardsStillWork:
    """Sanity: other render-time guards (SELECT requires FROM, UPDATE
    requires WHERE, etc.) are unchanged by the new defensive checks."""

    async def test_bare_select_still_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="SELECT requires FROM"):
            await cygnet.SELECT(db)

    async def test_unrestricted_update_still_raises(self):
        db = FakeDB()
        with pytest.raises(ValueError, match="UPDATE requires a WHERE"):
            await cygnet.UPDATE(db).SET(AccountTable, name="x")

    async def test_join_with_wrong_type_caught_at_join_call(self):
        """The new TableSource check fires on JOIN/LEFT_JOIN, not just FROM —
        prevents a builder reaching render with a broken joins list."""
        db = FakeDB()
        with pytest.raises(TypeError, match=r"cygnet\.Table"):
            (
                cygnet.SELECT(db)
                .FROM(AccountTable)
                .LEFT_JOIN(Account, ON=AccountTable.id == LogTable.account_id)
            )
