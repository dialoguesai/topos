"""Contrastive, dimension-framed cluster labels.

Pins the two defects the isolated prompt had (measured on a live node: 152
clusters carrying 91 distinct labels, "personal projects" on fourteen of them
across five dimensions):

  1. no contrast — every cluster named from raw within-cluster frequency,
     blind to what its siblings are called;
  2. no lens — one generic instruction for every signal dimension.

Synthetic clusters only; nothing here opens a database.
"""

from __future__ import annotations

import pytest

from topos.features.signal.cluster_labels import (
    apply_llm_cluster_labels,
    build_contrastive_label_prompt,
    build_label_prompt,
    compute_distinguishing_terms,
    dimension_core_question,
    dimension_label_instruction,
    label_is_generic,
    label_is_urlish,
    label_base_name,
    label_is_wrong_length,
    member_bigrams,
    labeling_order,
    parse_label,
    sibling_labels,
)


def _cluster(
    cluster_id: str,
    dimension: str,
    previews: list[str],
    *,
    member_count: int | None = None,
    label: str = "term / label",
    centroid: list[float] | None = None,
) -> dict:
    cluster = {
        "cluster_id": cluster_id,
        "label": label,
        "dimension": dimension,
        "primary_dimension": dimension,
        "member_count": member_count if member_count is not None else len(previews),
        "label_terms": [],
        "members": [{"text_preview": p, "metadata": {}} for p in previews],
        "metadata": {},
    }
    if centroid is not None:
        cluster["centroid_vector"] = centroid
    return cluster


def _corpus() -> list[dict]:
    """Three clusters that share a dominant term and differ in one each."""
    shared = "personal notes personal notes personal"
    return [
        _cluster("tc_a", "memory", [f"{shared} kayaking river paddling"] * 4),
        _cluster("tc_b", "memory", [f"{shared} mortgage refinance lender"] * 4),
        _cluster("tc_c", "memory", [f"{shared} chemotherapy oncologist scan"] * 4),
    ]


class TestDistinguishingTerms:
    def test_shared_term_loses_to_the_term_only_this_cluster_has(self):
        distinctive = (
            {"kayaking", "river", "paddling"},
            {"mortgage", "refinance", "lender"},
            {"chemotherapy", "oncologist", "scan"},
        )
        terms = compute_distinguishing_terms(_corpus())
        for per_cluster, expected in zip(terms, distinctive):
            assert per_cluster, "a cluster must never lose all terms"
            words = {word for term in per_cluster for word in term.split()}
            assert words <= expected, per_cluster
        # "personal"/"notes" are equally frequent everywhere: no contrast. Not
        # as a term of their own, and not smuggled in as half of a phrase.
        for per_cluster in terms:
            assert not any("personal" in t or "notes" in t for t in per_cluster)

    def test_raw_frequency_would_have_picked_the_shared_term(self):
        """The old path fed exactly the term this one drops."""
        from topos.features.signal.topic_clustering import _member_term_counts

        counts = _member_term_counts(_corpus()[0]["members"])
        assert counts.most_common(1)[0][0] == "personal"

    def test_terms_survive_when_nothing_stands_out(self):
        identical = [_cluster(f"tc_{i}", "memory", ["same words every time"] * 3) for i in range(3)]
        terms = compute_distinguishing_terms(identical)
        assert all(per_cluster for per_cluster in terms), "a cluster must never lose all terms"

    def test_ranking_is_stable_and_index_aligned(self):
        corpus = _corpus()
        assert compute_distinguishing_terms(corpus) == compute_distinguishing_terms(corpus)
        assert len(compute_distinguishing_terms(corpus)) == len(corpus)


