"""The retrieval-text contract: what every door distils an ask into before the engine sees it.

protects: SYS-query I1 (answer when you should) at the routines entrance — a scheduled report must
reach the same evidence the same question reaches in home chat.

`retrieval_text` is the SUBJECT of a request. The engine reads it in two places: the rare-token
gate (`_query_tokens(needle_text or query_text)`) and the semantic query (`needle_text` replaces
`query_text` for the vector lane when it differs). Two production entrances build it — home chat
and the scheduled-routines path in the control plane — and until 2026-09-06 only home chat
removed output-shape words and style spans, so a scheduled "write a two paragraph summary of my
goals in the voice of a calm coach" asked the node about paragraphs, voices and coaches
(33 of 49 catalogue sentences distilled differently at the two doors).

The engine runs neither distiller. It owns the contract they are both tested against
(`topos/protocol/retrieval_text_contract.json`: vocabularies, algorithm, reference vectors) for
the same reason it owns the narrowing vocabulary — three codebases, one meaning — and it pins the
relations that are the engine's own:

  * everything a door strips as output shape is exempt from the engine's abstention gate, so
    stripping changes the semantic query but never the veto verdict relative to an entrance
    that does not strip (the "no new veto" rule, made checkable);
  * the door list stays NARROWER than the gate list: the gate also exempts generic nouns
    ("voice", "thread", "board", "summary") that are ordinary subjects — "voice memos", "board
    meeting" — and syncing the door up to the gate would remove subject words, which is the
    over-correction the product rule names;
  * the vectors are honest: none carries the intent placeholder, none exceeds the cap, some
    exercise the per-section parts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from topos.query.retrieval import _OUTPUT_SHAPE_TOKENS

pytestmark = [pytest.mark.check("C-quality-retrieval-text-parity")]

CONTRACT_PATH = Path(__file__).resolve().parents[2] / "topos" / "protocol" / "retrieval_text_contract.json"
CONTRACT = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
VECTORS = CONTRACT["vectors"]

#: Gate-only generic nouns that must NEVER become door words: each is an ordinary subject in
#: owner language. Named here so nobody "syncs the door up to the gate".
GENERIC_NOUNS_THE_DOOR_MUST_KEEP = ("voice", "thread", "board", "summary", "draft", "style", "post", "analysis")


class TestThePublishedContractIsWellFormed:
    def test_version_and_sections(self) -> None:
        assert CONTRACT["version"] == 1
        for key in ("algorithm", "stop_words", "output_shape_words", "style_span", "vectors",
                    "query_text_max", "intent_keyword_cap", "intent_token_pattern"):
            assert key in CONTRACT, key
        assert CONTRACT["query_text_max"] == 500 and CONTRACT["intent_keyword_cap"] == 14

    @pytest.mark.parametrize("key", ["stop_words", "output_shape_words"])
    def test_vocabularies_are_lowercase_ascii_and_deduplicated(self, key: str) -> None:
        words = CONTRACT[key]
        assert words and len(words) == len(set(words)), key
        assert all(re.fullmatch(r"[a-z0-9]+", w) for w in words), key

    def test_every_door_word_is_reachable(self) -> None:
        """Reachable = a token the intent pattern can produce. A door word the tokeniser can
        never emit is dead weight that reads as protection. (`stop_words` is deliberately NOT
        checked: it carries two-letter members — "do", "is", "my", "me" — inherited from the
        intent digest, where the same list also filters untokenised text. Dead in this pipeline,
        harmless, and identical at both entrances, which is what this contract is about.)"""
        token = re.compile(CONTRACT["intent_token_pattern"])
        dead = [w for w in CONTRACT["output_shape_words"] if not token.fullmatch(w)]
        assert not dead, f"door words the tokeniser can never emit: {dead}"

    def test_vector_ids_are_unique_and_each_has_an_input_and_an_output(self) -> None:
        ids = [v["id"] for v in VECTORS]
        assert len(ids) == len(set(ids))
        assert all(v.get("input") and "retrieval_text" in v for v in VECTORS)

    def test_the_style_span_patterns_compile_unchanged_under_re(self) -> None:
        """A port compiles these strings; it does not retype them. If a construct that only
        JavaScript accepts ever lands here, this is where it is caught."""
        for source in CONTRACT["style_span"]["patterns"]:
            re.compile(source, re.IGNORECASE)
        assert CONTRACT["style_span"]["stops"], "a span with no stop word eats the subject"


class TestEveryDoorWordIsExemptAtTheGate:
    def test_output_shape_words_are_a_subset_of_the_gate(self) -> None:
        missing = sorted(set(CONTRACT["output_shape_words"]) - set(_OUTPUT_SHAPE_TOKENS))
        assert not missing, f"stripped at the door but able to veto at the gate: {missing}"


class TestTheDoorStaysNarrowerThanTheGate:
    @pytest.mark.parametrize("noun", GENERIC_NOUNS_THE_DOOR_MUST_KEEP)
    def test_generic_shape_nouns_are_not_door_words(self, noun: str) -> None:
        assert noun in _OUTPUT_SHAPE_TOKENS, "the premise: the gate exempts it"
        assert noun not in CONTRACT["output_shape_words"], (
            f"{noun!r} is a subject in owner language ('voice memos', 'board meeting'); the door must keep it"
        )


class TestTheVectorsAreHonest:
    def test_some_vector_exercises_retrieval_parts(self) -> None:
        assert any(len(v.get("retrieval_parts") or []) >= 2 for v in VECTORS)

    def test_no_output_carries_the_intent_placeholder(self) -> None:
        for v in VECTORS:
            outputs = [v["retrieval_text"], *(v.get("retrieval_parts") or [])]
            assert not any("user query" in o for o in outputs), v["id"]

    def test_no_output_starts_with_punctuation(self) -> None:
        """A needle that opens with a stray comma is a distillation that mangled the sentence
        (the app emitted `, what did I work on ? this week` before 2026-09-06). Two legitimate
        exceptions, both the documented raw fallback: a whole message with nothing distillable
        goes through untouched, and a one-keyword SECTION falls back to its own text with the
        enumerator the owner typed ("- how did I sleep last week") — identical at both
        entrances, which is the property this file protects."""
        enumerator = re.compile(r"^\s*(?:\(\d{1,2}\)|\d{1,2}[).:]|#\d{1,2}\b|[-*\u2022]\s|section\s+\d{1,2}\b)", re.IGNORECASE)
        for v in VECTORS:
            if v["retrieval_text"] and v["retrieval_text"] != v["input"]:
                assert v["retrieval_text"][0].isalnum(), (v["id"], v["retrieval_text"])
            for part in v.get("retrieval_parts") or []:
                assert part and (part[0].isalnum() or enumerator.match(part)), (v["id"], part)

    def test_every_output_fits_the_cap(self) -> None:
        cap = CONTRACT["query_text_max"]
        for v in VECTORS:
            assert len(v["retrieval_text"]) <= cap, v["id"]

    def test_no_vector_carries_a_door_word_unless_it_fell_back_to_the_raw_sentence(self) -> None:
        shape = set(CONTRACT["output_shape_words"])
        for v in VECTORS:
            if v["retrieval_text"] == v["input"]:
                continue  # nothing distillable: the sentence went through untouched
            leaked = shape & set(v["retrieval_text"].lower().split())
            assert not leaked, (v["id"], sorted(leaked))
