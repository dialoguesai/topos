"""Topic cluster labels as a black-hole leak surface.

The label used to be term soup ("https / good / here") and named nobody. The
contrastive labeler names the subject, and on a live node 33 of 152 labels came
back as an entity name — six people, four places. That turns a row nobody had
enumerated as a leak surface into a name-string one, with two properties that
make it worse than the artifacts already covered:

  * it is stored ONCE and served to every caller, so there is no per-reader
    filter that can save it — the name must not be minted;
  * `write_top_topics_signal_facts` mints `top_topics` FROM it, so closing the
    derived object without cleaning the row means the next cluster pass writes
    the withdrawn name straight back.

Two enforcement points, one for each direction of time: the producer refuses to
name a protected entity (labels minted from here on), and the rebuild withdraws
the ones minted before the entity was protected.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore, blackholed_name_terms
from topos.features.lifecycle.blackhole_rebuild import rebuild_for_blackhole
from topos.features.signal.cluster_labels import (
    apply_llm_cluster_labels,
    build_contrastive_label_prompt,
    label_mentions_protected,
)
from tests.evals.privacy.blackhole.corpus import (
    BH_CANONICAL,
    BH_ID,
    OK_CANONICAL,
    build_blackhole_corpus,
)

pytestmark = [pytest.mark.bhlr, pytest.mark.private]


@pytest.fixture()
def corpus(tmp_path):
    c = build_blackhole_corpus(str(tmp_path / "cluster_labels.db"))
    yield c
    c.conn.close()


def _cluster(label: str = "term / label", *, preview: str = "dinner and a walk") -> dict:
    return {
        "cluster_id": "tc_x",
        "label": label,
        "dimension": "relationships",
        "primary_dimension": "relationships",
        "member_count": 6,
        "label_terms": [],
        "members": [{"text_preview": preview, "metadata": {}}] * 6,
        "metadata": {},
    }


# ------------------------------------------------------ producer refusal


def test_the_labeler_refuses_to_mint_a_protected_name(corpus):
    terms = blackholed_name_terms(corpus.conn)
    cluster = _cluster()
    count = apply_llm_cluster_labels(
        [cluster],
        complete=lambda prompt: BH_CANONICAL,
        mode="on",
        protected_terms=terms,
    )
    assert count == 0
    assert cluster["label"] == "term / label", "the term label is the fallback, not the name"


def test_a_clean_retry_answer_is_accepted(corpus):
    """The refusal is a gate on the name, not on naming."""
    terms = blackholed_name_terms(corpus.conn)
    answers = iter([BH_CANONICAL, "Sunday Dinners"])
    cluster = _cluster()
    count = apply_llm_cluster_labels(
        [cluster], complete=lambda p: next(answers), mode="on", protected_terms=terms
    )
    assert count == 1
    assert cluster["label"] == "Sunday Dinners"


def test_the_retry_prompt_never_echoes_the_protected_name(corpus):
    """Quoting the rejected answer back would put the name in the next prompt."""
    terms = blackholed_name_terms(corpus.conn)
    prompts: list[str] = []

    def _complete(prompt: str) -> str:
        prompts.append(prompt)
        return BH_CANONICAL

    apply_llm_cluster_labels(
        [_cluster()], complete=_complete, mode="on", protected_terms=terms
    )
    assert len(prompts) == 2, "the refusal should still buy one retry"
    assert BH_CANONICAL not in prompts[1]
    assert "must not appear" in prompts[1]


def test_an_alias_is_refused_too(corpus):
    terms = blackholed_name_terms(corpus.conn)
    cluster = _cluster()
    apply_llm_cluster_labels(
        [cluster],
        complete=lambda p: f"Weekends With {corpus.tokens['alias']}",
        mode="on",
        protected_terms=terms,
    )
    assert cluster["label"] == "term / label"


def test_the_control_entity_is_still_nameable(corpus):
    """A filter that refuses every name is worthless — the control must pass."""
    terms = blackholed_name_terms(corpus.conn)
    cluster = _cluster()
    count = apply_llm_cluster_labels(
        [cluster], complete=lambda p: OK_CANONICAL, mode="on", protected_terms=terms
    )
    assert count == 1 and cluster["label"] == OK_CANONICAL


def test_no_protected_terms_means_no_behaviour_change(corpus):
    cluster = _cluster()
    count = apply_llm_cluster_labels(
        [cluster], complete=lambda p: BH_CANONICAL, mode="on", protected_terms=set()
    )
    assert count == 1 and cluster["label"] == BH_CANONICAL


def test_the_prompt_itself_never_carries_the_protected_name(corpus):
    """The gate is on the answer; the prompt must not seed the name either.

    Member previews are the model's evidence and they DO carry the name — this
    pins the honest limit: the labeler reads protected text locally (it always
    has) and only the published artifact is gated.
    """
    prompt = build_contrastive_label_prompt(_cluster(preview=f"dinner with {BH_CANONICAL}"))
    assert BH_CANONICAL in prompt, "sample items are the evidence; local reading is allowed"
    assert label_mentions_protected(BH_CANONICAL, blackholed_name_terms(corpus.conn))


# --------------------------------------------------- withdrawal of the past


def _labels(conn: sqlite3.Connection) -> dict:
    return {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT cluster_id, label, centroid_preview FROM topic_clusters")
    }


def test_rebuild_withdraws_a_label_minted_before_protection(corpus):
    before = _labels(corpus.conn)
    assert BH_CANONICAL in before["tc-bh"][0], "corpus must plant the leak"

    report = rebuild_for_blackhole(corpus.conn, BH_ID)
    assert report.cluster_labels_withdrawn == 1

    after = _labels(corpus.conn)
    assert BH_CANONICAL not in after["tc-bh"][0]
    assert corpus.tokens["cluster_label"] not in after["tc-bh"][0]
    assert corpus.tokens["cluster_preview"] not in (after["tc-bh"][1] or "")


def test_rebuild_leaves_the_control_cluster_alone(corpus):
    rebuild_for_blackhole(corpus.conn, BH_ID)
    after = _labels(corpus.conn)
    assert after["tc-ok"][0] == f"{OK_CANONICAL} standup"


def test_the_cluster_and_its_members_survive_the_withdrawal(corpus):
    """Withdrawal takes the prose, not the owner's structure."""
    rebuild_for_blackhole(corpus.conn, BH_ID)
    row = corpus.conn.execute(
        "SELECT member_count FROM topic_clusters WHERE cluster_id='tc-bh'"
    ).fetchone()
    assert row and row[0] == 4
    members = corpus.conn.execute(
        "SELECT COUNT(*) FROM topic_cluster_members WHERE cluster_id='tc-bh'"
    ).fetchone()[0]
    assert members == 1


