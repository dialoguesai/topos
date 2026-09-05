"""A word that names the SHAPE OF THE ANSWER must never veto a lane.

The rare-token gate is absence honesty: a query token the corpus does not contain means
"you asked about something your data does not mention", and the honest answer is
nothing. That is right about SUBJECTS and wrong about FORMS. "Write it as an iambic
pentameter poem" contains three words no stored row will ever contain, and none of them
is what the owner is asking about.

Live 2026-09-05, home chat, `work_context:read`, owner app client:

    "Take the work I have been doing lately, and put it into a 3 stanza iambic
     pentameter poem for me"
    → rare_gate emptied, reason rare_token_unevidenced, dropped 63
    → "I couldn't find any recent work activity in your synced data"

while "what have I been working on lately" answered the same scope, node and window with
25 items. The subject reached the data; the instruction threw it away — and it could,
because `_residual_content_tokens` had already stripped the subject's own words as
framing (`lately` recency, `work` goals-surface), leaving the poem's metre as the only
"content" in the query.

The exemption is deliberately narrow in TWO ways:

  * It lives in `_rare_tokens`, whose every consumer is an abstention gate
    (`_rrf_fuse_summary_lists`' veto and `_route_canonical_rows`' browse block). Shape
    words are NOT removed from `_residual_content_tokens`, so "find the poem I wrote
    about my dad" still filters rows on `poem`. Removing a word from a veto set can
    only ever let a lane answer; it cannot manufacture a match.
  * It is a closed vocabulary. A fabricated SUBJECT is not in it and still vetoes —
    including in a sentence that carries a transform, which is what NEG-2/NEG-4 pin.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from topos.query.retrieval import (
    _OUTPUT_SHAPE_TOKENS,
    _query_tokens,
    _rare_tokens,
    _residual_content_tokens,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "gap" / "qq" / "engine"))
from transform_eval_cases import (  # noqa: E402
    ABSTAIN_CASES,
    ANSWERABLE_CASES,
    TRANSFORM_CASES,
)

#: `_rare_tokens` needs >= df_max*10 indexed rows before it trusts the frequency signal.
_CORPUS_ROWS = 400

#: The vocabulary a real corpus DOES contain: an ordinary working life, plus the ordinary
#: nouns the catalog's persona cases reach for. Everything here is a possible SUBJECT.
#:
#: What is deliberately absent is every word that names an OUTPUT — no "poem", no
#: "summarize", no "three". That absence is the whole instrument: a case that vetoes
#: against this corpus vetoes on a word describing the answer, which is the bug. It also
#: removes the corpus dependence that makes this class intermittent in production —
#: whether `sonnet` happens to appear in one owner's messages is not a property of the
#: code, and a regression test that turns on it would pass by luck.
_SUBJECT_SENTENCES = [
    "shipped the installer work and reviewed the release notes for the project",
    "meeting with the team about the roadmap and this month's priorities",
    "journal entry about the week, entries on what I worked on and finished",
    "tracked progress on goals, updated the status and sent the executive note",
    "standup: current projects, blockers, impact, next steps and the timeline",
    "talked with the team, said thank you and shared it on linkedin",
    "listened to a song by the sea, read a detective novel and the newspaper",
    "opened a fortune cookie at dinner and kept the slip",
]


@pytest.fixture(scope="module")
def corpus() -> sqlite3.Connection:
    """A scratch FTS index holding the SUBJECT vocabulary and none of the form words.

    Self-contained on purpose. The live-node numbers in `transform_eval_cases` are the
    evidence that this class fires in production; this fixture is what makes the
    contract testable on any machine, including a CI box with no node database — and it
    removes the corpus dependence that makes the bug intermittent in the first place.
    """
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE VIRTUAL TABLE signal_embeddings_fts USING fts5(content)")
    rows = [(_SUBJECT_SENTENCES[i % len(_SUBJECT_SENTENCES)],) for i in range(_CORPUS_ROWS)]
    conn.executemany("INSERT INTO signal_embeddings_fts(content) VALUES (?)", rows)
    conn.commit()
    return conn


def needles(query: str) -> list:
    return _residual_content_tokens(_query_tokens(query))


def vetoers(conn, query: str) -> dict:
    """Tokens that would veto: rare (df below the max) AND effectively absent (df<=2).

    Mirrors `_veto_for` in `_rrf_fuse_summary_lists`, minus the evidence check — nothing
    the corpus lacks can be evidenced by what the corpus returned.
    """
    rare = _rare_tokens(conn, needles(query))
    return {t: df for t, df in rare.items() if df <= 2}


class TestTheReportedFailure:
    Q = (
        "Take the work I have been doing lately, and put it into a 3 stanza iambic "
        "pentameter poem for me"
    )

    def test_the_live_query_no_longer_vetoes(self, corpus) -> None:
        assert vetoers(corpus, self.Q) == {}

    def test_the_metre_is_absent_from_the_corpus_but_harmless(self, corpus) -> None:
        """The words really are df 0 — the fix is the exemption, not a corpus accident."""
        raw = _rare_tokens(corpus, ["iambic", "pentameter", "chromodynamics"])
        assert raw.get("chromodynamics") == 0, "control: a fabricated subject is still rare"
        assert "iambic" not in raw and "pentameter" not in raw

    def test_the_plain_ask_behind_it_is_unaffected(self, corpus) -> None:
        assert vetoers(corpus, "what have I been working on lately") == {}


class TestTheExemptionIsGateOnly:
    """Shape words stay available to content matching; they only lose the power to veto.

    `_residual_content_tokens` feeds the canonical `contains=` filter as well as the
    gate. Stripping there would break "find the poem I wrote" — a real ask, about a
    stored row, that happens to use a form word as its subject.
    """

    def test_shape_words_survive_as_content_needles(self) -> None:
        assert "poem" in needles("find the poem I wrote about my dad")
        assert "memo" in needles("show me the memo about the migration")

    def test_but_they_are_dropped_before_the_df_pass(self, corpus) -> None:
        assert _rare_tokens(corpus, ["poem", "memo", "iambic"]) == {}


class TestAbsenceHonestyIsIntact:
    @pytest.mark.parametrize("case", ABSTAIN_CASES, ids=lambda c: c.case_id)
    def test_a_fabricated_subject_still_vetoes(self, corpus, case) -> None:
        got = vetoers(corpus, case.query)
        assert case.absent_subject in got, (
            f"{case.case_id}: absence honesty lost — {case.query!r} vetoed on {sorted(got)}"
        )

    @pytest.mark.parametrize("case", [c for c in ABSTAIN_CASES if c.shape_words],
                             ids=lambda c: c.case_id)
    def test_the_form_word_is_not_why_it_vetoed(self, corpus, case) -> None:
        """The hard half: one sentence, a form word that must stop vetoing and a
        fabricated subject that must keep vetoing."""
        got = vetoers(corpus, case.query)
        for word in case.shape_words:
            assert word not in got, f"{case.case_id}: {word!r} is form, not subject"


class TestTheWholeCatalog:
    @pytest.mark.parametrize("case", ANSWERABLE_CASES, ids=lambda c: c.case_id)
    def test_no_answerable_transform_is_vetoed(self, corpus, case) -> None:
        got = vetoers(corpus, case.query)
        assert got == {}, f"{case.case_id} would return an empty lane on {sorted(got)}"

    @pytest.mark.parametrize("case", [c for c in TRANSFORM_CASES if c.shape_words],
                             ids=lambda c: c.case_id)
    def test_declared_shape_words_are_all_in_the_vocabulary(self, case) -> None:
        """Keeps the catalog honest: a case may not claim a word is output-shape unless
        the engine agrees, or the catalog drifts into documenting a fix that isn't
        there."""
        for word in case.shape_words:
            assert word in _OUTPUT_SHAPE_TOKENS, (
                f"{case.case_id} declares {word!r} as output shape but "
                "_OUTPUT_SHAPE_TOKENS does not list it"
            )


class TestTheVocabularyIsClosed:
    """A stoplist that grows to cover subjects would silently disable absence honesty."""

    @pytest.mark.parametrize(
        "subject",
        ["zorblatt", "falconer", "chromodynamics", "ulaanbaatar", "keycloak",
         "migration", "installer", "invoice"],
    )
    def test_subjects_are_not_in_it(self, subject: str) -> None:
        assert subject not in _OUTPUT_SHAPE_TOKENS

    def test_no_entry_can_be_unreachable(self) -> None:
        """Every entry must be something the tokenizer can actually emit.

        `_query_tokens` splits on non-alphanumerics, so a hyphenated entry ("one-pager",
        "press-release") is dead on arrival — it reads as coverage the list does not
        have. Entries the tokenizer's own stoplist already removes are kept
        deliberately: those become live again if that stoplist shrinks.
        """
        unreachable = sorted(t for t in _OUTPUT_SHAPE_TOKENS if not t.isalnum())
        assert unreachable == [], f"entries the tokenizer can never emit: {unreachable}"

    @pytest.mark.parametrize("persona", ["detective", "cookie", "newspaper", "announcer"])
    def test_arbitrary_persona_nouns_are_NOT_covered(self, persona: str) -> None:
        """The honest limit of a closed list, pinned so nobody reads the fix as total.

        "as a noir detective monologue" names a genre with an ordinary noun. `noir` and
        `monologue` are listed; `detective` is not, and cannot be — the construction is
        open-ended ("in the voice of a 1920s radio announcer"). Two things carry the
        rest: the caller-side distiller strips a MARKED style span ("in the style/voice
        of X") because it has the sentence and can see the marker, and an ordinary noun
        is usually present in a real corpus anyway. An unmarked persona built from a
        word the corpus lacks is the residual hole, and it is a recall loss, not a
        correctness one — the lane abstains, which is the safe direction.
        """
        assert persona not in _OUTPUT_SHAPE_TOKENS


class TestBareNumbersAreQuantitiesNotTopics:
    def test_a_count_cannot_veto(self, corpus) -> None:
        assert vetoers(corpus, "give me 200 words on the project") == {}
        assert vetoers(corpus, "write 1000 words on my goals") == {}

    def test_but_a_number_still_filters_rows(self) -> None:
        """Exempt from the gate, not from matching — `contains=` still sees it.

        (`_query_tokens` keeps only tokens of three characters or more, so short
        numbers never reach the gate at all; this exemption is about the ones that do.)
        """
        assert "200" in needles("give me 200 words on the project")
