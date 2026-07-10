"""Rare-token gate: morphology + evidence-blob coverage.

Two live-corpus failures (C11/C23, 1.2.0 release battery) shared this site:
stat-insight items keep their content in `tag`, which the evidence blob
ignored — so no stat item could ever evidence a rare token; and df lookup did
no stemming despite the docstring, so 'journaling' (df 0) vetoed a corpus full
of 'journal'. Fabricated-topic abstention (NH lane) must survive both fixes.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query.retrieval import _item_text_blob, _rare_tokens, _token_variants

pytestmark = pytest.mark.public


@pytest.fixture()
def fts_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE signal_embeddings_fts USING fts5(search_text)")
    # A corpus big enough that the tiny-index guard (df_max*10 rows) doesn't
    # disable df signal entirely.
    rows = [("daily journal entry about the gym",)] * 400 + [
        ("weekly journal review",),
        ("message cadence with contacts",),
    ]
    conn.executemany("INSERT INTO signal_embeddings_fts VALUES (?)", rows)
    return conn


def test_stat_insight_tag_is_evidence():
    """Stat items carry content in `tag` — the blob must include it."""
    item = {"retrieval_source": "stat_insight", "tag": "Most-active hours: 9am-11am weekdays"}
    assert "active" in _item_text_blob(item)


def test_ing_form_stems_to_corpus_df(fts_conn):
    """'journaling' must inherit df from 'journal' — an -ing ask about an
    abundant concept is not a fabricated topic."""
    rare = _rare_tokens(fts_conn, ["journaling"])
    # Not rare at df<=2 severity: the stem is abundant (df 31).
    assert rare.get("journaling") is None or rare["journaling"] > 2


def test_fabricated_topic_still_zero_df(fts_conn):
    """Stemming must not resurrect fabricated topics (NH-lane guarantee)."""
    rare = _rare_tokens(fts_conn, ["zorblatt"])
    assert rare.get("zorblatt") == 0


def test_adjectival_e_matches_noun_form():
    """'active' must evidence items saying 'activity' (loader already prefix-
    matches; the fusion gate's evidence check needs the same morphology)."""
    variants = _token_variants("active")
    assert any(v in "most web activity happens around" for v in variants)


def test_token_variants_cover_common_suffixes():
    variants = _token_variants("journaling")
    assert "journal" in variants
    assert "journaling" in variants
    variants = _token_variants("messages")
    assert "message" in variants or "messages" in variants