def test_a_term_label_that_also_names_the_entity_is_not_used_as_the_fallback(corpus):
    """Term labels are built from member text, so they can carry the name too."""
    corpus.conn.execute(
        "UPDATE topic_clusters SET metadata_json=? WHERE cluster_id='tc-bh'",
        (json.dumps({"term_label": f"{BH_CANONICAL} / dinner"}),),
    )
    corpus.conn.commit()
    rebuild_for_blackhole(corpus.conn, BH_ID)
    label = _labels(corpus.conn)["tc-bh"][0]
    assert BH_CANONICAL not in label
    assert label == "topic cluster"


def test_rebuild_is_idempotent_for_cluster_labels(corpus):
    first = rebuild_for_blackhole(corpus.conn, BH_ID)
    second = rebuild_for_blackhole(corpus.conn, BH_ID)
    assert first.cluster_labels_withdrawn == 1
    assert second.cluster_labels_withdrawn == 0


def test_withdrawal_survives_the_next_top_topics_mint(corpus):
    """The row is the producer: a clean row is what keeps the fact clean.

    Closing the top_topics object alone would be undone by the next cluster
    pass, which re-mints the fact straight from topic_clusters.label.
    """
    rebuild_for_blackhole(corpus.conn, BH_ID)
    label = _labels(corpus.conn)["tc-bh"][0]
    store = BlackholeStore(corpus.conn)
    assert not any(term in label.lower() for term in store.blackholed_name_terms())


# ------------------------------------------------- raw excerpts on the row


def test_rebuild_blanks_member_excerpts_quoting_the_entity(corpus):
    report = rebuild_for_blackhole(corpus.conn, BH_ID)
    assert report.as_dict()["cluster_member_previews_blanked"] == 1
    rows = dict(
        corpus.conn.execute(
            "SELECT cluster_id, text_preview FROM topic_cluster_members"
        ).fetchall()
    )
    assert BH_CANONICAL not in (rows["tc-bh"] or "")
    assert OK_CANONICAL in rows["tc-ok"], "the control excerpt must survive"


def test_a_recompute_does_not_re_mint_the_excerpts(corpus):
    """The producer scrub is what makes the withdrawal outlive the next pass."""
    from topos.features.signal.topic_clustering import _scrub_protected_previews

    terms = blackholed_name_terms(corpus.conn)
    clusters = [
        {
            "cluster_id": "tc-fresh",
            "centroid_preview": f"coffee with {BH_CANONICAL}",
            "members": [
                {"text_preview": f"dinner with {BH_CANONICAL}"},
                {"text_preview": f"standup with {OK_CANONICAL}"},
            ],
        }
    ]
    assert _scrub_protected_previews(clusters, terms) == 2
    assert clusters[0]["centroid_preview"] == ""
    assert clusters[0]["members"][0]["text_preview"] == ""
    assert OK_CANONICAL in clusters[0]["members"][1]["text_preview"]


def test_scrubbing_is_a_no_op_without_black_holes():
    from topos.features.signal.topic_clustering import _scrub_protected_previews

    clusters = [{"centroid_preview": "coffee", "members": [{"text_preview": "dinner"}]}]
    assert _scrub_protected_previews(clusters, set()) == 0
    assert clusters[0]["centroid_preview"] == "coffee"
