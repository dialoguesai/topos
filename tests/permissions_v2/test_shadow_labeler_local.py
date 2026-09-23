"""The local second labeler: the pinned rubric, the pinned model, and the policy's own verdict (C6).

Every text in this file is synthetic, written here. No real content reaches this suite, and no test in it opens a
socket: the transport is a stub, and the one exercise that does talk to the host's model is the dry run
(`scripts/permissions_beta/shadow_labeler_dry_run.py`), which is run by hand and not collected here.

  L1  the rubric and the model binding are pinned by bytes and by digest, not by prose
  L2  the vocabulary is closed and nothing is coerced: an answer outside it makes the release unresolved
  L3  the policy decides, not the model: the same labels against different policies give different verdicts
  L4  a labeler that cannot label, a transport that fails, a policy that is missing: unresolved, never agree
  L5  the seam: registering it makes a sample score to a verdict instead of `labeler_unavailable`
"""
from __future__ import annotations

import hashlib

import pytest

from topos.permissions_v2 import shadow_labeler_local as local
from topos.permissions_v2 import shadow_labelers, shadow_rescore
from topos.permissions_v2.contract import PolicyV2

pytestmark = [pytest.mark.p0]

# Synthetic units, in the campaign's own shape: one message text and the labels the rubric asks for.
UNITS = {
    "work_none": ("Moving the deploy to Thursday so the release notes land first.",
                  {"domains": ["work", "plans"], "sensitivity": "none"}),
    "hobby_none": ("Finished the second volume last night; the middle third drags.",
                   {"domains": ["hobbies"], "sensitivity": "none"}),
    "health_special": ("The scan came back clear, so the physio starts again on Monday.",
                       {"domains": ["health", "plans"], "sensitivity": "special"}),
    "family_personal": ("My sister is staying over this weekend before she flies out.",
                        {"domains": ["family", "plans"], "sensitivity": "personal"}),
}


class _Stub:
    """Answers with whatever the test pinned, per call, in order. Opens nothing."""

    def __init__(self, *answers):
        self.answers, self.seen = list(answers), []

    async def label(self, text):
        self.seen.append(text)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer


def _policy(*, release_predicate=None, effect="permit", rule_id="rule-A"):
    """A minimal p2a-v1 policy whose one rule carries the predicate under test."""
    true = {"kind": "all_of", "terms": []}
    return PolicyV2.parse({
        "version": "topos-policy/v2", "policy_version_id": "policy-1",
        "binding": {"environment_id": "beta", "node_id": "node-1", "resource_id": "resource-1",
                    "owner_id": "owner-1", "actor_id": "actor-1", "client_id": "client-1",
                    "grant_id": "grant-1", "assignment_id": "assignment-1"},
        "versions": {"vocabulary": "vocabulary-1", "capability": "permissions-beta/p2a-v1"},
        "validity": {"starts_at": 1000, "expires_at": 5000},
        "source_universe": {"universe_id": "sources-1", "revision": 1, "source_ids": ["source-A"]},
        "hard_constraints": {"owner_only": "deny", "unknown_classification": "withhold",
                             "unknown_lineage": "withhold", "cross_rule_derivation": "deny",
                             "capability_growth": "require_consent"},
        "rules": [{"rule_id": rule_id, "effect": effect,
                   "evidence_use": {"sources": {"kind": "only", "values": ["source-A"]}, "predicate": true,
                                    "purpose": "reading",
                                    "processors": {"kind": "only", "values": ["owner-engine-local"]},
                                    "new_records": "include_if_predicate"},
                   "release": {"predicate": release_predicate or true, "ceiling": "raw",
                               "forms": [{"family": "canonical_record", "operation": "read",
                                          "view_id": "canonical.message_disclosure.v1",
                                          "tables": ["conversation_messages"]}]}}],
        "evaluator": {"kind": "hard_rules", "version": "hard-rules/p2a-v1"}, "natural_language": None})