class TestPhraseTerms:
    """Terms reach the prompt as phrases, not shredded into bare tokens.

    The extractor was a `[a-z]{4,}` findall, so "google search" arrived as
    "google", "search". The prompt then asks for a NAME from a list of bare
    tokens and gets one bare token back — which is most of what the 2-5 word
    rule was fighting.
    """

    def _phrases(self, text: str) -> list[str]:
        from topos.features.signal.topic_clustering import _STOPWORDS

        return member_bigrams(text, _STOPWORDS)

    def test_adjacent_content_words_form_a_phrase(self):
        assert "google search" in self._phrases("opened google search today")

    def test_punctuation_ends_a_phrase(self):
        """A comma is a boundary — "google, search" is not a thing."""
        assert self._phrases("google, search results") == ["search results"]

    def test_a_stopword_or_short_word_never_joins_one(self):
        assert self._phrases("the search was slow") == []

    def test_a_banned_word_never_joins_one(self):
        """Pairing a banned word with a rare one would top the contrast rank."""
        assert self._phrases("personal kayaking river") == ["kayaking river"]

    def test_a_phrase_displaces_its_own_words(self):
        cluster = _cluster("tc_p", "interests", ["google search results page"] * 3)
        other = _cluster("tc_q", "memory", ["unrelated content entirely"] * 3)
        terms = compute_distinguishing_terms([cluster, other])[0]
        assert any(" " in term for term in terms), terms
        for term in terms:
            if " " in term:
                for word in term.split():
                    assert word not in terms, f"{word!r} is redundant beside {term!r}"

    def test_a_cluster_with_no_phrase_still_gets_terms(self):
        corpus = [
            _cluster("tc_1", "memory", ["kayaking, river, paddling"] * 3),
            _cluster("tc_2", "memory", ["mortgage, refinance, lender"] * 3),
        ]
        assert all(compute_distinguishing_terms(corpus))


class TestSiblingContext:
    def test_sibling_labels_appear_in_the_prompt(self):
        corpus = _corpus()
        terms = compute_distinguishing_terms(corpus)
        siblings = sibling_labels(0, corpus, terms, {1: "Mortgage Refinance", 2: "Cancer Care"})
        prompt = build_contrastive_label_prompt(
            corpus[0], distinguishing_terms=terms[0], siblings=siblings
        )
        assert "Mortgage Refinance" in prompt
        assert "Cancer Care" in prompt
        assert "must be clearly different" in prompt

    def test_assigned_label_wins_over_the_stale_term_label(self):
        corpus = _corpus()
        terms = compute_distinguishing_terms(corpus)
        assert "Mortgage Refinance" in sibling_labels(0, corpus, terms, {1: "Mortgage Refinance"})

    def test_same_dimension_siblings_rank_first(self):
        corpus = _corpus()
        corpus[1]["dimension"] = corpus[1]["primary_dimension"] = "wellbeing"
        corpus[1]["label"] = "Other Dimension"
        corpus[2]["label"] = "Same Dimension"
        terms = compute_distinguishing_terms(corpus)
        assert sibling_labels(0, corpus, terms, {}, limit=1) == ["Same Dimension"]

    def test_isolated_prompt_carries_no_sibling_context(self):
        """The control arm must stay the prompt that shipped, or the eval is rigged."""
        prompt = build_label_prompt(_corpus()[0])
        assert "already used by neighbouring clusters" not in prompt
        assert "QUESTION THIS DIMENSION ANSWERS" not in prompt
        assert "Frequent terms:" in prompt

    def test_labeling_order_is_deterministic_and_biggest_first(self):
        corpus = [
            _cluster("tc_small", "memory", ["a"], member_count=3),
            _cluster("tc_big", "memory", ["b"], member_count=90),
            _cluster("tc_mid", "interests", ["c"], member_count=40),
        ]
        order = labeling_order(corpus)
        assert [corpus[i]["cluster_id"] for i in order] == ["tc_mid", "tc_big", "tc_small"]
        assert labeling_order(corpus) == order


def _defined_dimensions() -> list[str]:
    """Dimensions this checkout can actually resolve a `core_question` for.

    Asked of the loader, never hard-coded: `interests`, `wellbeing`, `memory`,
    `places` and `resources` were untracked working-tree files when this landed,
    so naming them directly made these tests pass locally and fail in CI, which
    only ever sees committed definitions.
    """
    from topos.features.signal.cluster_labels import _DIMENSION_LABEL_INSTRUCTIONS
    from topos.features.signal.dimension_definition_loader import list_definition_ids

    return sorted(set(list_definition_ids()) & set(_DIMENSION_LABEL_INSTRUCTIONS))


