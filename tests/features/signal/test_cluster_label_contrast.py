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
        terms = compute_distinguishing_terms(_corpus())
        assert set(terms[0]) == {"kayaking", "paddling", "river"}
        assert set(terms[1]) == {"mortgage", "refinance", "lender"}
        assert set(terms[2]) == {"chemotherapy", "oncologist", "scan"}
        # "personal"/"notes" are equally frequent everywhere: no contrast, dropped.
        for per_cluster in terms:
            assert "personal" not in per_cluster
            assert "notes" not in per_cluster

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
        prompt = build_label_prompt(_corpus()[0])
        assert "Sibling" not in prompt
        assert "Lens:" not in prompt

    def test_labeling_order_is_deterministic_and_biggest_first(self):
        corpus = [
            _cluster("tc_small", "memory", ["a"], member_count=3),
            _cluster("tc_big", "memory", ["b"], member_count=90),
            _cluster("tc_mid", "interests", ["c"], member_count=40),
        ]
        order = labeling_order(corpus)
        assert [corpus[i]["cluster_id"] for i in order] == ["tc_mid", "tc_big", "tc_small"]
        assert labeling_order(corpus) == order


class TestDimensionFraming:
    def test_two_dimensions_frame_identical_content_differently(self):
        previews = ["long run felt heavy", "slept badly before the race"]
        wellbeing = _cluster("tc_w", "wellbeing", previews)
        interests = _cluster("tc_i", "interests", previews)
        prompt_w = build_contrastive_label_prompt(wellbeing)
        prompt_i = build_contrastive_label_prompt(interests)
        assert prompt_w != prompt_i
        assert dimension_core_question("wellbeing") != dimension_core_question("interests")
        assert dimension_label_instruction("wellbeing") != dimension_label_instruction("interests")
        # Same items, same sample block — only the dimension lines moved.
        assert "long run felt heavy" in prompt_w and "long run felt heavy" in prompt_i

    def test_core_question_comes_from_the_definition_layer(self):
        """No second copy of dimension semantics in the labeler."""
        from topos.features.signal.dimension_definition_loader import get_definition

        for dimension in ("interests", "wellbeing", "memory", "relationships"):
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
        assert "personal" in build_contrastive_label_prompt(_corpus()[0]).lower()

    def test_a_generic_answer_earns_one_retry_and_the_better_answer_wins(self):
        answers = iter(["Personal Projects", "River Kayaking"])
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: next(answers), mode="on")
        assert corpus[0]["label"] == "River Kayaking"

    def test_a_stubbornly_generic_answer_still_beats_the_term_label(self):
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: "Personal Projects", mode="on")
        assert corpus[0]["label"] == "Personal Projects"


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
        """Two thirds of live answers came back in prose case ("alpha victor"),
        read side by side with term labels and entity names."""
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: "alpha victor", mode="on")
        assert corpus[0]["label"] == "Alpha Victor"

    def test_deliberate_casing_is_left_alone(self):
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(corpus, complete=lambda p: "iPhone Photography", mode="on")
        assert corpus[0]["label"] == "iPhone Photography"

    def test_the_isolated_arm_is_not_touched_up(self):
        corpus = _corpus()[:1]
        apply_llm_cluster_labels(
            corpus, complete=lambda p: "alpha victor", mode="on", contrastive=False
        )
        assert corpus[0]["label"] == "alpha victor"


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
