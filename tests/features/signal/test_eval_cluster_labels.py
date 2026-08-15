"""The label-quality metrics that gate a labeler change (scripts/eval_cluster_labels.py).

`label_metrics` is the arbiter: a prompt change ships or is reverted on the numbers
it returns. It had no tests, which is how the live node came to report 152 of 152
labels distinct while eighteen of them read "Social Connections (…)" — the gate was
reading `distinct_labels`, the one number in here that cannot see a suffixed repeat.

These pin the numbers that replaced it, the guards around an empty corpus, and the
contract that `stacked_suffix_labels` is zero or the labeler regressed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "eval_cluster_labels.py"


def _load():
    spec = importlib.util.spec_from_file_location("eval_cluster_labels", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ecl = _load()


def _rows(*labels: str, dimension: str = "memory") -> list[dict]:
    """One row per cluster. `dimension` is required by the metric, not optional."""
    return [{"label": label, "dimension": dimension} for label in labels]


class TestBaseNamesAreTheDistinctnessMetric:
    """`distinct_labels` counts strings; `distinct_base_names` counts names.

    Suffixing makes a repeat unique without making it different, so the two
    diverge exactly when the labeler is papering over a collision.
    """

    def test_suffixing_keeps_every_label_distinct_and_no_base_name_new(self):
        metrics = ecl.label_metrics(
            _rows(
                "Social Connections",
                "Social Connections (travels)",
                "Social Connections (affirmation)",
            )
        )
        # The metric that lied: three strings, none repeated.
        assert metrics["distinct_labels"] == 3
        assert metrics["max_duplication"] == 1
        # The metric a reader agrees with: one name, three times.
        assert metrics["distinct_base_names"] == 1
        assert metrics["max_base_repeat"] == 3

    def test_genuinely_different_names_agree_on_both_counts(self):
        metrics = ecl.label_metrics(
            _rows("Marathon Training", "Mortgage Refinance", "River Kayaking")
        )
        assert metrics["distinct_labels"] == metrics["distinct_base_names"] == 3
        assert metrics["max_base_repeat"] == 1
        assert metrics["base_name_share"] == 1.0

    def test_base_name_share_is_the_fraction_a_reader_can_tell_apart(self):
        metrics = ecl.label_metrics(
            _rows("Alpha Notes", "Alpha Notes (two)", "Beta Plans", "Gamma Trips")
        )
        assert metrics["distinct_base_names"] == 3
        assert metrics["base_name_share"] == 0.75

    def test_case_and_punctuation_do_not_mint_a_new_base_name(self):
        """Normalization is shared with the labeler, so "AT&T" is one name."""
        metrics = ecl.label_metrics(_rows("AT&T Billing", "at&t billing", "ATT Billing"))
        assert metrics["distinct_base_names"] == 1
        assert metrics["max_base_repeat"] == 3


class TestStackedSuffixIsAlwaysARegression:
    """A label carrying two disambiguators is one the labeler must never mint.

    The live node grew "Social Connections (travels) (affirmation) (logically)"
    because each pass appended to what the previous one produced.
    """

    def test_a_suffix_on_a_suffix_is_counted(self):
        metrics = ecl.label_metrics(_rows("Social Connections (travels) (affirmation)"))
        assert metrics["stacked_suffix_labels"] == 1

    def test_three_deep_still_counts_once_per_label(self):
        metrics = ecl.label_metrics(
            _rows("Social Connections (travels) (affirmation) (logically)")
        )
        assert metrics["stacked_suffix_labels"] == 1

    def test_one_suffix_is_not_stacked(self):
        metrics = ecl.label_metrics(_rows("Social Connections (travels)"))
        assert metrics["stacked_suffix_labels"] == 0

    def test_a_clean_corpus_reports_zero(self):
        """The shipped contract: non-zero fails the gate, always."""
        metrics = ecl.label_metrics(_rows("Marathon Training", "Mortgage Refinance"))
        assert metrics["stacked_suffix_labels"] == 0


class TestSuffixedLabels:
    def test_only_labels_carrying_a_disambiguator_are_counted(self):
        metrics = ecl.label_metrics(
            _rows("River Kayaking", "River Kayaking (weekend)", "Mortgage Refinance")
        )
        assert metrics["suffixed_labels"] == 1

    def test_a_label_that_is_only_a_parenthetical_is_not_suffixed(self):
        """"(7)" has no base to have been suffixed onto — it keeps itself.

        Term labels arrive in this shape, so counting them as suffixed would
        blame the LLM labeler for the term labeler's output.
        """
        metrics = ecl.label_metrics(_rows("(7)"))
        assert metrics["suffixed_labels"] == 0
        assert metrics["distinct_base_names"] == 1


class TestDuplicatesAndDimensions:
    def test_an_exact_repeat_is_reported_with_its_cluster_count(self):
        metrics = ecl.label_metrics(_rows("Personal Projects", "Personal Projects", "Beta"))
        assert metrics["duplicated_labels"] == 1
        assert metrics["clusters_carrying_a_duplicate_label"] == 2

    def test_the_same_name_in_two_dimensions_is_flagged(self):
        rows = _rows("Personal Projects", dimension="memory") + _rows(
            "Personal Projects", dimension="wellbeing"
        )
        metrics = ecl.label_metrics(rows)
        assert metrics["labels_spanning_multiple_dimensions"] == 1
        assert metrics["cross_dimension_duplicates"] == [
            {"label": "personal projects", "dimensions": ["memory", "wellbeing"]}
        ]

    def test_a_repeat_inside_one_dimension_does_not_span_dimensions(self):
        metrics = ecl.label_metrics(_rows("Personal Projects", "Personal Projects"))
        assert metrics["duplicated_labels"] == 1
        assert metrics["labels_spanning_multiple_dimensions"] == 0


class TestBannedVocabularyIsMeasuredNotAssumed:
    """Every duplicate measured on the live node is built from the banned words,
    so a prompt that bans them and still returns them has fixed nothing."""

    def test_a_banned_word_is_counted_per_row_and_per_word(self):
        metrics = ecl.label_metrics(_rows("Personal Projects", "Private Notes", "Marathon Training"))
        assert metrics["labels_with_a_banned_word"] == 2
        assert metrics["banned_word_share"] == 0.667
        assert set(metrics["banned_words_used"]) >= {"personal", "private", "notes"}

    def test_a_clean_corpus_reports_none(self):
        metrics = ecl.label_metrics(_rows("Marathon Training", "Mortgage Refinance"))
        assert metrics["labels_with_a_banned_word"] == 0
        assert metrics["banned_word_share"] == 0.0
        assert metrics["banned_words_used"] == {}


class TestWordRule:
    """Duplication and informativeness pull opposite ways: bare proper nouns are
    unique, so distinctness can read perfect while the labels say less."""

    def test_a_bare_noun_is_outside_the_rule_and_counted_as_one_word(self):
        metrics = ecl.label_metrics(_rows("Austin", "Marathon Training"))
        assert metrics["single_word_labels"] == 1
        assert metrics["labels_within_word_rule"] == 1
        assert metrics["word_rule_share"] == 0.5

    def test_words_per_label_is_a_histogram(self):
        metrics = ecl.label_metrics(_rows("Austin", "Marathon Training", "Weekly Grocery Run"))
        assert metrics["words_per_label"] == {1: 1, 2: 1, 3: 1}


class TestTopBaseNames:
    def test_a_base_used_once_is_not_listed(self):
        metrics = ecl.label_metrics(_rows("Alpha Notes", "Alpha Notes (two)", "Beta Plans"))
        assert metrics["top_base_names"] == [{"base": "alpha notes", "count": 2}]

    def test_ranked_by_count_then_name(self):
        rows = _rows(
            "Beta Plans",
            "Beta Plans (x)",
            "Alpha Notes",
            "Alpha Notes (x)",
            "Alpha Notes (y)",
        )
        listed = ecl.label_metrics(rows)["top_base_names"]
        assert listed == [
            {"base": "alpha notes", "count": 3},
            {"base": "beta plans", "count": 2},
        ]

    def test_the_listing_is_capped(self):
        """The cap is applied to the ranking before the count>1 filter, so a
        corpus of repeats yields at most eight entries and never more."""
        rows = []
        for index in range(10):
            rows += _rows(f"Name{index} Cluster", f"Name{index} Cluster (dup)")
        listed = ecl.label_metrics(rows)["top_base_names"]
        assert len(listed) == 8
        assert all(entry["count"] == 2 for entry in listed)


class TestDegenerateInput:
    def test_an_empty_corpus_never_divides_by_zero(self):
        metrics = ecl.label_metrics([])
        assert metrics["clusters"] == 0
        assert metrics["max_duplication"] == 0
        assert metrics["max_base_repeat"] == 0
        assert metrics["base_name_share"] == 0.0
        assert metrics["banned_word_share"] == 0.0
        assert metrics["word_rule_share"] == 0.0
        assert metrics["top_base_names"] == []

    def test_a_label_that_normalizes_to_nothing_claims_no_base(self):
        """Punctuation is not a name, so it is counted as a cluster but never
        as a base — `base_name_share` is deliberately below 1.0 here."""
        metrics = ecl.label_metrics(_rows("---", "Marathon Training"))
        assert metrics["clusters"] == 2
        assert metrics["distinct_base_names"] == 1
        assert metrics["base_name_share"] == 0.5


class TestTheMeasuredRegressionReproduces:
    def test_all_labels_distinct_while_half_the_names_are_one_name(self):
        """The live node's shape in miniature: perfect `distinct_labels`, a map
        no reader can use. This is the assertion that would have caught it."""
        rows = _rows(*[f"Social Connections ({n})" for n in range(9)]) + _rows(
            "Marathon Training", "Mortgage Refinance", "River Kayaking"
        )
        metrics = ecl.label_metrics(rows)
        assert metrics["distinct_labels"] == len(rows)  # reads 100% distinct
        assert metrics["max_duplication"] == 1  # and 100% unduplicated
        assert metrics["distinct_base_names"] == 4  # while carrying four names
        assert metrics["max_base_repeat"] == 9
        assert metrics["base_name_share"] < 0.5