class TestDimensionFraming:
    def test_two_dimensions_frame_identical_content_differently(self):
        defined = _defined_dimensions()
        assert len(defined) >= 2, f"need two defined dimensions, got {defined}"
        first, second = defined[0], defined[1]
        previews = ["long run felt heavy", "slept badly before the race"]
        prompt_a = build_contrastive_label_prompt(_cluster("tc_a2", first, previews))
        prompt_b = build_contrastive_label_prompt(_cluster("tc_b2", second, previews))
        assert prompt_a != prompt_b
        assert dimension_core_question(first) != dimension_core_question(second)
        assert dimension_label_instruction(first) != dimension_label_instruction(second)
        # Same items, same sample block — only the dimension lines moved.
        assert "long run felt heavy" in prompt_a and "long run felt heavy" in prompt_b

    def test_core_question_comes_from_the_definition_layer(self):
        """No second copy of dimension semantics in the labeler."""
        from topos.features.signal.dimension_definition_loader import get_definition

        defined = _defined_dimensions()
        assert defined, "no dimension definition carries a labeling directive"
        for dimension in defined:
            declared = str(get_definition(dimension)["core_question"])
            assert dimension_core_question(dimension) == declared
            assert declared in build_contrastive_label_prompt(
                _cluster("tc_x", dimension, ["some item"])
            )

    def test_every_clustered_dimension_has_its_own_directive(self):
        """The directive is a labeler concern and lives in one map — but every
        dimension clusters produce must have one, or the lens does nothing."""
        instructions = {
            dim: dimension_label_instruction(dim)
            for dim in ("interests", "relationships", "wellbeing", "memory")
        }
        assert len(set(instructions.values())) == len(instructions), instructions
        generic = dimension_label_instruction("")
        assert generic not in instructions.values()

    def test_unknown_facet_gets_a_generic_frame(self):
        assert dimension_core_question("network_bridge") == dimension_core_question("")
        assert dimension_label_instruction("network_bridge") == dimension_label_instruction("")
        # A facet with no definition still produces the same prompt shape.
        prompt = build_contrastive_label_prompt(_cluster("tc_b", "network_bridge", ["item"]))
        assert "QUESTION THIS DIMENSION ANSWERS:" in prompt


class TestGenericLabelGate:
    @pytest.mark.parametrize(
        "label",
        ["Personal Projects", "Private Notes", "Personal Productivity", "my personal notes"],
    )
    def test_the_observed_duplicates_are_flagged_generic(self, label):
        assert label_is_generic(label)

    @pytest.mark.parametrize(
        "label", ["Marathon Training", "Texas Workforce Commission", "Mortgage Refinance"]
    )
    def test_specific_names_pass(self, label):
        assert not label_is_generic(label)

    def test_the_banned_vocabulary_reaches_the_prompt(self):
        """Every word the live duplicates are built from is named in the rule."""
        prompt = build_contrastive_label_prompt(_corpus()[0])
        assert (
            "- Never use: personal, private, general, misc, various, notes, stuff,"
            " activities." in prompt
        )

    def test_a_generic_answer_earns_one_retry_and_the_better_answer_wins(self):
        answers = iter(["Personal Projects", "River Kayaking"])
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: next(answers), mode="on")
        assert corpus[0]["label"] == "River Kayaking"

    def test_a_stubbornly_generic_answer_still_beats_the_term_label(self):
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: "Personal Projects", mode="on")
        assert corpus[0]["label"] == "Personal Projects"


