"""An absent word narrows an ask the store otherwise answers; it does not erase it.

The rare-token gate in `_rrf_fuse_summary_lists` is absence honesty: a query word the
corpus never uses means "you asked about something your data does not mention", and the
honest packet is empty. Measured 31 Oct 2026 on three LongMemEval S-split bench stores
(built through the real ingest path, ~500 AI-chat turns each), the gate emptied the
summary packet on asks the store plainly answers:

    "I noticed my bike seems to be performing even better during my Sunday group rides.
     Could there be a reason for this?"
    → rare_gate emptied, rare_token_unevidenced, dropped 43
      `sunday` df 0; `bike` df 17 and `rides` df 16 evidenced by 13 and 16 of the 43
      candidates; the gold turn sat at vector rank 2. Raw mode returned it at rank 5.

    "What should I serve for dinner this weekend with my homegrown ingredients?"
    → `homegrown` df 0; serve / dinner / weekend / ingredients all evidenced. Raw: rank 27.

    "… what was the rotation for Admon on a Sunday?"
    → `admon` df 0 — the name IS in two stored turns, but the FTS text is the
      privacy-filtered rendering ([NAME]), so a person's name can never be evidenced.
      shift / rotation / sheet / agents / sunday evidenced; gold at vector rank 4.

One absent qualifier took down an ask whose every other content word the candidates
evidenced. The rule here: an absent word (df ≤ 2, unevidenced) empties a part only when
the absent words are not outnumbered by the words the candidates DO evidence. A lone
fabricated subject (`zorblatt`) still vetoes; a tie (`competitive falconer` with only
`competitive` evidenced) still vetoes; `Sunday bike rides` with `bike` and `rides`
evidenced is narrowed, and the ledger says so, not emptied.

Counting runs over the part's CONTENT tokens when the caller hands them in
(`content_token_groups`): on a large store the subject words (`bike` df 400) are not in
the rare map at all, and a count over the rare map alone would see only `sunday` and
veto exactly as before. Without them the rare map's own keys are counted (the unit form
the older tests use).
"""

from __future__ import annotations

from topos.query import narrowing as _N
from topos.query.narrowing import NarrowingLedger
from topos.query.retrieval import _rrf_fuse_summary_lists


def _item(record_id: str, text: str) -> dict:
    return {"record_id": record_id, "summary_text": text, "retrieval_source": "vector"}


def _fuse(items, **kwargs):
    return _rrf_fuse_summary_lists([("vector", 1.0, items)], **kwargs)


# The bike-ride bench store, in miniature: candidates about the subject, none of them
# saying the day. `sunday` is the absent word; every other content word is evidenced.
BIKE_ITEMS = [
    _item("r1", "my bike seems to be performing better on the group rides lately"),
    _item("r2", "noticed the bike feels faster, could the reason be the new tyres"),
    _item("r3", "group ride pacing felt better this week"),
]
BIKE_RARE = {"noticed": 11, "bike": 17, "seems": 8, "better": 23, "sunday": 0,
             "group": 23, "rides": 16, "reason": 10}
BIKE_CONTENT = ["noticed", "bike", "seems", "performing", "better", "during", "sunday",
                "group", "rides", "could", "reason"]


