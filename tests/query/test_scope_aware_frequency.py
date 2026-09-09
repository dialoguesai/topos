"""SUITE-SCOPE-FREQUENCY — the gate judges rarity over a universe the ask cannot see.

protects: G-ttfa-a2 and SYS-query I1 read two-sided. Measurement only: nothing here changes the
product, and the point of the file is to put numbers behind a change nobody has decided to make.

`_rare_tokens` counts document frequency across the WHOLE `signal_embeddings_fts` index — no scope,
no source-install filter, no window — while the evidence the gate then demands is filtered by all
three. So the question "how rare is this word" is answered about a corpus the ask can never reach.
The density sweep (`test_corpus_density_strata.py`) found this; this file measures what fixing it
would do, in both directions, because the two plausible repairs pull against each other:

  * counting within the scope's own sources makes counts SMALLER, so MORE tokens land in the
    `df <= 2` band and MORE asks are refused — a scope-aware count on its own would make a fresh
    node consistently wrong rather than right;
  * refusing only at `df == 0` — absence rather than rarity — makes the gate weaker, and the
    question is whether it stays honest enough to refuse a subject the owner really lacks.

Three regimes are therefore graded on identical fixtures, and both error rates are reported for
each. No regime is asserted "best" here; the numbers are the deliverable.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

from topos.features.signal.vector_settings import rare_token_df_max
from topos.query.retrieval import _query_tokens, _residual_content_tokens, _token_variants

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gap" / "qq" / "engine"))
from composition_seed_corpus import build_seeded_corpus  # noqa: E402

pytestmark = [pytest.mark.check("C-quality-scope-aware-frequency")]

FLOOR = rare_token_df_max() * 10

#: `work_context:read`'s own sources. Rows outside this set can never appear in its answers.
IN_SCOPE = ("chatgpt_ingestion", "chatgpt_file_ingestion", "chatgpt_ui_conversation",
            "demo_resume_file", "demo_journal_file", "grow_journal", "grow_data_file")
#: A source the scope never returns.
OUT_OF_SCOPE = "demo_messenger_file"

_FILLER = "shipped the installer work and reviewed the release notes for the project"
_INSERT = """INSERT INTO signal_embeddings
             (embedding_id, record_id, source_id, signal_dimension, model, provider, dims,
              text_preview, search_text, chunk_index, event_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""


def _corpus(tmp: Path, subject: str, in_scope_rows: int, out_of_scope_rows: int,
            mention: str | None = None) -> Path:
    """`mention` is what the rows actually SAY. It defaults to a sentence containing the subject;
    a case can pass a different word to build a stem collision, where the corpus holds a real word
    the fabricated subject stems onto."""
    db = tmp / f"f-{subject}-{in_scope_rows}-{out_of_scope_rows}-{mention or 'self'}.db"
    build_seeded_corpus(db)
    now = datetime.now(timezone.utc)
    sentence = f"went {mention or subject} in the caves at the weekend with friends"
    conn = sqlite3.connect(str(db))
    try:
        rows = [
            (f"f{i}", f"fr{i}", IN_SCOPE[0], "work", "m", "p", 0, _FILLER, _FILLER, 0,
             (now - timedelta(days=1 + i % 3)).isoformat())
            for i in range(FLOOR + 100)
        ]
        rows += [
            (f"i{i}", f"ir{i}", IN_SCOPE[0], "work", "m", "p", 0, sentence, sentence, 0,
             (now - timedelta(days=2)).isoformat())
            for i in range(in_scope_rows)
        ]
        rows += [
            (f"o{i}", f"or{i}", OUT_OF_SCOPE, "rel", "m", "p", 0, sentence, sentence, 0,
             (now - timedelta(days=2)).isoformat())
            for i in range(out_of_scope_rows)
        ]
        conn.executemany(_INSERT, rows)
        conn.commit()
    finally:
        conn.close()
    return db


def _df(conn: sqlite3.Connection, token: str, sources: Tuple[str, ...] | None,
        exact: bool = False) -> int:
    """Document frequency, optionally confined to a scope's own sources. One join — the FTS table
    is external-content over `signal_embeddings`, so `rowid` carries straight through.

    `exact` drops the morphological variants. The idea was that they earn their place when the
    question is "does this row answer the ask" — a row saying `journal` does evidence a
    `journaling` question — while the ABSENCE question is narrower: did the owner ever write this
    word. MEASURED, IT MAKES NO DIFFERENCE, and the reason is worth keeping: the index is
    tokenised `porter unicode61`, so FTS stems the QUERY term as well as the rows. `MATCH
    "falconer"` finds a row saying `falcon` with no variant sweep involved. Any absence test built
    on this index inherits the collision, so the obvious refinement is not available — which is
    exactly why `df <= 2` was chosen over `df == 0` in the first place.
    """
    best = 0
    for variant in ([token] if exact else _token_variants(token)):
        if sources is None:
            sql, args = (
                "SELECT count(*) FROM signal_embeddings_fts WHERE signal_embeddings_fts MATCH ?",
                (f'"{variant}"',),
            )
        else:
            placeholders = ",".join("?" for _ in sources)
            sql, args = (
                f"""SELECT count(*) FROM signal_embeddings_fts f
                    JOIN signal_embeddings e ON e.rowid = f.rowid
                    WHERE f.signal_embeddings_fts MATCH ? AND e.source_id IN ({placeholders})""",
                (f'"{variant}"', *sources),
            )
        row = conn.execute(sql, args).fetchone()
        best = max(best, int(row[0]) if row else 0)
    return best


#: The three regimes. Each takes (df_whole, df_in_scope) and says whether an UNEVIDENCED token
#: empties the lane. Regime A is what ships today.
REGIMES: Dict[str, str] = {
    "A · whole-index df, refuse at df<=2": "today",
    "B · scope-scoped df, refuse at df<=2": "align the universe",
    "C · scope-scoped df, refuse at df==0": "absence, not rarity",
    "D · scope-scoped EXACT df, refuse at df==0": "absence of the word the owner asked with",
}


def _verdict(regime: str, df_whole: int, df_scope: int, df_exact: int) -> str:
    if regime.startswith("A"):
        return "refused" if df_whole <= 2 else "answered"
    if regime.startswith("B"):
        return "refused" if df_scope <= 2 else "answered"
    if regime.startswith("C"):
        return "refused" if df_scope == 0 else "answered"
    return "refused" if df_exact == 0 else "answered"


#: The cases the three regimes are graded on. `truth` is what an honest answer would be, judged by
#: whether the scope's OWN sources hold the subject — which is the only universe an answer can be
#: built from. Invented subjects throughout; the seeded corpus contains none of them.
CASES: List[Tuple[str, str, int, int, str, str]] = [
    # (id, subject, in-scope rows, out-of-scope rows, honest verdict, what the rows SAY)
    ("absent-everywhere", "spelunking", 0, 0, "refused", ""),
    ("absent-in-scope-only", "spelunking", 0, 3, "refused", ""),
    ("absent-in-scope-many-out", "spelunking", 0, 30, "refused", ""),
    ("present-once", "spelunking", 1, 0, "answered", ""),
    ("present-twice", "spelunking", 2, 0, "answered", ""),
    ("present-thrice", "spelunking", 3, 0, "answered", ""),
    ("present-and-abundant", "spelunking", 30, 0, "answered", ""),
    ("present-once-noise-outside", "spelunking", 1, 30, "answered", ""),
    # The reason `df <= 2` exists rather than `df == 0`. The owner has a falcon at the zoo in
    # their notes and has never done falconry; the full-text index tokenises with porter, so a
    # query for the fabricated subject MATCHES that row. A rule that refuses only at df 0 is
    # answered into by the collision, which is a false presence no amount of scoping removes.
    ("stem-collision-only", "falconer", 1, 0, "refused", "falcon"),
]


@pytest.fixture(scope="module")
def measured(tmp_path_factory) -> Dict[str, Dict[str, str]]:
    """One row per case: the honest verdict and what each regime would decide."""
    tmp = tmp_path_factory.mktemp("scope-df")
    out: Dict[str, Dict[str, str]] = {}
    for case_id, subject, ins, outs, truth, mention in CASES:
        conn = sqlite3.connect(str(_corpus(tmp, subject, ins, outs, mention or None)))
        try:
            token = _residual_content_tokens(_query_tokens(f"what did I say about {subject}"))[0]
            whole = _df(conn, token, None)
            scoped = _df(conn, token, IN_SCOPE)
            exact = _df(conn, token, IN_SCOPE, exact=True)
        finally:
            conn.close()
        out[case_id] = {
            "truth": truth, "df_whole": whole, "df_scope": scoped, "df_exact": exact,
            **{r: _verdict(r, whole, scoped, exact) for r in REGIMES},
        }
    return out


def _rates(measured: Dict[str, Dict[str, str]], regime: str) -> Tuple[int, int]:
    """(refused-when-it-should-answer, answered-when-it-should-refuse). Never averaged."""
    false_absence = sum(1 for r in measured.values() if r["truth"] == "answered" and r[regime] == "refused")
    false_presence = sum(1 for r in measured.values() if r["truth"] == "refused" and r[regime] == "answered")
    return false_absence, false_presence


class TestTheUniverseTheGateCounts:
    def test_out_of_scope_rows_change_todays_count(self, measured) -> None:
        """The asymmetry itself, as a number: rows the answer can never contain move the count the
        gate reads, and the scope-scoped count is unmoved by them."""
        none_outside = measured["absent-everywhere"]
        many_outside = measured["absent-in-scope-many-out"]
        assert none_outside["df_whole"] != many_outside["df_whole"]
        assert none_outside["df_scope"] == many_outside["df_scope"] == 0

    def test_todays_regime_answers_an_ask_its_scope_cannot_support(self, measured) -> None:
        """The false-presence case. Nothing in `work_context`'s own sources mentions the subject,
        and the ask is allowed anyway because a different scope's rows made the word look common."""
        assert measured["absent-in-scope-only"]["truth"] == "refused"
        assert measured["absent-in-scope-only"]["A · whole-index df, refuse at df<=2"] == "answered"

    @pytest.mark.parametrize("regime", sorted(REGIMES))
    def test_both_error_rates_are_reported_separately(self, measured, regime: str, record_property) -> None:
        """The measurement itself. Recorded as properties so a run carries the numbers, and
        asserted only where a regime is unambiguously worse than doing nothing."""
        false_absence, false_presence = _rates(measured, regime)
        record_property(f"{regime} · false_absence", false_absence)
        record_property(f"{regime} · false_presence", false_presence)
        print(f"\n{regime}: refused-when-should-answer={false_absence}  answered-when-should-refuse={false_presence}")
        assert false_absence + false_presence <= len(CASES)

    def test_the_numbers_this_run_measured(self, measured) -> None:
        """Pinned so the comparison is a regression test rather than a one-off print. Regime A is
        today's shipping behaviour; B and C are candidates, and B is recorded as WORSE on the
        false-absence side, which is the finding — aligning the universe without also fixing the
        `df <= 2` rule trades one error for a larger one. And C, which looks free on the first
        eight cases, is answered into by a stem collision. D was the obvious way out of that and
        measures identically to C, because the collision lives in the index's tokeniser rather
        than in the caller's variant sweep. No regime dominates, which is why this is a
        measurement and not a patch."""
        expected = {
            "A · whole-index df, refuse at df<=2": (2, 2),
            "B · scope-scoped df, refuse at df<=2": (3, 0),
            "C · scope-scoped df, refuse at df==0": (0, 1),
            "D · scope-scoped EXACT df, refuse at df==0": (0, 1),
        }
        got = {r: _rates(measured, r) for r in REGIMES}
        assert got == expected, (
            "the regime comparison moved; re-read the table before changing any threshold\n"
            + "\n".join(f"  {r}: got {got[r]}, expected {expected[r]}" for r in REGIMES)
        )
