"""The rare gate runs PER PART, because one flattened needle set cannot gate a
multi-part request.

`_rrf_fuse_summary_lists` vetoes the whole result when ANY needle is unevidenced. That
is correct for one ask. For "1) how did the Threnody-7 rewrite go 2) how did I sleep",
one needle set derived from the whole request contains `threnody` — a word the corpus
does not contain — so the sleep lane is emptied by a question that was not about sleep.

The consequence is not a degraded gate, it is an ABSENT one: on a multi-part request the
gate fires on every part or none, so the sections that most need protection from filler
are exactly the ones it can never fire for correctly. The adversarial panel called it
dead in production; these tests are the shape of alive.

The degenerate case is the whole compatibility story: no parts → exactly one group →
token-for-token what the single-needle path did.
"""

from __future__ import annotations

from topos.query.retrieval import (
    _needle_token_groups,
    _rrf_fuse_summary_lists,
    _rare_token_groups,
)
from topos.query.narrowing import NarrowingLedger


def _item(record_id: str, text: str) -> dict:
    return {"record_id": record_id, "summary_text": text, "retrieval_source": "canonical"}


def _fuse(items, **kwargs):
    return _rrf_fuse_summary_lists([("canonical", 1.0, items)], **kwargs)


class TestNeedlesSplitPerPart:
    def test_no_parts_is_one_group_identical_to_the_flat_needles(self) -> None:
        flat = _needle_token_groups("threnody rewrite sleep quality")
        assert len(flat) == 1
        assert "threnody" in flat[0] and "sleep" in flat[0]

    def test_each_part_keeps_only_its_own_needles(self) -> None:
        groups = _needle_token_groups(
            "threnody rewrite sleep quality",
            ["threnody rewrite", "sleep quality"],
        )
        assert len(groups) == 2
        assert "threnody" in groups[0] and "threnody" not in groups[1]
        assert "sleep" in groups[1] and "sleep" not in groups[0]

    def test_blank_parts_are_dropped_not_hashed_as_empty_asks(self) -> None:
        groups = _needle_token_groups("threnody rewrite", ["threnody rewrite", "   ", ""])
        assert len(groups) == 1

    def test_date_framing_is_still_stripped_inside_each_part(self) -> None:
        """Naming a window must not empty the lane it was meant to narrow — per part
        too. `_residual_content_tokens` runs on each part, so the absolute-date rules
        (`_MONTH_TOKENS`, year and day-number handling) apply within a part exactly as
        they do to a whole request."""
        groups = _needle_token_groups(
            "", ["my work goals Aug 11-16, 2026", "dining spend in March 2026"]
        )
        assert groups[0] == []
        assert "march" not in groups[1] and "2026" not in groups[1]
        assert "dining" in groups[1]


class TestOnePartCannotVetoAnother:
    def test_the_live_regression_an_absent_part_no_longer_empties_the_other(self) -> None:
        items = [_item("r1", "slept badly on tuesday"), _item("r2", "sleep was fine")]
        groups = [{"threnody": 0}, {"sleep": 1}]
        assert _fuse(items, rare_token_groups=groups), (
            "part A named something the corpus does not contain and took part B's "
            "answer with it — the companion veto is not per part"
        )

    def test_flattening_those_same_needles_still_empties_everything(self) -> None:
        """The bug, stated as a test: this is what production does today."""
        items = [_item("r1", "slept badly on tuesday")]
        assert _fuse(items, rare_tokens={"threnody": 0, "sleep": 1}) == []

    def test_every_part_unanswerable_is_still_an_honest_empty(self) -> None:
        items = [_item("r1", "slept badly on tuesday")]
        assert _fuse(items, rare_token_groups=[{"threnody": 0}, {"zorblatt": 0}]) == []

    def test_a_part_with_no_needles_keeps_the_lane_alive(self) -> None:
        """A pure date-scoped part ("my week") carries no specific ask, so it has
        nothing to be unanswerable about — the window alone narrows it."""
        items = [_item("r1", "slept badly on tuesday")]
        assert _fuse(items, rare_token_groups=[{"threnody": 0}, {}])

    def test_a_single_group_behaves_exactly_like_the_flat_form(self) -> None:
        items = [_item("r1", "slept badly on tuesday")]
        for tokens in ({"threnody": 0}, {"sleep": 1}, {"threnody": 0, "zorblatt": 0}):
            assert _fuse(items, rare_token_groups=[dict(tokens)]) == _fuse(
                [dict(i) for i in items], rare_tokens=dict(tokens)
            )

    def test_the_multi_token_no_evidence_rule_is_also_per_part(self) -> None:
        """Two weakly-rare tokens, none evidenced, veto their OWN part only."""
        items = [_item("r1", "sleep was fine")]
        assert _fuse(
            items, rare_token_groups=[{"falconer": 1, "competitive": 2}, {"sleep": 1}]
        )
        assert _fuse(
            [dict(i) for i in items],
            rare_token_groups=[{"falconer": 1, "competitive": 2}],
        ) == []