class TestLabelLengthGate:
    """The prompt's own first rule, enforced after parsing.

    Measured on the first full relabel of the live node: the contrastive prompt
    obeyed "2-5 words" on 46% of answers against the isolated prompt's 90%,
    single-word answers going 15 -> 81 ("Met", "America", "Friend"). Bare proper
    nouns are unique, so duplication looked fixed while the labels said less.
    """

    @pytest.mark.parametrize("label", ["Austin", "Met", "America", "Internet"])
    def test_a_bare_noun_is_wrong_length(self, label):
        assert label_is_wrong_length(label)

    @pytest.mark.parametrize(
        "label", ["Austin Trips", "Marathon Training", "Weekly Grocery Run"]
    )
    def test_a_qualified_name_passes(self, label):
        assert not label_is_wrong_length(label)

    def test_an_over_long_answer_is_also_gated(self):
        assert label_is_wrong_length("Notes About The Six Word Limit")

    def test_the_rule_reaches_the_prompt(self):
        prompt = build_contrastive_label_prompt(_corpus()[0])
        assert "never one word on its own" in prompt

    def test_one_word_earns_a_retry_and_the_qualified_answer_wins(self):
        answers = iter(["Austin", "Austin Kayaking Trips"])
        prompts: list[str] = []

        def _complete(prompt: str) -> str:
            prompts.append(prompt)
            return next(answers)

        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=_complete, mode="on")
        assert corpus[0]["label"] == "Austin Kayaking Trips"
        assert len(prompts) == 2
        assert '"Austin" is one word' in prompts[1]

    def test_a_stubbornly_terse_answer_still_beats_the_term_label(self):
        """One bare noun still says more than "term / label" — it ships."""
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: "Austin", mode="on")
        assert corpus[0]["label"] == "Austin"

    def test_length_never_outranks_distinctness(self):
        """A distinct bare noun beats a duplicate phrase: length ranks last."""
        answers = iter(["Mortgage Refinance", "Kayaking", "Mortgage Refinance"])
        corpus = _corpus()[:2]
        apply_llm_cluster_labels(corpus, complete=lambda p: next(answers), mode="on")
        assert sorted(c["label"] for c in corpus) == ["Kayaking", "Mortgage Refinance"]


class TestLinkGate:
    """A label is published — into top_topics facts and every topics surface.

    Distinguishing terms come from page titles and links, so the model does
    answer with one: a measured relabel of the live node returned
    "maps.app.goo.gl/vNHAeBdiJRphhWdt9?g_st=i&utm_campaign=ac-im" as a label.
    """

    @pytest.mark.parametrize(
        "label",
        [
            "maps.app.goo.gl/vNHAeBdiJRphhWdt9?g_st=i",
            "https://github.com/foo",
            "dialoguesai/control-plane",
            "www.nytimes.com",
            "@jonnyjohnson1",
            "example.com",
        ],
    )
    def test_links_are_flagged(self, label):
        assert label_is_urlish(label)

    @pytest.mark.parametrize("label", ["Marathon Training", "AT&T Billing", "R&D Planning"])
    def test_ordinary_names_are_not(self, label):
        assert not label_is_urlish(label)

    def test_a_link_answer_earns_a_retry_and_the_clean_answer_wins(self):
        answers = iter(["maps.app.goo.gl/vNHAe?g_st=i", "Denver Music Venues"])
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: next(answers), mode="on")
        assert corpus[0]["label"] == "Denver Music Venues"

    def test_a_link_that_survives_its_retry_is_dropped_not_published(self):
        corpus = _corpus()[:1]
        count = apply_llm_cluster_labels(
            corpus, complete=lambda p: "https://maps.app.goo.gl/x", mode="on"
        )
        assert count == 0
        assert corpus[0]["label"] == "term / label", "a link must never reach a label"

    def test_the_rule_is_in_the_prompt(self):
        assert "Never a URL" in build_contrastive_label_prompt(_corpus()[0])


class TestPresentation:
    def test_an_all_lowercase_answer_is_title_cased(self):
        """Two thirds of live answers came back in prose case ("rami malek"),
        read side by side with term labels and entity names."""
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: "rami malek", mode="on")
        assert corpus[0]["label"] == "Rami Malek"

    def test_deliberate_casing_is_left_alone(self):
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: "iPhone Photography", mode="on")
        assert corpus[0]["label"] == "iPhone Photography"

    def test_the_isolated_arm_is_not_touched_up(self):
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(
            corpus, complete=lambda p: "rami malek", mode="on", contrastive=False
        )
        assert corpus[0]["label"] == "rami malek"


class TestPromptDeterminism:
    def test_same_cluster_same_prompt(self):
        corpus = _corpus()
        terms = compute_distinguishing_terms(corpus)
        siblings = sibling_labels(0, corpus, terms, {})
        first = build_contrastive_label_prompt(
            corpus[0], distinguishing_terms=terms[0], siblings=siblings
        )
        second = build_contrastive_label_prompt(
            corpus[0], distinguishing_terms=terms[0], siblings=siblings
        )
        assert first == second

    def test_prompts_do_not_depend_on_key_insertion_order(self):
        cluster = _corpus()[0]
        shuffled = {k: cluster[k] for k in reversed(list(cluster))}
        assert build_contrastive_label_prompt(shuffled) == build_contrastive_label_prompt(cluster)


