"""The narrowing ledger, and why an empty lane is empty.

Ten defects in one afternoon, ten well-formed results, zero warnings. Six of the eight
stages a request passes through can shrink it and none of them said so, and an empty
lane had four indistinguishable causes — so the model told the owner their data "may
not be synced" about data that was present and indexed, twice in one report.

These tests hold two properties. First, that the stages which narrow now record
``{stage, action, reason, dropped}``. Second — the half that matters — that every
empty result carries WHICH of the causes produced it.

Plus the two invariants the ledger is only allowed to exist under: passing no ledger
leaves every path byte-identical, and nothing that could leave the node carries the
owner's words.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.query import narrowing as N
from topos.query.narrowing import NarrowingLedger


class TestTheLedgerItself:
    def test_an_entry_is_stage_action_reason_dropped(self) -> None:
        # `emptied`, not the `vetoed` this test used to invent: the gate's real veto
        # goes through `empty()`, which writes `emptied`, and a fixture that speaks a
        # verb no production call site emits is not evidence about the production path.
        # It is also now a non-member, so the ledger would have collapsed it.
        led = NarrowingLedger()
        led.record(N.STAGE_RARE_GATE, "emptied", "rare_token_unevidenced", dropped=25)
        assert led.as_public()["ledger"] == [
            {
                "stage": "rare_gate",
                "action": "emptied",
                "reason": "rare_token_unevidenced",
                "dropped": 25,
            }
        ]

    def test_recording_never_raises(self) -> None:
        class Hostile:
            def __str__(self) -> str:
                raise RuntimeError("boom")

        led = NarrowingLedger()
        led.record(Hostile(), "x", "y", dropped="not a number")  # type: ignore[arg-type]
        led.empty("not-a-real-cause")
        assert led.empty_cause is None

    def test_public_fields_cannot_carry_the_owners_words(self) -> None:
        """The privacy guarantee is structural, not a call-site convention.

        This test used to assert the slugged text — ``how_often_do_i_go_to_the_gym`` —
        and call that safe, which is how it stayed green while the mechanism was a
        character filter. It asserts membership now: the words are gone, not merely
        punctuated differently.
        """
        led = NarrowingLedger()
        led.record("gate", "vetoed", "How often do I go to the gym? (Threnody-7)")
        entry = led.as_public()["ledger"][0]
        assert entry == {
            "stage": N.UNRECOGNIZED,
            "action": N.UNRECOGNIZED,
            "reason": N.UNRECOGNIZED,
        }

    def test_detail_stays_on_the_node(self) -> None:
        led = NarrowingLedger()
        led.record(
            "rare_gate", "emptied", "rare_token_unevidenced", detail={"tokens": ["threnody"]}
        )
        assert "detail" not in led.as_public()["ledger"][0]
        assert led.as_local()["ledger"][0]["detail"] == {"tokens": ["threnody"]}

    def test_a_more_authoritative_cause_wins_whatever_the_order(self) -> None:
        led = NarrowingLedger()
        led.empty(N.CAUSE_STORE_EMPTY)
        led.empty(N.CAUSE_GATE_VETOED)
        assert led.empty_cause == N.CAUSE_GATE_VETOED
        led.empty(N.CAUSE_NO_MATCH)
        assert led.empty_cause == N.CAUSE_GATE_VETOED, "the veto is *why* nothing matched"
        led.empty(N.CAUSE_SCOPE_DENIED)
        assert led.empty_cause == N.CAUSE_SCOPE_DENIED

    def test_telemetry_is_counts_and_enums(self) -> None:
        led = NarrowingLedger()
        led.record(
            "rare_gate", "emptied", "rare_token_unevidenced", detail={"tokens": ["threnody"]}
        )
        led.empty(N.CAUSE_GATE_VETOED)
        tel = led.as_telemetry()
        assert tel == {"n_entries": 1, "stages": {"rare_gate": 1}, "empty_cause": "gate_vetoed"}

    def test_result_is_empty_reads_all_three_modes(self) -> None:
        assert N.result_is_empty(None)
        assert N.result_is_empty({"answer_type": "summary", "summaries": []})
        assert N.result_is_empty({"answer_type": "raw", "rows": []})
        assert not N.result_is_empty({"answer_type": "summary", "summaries": [{"topic": "a"}]})
        assert not N.result_is_empty({"answer_type": "raw", "rows": [{"a": 1}]})
        assert not N.result_is_empty({"answer_type": "yes_no", "answer": "yes"})
        assert N.result_is_empty({"answer_type": "yes_no", "answer": "no"})


class TestTheGateSaysSo:
    """The rare-token gate is the good mechanism that produced every silent empty. It
    stays exactly as strict; it now writes down that it fired."""

    def _lists(self, blob: str):
        return [("canonical", 1.0, [{"record_id": "r1", "summary_text": blob}])]

    def test_a_veto_is_recorded_with_what_it_dropped(self) -> None:
        from topos.query.retrieval import _rrf_fuse_summary_lists

        led = NarrowingLedger()
        out = _rrf_fuse_summary_lists(
            self._lists("a normal journal entry"),
            rare_tokens={"threnody": 0},
            ledger=led,
        )
        assert out == []
        assert led.empty_cause == N.CAUSE_GATE_VETOED
        entry = [e for e in led.as_public()["ledger"] if e["stage"] == "rare_gate"]
        assert entry, "the gate emptied the lane and did not say so"
        assert entry[0]["dropped"] == 1

    def test_nothing_to_fuse_is_an_empty_store_not_a_veto(self) -> None:
        from topos.query.retrieval import _rrf_fuse_summary_lists

        led = NarrowingLedger()
        assert _rrf_fuse_summary_lists([("canonical", 1.0, [])], ledger=led) == []
        assert led.empty_cause == N.CAUSE_STORE_EMPTY

    def test_no_ledger_leaves_the_gate_byte_identical(self) -> None:
        from topos.query.retrieval import _rrf_fuse_summary_lists

        args = self._lists("a normal journal entry")
        assert _rrf_fuse_summary_lists(args, rare_tokens={"threnody": 0}) == []
        assert _rrf_fuse_summary_lists(self._lists("threnody was here")) != []


class TestTheRequestCarriesOne:
    def test_retrieval_request_takes_an_optional_ledger(self) -> None:
        from topos.query.types import RetrievalRequest

        req = RetrievalRequest(manifest=object(), access_mode="summary", query_text="q")
        assert req.ledger is None

    def test_skipped_retrieval_is_not_queried(self) -> None:
        from topos.query.retrieval import DefaultSignalRetrievalAdapter
        from topos.query.types import RetrievalRequest

        led = NarrowingLedger()
        adapter = DefaultSignalRetrievalAdapter.__new__(DefaultSignalRetrievalAdapter)
        adapter._adapters = None  # type: ignore[attr-defined]
        adapter._last_stores = []  # type: ignore[attr-defined]
        adapter.retrieve_call_count = 0
        adapter.retrieve(
            RetrievalRequest(
                manifest=object(), access_mode="summary", skip_retrieval=True, ledger=led
            )
        )
        assert led.empty_cause == N.CAUSE_NOT_QUERIED


class TestTheDisclosureFilterSaysSo:
    def test_dropping_grantee_items_is_recorded(self) -> None:
        from topos.query.disclosure import DisclosureFilterPipeline
        from topos.query.types import RetrievalBundle

        led = NarrowingLedger()
        bundle = RetrievalBundle(
            context_packet={
                "summaries": [
                    {"topic": "flagged", "summary_text": "x", "content_nsfw": 1},
                    {"topic": "also flagged", "summary_text": "y", "content_nsfw": 1},
                ]
            }
        )
        DisclosureFilterPipeline().apply(
            bundle,
            access_mode="summary",
            disclosure_tier="default_disclosure",
            ledger=led,
        )
        assert any(e["stage"] == "disclosure" for e in led.as_public()["ledger"])

    def test_no_ledger_is_unchanged(self) -> None:
        from topos.query.disclosure import DisclosureFilterPipeline
        from topos.query.types import RetrievalBundle

        bundle = RetrievalBundle(context_packet={"summaries": [{"topic": "fine"}]})
        out = DisclosureFilterPipeline().apply(bundle, access_mode="summary")
        assert out.context_packet["summaries"] == [{"topic": "fine"}]


@pytest.fixture
def empty_conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(str(tmp_path / "narrowing.db"))
    apply_all_migrations(conn)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def orchestrator(empty_conn):
    from topos.query.pipeline import QueryPipelineOrchestrator
    from topos.storage.adapters.factory import AdapterFactory

    return QueryPipelineOrchestrator(adapters=AdapterFactory.create("local_database", conn=empty_conn))


class TestTheResponseCarriesIt:
    @pytest.mark.asyncio
    async def test_an_empty_summary_names_its_cause(self, orchestrator) -> None:
        from topos.query.manifest_validation import resolve_scope_manifest

        result = await orchestrator.execute(
            query_text="what did I journal about last week?",
            scope_id="health:read",
            access_mode="summary",
            manifest=resolve_scope_manifest("health:read"),
            query_session_id="qs-narrowing-empty",
        )
        assert result["turn_outcome"] == "live_query"
        assert result["public_result"]["summaries"] == []
        narrowing = result.get("narrowing")
        assert isinstance(narrowing, dict), "the response carries no narrowing record"
        assert narrowing.get("empty_cause") in N.EMPTY_CAUSES
        # The model that writes the answer reads public_result, not the envelope.
        assert result["public_result"].get("empty_cause") == narrowing["empty_cause"]

    @pytest.mark.asyncio
    async def test_a_denial_is_attributed_as_a_denial(self, orchestrator) -> None:
        from topos.query.manifest_validation import resolve_scope_manifest

        import dataclasses

        manifest = dataclasses.replace(
            resolve_scope_manifest("health:read"), access_mode_ceiling="summary"
        )
        result = await orchestrator.execute(
            query_text="everything, raw",
            scope_id="health:read",
            access_mode="raw",
            manifest=manifest,
            query_session_id="qs-narrowing-denied",
        )
        assert result["turn_outcome"] == "denied"
        assert (result.get("narrowing") or {}).get("empty_cause") == N.CAUSE_SCOPE_DENIED

    @pytest.mark.asyncio
    async def test_a_result_that_is_not_empty_carries_no_empty_cause(self) -> None:
        """The turn-level verdict may not contradict the payload it travels with.

        ``empty_cause`` answers "why did this come back with nothing". A stage can
        empty its own lane without emptying the turn — the rare gate returns ``[]``
        for evidence while the derived lanes still produce summaries — and publishing
        ``gate_vetoed`` beside a payload that has summaries in it is the ledger and
        the result telling two stories about one turn. The front end's coverage map
        believes the ledger by construction, so it would label that section "not
        retrieved" over data sitting in the same response: the false-absence bug,
        re-entered through the fix for it.

        The gate's ledger ENTRY survives — only the turn-level claim is withheld.
        """
        from topos.query.pipeline import _attach_narrowing

        ledger = NarrowingLedger()
        ledger.empty(
            N.CAUSE_GATE_VETOED, stage=N.STAGE_RARE_GATE, reason="rare_token_unevidenced"
        )
        assert ledger.empty_cause == N.CAUSE_GATE_VETOED

        result = _attach_narrowing(
            {
                "turn_outcome": "live_query",
                "public_result": {"summaries": [{"topic": "sleep"}]},
            },
            ledger,
        )
        narrowing = result["narrowing"]
        assert narrowing["result_empty"] is False
        assert "empty_cause" not in narrowing
        # The stage still admits what it did; only the turn-level verdict is withheld.
        assert [e["stage"] for e in narrowing["ledger"]] == [N.STAGE_RARE_GATE]

    @pytest.mark.asyncio
    async def test_an_empty_result_still_publishes_its_cause(self) -> None:
        from topos.query.pipeline import _attach_narrowing

        ledger = NarrowingLedger()
        ledger.empty(
            N.CAUSE_GATE_VETOED, stage=N.STAGE_RARE_GATE, reason="rare_token_unevidenced"
        )
        result = _attach_narrowing(
            {"turn_outcome": "live_query", "public_result": {"summaries": []}}, ledger
        )
        assert result["narrowing"]["result_empty"] is True
        assert result["narrowing"]["empty_cause"] == N.CAUSE_GATE_VETOED

    @pytest.mark.asyncio
    async def test_the_ledger_never_carries_query_text(self, orchestrator) -> None:
        from topos.query.manifest_validation import resolve_scope_manifest

        result = await orchestrator.execute(
            query_text="what did I journal about Threnody last week?",
            scope_id="health:read",
            access_mode="summary",
            manifest=resolve_scope_manifest("health:read"),
            query_session_id="qs-narrowing-privacy",
        )
        narrowing = result.get("narrowing")
        assert isinstance(narrowing, dict), "no ledger to check"
        blob = repr(narrowing)
        for word in ("journal", "Threnody", "last week"):
            assert word.lower() not in blob.lower()


def test_inference_confidence_survives_dict_valued_scores():
    """Live 2026-09-03: a fact item whose value is a value_struct dict crashed
    the whole inference turn at the game layer's float() coercion. Confidence
    is telemetry; its coercion must never cost the answer."""
    from topos.query.game_layer import _coerce_confidence

    assert _coerce_confidence({"dimension": "stress"}, None) == 0.0
    assert _coerce_confidence("not-a-number", 0.7) == 0.7
    assert _coerce_confidence(None, "0.5") == 0.5
    assert _coerce_confidence() == 0.0
