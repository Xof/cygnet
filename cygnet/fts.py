# fts.py — Curated helpers for PostgreSQL full-text search.
#
# Full-text search has more moving parts than JSONB or arrays: a tsvector
# (the indexed/parsed document), a tsquery (the search expression), and
# the @@ match operator that joins them.  Helpers here cover the typical
# pipeline:
#   to_tsvector(config, body) @@ websearch_to_tsquery(config, user_input)
#
# The default text-search config is "english"; pass a different one as
# the keyword arg if your column or query is in another language.
#
# Index notes: a GIN index on `to_tsvector('english', body)` (a functional
# index) is the typical accelerator for @@.  GIST works too but is
# generally slower for static documents.  For the index to be usable the
# expression in the WHERE clause must match the indexed expression
# exactly — same config string, same column.  Storing a generated
# tsvector column is the most reliable path when search is hot.
#
# Immutability gotcha: to_tsvector(text) (one-arg form) uses the session
# `default_text_search_config` GUC and is therefore STABLE, not
# IMMUTABLE — it cannot back a functional index.  Always pass the
# config explicitly (which is what these helpers do).
#
# Ranking is not pushed into the index: rank() / rank_cd() recompute on
# each row that survives the @@ filter, so they're cheap only when the
# match set is small.  ts_rank itself does not normalize by document
# length unless you pass a normalization flag (not exposed here — use
# cygnet.lit for that).
#
# Usage:
#   import cygnet.fts as fts
#   .WHERE(fts.matches(T.body, fts.web_query("fierce small ORM")))
#   .ORDER_BY(fts.rank(fts.to_tsvector(T.body), fts.web_query("ORM")), DESC=True)

from __future__ import annotations

from typing import Any

from .expression import FunctionCall, fn, op
from .predicate import Predicate


def matches(vector: Any, query: Any) -> Predicate:
    """`vector @@ query` — does the document match the search query?

    Either side can be a column, a function call, or a literal.  Most
    commonly: `matches(to_tsvector(T.body), web_query(user_input))`.
    """
    return op(vector, "@@", query)


def to_tsvector(text: Any, config: str = "english") -> FunctionCall:
    """`to_tsvector(config, text)` — parse text into a searchable tsvector.

    The config (default "english") controls stemming, stop words, and
    locale rules.  Pass a regconfig name like "simple" or "spanish".
    """
    return fn("to_tsvector")(config, text)


def to_tsquery(text: Any, config: str = "english") -> FunctionCall:
    """`to_tsquery(config, text)` — strict tsquery syntax (`a & b | c`).

    This is the low-level constructor — most callers want one of the
    user-input-friendly variants below (plain_query, phrase_query,
    web_query) which accept natural language and don't raise on
    syntactically odd input.
    """
    return fn("to_tsquery")(config, text)


def plain_query(text: Any, config: str = "english") -> FunctionCall:
    """`plainto_tsquery(config, text)` — split text on whitespace, AND together."""
    return fn("plainto_tsquery")(config, text)


def phrase_query(text: Any, config: str = "english") -> FunctionCall:
    """`phraseto_tsquery(config, text)` — like plain_query, but words must be adjacent."""  # noqa: E501
    return fn("phraseto_tsquery")(config, text)


# Choosing among to_tsquery / plain_query / phrase_query / web_query:
#   - to_tsquery: caller controls operators (& | ! <->), syntax errors raise.
#   - plain_query: words AND-ed, no operators honoured.
#   - phrase_query: words must appear in order (uses <-> internally).
#   - web_query: handles bare user input safely; the right default for
#     anything driven by an end-user search box.
def web_query(text: Any, config: str = "english") -> FunctionCall:
    """`websearch_to_tsquery(config, text)` — Google-style query syntax.

    Supports quoted phrases, OR, and `-` for exclusion.  This is the
    most user-input-friendly variant; it never raises on malformed
    input, instead degrading gracefully.  Use for search boxes.
    """
    return fn("websearch_to_tsquery")(config, text)


def rank(vector: Any, query: Any) -> FunctionCall:
    """`ts_rank(vector, query)` — relevance score, higher is better.

    Pair with ORDER_BY to surface the best matches first:
        .ORDER_BY(fts.rank(to_tsvector(T.body), q), DESC=True)
    """
    return fn("ts_rank")(vector, query)


def rank_cd(vector: Any, query: Any) -> FunctionCall:
    """`ts_rank_cd(vector, query)` — cover-density rank.

    Like rank(), but weights phrases that cluster together more
    heavily.  Use when phrase proximity matters; otherwise rank() is
    fine and slightly cheaper.
    """
    return fn("ts_rank_cd")(vector, query)


def headline(document: Any, query: Any, config: str = "english") -> FunctionCall:
    """`ts_headline(config, document, query)` — produce a search-result snippet.

    Useful in the SELECT list to highlight matched terms in returned
    rows.  PG's default options work fine for most cases; pass a
    custom options string via cygnet.lit if you need MaxWords / MinWords
    / StartSel / StopSel control.
    """
    return fn("ts_headline")(config, document, query)
