"""Scoring against an external gold file; synthetic observations, no model or database.

The interval checks reproduce Newcombe's published worked examples so the method
named in a report is the method computed, not a look-alike.
"""
import hashlib
import json

import pytest

from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.experiments.scoring import (ARM_A, ARM_B, CASE_FILE_VERSION, GOLD_KEYS, METHODS, Observation,
    evaluator_view, load_cases, nearest_rank, paired_gap, score, square_and_add, wilson)

CANARY = "gold-rationale-canary-contoso"


def case(case_id, release, group=None, **inputs):
    return {"case_id": case_id, "input": {"message": "Synthetic Fabrikam note about " + case_id, **inputs},
            "gold": {"expected_release": release, "paraphrase_group": group, "rationale": CANARY + "-" + case_id}}


def document(cases=None, **changes):
    return {"version": CASE_FILE_VERSION, "synthetic": True, "held_out": True,
            "cases": cases or [case("case-1", "permit", "group-p"), case("case-2", "permit", "group-p"),
                               case("case-3", "permit"), case("case-4", "withhold", "group-w"),
                               case("case-5", "withhold", "group-w")], **changes}


def write(tmp_path, value, name="cases.json"):
    path = tmp_path / name
    path.write_bytes(value if isinstance(value, bytes) else json.dumps(value, indent=1).encode())
    return path


def test_the_report_pins_the_exact_bytes_and_held_out_comes_from_the_file(tmp_path):
    path = write(tmp_path, document())
    loaded = load_cases(path)
    assert loaded.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert (loaded.held_out, loaded.synthetic, len(loaded.cases)) == (True, True, 5)
    # Same labels, different bytes: a different pin, so a report names its exact file.
    other = load_cases(write(tmp_path, json.dumps(document()).encode(), "compact.json"))
    assert other.sha256 != loaded.sha256 and other.cases == loaded.cases
    assert load_cases(write(tmp_path, document(held_out=False), "public.json")).held_out is False


@pytest.mark.parametrize("damage", ["gold_key_in_input", "nested_gold_key", "rationale_in_input", "duplicate_case",
                                    "unknown_gold_field", "unknown_release", "missing_held_out", "version", "float_input",
                                    "mixed_paraphrase_gold"])
def test_case_files_that_could_leak_or_blur_gold_are_refused(tmp_path, damage):
    value = document()
    first = value["cases"][0]
    if damage == "gold_key_in_input": first["input"]["expected_release"] = "permit"
    elif damage == "mixed_paraphrase_gold": value["cases"][3]["gold"]["paraphrase_group"] = "group-p"
    elif damage == "nested_gold_key": first["input"]["context"] = [{"paraphrase_group": "group-p"}]
    elif damage == "rationale_in_input": first["input"]["message"] += " " + first["gold"]["rationale"]
    elif damage == "duplicate_case": value["cases"].append(dict(first))
    elif damage == "unknown_gold_field": first["gold"]["hint"] = "permit"
    elif damage == "unknown_release": first["gold"]["expected_release"] = "deny"
    elif damage == "missing_held_out": value.pop("held_out")
    elif damage == "version": value["version"] = "topos-experiment-cases/v0"
    else: first["input"]["event_offset"] = 1.5
    with pytest.raises(PolicyError):
        load_cases(write(tmp_path, value))


def test_evaluator_views_carry_no_gold_key_or_value(tmp_path):
    loaded = load_cases(write(tmp_path, document()))
    views = [evaluator_view(item) for item in loaded.cases]
    serialized = json.dumps(views)
    assert all(set(view) == {"case_id", "input"} for view in views)
    for key in GOLD_KEYS:
        assert '"%s"' % key not in serialized
    assert CANARY not in serialized and "withhold" not in serialized
    # A view is a copy: an evaluator mutating it cannot reach the loaded case.
    views[0]["input"]["message"] = "changed"
    assert loaded.cases[0].input["message"] != "changed"


def observations(verdicts, latency=None):
    return [Observation(case_id, arm, verdict, (latency or {}).get((case_id, arm), 0.5))
            for (case_id, arm), verdict in verdicts.items()]


