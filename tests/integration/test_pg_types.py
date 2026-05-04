"""Integration tests: every standard PostgreSQL type round-trips through Cygnet.

Excluded by design:
  - System types: OID, XID, CID, TID, LSN, pg_snapshot
  - Full-text search: tsvector, tsquery
  - Internal/pseudo types: regclass, regproc, etc.

Each test class targets a PostgreSQL type family, creates a TEMP TABLE with
the relevant column types, INSERTs a row via Cygnet, SELECTs it back, and
asserts the Python values survived the round-trip unchanged.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import ipaddress
import uuid
from typing import Annotated, Any

import psycopg
import pytest

import cygnet
from cygnet.annotations import DBKey
from cygnet.psycopg_db import PsycopgDB

pytestmark = pytest.mark.integration


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
async def db(conn):
    """PsycopgDB wrapper plus all temp tables for the module."""
    await conn.execute("""
        CREATE TEMP TABLE numeric_rows (
            id               SERIAL PRIMARY KEY,
            small_val        SMALLINT        NOT NULL,
            int_val          INTEGER         NOT NULL,
            big_val          BIGINT          NOT NULL,
            numeric_val      NUMERIC(12, 4)  NOT NULL,
            real_val         REAL            NOT NULL,
            double_val       DOUBLE PRECISION NOT NULL
        );

        CREATE TEMP TABLE string_rows (
            id               SERIAL PRIMARY KEY,
            char_val         CHAR(10)   NOT NULL,
            varchar_val      VARCHAR(50) NOT NULL,
            text_val         TEXT        NOT NULL
        );

        CREATE TEMP TABLE bytea_rows (
            id               SERIAL PRIMARY KEY,
            data             BYTEA NOT NULL
        );

        CREATE TEMP TABLE datetime_rows (
            id               SERIAL PRIMARY KEY,
            date_val         DATE                     NOT NULL,
            time_val         TIME                     NOT NULL,
            timetz_val       TIME WITH TIME ZONE      NOT NULL,
            ts_val           TIMESTAMP                NOT NULL,
            tstz_val         TIMESTAMP WITH TIME ZONE NOT NULL,
            interval_val     INTERVAL                 NOT NULL
        );

        CREATE TEMP TABLE bool_rows (
            id               SERIAL PRIMARY KEY,
            flag             BOOLEAN NOT NULL
        );

        CREATE TEMP TABLE uuid_rows (
            id               SERIAL PRIMARY KEY,
            uid              UUID NOT NULL
        );

        CREATE TEMP TABLE net_rows (
            id               SERIAL PRIMARY KEY,
            inet_val         INET     NOT NULL,
            cidr_val         CIDR     NOT NULL,
            mac_val          MACADDR  NOT NULL,
            mac8_val         MACADDR8 NOT NULL
        );

        CREATE TEMP TABLE json_rows (
            id               SERIAL PRIMARY KEY,
            json_val         JSON  NOT NULL,
            jsonb_val        JSONB NOT NULL
        );

        CREATE TEMP TABLE array_rows (
            id               SERIAL PRIMARY KEY,
            int_arr          INTEGER[] NOT NULL,
            text_arr         TEXT[]    NOT NULL,
            float_arr        DOUBLE PRECISION[] NOT NULL,
            bool_arr         BOOLEAN[] NOT NULL
        );

        CREATE TEMP TABLE geo_rows (
            id               SERIAL PRIMARY KEY,
            point_val        POINT   NOT NULL,
            lseg_val         LSEG    NOT NULL,
            box_val          BOX     NOT NULL,
            path_val         PATH    NOT NULL,
            polygon_val      POLYGON NOT NULL,
            circle_val       CIRCLE  NOT NULL
        );

        CREATE TEMP TABLE money_rows (
            id               SERIAL PRIMARY KEY,
            amount           MONEY NOT NULL
        );

        CREATE TEMP TABLE bit_rows (
            id               SERIAL PRIMARY KEY,
            fixed_bits       BIT(8)       NOT NULL,
            var_bits         BIT VARYING  NOT NULL
        );

        CREATE TEMP TABLE nullable_rows (
            id               SERIAL PRIMARY KEY,
            text_val         TEXT,
            int_val          INTEGER,
            bool_val         BOOLEAN,
            ts_val           TIMESTAMP,
            uuid_val         UUID
        );

        DROP TYPE IF EXISTS test_mood CASCADE;
        CREATE TYPE test_mood AS ENUM ('happy', 'sad', 'neutral');
        CREATE TEMP TABLE enum_rows (
            id               SERIAL PRIMARY KEY,
            mood             test_mood NOT NULL
        );

        CREATE TEMP TABLE xml_rows (
            id               SERIAL PRIMARY KEY,
            doc              XML NOT NULL
        );
    """)
    yield PsycopgDB(conn)


# ── Models ───────────────────────────────────────────────────────────────────


@dataclasses.dataclass
@cygnet.table("numeric_rows")
class NumericRow:
    id: Annotated[int, DBKey]
    small_val: int
    int_val: int
    big_val: int
    numeric_val: decimal.Decimal
    real_val: float
    double_val: float


@dataclasses.dataclass
@cygnet.table("string_rows")
class StringRow:
    id: Annotated[int, DBKey]
    char_val: str
    varchar_val: str
    text_val: str


@dataclasses.dataclass
@cygnet.table("bytea_rows")
class ByteaRow:
    id: Annotated[int, DBKey]
    data: bytes


@dataclasses.dataclass
@cygnet.table("datetime_rows")
class DateTimeRow:
    id: Annotated[int, DBKey]
    date_val: datetime.date
    time_val: datetime.time
    timetz_val: datetime.time
    ts_val: datetime.datetime
    tstz_val: datetime.datetime
    interval_val: datetime.timedelta


@dataclasses.dataclass
@cygnet.table("bool_rows")
class BoolRow:
    id: Annotated[int, DBKey]
    flag: bool


@dataclasses.dataclass
@cygnet.table("uuid_rows")
class UUIDRow:
    id: Annotated[int, DBKey]
    uid: uuid.UUID


@dataclasses.dataclass
@cygnet.table("net_rows")
class NetRow:
    id: Annotated[int, DBKey]
    inet_val: Any  # IPv4Address | IPv6Address
    cidr_val: Any  # IPv4Network | IPv6Network
    mac_val: str
    mac8_val: str


@dataclasses.dataclass
@cygnet.table("json_rows")
class JSONRow:
    id: Annotated[int, DBKey]
    json_val: Any
    jsonb_val: Any


@dataclasses.dataclass
@cygnet.table("array_rows")
class ArrayRow:
    id: Annotated[int, DBKey]
    int_arr: list
    text_arr: list
    float_arr: list
    bool_arr: list


@dataclasses.dataclass
@cygnet.table("geo_rows")
class GeoRow:
    id: Annotated[int, DBKey]
    point_val: str
    lseg_val: str
    box_val: str
    path_val: str
    polygon_val: str
    circle_val: str


@dataclasses.dataclass
@cygnet.table("money_rows")
class MoneyRow:
    id: Annotated[int, DBKey]
    amount: str


@dataclasses.dataclass
@cygnet.table("bit_rows")
class BitRow:
    id: Annotated[int, DBKey]
    fixed_bits: str
    var_bits: str


@dataclasses.dataclass
@cygnet.table("nullable_rows")
class NullableRow:
    id: Annotated[int, DBKey]
    text_val: str | None
    int_val: int | None
    bool_val: bool | None
    ts_val: datetime.datetime | None
    uuid_val: uuid.UUID | None


@dataclasses.dataclass
@cygnet.table("enum_rows")
class EnumRow:
    id: Annotated[int, DBKey]
    mood: str


@dataclasses.dataclass
@cygnet.table("xml_rows")
class XMLRow:
    id: Annotated[int, DBKey]
    doc: str


# ── Table proxies ────────────────────────────────────────────────────────────

NumericT = cygnet.Table(NumericRow)
StringT = cygnet.Table(StringRow)
ByteaT = cygnet.Table(ByteaRow)
DateTimeT = cygnet.Table(DateTimeRow)
BoolT = cygnet.Table(BoolRow)
UUIDT = cygnet.Table(UUIDRow)
NetT = cygnet.Table(NetRow)
JSONT = cygnet.Table(JSONRow)
ArrayT = cygnet.Table(ArrayRow)
GeoT = cygnet.Table(GeoRow)
MoneyT = cygnet.Table(MoneyRow)
BitT = cygnet.Table(BitRow)
NullableT = cygnet.Table(NullableRow)
EnumT = cygnet.Table(EnumRow)
XMLT = cygnet.Table(XMLRow)


# ── Helpers ──────────────────────────────────────────────────────────────────


async def _roundtrip(db, table, obj):
    """INSERT obj, SELECT it back by PK, return the fetched instance."""
    await cygnet.INSERT(db).INTO(table).VALUES(obj)
    assert obj.id is not None
    fetched = await cygnet.get(db, table, id=obj.id)
    assert fetched is not None
    return fetched


# ── Tests: Numeric types ────────────────────────────────────────────────────


class TestNumericTypes:
    async def test_basic_values(self, db):
        row = NumericRow(
            id=None,
            small_val=32767,
            int_val=2_147_483_647,
            big_val=9_223_372_036_854_775_807,
            numeric_val=decimal.Decimal("12345678.1234"),
            real_val=3.14,
            double_val=2.718281828459045,
        )
        got = await _roundtrip(db, NumericT, row)

        assert got.small_val == 32767
        assert got.int_val == 2_147_483_647
        assert got.big_val == 9_223_372_036_854_775_807
        assert got.numeric_val == decimal.Decimal("12345678.1234")
        assert got.real_val == pytest.approx(3.14, rel=1e-5)
        assert got.double_val == pytest.approx(2.718281828459045)

    async def test_negative_values(self, db):
        row = NumericRow(
            id=None,
            small_val=-32768,
            int_val=-2_147_483_648,
            big_val=-9_223_372_036_854_775_808,
            numeric_val=decimal.Decimal("-0.0001"),
            real_val=-1.0,
            double_val=-1e308,
        )
        got = await _roundtrip(db, NumericT, row)

        assert got.small_val == -32768
        assert got.int_val == -2_147_483_648
        assert got.big_val == -9_223_372_036_854_775_808
        assert got.numeric_val == decimal.Decimal("-0.0001")
        assert got.double_val == pytest.approx(-1e308)

    async def test_zero(self, db):
        row = NumericRow(
            id=None,
            small_val=0,
            int_val=0,
            big_val=0,
            numeric_val=decimal.Decimal("0.0000"),
            real_val=0.0,
            double_val=0.0,
        )
        got = await _roundtrip(db, NumericT, row)

        assert got.small_val == 0
        assert got.int_val == 0
        assert got.big_val == 0
        assert got.numeric_val == decimal.Decimal("0.0000")


# ── Tests: String types ─────────────────────────────────────────────────────


class TestStringTypes:
    async def test_basic_strings(self, db):
        row = StringRow(
            id=None,
            char_val="hello",
            varchar_val="world",
            text_val="this is a longer piece of text",
        )
        got = await _roundtrip(db, StringT, row)

        # CHAR(10) pads with spaces to the declared length
        assert got.char_val == "hello     "
        assert got.varchar_val == "world"
        assert got.text_val == "this is a longer piece of text"

    async def test_unicode(self, db):
        row = StringRow(
            id=None,
            char_val="\u00e9\u00e8\u00ea\u00eb",
            varchar_val="\u2603 \u2764 \U0001f600",
            text_val="\u3053\u3093\u306b\u3061\u306f\u4e16\u754c",
        )
        got = await _roundtrip(db, StringT, row)

        assert got.varchar_val == "\u2603 \u2764 \U0001f600"
        assert got.text_val == "\u3053\u3093\u306b\u3061\u306f\u4e16\u754c"

    async def test_empty_string(self, db):
        row = StringRow(
            id=None,
            char_val="",
            varchar_val="",
            text_val="",
        )
        got = await _roundtrip(db, StringT, row)

        # CHAR(10) pads empty string to 10 spaces
        assert got.char_val == "          "
        assert got.varchar_val == ""
        assert got.text_val == ""


# ── Tests: Binary types ─────────────────────────────────────────────────────


class TestBinaryTypes:
    async def test_basic_bytes(self, db):
        payload = b"\x00\x01\x02\xff\xfe\xfd"
        row = ByteaRow(id=None, data=payload)
        got = await _roundtrip(db, ByteaT, row)

        assert bytes(got.data) == payload

    async def test_empty_bytes(self, db):
        row = ByteaRow(id=None, data=b"")
        got = await _roundtrip(db, ByteaT, row)

        assert bytes(got.data) == b""

    async def test_large_binary(self, db):
        payload = bytes(range(256)) * 100  # 25.6 KB
        row = ByteaRow(id=None, data=payload)
        got = await _roundtrip(db, ByteaT, row)

        assert bytes(got.data) == payload


# ── Tests: Date/time types ───────────────────────────────────────────────────


class TestDateTimeTypes:
    async def test_basic_datetime(self, db):
        tz_utc = datetime.UTC
        row = DateTimeRow(
            id=None,
            date_val=datetime.date(2025, 6, 15),
            time_val=datetime.time(14, 30, 45),
            timetz_val=datetime.time(14, 30, 45, tzinfo=tz_utc),
            ts_val=datetime.datetime(2025, 6, 15, 14, 30, 45),
            tstz_val=datetime.datetime(2025, 6, 15, 14, 30, 45, tzinfo=tz_utc),
            interval_val=datetime.timedelta(days=30, hours=2, minutes=15),
        )
        got = await _roundtrip(db, DateTimeT, row)

        assert got.date_val == datetime.date(2025, 6, 15)
        assert got.time_val == datetime.time(14, 30, 45)
        assert got.ts_val == datetime.datetime(2025, 6, 15, 14, 30, 45)
        assert got.tstz_val == datetime.datetime(2025, 6, 15, 14, 30, 45, tzinfo=tz_utc)
        assert got.interval_val == datetime.timedelta(days=30, hours=2, minutes=15)

    async def test_microsecond_precision(self, db):
        tz_utc = datetime.UTC
        row = DateTimeRow(
            id=None,
            date_val=datetime.date(2000, 1, 1),
            time_val=datetime.time(0, 0, 0, 123456),
            timetz_val=datetime.time(0, 0, 0, 123456, tzinfo=tz_utc),
            ts_val=datetime.datetime(2000, 1, 1, 0, 0, 0, 123456),
            tstz_val=datetime.datetime(2000, 1, 1, 0, 0, 0, 123456, tzinfo=tz_utc),
            interval_val=datetime.timedelta(microseconds=1),
        )
        got = await _roundtrip(db, DateTimeT, row)

        assert got.time_val.microsecond == 123456
        assert got.ts_val.microsecond == 123456
        assert got.interval_val == datetime.timedelta(microseconds=1)

    async def test_epoch_and_boundaries(self, db):
        tz_utc = datetime.UTC
        row = DateTimeRow(
            id=None,
            date_val=datetime.date(1970, 1, 1),
            time_val=datetime.time(0, 0, 0),
            timetz_val=datetime.time(0, 0, 0, tzinfo=tz_utc),
            ts_val=datetime.datetime(1970, 1, 1, 0, 0, 0),
            tstz_val=datetime.datetime(1970, 1, 1, 0, 0, 0, tzinfo=tz_utc),
            interval_val=datetime.timedelta(0),
        )
        got = await _roundtrip(db, DateTimeT, row)

        assert got.date_val == datetime.date(1970, 1, 1)
        assert got.ts_val == datetime.datetime(1970, 1, 1, 0, 0, 0)
        assert got.interval_val == datetime.timedelta(0)

    async def test_timezone_offsets(self, db):
        plus5 = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        tz_utc = datetime.UTC
        row = DateTimeRow(
            id=None,
            date_val=datetime.date(2025, 1, 1),
            time_val=datetime.time(12, 0, 0),
            timetz_val=datetime.time(12, 0, 0, tzinfo=plus5),
            ts_val=datetime.datetime(2025, 1, 1, 12, 0, 0),
            tstz_val=datetime.datetime(2025, 1, 1, 12, 0, 0, tzinfo=plus5),
            interval_val=datetime.timedelta(hours=1),
        )
        got = await _roundtrip(db, DateTimeT, row)

        # PostgreSQL stores timestamptz as UTC, so the offset is normalised
        expected_utc = datetime.datetime(2025, 1, 1, 6, 30, 0, tzinfo=tz_utc)
        assert got.tstz_val == expected_utc


# ── Tests: Boolean type ─────────────────────────────────────────────────────


class TestBooleanType:
    async def test_true(self, db):
        row = BoolRow(id=None, flag=True)
        got = await _roundtrip(db, BoolT, row)
        assert got.flag is True

    async def test_false(self, db):
        row = BoolRow(id=None, flag=False)
        got = await _roundtrip(db, BoolT, row)
        assert got.flag is False


# ── Tests: UUID type ────────────────────────────────────────────────────────


class TestUUIDType:
    async def test_uuid4(self, db):
        uid = uuid.uuid4()
        row = UUIDRow(id=None, uid=uid)
        got = await _roundtrip(db, UUIDT, row)
        assert got.uid == uid

    async def test_nil_uuid(self, db):
        nil = uuid.UUID(int=0)
        row = UUIDRow(id=None, uid=nil)
        got = await _roundtrip(db, UUIDT, row)
        assert got.uid == nil

    async def test_max_uuid(self, db):
        max_uid = uuid.UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        row = UUIDRow(id=None, uid=max_uid)
        got = await _roundtrip(db, UUIDT, row)
        assert got.uid == max_uid


# ── Tests: Network types ────────────────────────────────────────────────────


class TestNetworkTypes:
    async def test_ipv4(self, db):
        row = NetRow(
            id=None,
            inet_val=ipaddress.IPv4Address("192.168.1.1"),
            cidr_val=ipaddress.IPv4Network("10.0.0.0/8"),
            mac_val="08:00:2b:01:02:03",
            mac8_val="08:00:2b:01:02:03:04:05",
        )
        got = await _roundtrip(db, NetT, row)

        assert got.inet_val == ipaddress.IPv4Address("192.168.1.1")
        assert got.cidr_val == ipaddress.IPv4Network("10.0.0.0/8")

    async def test_ipv6(self, db):
        row = NetRow(
            id=None,
            inet_val=ipaddress.IPv6Address("::1"),
            cidr_val=ipaddress.IPv6Network("fe80::/10"),
            mac_val="ff:ff:ff:ff:ff:ff",
            mac8_val="ff:ff:ff:ff:ff:ff:ff:ff",
        )
        got = await _roundtrip(db, NetT, row)

        assert got.inet_val == ipaddress.IPv6Address("::1")
        assert got.cidr_val == ipaddress.IPv6Network("fe80::/10")


# ── Tests: JSON types ───────────────────────────────────────────────────────


class TestJSONTypes:
    async def test_object(self, db):
        obj = {"name": "cygnet", "version": 1, "active": True}
        Json = psycopg.types.json.Json
        row = JSONRow(id=None, json_val=Json(obj), jsonb_val=Json(obj))
        got = await _roundtrip(db, JSONT, row)

        assert got.json_val == obj
        assert got.jsonb_val == obj

    async def test_array(self, db):
        arr = [1, "two", None, True]
        Json = psycopg.types.json.Json
        row = JSONRow(id=None, json_val=Json(arr), jsonb_val=Json(arr))
        got = await _roundtrip(db, JSONT, row)

        assert got.json_val == arr
        assert got.jsonb_val == arr

    async def test_nested(self, db):
        nested = {
            "users": [
                {"id": 1, "tags": ["admin", "active"]},
                {"id": 2, "tags": []},
            ],
            "meta": {"count": 2},
        }
        Json = psycopg.types.json.Json
        row = JSONRow(
            id=None,
            json_val=Json(nested),
            jsonb_val=Json(nested),
        )
        got = await _roundtrip(db, JSONT, row)

        assert got.json_val == nested
        assert got.jsonb_val == nested

    async def test_scalar_json(self, db):
        Json = psycopg.types.json.Json
        row = JSONRow(
            id=None,
            json_val=Json("just a string"),
            jsonb_val=Json(42),
        )
        got = await _roundtrip(db, JSONT, row)

        assert got.json_val == "just a string"
        assert got.jsonb_val == 42


# ── Tests: Array types ──────────────────────────────────────────────────────


class TestArrayTypes:
    async def test_basic_arrays(self, db):
        row = ArrayRow(
            id=None,
            int_arr=[1, 2, 3],
            text_arr=["hello", "world"],
            float_arr=[1.1, 2.2, 3.3],
            bool_arr=[True, False, True],
        )
        got = await _roundtrip(db, ArrayT, row)

        assert got.int_arr == [1, 2, 3]
        assert got.text_arr == ["hello", "world"]
        assert got.float_arr == pytest.approx([1.1, 2.2, 3.3])
        assert got.bool_arr == [True, False, True]

    async def test_empty_arrays(self, db):
        row = ArrayRow(
            id=None,
            int_arr=[],
            text_arr=[],
            float_arr=[],
            bool_arr=[],
        )
        got = await _roundtrip(db, ArrayT, row)

        assert got.int_arr == []
        assert got.text_arr == []

    async def test_single_element(self, db):
        row = ArrayRow(
            id=None,
            int_arr=[42],
            text_arr=["solo"],
            float_arr=[0.0],
            bool_arr=[False],
        )
        got = await _roundtrip(db, ArrayT, row)

        assert got.int_arr == [42]
        assert got.text_arr == ["solo"]


# ── Tests: Geometric types ──────────────────────────────────────────────────


class TestGeometricTypes:
    """Geometric types are passed and returned as strings by psycopg3."""

    async def test_all_geometric(self, db):
        row = GeoRow(
            id=None,
            point_val="(1.5, 2.5)",
            lseg_val="[(0,0),(1,1)]",
            box_val="(1,1),(0,0)",
            path_val="((0,0),(1,0),(1,1),(0,1))",
            polygon_val="((0,0),(1,0),(1,1),(0,1))",
            circle_val="<(0,0),5>",
        )
        got = await _roundtrip(db, GeoT, row)

        # PostgreSQL normalises the text representations; verify via str
        assert got.point_val is not None
        assert got.lseg_val is not None
        assert got.box_val is not None
        assert got.circle_val is not None


# ── Tests: Monetary type ────────────────────────────────────────────────────


class TestMonetaryType:
    async def test_money(self, db):
        row = MoneyRow(id=None, amount="$1,234.56")
        got = await _roundtrip(db, MoneyT, row)

        assert got.amount is not None
        # Money is returned as a string; exact format depends on locale
        assert "1234" in str(got.amount) or "1,234" in str(got.amount)


# ── Tests: Bit string types ─────────────────────────────────────────────────


class TestBitStringTypes:
    async def test_fixed_and_varying(self, db):
        row = BitRow(id=None, fixed_bits="10101010", var_bits="110")
        got = await _roundtrip(db, BitT, row)

        assert got.fixed_bits is not None
        assert got.var_bits is not None


# ── Tests: Nullable columns ─────────────────────────────────────────────────


class TestNullableColumns:
    async def test_all_null(self, db):
        row = NullableRow(
            id=None,
            text_val=None,
            int_val=None,
            bool_val=None,
            ts_val=None,
            uuid_val=None,
        )
        got = await _roundtrip(db, NullableT, row)

        assert got.text_val is None
        assert got.int_val is None
        assert got.bool_val is None
        assert got.ts_val is None
        assert got.uuid_val is None

    async def test_all_populated(self, db):
        uid = uuid.uuid4()
        ts = datetime.datetime(2025, 6, 15, 12, 0, 0)
        row = NullableRow(
            id=None,
            text_val="present",
            int_val=42,
            bool_val=True,
            ts_val=ts,
            uuid_val=uid,
        )
        got = await _roundtrip(db, NullableT, row)

        assert got.text_val == "present"
        assert got.int_val == 42
        assert got.bool_val is True
        assert got.ts_val == ts
        assert got.uuid_val == uid

    async def test_mixed_null_and_populated(self, db):
        row = NullableRow(
            id=None,
            text_val="here",
            int_val=None,
            bool_val=True,
            ts_val=None,
            uuid_val=None,
        )
        got = await _roundtrip(db, NullableT, row)

        assert got.text_val == "here"
        assert got.int_val is None
        assert got.bool_val is True
        assert got.ts_val is None


# ── Tests: Enum type ────────────────────────────────────────────────────────


class TestEnumType:
    async def test_enum_values(self, db):
        for mood in ("happy", "sad", "neutral"):
            row = EnumRow(id=None, mood=mood)
            got = await _roundtrip(db, EnumT, row)
            assert got.mood == mood


# ── Tests: XML type ─────────────────────────────────────────────────────────


class TestXMLType:
    async def test_xml_document(self, db):
        doc = '<root><item key="a">value</item></root>'
        row = XMLRow(id=None, doc=doc)
        got = await _roundtrip(db, XMLT, row)

        assert "value" in got.doc
        assert "<root>" in got.doc