class TestApplyContrastive:
    def test_duplicate_label_triggers_one_retry_then_a_deterministic_suffix(self):
        prompts: list[str] = []

        def _always_same(prompt: str) -> str:
            prompts.append(prompt)
            return "Personal Projects"

        corpus = _corpus()
        count = apply_llm_cluster_labels(corpus, complete=_always_same, mode="on")
        labels = [c["label"] for c in corpus]
        assert count == 3
        assert len(set(labels)) == 3, labels
        assert "Personal Projects" in labels
        # one call per cluster + one retry each (generic once, then taken twice)
        assert len(prompts) == 6
        assert any("already taken" in p for p in prompts)
        assert any("too generic" in p for p in prompts)

    def test_an_assigned_label_never_lands_on_a_term_label_still_in_use(self):
        """Term labels carry the same "(n)" suffix shape the disambiguator mints.

        A live relabel put an assigned "Hello (7)" beside a cluster that had
        been skipped and kept the term label "hello (7)" — two clusters, one
        name, from two different labelers.
        """
        corpus = _corpus()
        # tc_b is unlabelable (the model refuses it) and keeps this term label,
        # which is exactly what the disambiguator would mint for tc_a.
        corpus[1]["label"] = "Hello (4)"
        answers = {id(corpus[1]): "As an AI, I cannot name this"}

        def _complete(prompt: str) -> str:
            for cluster in corpus:
                if (cluster["members"][0]["text_preview"][:40] in prompt) and id(cluster) in answers:
                    return answers[id(cluster)]
            return "hello"

        apply_llm_cluster_labels(corpus, complete=_complete, mode="on")
        labels = [c["label"] for c in corpus]
        assert corpus[1]["label"] == "Hello (4)", "the unlabelable cluster keeps its term label"
        assert len(set(label.lower() for label in labels)) == 3, labels

    def test_retry_answer_is_used_when_it_is_distinct(self):
        answers = iter(["Personal Projects", "Personal Projects", "River Kayaking"])

        corpus = _corpus()[:2]
        apply_llm_cluster_labels(corpus, complete=lambda p: next(answers), mode="on")
        assert sorted(c["label"] for c in corpus) == ["Personal Projects", "River Kayaking"]

    def test_labels_are_applied_in_labeling_order_not_list_order(self):
        seen: list[str] = []

        def _echo(prompt: str) -> str:
            seen.append(prompt)
            return f"Name {len(seen)}"

        corpus = [
            _cluster("tc_1", "wellbeing", ["sleep tracker"], member_count=5),
            _cluster("tc_2", "interests", ["kayak gear"], member_count=50),
        ]
        apply_llm_cluster_labels(corpus, complete=_echo, mode="on")
        assert corpus[1]["label"] == "Name 1"  # interests sorts first
        assert corpus[0]["label"] == "Name 2"

    def test_isolated_arm_keeps_its_duplicates(self):
        """The control arm must be what shipped, dedup help included — without
        this the relabel eval would credit the new prompt with the suffixing."""
        corpus = _corpus()
        count = apply_llm_cluster_labels(
            corpus, complete=lambda p: "Personal Projects", mode="on", contrastive=False
        )
        assert count == 3
        assert [c["label"] for c in corpus] == ["Personal Projects"] * 3

    def test_contrastive_off_uses_the_isolated_prompt(self):
        prompts: list[str] = []
        corpus = _corpus()
        apply_llm_cluster_labels(
            corpus,
            complete=lambda p: prompts.append(p) or "Some Label",
            mode="on",
            contrastive=False,
        )
        assert all("Lens:" not in p for p in prompts)
        assert all("Frequent terms:" in p for p in prompts)

    def test_env_switch_restores_the_isolated_prompt(self, monkeypatch):
        monkeypatch.setenv("TOPOS_CLUSTER_LABEL_CONTRAST", "off")
        prompts: list[str] = []
        apply_llm_cluster_labels(
            _corpus(), complete=lambda p: prompts.append(p) or "Some Label", mode="on"
        )
        assert prompts and all("Lens:" not in p for p in prompts)

    def test_mode_off_never_calls_the_model(self):
        calls: list[str] = []
        assert apply_llm_cluster_labels(_corpus(), complete=calls.append, mode="off") == 0
        assert not calls

    def test_model_failure_keeps_every_term_label(self):
        def _boom(prompt: str) -> str:
            raise RuntimeError("ollama down")

        corpus = _corpus()
        assert apply_llm_cluster_labels(corpus, complete=_boom, mode="on") == 0
        assert [c["label"] for c in corpus] == ["term / label"] * 3

    def test_junk_output_keeps_the_term_label(self):
        corpus = _corpus()
        assert (
            apply_llm_cluster_labels(
                corpus, complete=lambda p: "As an AI, I cannot name this", mode="on"
            )
            == 0
        )
        assert [c["label"] for c in corpus] == ["term / label"] * 3

    def test_metadata_records_the_style_and_keeps_the_term_label(self):
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: "River Kayaking", mode="on")
        assert corpus[0]["metadata"]["term_label"] == "term / label"
        assert corpus[0]["metadata"]["label_style"] == "contrastive"