WORK_ONLY = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["work"]}
NOT_SPECIAL = {"kind": "not", "term": {"kind": "atom", "attribute": "sensitivity", "operator": "intersects",
                                       "values": ["special"]}}


def _records(*keys):
    return [{"record_id": "r.%s" % key, "canonical_table": "conversation_messages", "source_id": "source-A",
             "content": UNITS[key][0]} for key in keys]


def _answers(*keys):
    import json
    return [json.dumps(UNITS[key][1]) for key in keys]


@pytest.fixture(autouse=True)
def _clean():
    shadow_labelers.clear()
    yield
    shadow_labelers.clear()


# --------------------------------------------------------------------------- L1


def test_L1_the_rubric_is_pinned_by_bytes_and_carries_the_closed_vocabulary():
    raw = local.RUBRIC_PATH.read_bytes()
    assert len(raw) == local.RUBRIC_BYTES == 1702
    assert hashlib.sha256(raw).hexdigest() == local.RUBRIC_SHA256
    # The same bytes the gold set and the M1 machine-label track pin, so three readers cannot drift into scoring
    # different things while all claiming "the rubric".
    assert local.RUBRIC_SHA256 == "d03b3358357cc44f85976a5eb3e80840158b701fc4e30d0e4ec5a819c959701d"
    text = local.rubric()
    for value in local.DOMAINS + local.SENSITIVITIES:
        assert "`%s`" % value in text, value
    assert local.system_prompt().endswith(text) and "JSON only" in local.system_prompt()


def test_L1b_the_model_binding_is_the_campaigns_own():
    assert local.MODEL == "qwen3.5:9b-mlx" and local.FAMILY == "qwen"
    assert local.MODEL_REVISION == "203e30078279db51132b9e026ceb7bb21330e5b1af67ef190671b375c9770404"
    assert local.ORIGIN == "http://127.0.0.1:11434"
    assert local.LABELER_ID == "local-qwen3.5-9b-mlx"


def test_L1c_a_rubric_that_is_not_the_reviewed_one_refuses_rather_than_labels(monkeypatch, tmp_path):
    other = tmp_path / "rubric.md"
    other.write_text("## A rubric somebody edited\n")
    monkeypatch.setattr(local, "RUBRIC_PATH", other)
    with pytest.raises(ValueError):
        local.rubric()
    # And the whole labeler goes unresolved rather than scoring against it.
    assert local.LocalRubricLabeler(_Stub(*_answers("work_none"))).score(_records("work_none"), _policy()) == "unresolved"


# --------------------------------------------------------------------------- L2


@pytest.mark.parametrize("answer", [
    '{"domains": ["work"], "sensitivity": "none"}',
])
def test_L2_a_well_formed_answer_in_the_vocabulary_parses(answer):
    assert local.parse_labels(answer) == {"domains": ["work"], "sensitivity": "none"}


@pytest.mark.parametrize("answer", [
    '{"domains": ["work", "banking"], "sensitivity": "none"}',      # a domain the rubric does not define
    '{"domains": ["work"], "sensitivity": "sensitive"}',            # a sensitivity it does not define
    '{"domains": ["work"], "sensitivity": ["none"]}',               # the right value, the wrong shape
    '{"domains": "work", "sensitivity": "none"}',
    '{"domains": ["work", "work"], "sensitivity": "none"}',         # a duplicate is not a label set
    '{"domains": ["work"]}',                                        # a missing field
    '{"domains": ["work"], "sensitivity": "none", "why": "it is about the job"}',   # a rationale
    '{"domains": ["Work"], "sensitivity": "none"}',                 # case is not coerced
    "I would say this one is about work.",
    "", "null", "[]", "{}",
])
def test_L2b_an_answer_outside_the_vocabulary_is_refused_and_never_coerced(answer):
    assert local.parse_labels(answer) is None