class TestTheLedgerTellsThePartialCaseApart:
    def test_all_parts_vetoed_records_the_gate_veto_as_before(self) -> None:
        ledger = NarrowingLedger()
        _fuse([_item("r1", "sleep was fine")], rare_token_groups=[{"threnody": 0}], ledger=ledger)
        assert ledger.empty_cause == "gate_vetoed"
        assert any(e.stage == "rare_gate" for e in ledger.entries)

    def test_a_partial_veto_is_a_narrowing_not_an_empty(self) -> None:
        ledger = NarrowingLedger()
        kept = _fuse(
            [_item("r1", "sleep was fine")],
            rare_token_groups=[{"threnody": 0}, {"sleep": 1}],
            ledger=ledger,
        )
        assert kept
        assert ledger.empty_cause is None, (
            "a part that found nothing must not declare the whole turn empty"
        )
        partial = [e for e in ledger.entries if e.reason == "rare_gate_partial_veto"]
        assert partial and partial[0].stage == "rare_gate"
        assert partial[0].detail["parts_vetoed"] == [0]

    def test_the_public_ledger_carries_no_owner_text(self) -> None:
        """`detail` (the tokens, the owner's words) is local-only; the public entry is
        closed-set slugs and integers. Same split as `scope_shadow.as_telemetry`."""
        ledger = NarrowingLedger()
        _fuse(
            [_item("r1", "sleep was fine")],
            rare_token_groups=[{"threnody": 0}, {"sleep": 1}],
            ledger=ledger,
        )
        public = ledger.as_public()
        assert "threnody" not in repr(public)
        for entry in public["ledger"]:
            assert set(entry) <= {"stage", "action", "reason", "dropped"}


class TestOneDfPassPerRequest:
    def test_groups_share_a_single_frequency_lookup(self) -> None:
        """Per-part gating must not cost a df lookup per part — token frequency does
        not depend on which part asked for it."""
        calls = []

        class _Conn:
            def execute(self, sql, params=()):
                calls.append(params)
                raise RuntimeError("no FTS")

        _rare_token_groups(_Conn(), [["alpha"], ["beta"], ["alpha"]])
        # `_rare_tokens` bails on the corpus-size probe here; the point is that the
        # union — not the concatenation — is what gets looked up.
        assert len(calls) <= 1

    def test_a_token_in_two_parts_gets_the_same_df_in_both(self) -> None:
        groups = _rare_token_groups(None, [["alpha", "beta"], ["alpha"]])
        # No connection → nothing is rare → both groups empty, and crucially equal.
        assert groups == [{}, {}]


class TestTheBrowseGateIsAlsoPerPart:
    """`_route_canonical_rows` has the second rare gate: when nothing matched the
    content tokens, an effectively-absent term means the specific thing isn't there and
    the recency browse is suppressed. Flattened, one part's absent term suppressed the
    browse for every other part's lane too."""

    def _routed(self, monkeypatch, **kwargs):
        import topos.query.retrieval as module

        def _fake_list(adapters, table, *, contains=None, **rest):
            # Nothing matches the content tokens; the browse page is what is at stake.
            return [] if contains else [{"record_id": "browsed"}]

        monkeypatch.setattr(module, "_list_canonical_rows", _fake_list)

        class _Manifest:
            scope_id = "health:read"

        return module._route_canonical_rows(
            object(),
            "conversation_messages",
            manifest=_Manifest(),
            query_text="threnody rewrite sleep quality",
            source_ids=[],
            limit=10,
            disclosure_tier="owner_raw",
            browse_fallback=True,
            **kwargs,
        )

    def test_one_absent_part_no_longer_suppresses_the_browse(self, monkeypatch) -> None:
        assert self._routed(
            monkeypatch, rare_query_token_groups=[{"threnody": 0}, {"sleep": 5}]
        ), "part A's absent term suppressed the browse part B was asking for"

    def test_every_part_absent_still_suppresses_it(self, monkeypatch) -> None:
        assert (
            self._routed(
                monkeypatch,
                rare_query_token_groups=[{"threnody": 0}, {"quality": 0}],
            )
            == []
        )

    def test_the_flat_form_is_unchanged(self, monkeypatch) -> None:
        assert self._routed(monkeypatch, rare_query_tokens={"threnody": 0}) == []
        assert self._routed(monkeypatch, rare_query_tokens={"sleep": 5})
        assert self._routed(monkeypatch, rare_query_tokens=None)