class TestRelabelExistingClusters:
    """The relabel-only path: labels move, the partition does not."""

    @pytest.fixture()
    def conn(self):
        import json
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.executescript(
            """
            CREATE TABLE topic_clusters (
                cluster_id TEXT PRIMARY KEY, label TEXT, dimension TEXT,
                member_count INTEGER, source_mix_json TEXT DEFAULT '{}',
                label_terms_json TEXT DEFAULT '[]', centroid_preview TEXT,
                metadata_json TEXT DEFAULT '{}', updated_at TEXT
            );
            CREATE TABLE topic_cluster_members (
                member_id TEXT PRIMARY KEY, cluster_id TEXT, record_id TEXT,
                source_id TEXT, record_type TEXT, text_preview TEXT,
                weight REAL DEFAULT 1.0, metadata_json TEXT DEFAULT '{}'
            );
            """
        )
        for index, cluster in enumerate(_corpus()):
            conn.execute(
                "INSERT INTO topic_clusters (cluster_id, label, dimension, member_count,"
                " metadata_json) VALUES (?, ?, ?, ?, ?)",
                (
                    cluster["cluster_id"],
                    cluster["label"],
                    cluster["dimension"],
                    cluster["member_count"],
                    json.dumps({"primary_dimension": cluster["dimension"]}),
                ),
            )
            for member_index, member in enumerate(cluster["members"]):
                conn.execute(
                    "INSERT INTO topic_cluster_members (member_id, cluster_id, record_id,"
                    " source_id, record_type, text_preview) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        f"m_{index}_{member_index}",
                        cluster["cluster_id"],
                        f"r_{index}_{member_index}",
                        "chatgpt_file_ingestion",
                        "ai_chat_message",
                        member["text_preview"],
                    ),
                )
        conn.commit()
        yield conn
        conn.close()

    def test_members_load_with_their_clusters(self, conn):
        from topos.features.signal.topic_clustering import load_clusters_with_members

        clusters = load_clusters_with_members(conn)
        assert [c["cluster_id"] for c in clusters] == ["tc_a", "tc_b", "tc_c"]
        assert all(len(c["members"]) == 4 for c in clusters)
        assert load_clusters_with_members(conn, dimensions=["wellbeing"]) == []

    def test_relabel_writes_distinct_labels_and_keeps_membership(self, conn):
        from topos.features.signal.topic_clustering import relabel_existing_clusters

        before_members = conn.execute(
            "SELECT cluster_id, record_id FROM topic_cluster_members ORDER BY member_id"
        ).fetchall()
        result = relabel_existing_clusters(conn, complete=lambda p: "Personal Projects", mode="on")
        assert result["relabeled"] == 3
        labels = [row[0] for row in conn.execute("SELECT label FROM topic_clusters")]
        assert len(set(labels)) == 3
        assert (
            conn.execute(
                "SELECT cluster_id, record_id FROM topic_cluster_members ORDER BY member_id"
            ).fetchall()
            == before_members
        )

    def test_dry_run_leaves_the_database_alone(self, conn):
        from topos.features.signal.topic_clustering import relabel_existing_clusters

        result = relabel_existing_clusters(
            conn, complete=lambda p: "River Kayaking", mode="on", dry_run=True
        )
        assert result["dry_run"] is True
        assert [row[0] for row in conn.execute("SELECT label FROM topic_clusters")] == [
            "term / label"
        ] * 3

    def test_model_down_writes_nothing(self, conn):
        from topos.features.signal.topic_clustering import relabel_existing_clusters

        def _boom(prompt: str) -> str:
            raise RuntimeError("ollama down")

        result = relabel_existing_clusters(conn, complete=_boom, mode="on")
        assert result["relabeled"] == 0 and result["changed"] == 0
        assert [row[0] for row in conn.execute("SELECT label FROM topic_clusters")] == [
            "term / label"
        ] * 3


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Label: Marathon Training", "Marathon Training"),
        ("Name - Race Prep", "Race Prep"),
        ("Marathon Training", "Marathon Training"),
        ("", None),
    ],
)
def test_parse_label_strips_a_restated_prefix(raw, expected):
    assert parse_label(raw) == expected