class TestTheMeasuredFailure:
    def test_an_absent_day_does_not_empty_an_ask_about_bike_rides(self) -> None:
        kept = _fuse([dict(i) for i in BIKE_ITEMS], rare_tokens=dict(BIKE_RARE))
        assert kept, (
            "`sunday` is nowhere in the corpus, `bike` and `rides` are in every "
            "candidate — the gate erased an ask the store answers"
        )

    def test_the_per_part_form_agrees(self) -> None:
        flat = _fuse([dict(i) for i in BIKE_ITEMS], rare_tokens=dict(BIKE_RARE))
        grouped = _fuse([dict(i) for i in BIKE_ITEMS], rare_token_groups=[dict(BIKE_RARE)])
        assert [i["record_id"] for i in flat] == [i["record_id"] for i in grouped]

    def test_a_redacted_name_is_outnumbered_by_the_sentence_around_it(self) -> None:
        """`admon` is df 0 because the FTS text says [NAME]; the rotation sheet it names
        is evidenced on every other word. `remind` is weakly rare and unevidenced, which
        has never vetoed alone and still does not."""
        items = [
            _item("r1", "updated shift rotation sheet for the social media agents, "
                        "sunday through saturday, [NAME] on the morning shift"),
            _item("r2", "agent names for the rotation sheet: [NAME] [NAME] [NAME]"),
        ]
        rare = {"checking": 25, "previous": 4, "shift": 17, "rotation": 9, "sheet": 14,
                "media": 27, "agents": 12, "remind": 3, "admon": 0, "sunday": 6}
        assert _fuse(items, rare_tokens=rare)

    def test_a_large_store_counts_the_subject_words_too(self) -> None:
        """On the owner's node `bike` and `rides` are common (df ≥ df_max) and absent
        from the rare map; the content tokens are what say the candidates are about
        the ask."""
        rare = {"sunday": 0}
        assert _fuse([dict(i) for i in BIKE_ITEMS], rare_tokens=dict(rare)) == [], (
            "control: with nothing but the absent word to count, the gate still vetoes"
        )
        assert _fuse(
            [dict(i) for i in BIKE_ITEMS],
            rare_token_groups=[dict(rare)],
            content_token_groups=[list(BIKE_CONTENT)],
        ), "the subject words were evidenced and not counted"


class TestTheLedgerSaysWhatWasNotFound:
    def test_a_narrowed_ask_is_not_an_empty(self) -> None:
        ledger = NarrowingLedger()
        kept = _fuse([dict(i) for i in BIKE_ITEMS], rare_tokens=dict(BIKE_RARE), ledger=ledger)
        assert kept
        assert ledger.empty_cause is None
        scoped = [e for e in ledger.entries if e.stage == _N.STAGE_RARE_GATE]
        assert scoped and scoped[0].action == "scoped"
        assert scoped[0].reason == _N.REASON_ABSENT_TOKEN_OUTNUMBERED
        assert scoped[0].detail["tokens"] == ["sunday"]

    def test_the_public_ledger_carries_no_owner_text(self) -> None:
        ledger = NarrowingLedger()
        _fuse([dict(i) for i in BIKE_ITEMS], rare_tokens=dict(BIKE_RARE), ledger=ledger)
        public = ledger.as_public()
        assert "sunday" not in repr(public)
        for entry in public["ledger"]:
            assert set(entry) <= {"stage", "action", "reason", "dropped"}


class TestAbsenceHonestyIsIntact:
    def test_a_lone_fabricated_subject_still_vetoes(self) -> None:
        items = [_item("r1", "slept badly on tuesday")]
        assert _fuse(items, rare_tokens={"zorblatt": 0}) == []

    def test_a_fabricated_subject_beside_one_evidenced_word_still_vetoes(self) -> None:
        """NEG-2: `competitive falconer`. `competitive` is in the bio, `falconer` is not;
        one for one is a tie and a tie is a veto."""
        items = [_item("r1", "public bio: competitive runner, ten years in product")]
        assert _fuse(items, rare_tokens={"falconer": 1, "competitive": 26}) == []

    def test_two_absent_words_beside_two_evidenced_ones_still_veto(self) -> None:
        items = [_item("r1", "the group ride on the bike")]
        assert _fuse(items, rare_tokens={"zorblatt": 0, "threnody": 0, "bike": 17, "ride": 9}) == []

    def test_the_veto_still_records_the_gate_as_the_empty_cause(self) -> None:
        ledger = NarrowingLedger()
        _fuse([_item("r1", "slept badly")], rare_tokens={"zorblatt": 0}, ledger=ledger)
        assert ledger.empty_cause == _N.CAUSE_GATE_VETOED

    def test_several_weakly_rare_words_none_evidenced_still_veto(self) -> None:
        items = [_item("r1", "sleep was fine")]
        assert _fuse(items, rare_tokens={"falconer": 1, "competitive": 2}) == []

    def test_an_absent_part_still_cannot_veto_another(self) -> None:
        items = [_item("r1", "slept badly on tuesday"), _item("r2", "sleep was fine")]
        assert _fuse(items, rare_token_groups=[{"threnody": 0}, {"sleep": 1}])