def test_L2c_one_unlabelled_record_makes_the_whole_release_unresolved():
    """A release is released or withheld whole, so it is scored whole: dropping the record the model fumbled and
    scoring the rest would turn a model that misunderstood the task into a data point."""
    stub = _Stub(_answers("work_none")[0], '{"domains": ["astrology"], "sensitivity": "none"}')
    assert local.LocalRubricLabeler(stub).score(_records("work_none", "hobby_none"), _policy()) == "unresolved"
    assert len(stub.seen) == 2


# --------------------------------------------------------------------------- L3


def test_L3_the_policy_decides_not_the_model():
    """The same labels, from the same model answer, against two policies: two verdicts. The model is a classifier
    and the policy is the judge, which is what keeps a re-score a re-score and not a second opinion about rules."""
    records, answers = _records("work_none"), _answers("work_none")
    assert local.LocalRubricLabeler(_Stub(*answers)).score(records, _policy()) == "agree"
    assert local.LocalRubricLabeler(_Stub(*answers)).score(
        records, _policy(release_predicate=WORK_ONLY)) == "agree"
    # A grant that permits only hobbies would not have released this work message.
    hobbies = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["hobbies"]}
    assert local.LocalRubricLabeler(_Stub(*answers)).score(
        records, _policy(release_predicate=hobbies)) == "candidate_miss"


def test_L3b_a_special_category_release_under_a_not_special_rule_is_flagged():
    """The shape the audit exists to catch: the node released it, and the second labeler's labels say the policy
    would not have."""
    records, answers = _records("health_special"), _answers("health_special")
    assert local.LocalRubricLabeler(_Stub(*answers)).score(
        records, _policy(release_predicate=NOT_SPECIAL)) == "candidate_miss"
    # The same rule over a message the labeler calls `none` is an agreement.
    assert local.LocalRubricLabeler(_Stub(*_answers("hobby_none"))).score(
        _records("hobby_none"), _policy(release_predicate=NOT_SPECIAL)) == "agree"


def test_L3c_a_deny_rule_flags_and_a_flag_is_never_a_miss():
    records, answers = _records("family_personal"), _answers("family_personal")
    family = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["family"]}
    verdict = local.LocalRubricLabeler(_Stub(*answers)).score(records, _policy(release_predicate=family, effect="deny"))
    assert verdict == "candidate_miss"
    assert local.verdict_of("deny") == "candidate_miss" and "miss" not in {local.verdict_of("permit")}
    assert local.verdict_of("indeterminate") == "unresolved"


def test_L3d_authorship_and_subject_are_what_qualification_proved_not_what_a_model_guessed():
    """A model reading text cannot re-prove native owner authorship or an owner-only subject, so the re-score
    keeps them and re-derives only the two the rubric is about."""
    attributes = local.attributes_of({"domains": ["work"], "sensitivity": "none"})
    assert attributes == {"domain": ["work"], "actor_role": ["authored"], "subject": ["owner"],
                          "sensitivity": ["none"]}


def test_L3e_unknown_maps_to_unresolved_and_cannot_arise_from_these_labels():
    """`indeterminate` is in the mapping but unreachable from a re-score, and both halves matter.

    `attributes_of` fills all four attributes for every record, so `evaluate_predicate` never returns Unknown
    here: a re-score cannot produce `indeterminate` the way the release path can, where a missing classification
    does. The mapping still exists because a later attribute the labeler cannot fill would make it reachable, and
    the direction it must fall in then is `unresolved` -- never an agreement.
    """
    from topos.permissions_v2.contract import Atom, evaluate_predicate
    assert local.verdict_of("indeterminate") == "unresolved"
    attributes = local.attributes_of({"domains": [], "sensitivity": "none"})
    for name in ("domain", "actor_role", "subject", "sensitivity"):
        atom = Atom.parse({"kind": "atom", "attribute": name, "operator": "intersects", "values": ["anything"]})
        assert evaluate_predicate(atom, attributes) is not None, name
    # A policy whose permit rule simply does not match is a deny, which is a flag, not an agreement.
    hobbies = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["hobbies"]}
    assert local.policy_verdict(_policy(release_predicate=hobbies),
                                [{"domains": ["work"], "sensitivity": "none"}]) == "deny"


