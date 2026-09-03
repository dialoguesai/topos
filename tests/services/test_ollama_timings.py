"""The load / prefill / decode split, and the gate that keeps it.

protects: time-to-first-token is measurable. Until 2026-09-03 it was not —
Ollama reported four duration fields on the same JSON object we already parsed
for token counts, and all four were discarded at every parse site.

Two separate defects are pinned here:

1. The split itself. Without it, "the model was slow" has three
   indistinguishable causes and every argument about latency is a live probe.
2. The ttfb sentinel. `ttfb_ms` used to default to `duration_ms`, so 120 of
   135 live home-chat rows had them exactly equal — a duration percentile
   wearing a TTFB label. An honest NULL is a gap you can see.

Numbers below are from live probes on this machine (Qwen3.8-27B, 2026-09-03):
cold load 5,694ms vs warm 4.7ms; prefill 65-90 tok/s; decode 3.4-10.1 tok/s.
"""

from __future__ import annotations

from topos.services.llm.openai import _ms, _ollama_timings

#: A real terminating chunk, shape verbatim from /api/generate (ns everywhere).
COLD_27B = {
    "done": True,
    "total_duration": 7_409_900_000,
    "load_duration": 5_694_700_000,
    "prompt_eval_count": 55,
    "prompt_eval_duration": 996_300_000,
    "eval_count": 8,
    "eval_duration": 715_900_000,
}

WARM_LONG_PROMPT = {
    "done": True,
    "total_duration": 63_558_000_000,
    "load_duration": 8_000_000,
    "prompt_eval_count": 5_780,
    "prompt_eval_duration": 61_754_200_000,
    "eval_count": 16,
    "eval_duration": 1_700_500_000,
}

CACHE_HIT = {
    "done": True,
    "total_duration": 8_900_000_000,
    "load_duration": 4_700_000,
    "prompt_eval_count": 5_621,  # NOTE: still the FULL prompt on a hit
    "prompt_eval_duration": 319_900_000,
    "eval_count": 80,
    "eval_duration": 8_600_400_000,
}


class TestUnitConversion:
    def test_nanoseconds_become_milliseconds(self):
        assert _ms(5_694_700_000) == 5695
        assert _ms(0) == 0

    def test_junk_is_zero_not_an_exception(self):
        # Telemetry must never be the thing that breaks a generation.
        assert _ms(None) == 0
        assert _ms("nonsense") == 0
        assert _ms({}) == 0


class TestTheSplit:
    def test_cold_load_is_separated_from_generation(self):
        t = _ollama_timings(COLD_27B)
        assert t["load_ms"] == 5695
        assert t["prefill_ms"] == 996
        assert t["decode_ms"] == 716
        assert t["cold"] is True

    def test_a_warm_model_is_not_reported_as_cold(self):
        # Measured warm load is single-digit ms; the threshold sits in the
        # empty middle of a ~544x gap, not near either edge.
        assert _ollama_timings(WARM_LONG_PROMPT)["cold"] is False
        assert _ollama_timings(CACHE_HIT)["cold"] is False

    def test_prefill_dominates_a_long_prompt(self):
        # THE finding this whole lane exists to make visible: 97% of that turn
        # was reading the prompt, and none of it was loading the model.
        t = _ollama_timings(WARM_LONG_PROMPT)
        assert t["prefill_ms"] == 61754
        assert t["load_ms"] == 8
        assert t["prefill_ms"] > 30 * t["decode_ms"]

    def test_rates_are_computed_where_they_are_meaningful(self):
        t = _ollama_timings(WARM_LONG_PROMPT)
        assert 90 < t["prefill_tok_s"] < 100  # measured 93.6
        assert 9 < t["decode_tok_s"] < 10  # measured 9.4

    def test_a_zero_duration_yields_no_rate_rather_than_a_division_error(self):
        t = _ollama_timings({"prompt_eval_count": 100, "prompt_eval_duration": 0})
        assert "prefill_tok_s" not in t

    def test_missing_fields_degrade_to_zeros(self):
        t = _ollama_timings({"done": True})
        assert t["load_ms"] == 0 and t["prefill_ms"] == 0 and t["decode_ms"] == 0
        assert t["cold"] is False


class TestCacheHitDetection:
    def test_a_hit_is_inferred_from_the_rate_not_from_a_zero(self):
        # A hit is NOT prompt_eval_duration == 0: measured hits ran 74-191ms on
        # a 3B and ~6,300ms on a 27B. And prompt_eval_count still reports the
        # full prompt, so the duration is the only signal there is.
        t = _ollama_timings(CACHE_HIT)
        assert t["prefill_cached"] is True
        assert t["prefill_tok_s"] > 500

    def test_an_ordinary_prefill_is_not_flagged_as_cached(self):
        assert "prefill_cached" not in _ollama_timings(WARM_LONG_PROMPT)
        assert "prefill_cached" not in _ollama_timings(COLD_27B)


class TestReversionGate:
    """If these fail, someone removed the measurement. Do not delete them."""

    def test_every_ollama_parse_site_still_captures_the_split(self):
        # The split was specified once in a plan doc and never built. It is
        # cheap to drop again during a refactor, and nothing else would notice.
        import inspect

        from topos.engine.backends import ollama as backend
        from topos.services.llm import openai as svc

        src = inspect.getsource(svc)
        assert src.count("_ollama_timings(") >= 3, (
            "a parse site stopped capturing timings — the streaming and "
            "non-streaming paths must both call _ollama_timings"
        )
        assert "_ollama_timings" in inspect.getsource(backend), (
            "the derivation lane stopped capturing timings; it is the lane "
            "that evicts the chat model, so its cold loads must stay visible"
        )

    def test_timings_ride_under_usage_so_the_browser_receives_them(self):
        # The browser whitelists exactly output/model/usage. Nesting under
        # usage is what lets timings reach the UI with NO control-plane change.
        import inspect

        from topos.services.llm import openai as svc

        src = inspect.getsource(svc)
        assert '"timings": timings' in src
        assert '{**usage, "timings": timings}' in src

    def test_ttfb_is_never_inferred_from_duration(self):
        # The sentinel that poisoned 120 of 135 rows. If this reappears, every
        # TTFB number in the product silently becomes a duration number.
        import inspect

        from topos.engine import engine, usage_observation

        assert "ttfb_ms=duration_ms" not in inspect.getsource(engine), (
            "Engine.run is non-streaming and has no first-token moment; "
            "passing duration as ttfb manufactures the sentinel again"
        )
        obs = inspect.getsource(usage_observation)
        assert "else resolved_duration" not in obs, (
            "ttfb_ms must stay RECORDED OR ABSENT, never defaulted to duration"
        )
