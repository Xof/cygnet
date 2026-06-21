# test_column_defaults.py — Integration tests for PsycopgDB.column_defaults.
#
# column_defaults underpins DEFAULT-aware INSERT codegen (commit a2156bf,
# 2026-05-17): the executor uses it to decide which None-valued columns to
# omit from INSERT so the schema's DEFAULT clause fires.  These tests pin
# the schema-resolution semantics — particularly that the lookup honours
# search_path rather than returning the union of defaults across every
# schema visible to the role.  Regression for B1.

from __future__ import annotations

import pytest

from cygnet.psycopg_db import PsycopgDB

pytestmark = pytest.mark.integration


class TestColumnDefaultsRespectsSearchPath:
    """B1: column_defaults must resolve unqualified table names via the
    connection's search_path, not return defaults from every schema where
    a same-named table happens to exist.
    """

    @pytest.fixture(autouse=True)
    async def setup_schemas(self, conn):
        # Two schemas with same-named tables but different DEFAULT shapes.
        # s1.events: created_at has a DEFAULT.
        # s2.events: archived_at has a DEFAULT (a column NOT in s1).
        # If column_defaults respects search_path=s1, it must return
        # {'created_at'}, never include 'archived_at'.  The asymmetric
        # column names make any cross-schema leak immediately visible.
        await conn.execute("CREATE SCHEMA s1")
        await conn.execute("CREATE SCHEMA s2")
        await conn.execute("""
            CREATE TABLE s1.events (
                id SERIAL PRIMARY KEY,
                payload TEXT,
                created_at TIMESTAMP NOT NULL DEFAULT now()
            )
        """)
        await conn.execute("""
            CREATE TABLE s2.events (
                id SERIAL PRIMARY KEY,
                payload TEXT,
                archived_at TIMESTAMP NOT NULL DEFAULT now()
            )
        """)
        yield
        # search_path changes are session-level and the conn fixture is
        # module-scoped, so a leaked SET would corrupt the next test.
        # Explicit RESET before the schema drops keeps the suite hermetic.
        await conn.execute("RESET search_path")
        await conn.execute("DROP SCHEMA s1 CASCADE")
        await conn.execute("DROP SCHEMA s2 CASCADE")

    async def test_returns_only_searchpath_schemas_defaults(self, conn):
        # search_path includes s1 but not s2.  Unqualified `events`
        # resolves to s1.events; column_defaults must mirror that —
        # so s2.events's archived_at default must NOT appear.  `id`
        # is included because SERIAL creates a nextval() DEFAULT, which
        # is real and what the executor relies on for RETURNING.
        await conn.execute("SET search_path TO s1")
        db = PsycopgDB(conn)
        defaults = await db.column_defaults("events")
        assert defaults == {"id", "created_at"}, (
            f"expected {{'id', 'created_at'}} from s1, got {defaults} "
            f"(B1: column_defaults leaking across schemas)"
        )
        assert "archived_at" not in defaults, (
            "s2's archived_at leaked into s1 result (B1)"
        )

    async def test_does_not_leak_other_schemas_defaults(self, conn):
        # Pointed at s2: s1.events's `created_at` default must not appear.
        await conn.execute("SET search_path TO s2")
        db = PsycopgDB(conn)
        defaults = await db.column_defaults("events")
        assert defaults == {"id", "archived_at"}, (
            f"expected {{'id', 'archived_at'}} from s2, got {defaults} "
            f"(B1: column_defaults leaking across schemas)"
        )
        assert "created_at" not in defaults, (
            "s1's created_at leaked into s2 result (B1)"
        )

    async def test_missing_table_returns_empty_set(self, conn):
        # Resolution-failure semantics: to_regclass returns NULL for an
        # unknown name, which must come out as no defaults rather than
        # raise.  Mirrors the behaviour callers depended on under the
        # information_schema implementation.
        await conn.execute("SET search_path TO s1")
        db = PsycopgDB(conn)
        defaults = await db.column_defaults("definitely_not_a_table_xyz")
        assert defaults == set()