# --------------------------------------------------------------------------- L4


def test_L4_a_transport_that_fails_is_unresolved_never_agree():
    assert local.LocalRubricLabeler(_Stub(RuntimeError("the host is not there"))).score(
        _records("work_none"), _policy()) == "unresolved"


def test_L4b_a_missing_policy_or_an_empty_release_is_unresolved():
    assert local.LocalRubricLabeler(_Stub(*_answers("work_none"))).score(_records("work_none"), None) == "unresolved"
    assert local.LocalRubricLabeler(_Stub()).score([], _policy()) == "unresolved"
    assert local.LocalRubricLabeler(_Stub()).score([{"content": "   "}], _policy()) == "unresolved"
    assert local.LocalRubricLabeler(_Stub()).score([{"content": None}], _policy()) == "unresolved"


def test_L4c_the_installed_model_must_be_the_reviewed_revision():
    """A re-score by a model nobody reviewed is not a second opinion, and must not become one by a newer pull."""
    import asyncio

    class _Tags:
        def __init__(self, digest):
            self.digest = digest

        def raise_for_status(self):
            return None

        def json(self):
            return {"models": [{"name": local.MODEL, "digest": self.digest}]}

    class _Client:
        def __init__(self, digest):
            self.digest = digest

        async def get(self, url, **kwargs):
            return _Tags(self.digest)

    asyncio.run(local._PinnedTransport(_Client(local.MODEL_REVISION)).verify())
    with pytest.raises(ValueError):
        asyncio.run(local._PinnedTransport(_Client("0" * 64)).verify())


# --------------------------------------------------------------------------- L5


def test_L5_registering_it_turns_labeler_unavailable_into_a_verdict(monkeypatch):
    """The whole point of this file: before, every sample re-scored to a hole."""
    request = {"version": shadow_rescore.VERSION, "request_id": "req-1", "grant_id": "grant-1",
               "capability": "permissions-beta/p2a-v1", "output_sha256": "a" * 64, "labeler_mode": "local"}
    assert shadow_rescore.rescore(object(), request).reason == "labeler_unavailable"

    local.register(_Stub(*_answers("work_none")))
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, req: _records("work_none"))
    monkeypatch.setattr(shadow_rescore, "resolve_policy", lambda runtime, req: _policy())
    result = shadow_rescore.rescore(object(), request)
    assert result.verdict == "agree" and result.reason is None
    assert result.labeler == local.LABELER_ID and result.family == "qwen"


def test_L5b_the_same_family_refusal_still_bites_once_a_capability_has_a_primary_labeler(monkeypatch):
    """Today no capability has one, so nothing is refused for it. When one ships, this is the path."""
    request = {"version": shadow_rescore.VERSION, "request_id": "req-1", "grant_id": "grant-1",
               "capability": "permissions-beta/p2a-v1", "output_sha256": "a" * 64, "labeler_mode": "local"}
    local.register(_Stub(*_answers("work_none")))
    monkeypatch.setattr(shadow_rescore, "resolve_records", lambda runtime, req: _records("work_none"))
    monkeypatch.setattr(shadow_rescore, "resolve_policy", lambda runtime, req: _policy())
    monkeypatch.setattr(shadow_rescore, "primary_family_of", lambda capability: "qwen")
    result = shadow_rescore.rescore(object(), request)
    assert result.verdict == "unresolved" and result.reason == "same_family"
    monkeypatch.setattr(shadow_rescore, "primary_family_of", lambda capability: "llama")
    assert shadow_rescore.rescore(object(), request).verdict == "agree"


def test_L5c_the_seam_refuses_a_labeler_that_is_not_one():
    for broken in [object(), type("X", (), {"id": "x"})(), type("Y", (), {"id": "y", "family": "f"})()]:
        with pytest.raises(ValueError):
            shadow_labelers.register("local", broken)
    with pytest.raises(ValueError):
        shadow_labelers.register("nowhere", local.LocalRubricLabeler())