def test_per_arm_metrics_and_the_paired_recall_gap(tmp_path):
    loaded = load_cases(write(tmp_path, document()))
    verdicts = {("case-1", ARM_A): "permit", ("case-2", ARM_A): "permit", ("case-3", ARM_A): "permit",
                ("case-4", ARM_A): "deny", ("case-5", ARM_A): "deny",
                ("case-1", ARM_B): "permit", ("case-2", ARM_B): "deny", ("case-3", ARM_B): "indeterminate",
                ("case-4", ARM_B): "permit", ("case-5", ARM_B): "deny"}
    latency = {("case-1", ARM_B): 1.0, ("case-2", ARM_B): 2.0, ("case-3", ARM_B): 3.0,
               ("case-4", ARM_B): 4.0, ("case-5", ARM_B): 10.0}
    report = score(loaded, observations(verdicts, latency))
    a, b = report["arms"][ARM_A], report["arms"][ARM_B]
    assert (report["cases_sha256"], report["held_out"], report["gold_positives"], report["gold_withholds"]) == (
        loaded.sha256, True, 3, 2)
    assert (a["permits"], a["prohibited_permits"], a["recall"], a["unresolved"]) == (3, 0, 1.0, 0)
    assert (b["permits"], b["prohibited_permits"], b["prohibited_permit_case_ids"], b["unresolved"]) == (2, 1, ["case-4"], 1)
    assert b["recall"] == round(1 / 3, 6) and b["recall_wilson95"] == [round(value, 6) for value in wilson(1, 3)]
    assert b["latency_seconds"] == {"p50": 3.0, "p95": 10.0} and a["latency_seconds"] == {"p50": 0.5, "p95": 0.5}
    assert a["paraphrase_stability"] == {"groups": 2, "stable_groups": 2, "rate": 1.0, "unstable_groups": []}
    assert b["paraphrase_stability"] == {"groups": 2, "stable_groups": 0, "rate": 0.0, "unstable_groups": ["group-p", "group-w"]}
    gap = report["recall_gap_b_minus_a_pp"]
    assert gap["paired_positives"] == {"both": 1, "b_only": 0, "a_only": 2, "neither": 0}
    point, (low, high) = paired_gap(1, 0, 2, 0)
    assert gap["point"] == round(point * 100, 6) == round(-200 / 3, 6)
    assert gap["ci95"] == [round(low * 100, 6), round(high * 100, 6)] and gap["ci95"][0] < gap["point"] < gap["ci95"][1]
    assert report["methods"] == METHODS and "wilson" in METHODS["recall_interval"]
    # The report is metadata: no input text or rationale reaches it.
    assert "Fabrikam" not in json.dumps(report) and CANARY not in json.dumps(report)


def test_an_unresolved_withhold_is_unresolved_never_a_prohibited_permit(tmp_path):
    loaded = load_cases(write(tmp_path, document()))
    verdicts = {(item.case_id, arm): "indeterminate" if item.gold.expected_release == "withhold" else "permit"
                for item in loaded.cases for arm in (ARM_A, ARM_B)}
    arms = score(loaded, observations(verdicts))["arms"]
    for arm in (ARM_A, ARM_B):
        assert (arms[arm]["permits"], arms[arm]["prohibited_permits"], arms[arm]["unresolved"]) == (3, 0, 2)


def test_wilson_and_newcombe_reproduce_published_values():
    assert [round(value, 4) for value in wilson(5, 10)] == [0.2366, 0.7634]
    assert [round(value, 4) for value in wilson(0, 10)] == [0.0, 0.2775]
    # Newcombe (1998), independent proportions, method 10: 56/70 - 48/80.
    low, high = square_and_add(56 / 70, wilson(56, 70), 48 / 80, wilson(48, 80))
    assert (round(low, 4), round(high, 4)) == (0.0524, 0.3339)
    # Newcombe (1998), paired proportions, method 10: a=36, b=12, c=2, d=0.
    point, (low, high) = paired_gap(36, 12, 2, 0)
    assert (round(point, 4), round(low, 4), round(high, 4)) == (0.2, 0.0569, 0.3404)
    assert paired_gap(0, 0, 0, 0) is None
    point, (low, high) = paired_gap(0, 4, 0, 0)
    assert point == 1.0 and 0 < low < high == 1.0


def test_nearest_rank_percentiles():
    assert nearest_rank([], 50) is None
    assert nearest_rank([3.0], 95) == 3.0
    assert nearest_rank([5, 1, 4, 2, 3], 50) == 3 and nearest_rank(list(range(1, 21)), 95) == 19


@pytest.mark.parametrize("damage", ["missing", "duplicate", "unknown_case", "unknown_arm", "bad_verdict", "negative_latency",
                                    "nan_latency", "not_an_observation"])
def test_scoring_refuses_incomplete_or_malformed_observations(tmp_path, damage):
    loaded = load_cases(write(tmp_path, document()))
    items = observations({(item.case_id, arm): "deny" for item in loaded.cases for arm in (ARM_A, ARM_B)})
    if damage == "missing": items.pop()
    elif damage == "duplicate": items.append(items[0])
    elif damage == "unknown_case": items[0] = Observation("other", ARM_A, "deny", 0.1)
    elif damage == "unknown_arm": items[0] = Observation(items[0].case_id, "arm_c", "deny", 0.1)
    elif damage == "bad_verdict": items[0] = Observation(items[0].case_id, ARM_A, "allow", 0.1)
    elif damage == "negative_latency": items[0] = Observation(items[0].case_id, ARM_A, "deny", -1.0)
    elif damage == "nan_latency": items[0] = Observation(items[0].case_id, ARM_A, "deny", float("nan"))
    else: items[0] = {"case_id": items[0].case_id, "arm": ARM_A, "verdict": "deny", "latency_seconds": 0.1}
    with pytest.raises(PolicyError):
        score(loaded, items)