def test_term_labels_are_disambiguated_against_each_other_not_just_counted():
    """Two clusters of the same size that produce the same terms.

    The old suffix was member_count, so both became "hello (7)" — a live node
    carried that pair, and it is the only duplicate the LLM labeler cannot
    resolve (it inherits the term label whenever the model declines a cluster).
    """
    from topos.features.signal.topic_clustering import _disambiguate_labels

    clusters = [
        {"label": "hello", "member_count": 7, "label_terms": []},
        {"label": "hello", "member_count": 7, "label_terms": []},
        {"label": "hello", "member_count": 7, "label_terms": []},
    ]
    _disambiguate_labels(clusters)
    labels = [c["label"] for c in clusters]
    assert len(set(labels)) == 3, labels
    assert labels[0] == "hello"


class TestBaseNameGate:
    """Distinctness is what a reader can tell apart, not what `set()` can.

    The live node shipped 152 clusters under 152 distinct labels and 100 base
    names, because a repeat was silently suffixed instead of renamed, and the
    suffixes then stacked across passes.
    """

    def test_base_name_strips_every_stacked_suffix(self):
        from topos.features.signal.cluster_labels import label_base_name

        assert label_base_name("Social Connections") == "social connections"
        assert label_base_name("Social Connections (travels)") == "social connections"
        assert (
            label_base_name("Social Connections (travels) (affirmation) (logically)")
            == "social connections"
        )
        # A name that is only a parenthetical keeps itself rather than vanishing.
        assert label_base_name("(7)") == "7"

    def test_a_repeat_that_arrives_suffixed_is_still_a_repeat(self):
        """The model is shown its siblings' names and copies them back, suffix
        and all — so the collision check has to see through the suffix.

        Before this, "River Kayaking Trips (weekend)" was a brand-new string
        and shipped unchallenged; now it earns the retry a bare repeat earns,
        and the model gets a chance to actually rename.
        """
        prompts: list[str] = []
        answers = iter(["River Kayaking Trips", "River Kayaking Trips (weekend)"])

        def _echo_sibling(prompt: str) -> str:
            prompts.append(prompt)
            try:
                return next(answers)
            except StopIteration:
                return "Mortgage Refinance Search"

        corpus = _corpus()
        apply_llm_cluster_labels(corpus, complete=_echo_sibling, mode="on")
        labels = [c["label"] for c in corpus]
        assert any("already taken" in p for p in prompts), prompts
        # The echo was rejected, so the rename the retry produced is what shipped.
        assert label_base_name(labels[1]) != label_base_name(labels[0]), labels

    def test_a_suffix_is_never_stacked_on_a_suffix(self):
        def _always_suffixed(prompt: str) -> str:
            return "River Kayaking (weekend)"

        corpus = _corpus()
        apply_llm_cluster_labels(corpus, complete=_always_suffixed, mode="on")
        labels = [c["label"] for c in corpus]
        for label in labels:
            assert label.count("(") <= 1, labels

    def test_stats_report_base_names_not_just_distinct_labels(self):
        stats: dict = {}

        def _always_same(prompt: str) -> str:
            return "River Kayaking Trips"

        corpus = _corpus()
        apply_llm_cluster_labels(corpus, complete=_always_same, mode="on", stats=stats)
        labels = [c["label"] for c in corpus]
        # Suffixing keeps every label distinct — that is the metric that lied.
        assert len(set(labels)) == len(labels), labels
        # The base name is what a reader actually compares, and it does not.
        assert stats["distinct_base_names"] < len(labels)
        assert stats["max_base_repeat"] > 1
