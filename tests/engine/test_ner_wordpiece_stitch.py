"""Wordpiece stitching for NER output (the ##-junk fix).

dslim/bert-base-NER + aggregation_strategy="simple" splits entities at subword
boundaries whenever the model tags a word's pieces inconsistently: "Jonny" →
"Jon" (kept, WRONG partial surface) + "##ny" (junk, filtered downstream). 5.2%
of live raw NER hits carried "##". The stitcher repairs output against the
ORIGINAL text using char offsets: merge contiguous ## fragments, snap spans to
word boundaries, rebuild surfaces from the source text.
"""

from __future__ import annotations

from topos.engine.backends.huggingface import _stitch_wordpiece_entities


def _ent(word, group, score, start, end):
    return {"word": word, "entity_group": group, "score": score, "start": start, "end": end}


def test_contiguous_fragments_merge_into_full_word():
    text = "Jonny went to Williamsburg"
    raw = [
        _ent("Jon", "PER", 0.99, 0, 3),
        _ent("##ny", "ORG", 0.40, 3, 5),  # mis-tagged subword fragment
    ]
    out = _stitch_wordpiece_entities(text, raw)
    assert len(out) == 1
    assert out[0]["word"] == "Jonny"
    assert out[0]["entity_group"] == "PER"  # first fragment's type wins
    assert (out[0]["start"], out[0]["end"]) == (0, 5)


def test_fragment_chain_reassembles():
    text = "Visited Williamsburg today"
    raw = [
        _ent("Wil", "LOC", 0.95, 8, 11),
        _ent("##liams", "LOC", 0.90, 11, 16),
        _ent("##burg", "LOC", 0.85, 16, 20),
    ]
    out = _stitch_wordpiece_entities(text, raw)
    assert len(out) == 1
    assert out[0]["word"] == "Williamsburg"
    assert (out[0]["start"], out[0]["end"]) == (8, 20)


def test_standalone_fragment_snaps_to_word():
    text = "Jonny shipped it"
    raw = [_ent("##ny", "PER", 0.7, 3, 5)]  # orphan fragment, no anchor
    out = _stitch_wordpiece_entities(text, raw)
    assert len(out) == 1
    assert out[0]["word"] == "Jonny"


def test_partial_surface_snaps_to_word_boundary():
    """The silent-truncation case: 'Jon' kept but the word is 'Jonny'."""
    text = "Jonny and Marcus"
    raw = [_ent("Jon", "PER", 0.99, 0, 3)]
    out = _stitch_wordpiece_entities(text, raw)
    assert out[0]["word"] == "Jonny"
    assert (out[0]["start"], out[0]["end"]) == (0, 5)


def test_clean_entities_pass_through():
    text = "Jon lives in Austin's house"
    raw = [
        _ent("Jon", "PER", 0.99, 0, 3),     # followed by space → stays Jon
        _ent("Austin", "LOC", 0.97, 13, 19),  # followed by apostrophe → stays
    ]
    out = _stitch_wordpiece_entities(text, raw)
    assert [e["word"] for e in out] == ["Jon", "Austin"]


def test_duplicate_spans_dedupe_keeping_best_score():
    text = "Jonny here"
    raw = [
        _ent("Jon", "PER", 0.99, 0, 3),
        _ent("Jonny", "PER", 0.80, 0, 5),  # overlapping detection of same word
    ]
    out = _stitch_wordpiece_entities(text, raw)
    assert len(out) == 1
    assert out[0]["word"] == "Jonny"
    assert out[0]["score"] == 0.99  # best fragment score survives


def test_defensive_on_missing_offsets():
    text = "whatever"
    raw = [{"word": "thing", "entity_group": "MISC", "score": 0.9}]
    out = _stitch_wordpiece_entities(text, raw)
    assert out == raw  # no offsets → untouched
