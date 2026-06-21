# conftest.py — Shared fixtures for integration tests.
#
# Provides the raw `conn` fixture (a psycopg connection with DSN-skip
# logic).  Test modules that need the Cygnet adapter import PsycopgDB
# directly from `cygnet.psycopg_db` and wrap the connection themselves.
# (Earlier revisions re-exported PsycopgDB from this module for back-
# compat after the adapter moved into the package; the re-export was
# removed once all integration tests switched to the canonical import.)

from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.types.json import JsonbDumper

DSN = os.environ.get("CYGNET_TEST_DSN", "")


# Register a global dumper so plain Python dicts adapt to JSONB without
# every test having to wrap values in Jsonb(...).  This matches how real
# users typically configure their psycopg connection — once at app
# startup — and keeps cygnet.jsonb tests free of adapter boilerplate.
# Kept in conftest because it's a test-environment concern; library
# code shouldn't touch psycopg adapters.
psycopg.adapters.register_dumper(dict, JsonbDumper)


@pytest.fixture(scope="module")
async def conn():
    """Raw psycopg connection shared across the module."""
    if not DSN:
        pytest.skip("CYGNET_TEST_DSN not set")
    async with await psycopg.AsyncConnection.connect(DSN, autocommit=True) as c:
        yield c
