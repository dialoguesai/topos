"""`retrieval_text` narrows the NEEDLES and nothing else.

An instruction is not a query. `_residual_content_tokens` turns every non-surface,
non-recency token into a word the retrieved rows must contain, and a rare needle that
matches nothing empties the lane — correct for a specific ask, wrong for a request that
merely describes the report it wants. Measured live 2026-08-17, one node / window /
scope: `health:read` returned 0 summaries for the full prompt and 25 for the same
question distilled.

The fix is a SECOND field rather than a different value in `query`, because 2026-08-16
already measured what happens when the planner, the vector ranking and the scope
classifier are handed a keyword digest instead of a sentence: lost time windows, an
abstaining classifier, an embedding of a fragment (see `handlers/query.py`). So the
distilled text may reach the rare gate and must reach nothing else.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import pytest

from topos.query.types import RetrievalRequest


class TestTheFieldIsOptional:
    def test_defaults_to_none_so_existing_callers_are_unchanged(self) -> None:
        req = RetrievalRequest(manifest=object(), access_mode="summary", query_text="q")
        assert req.needle_text is None

    def test_absent_needle_text_falls_back_to_query_text(self) -> None:
        """`needle_text or query_text` — every MCP client that never sends it is
        byte-identical to before."""
        req = RetrievalRequest(manifest=object(), access_mode="summary", query_text="q")
        assert (str(req.needle_text or "").strip() or req.query_text) == "q"

    def test_a_blank_needle_text_is_treated_as_absent(self) -> None:
        req = RetrievalRequest(
            manifest=object(), access_mode="summary", query_text="q", needle_text="   "
        )
        assert (str(req.needle_text or "").strip() or req.query_text) == "q"


class TestTheSeparationHolds:
    """Guards on the wiring itself: the distilled text must not leak into the lanes that
    need the sentence. Asserted against the source because the alternative is an
    end-to-end retrieval fixture per lane, and this is the property that matters."""

    def _retrieval_source(self) -> str:
        from pathlib import Path

        import topos.query.retrieval as module

        return Path(module.__file__).read_text()

    def test_the_planner_reads_query_text_not_needles(self) -> None:
        src = self._retrieval_source()
        assert "build_query_plan(get_db_connection(), query_text" in src, (
            "the planner must parse its time window from the owner's sentence — a "
            "keyword digest loses `this week` (measured 2026-08-16)"
        )
        assert "build_query_plan(get_db_connection(), needle_text" not in src

    def test_every_rare_token_site_uses_needles(self) -> None:
        src = self._retrieval_source()
        # Three sites feed `_rare_tokens`: two inline in `retrieve`, one in the summary
        # builder. All three must read the needles; a new one must opt in explicitly.
        assert src.count("_residual_content_tokens(_query_tokens(needle_text)") == 2
        assert "_query_tokens(needle_text or query_text)" in src
        assert "_residual_content_tokens(_query_tokens(query_text)" not in src, (
            "a rare-gate site still reads the raw request text; instructional words "
            "will keep gating lanes empty there"
        )

    def test_the_per_part_site_opts_in_by_name(self) -> None:
        """The FOURTH site, added deliberately by P3 multi-needle.

        `_needle_token_groups` tokenises each PART instead of the whole request, so it
        cannot use the `needle_text` literal the three counted sites use — which is
        exactly why it is asserted separately rather than by relaxing the count above.
        It must still be fed the needles, never the raw request text.
        """
        src = self._retrieval_source()
        assert "def _needle_token_groups(" in src
        assert "_residual_content_tokens(_query_tokens(part))" in src
        # It is reached only via the needles, in `retrieve` and in the summary builder.
        assert "_needle_token_groups(needle_text, needle_parts)" in src
        assert "_needle_token_groups(needle_text or query_text, needle_parts)" in src
        assert "_needle_token_groups(query_text" not in src, (
            "the per-part site reads the raw request text; every part would carry the "
            "instructional words the second field exists to strip"
        )

    def test_the_pipeline_keeps_the_shadow_on_the_raw_text(self) -> None:
        from pathlib import Path

        import topos.query.pipeline as module

        src = Path(module.__file__).read_text()
        assert "_shadow_observe(query_text, scope_id)" in src, (
            "Horos is trained on natural questions; classifying a keyword digest was "
            "measured to make it abstain, and it would poison the shadow training set"
        )
        assert "_shadow_observe(retrieval_text" not in src


class TestThePartsAreTheSameSecondField:
    """`needle_parts` splits the needles; it must not become a third text that reaches
    the planner, the embedding or the classifier. Same separation, per part."""

    def test_the_field_is_optional_and_defaults_to_none(self) -> None:
        req = RetrievalRequest(manifest=object(), access_mode="summary", query_text="q")
        assert req.needle_parts is None

    def test_absent_parts_are_one_part_from_the_needles(self) -> None:
        from topos.query.retrieval import _needle_token_groups

        assert _needle_token_groups("dialogues topos", None) == _needle_token_groups(
            "dialogues topos", []
        )
        assert len(_needle_token_groups("dialogues topos", None)) == 1

    def test_parts_never_reach_the_planner_or_the_embedding(self) -> None:
        from pathlib import Path

        import topos.query.retrieval as module

        src = Path(module.__file__).read_text()
        assert "build_query_plan(get_db_connection(), needle_parts" not in src
        assert "semantic_query = needle_parts" not in src

    def test_the_pipeline_sends_parts_only_to_the_needles(self) -> None:
        from pathlib import Path

        import topos.query.pipeline as module

        src = Path(module.__file__).read_text()
        assert "needle_parts=needle_parts or None" in src
        assert "_shadow_observe(retrieval_parts" not in src
        assert "query_text=retrieval_parts" not in src


# --------------------------------------------------------------------------- SG2
#
# The rewrite at the end of the separation: the vector lane embeds the SUBJECT.
#
# Everything above asserts where the needles must NOT go. This is the one place
# they must, and it was the hole the 2026-08-19 sweep found: probe r07b deleted
# the rewrite outright and 1,058 tests passed with zero new reds, while the slug
# it emits — `embedded_subject_not_instruction` — is declared in four places
# across three repos (topos/protocol/narrowing_vocabulary.json,
# topos/query/narrowing.py, next/lib/mcp/narrowingLedger.ts,
# control_plane/narrowing.py). Four declarations of a token nothing verified was
# ever spoken.


QUERY = "generate a personal work report summarizing my achievements and adjustments"
NEEDLE = "achievements adjustments"


class _EmbeddingSpy:
    """Every string handed to the embedding backend, tagged with the lane.

    Asserted AT THE CALL, not on a return value. The failure mode is that the
    wrong string is embedded and every downstream shape stays plausible — a
    packet built from the instruction's neighbourhood looks exactly like a
    packet built from the subject's, so there is nothing to assert afterwards.

    Tagged by lane because up to two lanes embed in one `retrieve()`: the
    vector search takes the needles, and `_load_ranked_clusters` deliberately
    keeps the owner's sentence (it only reaches the encoder on a node that has
    clusters, which is why it is a tag here and not an assertion). An untagged
    recorder would see both strings and could be satisfied by either.
    """

    def __init__(self) -> None:
        self.lane = "other"
        self.embedded: List[Tuple[str, str]] = []
        self.vector_search_queries: List[Optional[str]] = []

    def texts_for(self, lane: str) -> List[str]:
        return [text for tag, text in self.embedded if tag == lane]


@pytest.fixture()
def embedding_spy(monkeypatch) -> _EmbeddingSpy:
    """A real `SignalService` over in-memory adapters, with the embedding
    backend replaced by a recorder.

    The service is real so the string is followed all the way from `retrieve()`
    through `_semantic_hits` into `search_vectors` and out to the encoder; only
    the encoder itself and the vector page are fakes. The query-embedding cache
    is forced to miss, or the encoder is never reached at all.
    """
    from topos.features.signal import query_embed_cache as embed_cache
    from topos.features.signal import service as signal_service
    from topos.features.signal.service import SignalService
    from topos.storage.adapters.factory import AdapterFactory

    spy = _EmbeddingSpy()

    class _SpyEncoder:
        def run_inference(self, payload: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
            if str(options.get("subtype") or "") == "embedding":
                spy.embedded.append((spy.lane, str(payload.get("text") or "")))
            return {"vectors": [[0.1] * 8]}

    monkeypatch.setattr(
        "topos.engine.backends.huggingface.HuggingFaceAdapter", _SpyEncoder, raising=True
    )
    monkeypatch.setattr(embed_cache, "get_cached_query_embedding", lambda *a, **k: None)
    monkeypatch.setattr(embed_cache, "set_cached_query_embedding", lambda *a, **k: None)

    class _EmptyPage:
        total = 0
        items: List[Dict[str, Any]] = []

    adapters = AdapterFactory.create("memory")
    monkeypatch.setattr(adapters.vector, "search_similar", lambda *a, **k: _EmptyPage())

    service = SignalService(adapters)
    real_search_vectors = service.search_vectors

    def _bracketed(**kwargs: Any) -> Dict[str, Any]:
        spy.vector_search_queries.append(kwargs.get("query"))
        spy.lane = "vector_search"
        try:
            return real_search_vectors(**kwargs)
        finally:
            spy.lane = "other"

    monkeypatch.setattr(service, "search_vectors", _bracketed)
    monkeypatch.setattr(signal_service, "get_signal_service", lambda *a, **k: service)

    spy.adapters = adapters  # type: ignore[attr-defined]
    return spy


def _retrieve_with(spy: _EmbeddingSpy, **request_kwargs: Any):
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.retrieval import DefaultSignalRetrievalAdapter

    adapters = spy.adapters  # type: ignore[attr-defined]
    return DefaultSignalRetrievalAdapter(adapters).retrieve(
        RetrievalRequest(
            manifest=resolve_scope_manifest("messages:read"),
            access_mode="summary",
            installed_source_ids=["imessage"],
            **request_kwargs,
        )
    )


class TestTheSubjectIsWhatGetsEmbedded:
    """Assertion 1 of 2: the string handed to the encoder IS the needle."""

    def test_the_encoder_receives_the_needles_not_the_instruction(
        self, embedding_spy
    ) -> None:
        _retrieve_with(embedding_spy, query_text=QUERY, needle_text=NEEDLE)

        embedded = embedding_spy.texts_for("vector_search")
        assert embedded, (
            "the vector lane embedded nothing — the spy never saw the encoder, so "
            "this test proves nothing about which string reaches it"
        )
        assert set(embedded) == {NEEDLE}, (
            "the vector lane embedded the owner's instruction instead of the "
            f"subject: {embedded!r}. Embedding the shape of a request rather than "
            "the thing it asks about is a silent recall regression (measured "
            "2026-08-18, 315 characters of report boilerplate)"
        )
        assert QUERY not in embedded
        # The lane retries per source when a page comes back empty, so this is a
        # set: what matters is that no retry quietly falls back to the sentence.
        assert set(embedding_spy.vector_search_queries) == {NEEDLE}, (
            "the needles did not survive the hop into the vector service: "
            f"{embedding_spy.vector_search_queries!r}"
        )

    def test_without_needles_the_sentence_is_what_gets_embedded(
        self, embedding_spy
    ) -> None:
        """The other half of the biconditional, and the control that makes the
        assertion above discriminating.

        A plain question sends no `needle_text`, and 2026-08-16 measured that the
        sentence is what the encoder needs then. It is also the proof that the
        spy CAN see the sentence at this seam — without it, a recorder that
        happened to observe nothing would satisfy the test above.
        """
        _retrieve_with(embedding_spy, query_text=QUERY)
        assert set(embedding_spy.texts_for("vector_search")) == {QUERY}

    def test_needles_equal_to_the_sentence_change_nothing(self, embedding_spy) -> None:
        _retrieve_with(embedding_spy, query_text=QUERY, needle_text=QUERY)
        assert set(embedding_spy.texts_for("vector_search")) == {QUERY}


class TestTheSlugFiresOnlyWhenTheRewriteDoes:
    """Assertion 2 of 2: the receipt is evidence of the action, not decoration.

    A receipt that appears without its action is the failure class this
    programme keeps meeting, and this slug is the most declared and least
    verified token in the vocabulary. Deliberately reads only the ledger and
    never the encoder, so that severing `ledger.record` here fails THIS class
    while the encoder assertions above stay green — if they failed together,
    one would be riding on the other and only one property would be pinned.
    """

    SLUG = "embedded_subject_not_instruction"

    def _rewrite_entries(self, ledger) -> List[Dict[str, Any]]:
        return [
            entry
            for entry in ledger.as_public()["ledger"]
            if entry.get("reason") == self.SLUG
        ]

    def _ledger(self):
        from topos.query.narrowing import NarrowingLedger

        return NarrowingLedger()

    def test_the_slug_is_recorded_when_the_needles_differ(self, embedding_spy) -> None:
        from topos.query import narrowing as N

        ledger = self._ledger()
        _retrieve_with(
            embedding_spy, query_text=QUERY, needle_text=NEEDLE, ledger=ledger
        )
        entries = self._rewrite_entries(ledger)
        assert entries == [
            {"stage": N.STAGE_RETRIEVAL, "action": "rewrote", "reason": self.SLUG}
        ], (
            "the rewrite fired and the ledger did not say so — four repos declare "
            f"this slug and none of them would notice: {ledger.as_public()['ledger']!r}"
        )

    def test_the_slug_is_absent_when_no_needles_are_sent(self, embedding_spy) -> None:
        ledger = self._ledger()
        _retrieve_with(embedding_spy, query_text=QUERY, ledger=ledger)
        assert self._rewrite_entries(ledger) == [], (
            "the ledger claims a rewrite on a request that carried no needles — "
            "the receipt has drifted loose of the action"
        )

    def test_the_slug_is_absent_when_the_needles_equal_the_sentence(
        self, embedding_spy
    ) -> None:
        ledger = self._ledger()
        _retrieve_with(
            embedding_spy, query_text=QUERY, needle_text=QUERY, ledger=ledger
        )
        assert self._rewrite_entries(ledger) == [], (
            "nothing was rewritten — `needle_text == query_text` — and the ledger "
            "recorded a rewrite anyway"
        )

    def test_the_slug_is_in_the_closed_set_it_claims_to_be_in(self) -> None:
        """Otherwise `as_public()` would collapse it to UNRECOGNIZED and the
        assertions above would be reading a token the protocol does not carry."""
        from topos.query import narrowing as N

        assert self.SLUG in N.REASONS
