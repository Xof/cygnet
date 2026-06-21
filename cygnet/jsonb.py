# jsonb.py — Curated helpers for PostgreSQL JSONB operators.
#
# Built on top of cygnet.op(): each helper is a thin alias that names a
# JSONB operator string in Pythonic form.  Anything not curated here
# remains reachable via cygnet.op(col, 'OP', val) directly — these
# wrappers exist for discoverability, readability in WHERE clauses, and
# typo prevention, not as a closed set.
#
# Index hints — worth memorializing because the operator choice
# determines which index can satisfy the query:
#   - @>, ?, ?|, ?&  : accelerated by the default jsonb_ops GIN index.
#   - get / get_text (->, ->>) : not accelerated by GIN unless wrapped in
#     a functional expression index (e.g., on (data->>'email')).
#   - @?, @@ (jsonpath) : accelerated by jsonb_path_ops or jsonb_ops GIN.
# Containment semantics (@>) are structural, not lexical: `{"a":1}` does
# not contain `{"a":1, "b":2}`, and array containment ignores order and
# duplicates.  Surprising for newcomers; cite the PG docs when in doubt.
#
# Usage:
#   import cygnet.jsonb as jb
#   .WHERE(jb.contains(T.data, '{"active": true}'))
#   .WHERE(jb.has_key(T.data, "email"))
#   .WHERE(jb.get_text(T.data, "name") == "Fred")

from __future__ import annotations

from typing import Any

from .expression import op
from .predicate import Predicate


def get(col: Any, key: Any) -> Predicate:
    """`col -> key` — return the JSON value at `key` (still JSON-typed)."""
    return op(col, "->", key)


def get_text(col: Any, key: Any) -> Predicate:
    """`col ->> key` — return the JSON value at `key` cast to text."""
    return op(col, "->>", key)


def get_path(col: Any, path: Any) -> Predicate:
    """`col #> path` — get value at the given path (path is text[]).

    PG expects an array literal: ['a', 'b'].  Pass a Python list and let
    psycopg adapt it, or pass a literal `cygnet.lit("'{a,b}'")`.
    """
    return op(col, "#>", path)


def get_path_text(col: Any, path: Any) -> Predicate:
    """`col #>> path` — get value at the given path cast to text."""
    return op(col, "#>>", path)


def contains(col: Any, val: Any) -> Predicate:
    """`col @> val` — does the LHS jsonb contain the RHS jsonb?"""
    return op(col, "@>", val)


def contained_by(col: Any, val: Any) -> Predicate:
    """`col <@ val` — is the LHS jsonb contained by the RHS?"""
    return op(col, "<@", val)


# `?`, `?|`, `?&` only inspect top-level keys of an object (or membership
# in a string array).  They do not recurse into nested objects — use a
# jsonpath operator (@?/@@) for deep checks.
def has_key(col: Any, key: Any) -> Predicate:
    """`col ? key` — does the JSON object have the named top-level key?"""
    return op(col, "?", key)


def has_any_key(col: Any, keys: Any) -> Predicate:
    """`col ?| keys` — does the object have any of the named keys?

    `keys` should be a text[] array.
    """
    return op(col, "?|", keys)


def has_all_keys(col: Any, keys: Any) -> Predicate:
    """`col ?& keys` — does the object have all of the named keys?"""
    return op(col, "?&", keys)


def concat(a: Any, b: Any) -> Predicate:
    """`a || b` — concatenate two jsonb values (object merge / array concat)."""
    return op(a, "||", b)


def delete_key(col: Any, key: Any) -> Predicate:
    """`col - key` — return the jsonb with `key` removed.

    Note: in plain SQL `-` is also subtraction; PG resolves the overload
    by operand types.  Use this on jsonb columns, not numeric ones.
    """
    return op(col, "-", key)


# @? and @@ take a jsonpath string ('$.foo[*] ? (@ > 3)'), not a text[]
# path like #> / #>>.  Mixing them up is a common source of "syntax error
# at or near" messages.
def path_exists(col: Any, path: Any) -> Predicate:
    """`col @? path` — does the JSON path expression yield any item?"""
    return op(col, "@?", path)


def path_match(col: Any, path: Any) -> Predicate:
    """`col @@ path` — does the JSON path predicate evaluate to true?"""
    return op(col, "@@", path)
