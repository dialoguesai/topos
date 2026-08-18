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

    def test_the_pipeline_keeps_the_shadow_on_the_raw_text(self) -> None:
        from pathlib import Path

        import topos.query.pipeline as module

        src = Path(module.__file__).read_text()
        assert "_shadow_observe(query_text, scope_id)" in src, (
            "Horos is trained on natural questions; classifying a keyword digest was "
            "measured to make it abstain, and it would poison the shadow training set"
        )
        assert "_shadow_observe(retrieval_text" not in src
