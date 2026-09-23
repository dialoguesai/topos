"""Per-arm scoring of an offline shadow run against externally held gold labels.

Gold lives only in a case file the caller supplies. Its exact bytes are hashed so
a report names the labels it was scored against, and `held_out` is read from that
file rather than asserted by a runner. An evaluator receives `evaluator_view` of a
case, which has no gold field, and labels meet verdicts only in `score`, after
every arm has returned. Nothing here decides, releases or calls a model.

Intervals: per-arm recall uses the 95% Wilson score interval. The B-minus-A recall
gap is paired (both arms see the same gold positives), so it uses Newcombe's
method 10 for paired proportions: the two Wilson intervals combined by square and
add with the phi correlation correction, without continuity correction.
Latency percentiles are nearest-rank.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from ..canonical import MAX_BYTES, PolicyError, canonical_bytes, parse_json
from ..contract import Identifier, StrictModel
from .models import Text

CASE_FILE_VERSION = "topos-experiment-cases/v1"
METRICS_VERSION = "topos-experiment-scoring/v1"
ARM_A, ARM_B = "rules_v2", "semantic_v1"
VERDICTS = ("permit", "deny", "indeterminate")
# Every gold field name, so an input that smuggles one in is refused at load.
GOLD_KEYS = frozenset({"gold", "expected_release", "paraphrase_group", "rationale", "held_out", "synthetic"})
Z95 = 1.959963984540054
METHODS = {"recall_interval": "wilson_score_95", "recall_gap_interval": "newcombe_1998_paired_method_10_no_continuity_correction",
           "latency_percentile": "nearest_rank", "paraphrase_stability": "all_variants_same_verdict_per_group"}


def _keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()


class Gold(StrictModel):
    expected_release: Literal["permit", "withhold"]
    paraphrase_group: Identifier | None
    rationale: Text | None


class Case(StrictModel):
    case_id: Identifier
    input: dict[str, Any]
    gold: Gold

    @model_validator(mode="after")
    def input_carries_no_gold(self):
        if _keys(self.input) & GOLD_KEYS:
            raise ValueError("gold field in evaluator input")
        rationale = self.gold.rationale
        if rationale and json.dumps(rationale, ensure_ascii=True)[1:-1] in canonical_bytes(self.input).decode("ascii"):
            raise ValueError("gold rationale in evaluator input")
        return self


class CaseFile(StrictModel):
    version: Literal["topos-experiment-cases/v1"]
    synthetic: bool
    held_out: bool
    cases: Annotated[list[Case], Field(min_length=1, max_length=4096)]

    @model_validator(mode="after")
    def unique(self):
        ids = [case.case_id for case in self.cases]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate case")
        labels = {}
        for case in self.cases:
            group, release = case.gold.paraphrase_group, case.gold.expected_release
            # Variants restate one case, so they share its label; otherwise a correct
            # arm would be scored unstable for getting both labels right.
            if group is not None and labels.setdefault(group, release) != release:
                raise ValueError("paraphrase group with mixed gold")
        return self


@dataclass(frozen=True)
class LoadedCases:
    sha256: str
    synthetic: bool
    held_out: bool
    cases: tuple[Case, ...]


@dataclass(frozen=True)
class Observation:
    case_id: str
    arm: str
    verdict: str
    latency_seconds: float


def load_cases(path) -> LoadedCases:
    """Hash the exact bytes read, then parse those same bytes; never re-read."""
    with open(path, "rb") as stream:
        raw = stream.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise PolicyError("case_file_size")
    parsed = CaseFile.parse(raw)
    return LoadedCases(hashlib.sha256(raw).hexdigest(), parsed.synthetic, parsed.held_out, tuple(parsed.cases))


def evaluator_view(case: Case) -> dict:
    """The only part of a case an evaluator may receive: its id and a copy of its inputs."""
    return {"case_id": case.case_id, "input": parse_json(canonical_bytes(case.input))}


def wilson(successes: int, total: int, z: float = Z95):
    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def square_and_add(p1, interval1, p2, interval2, phi=0.0):
    """Interval for p1 - p2 from each proportion's own interval; phi=0 is Newcombe's unpaired method 10."""
    (l1, u1), (l2, u2) = interval1, interval2
    lower = math.sqrt(max(0.0, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2))
    upper = math.sqrt(max(0.0, (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2))
    return max(-1.0, p1 - p2 - lower), min(1.0, p1 - p2 + upper)


def paired_gap(both: int, first_only: int, second_only: int, neither: int):
    """First-minus-second difference of paired proportions with its 95% interval, or None."""
    total = both + first_only + second_only + neither
    if total == 0:
        return None
    first, second = both + first_only, both + second_only
    product = first * (total - first) * second * (total - second)
    phi = (both * neither - first_only * second_only) / math.sqrt(product) if product else 0.0
    p1, p2 = first / total, second / total
    return p1 - p2, square_and_add(p1, wilson(first, total), p2, wilson(second, total), phi)


def nearest_rank(values, percentile):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(1, math.ceil(percentile / 100 * len(ordered))) - 1]


def _round(value):
    return None if value is None else round(value, 6)


def _arm(cases, observed):
    positives = [case.case_id for case in cases if case.gold.expected_release == "permit"]
    permitted = [case_id for case_id in positives if observed[case_id].verdict == "permit"]
    prohibited = [case.case_id for case in cases
                  if case.gold.expected_release == "withhold" and observed[case.case_id].verdict == "permit"]
    groups = {}
    for case in cases:
        if case.gold.paraphrase_group is not None:
            groups.setdefault(case.gold.paraphrase_group, []).append(observed[case.case_id].verdict)
    variants = {group: verdicts for group, verdicts in groups.items() if len(verdicts) > 1}
    unstable = sorted(group for group, verdicts in variants.items() if len(set(verdicts)) > 1)
    interval = wilson(len(permitted), len(positives))
    latencies = [observed[case.case_id].latency_seconds for case in cases]
    return {"cases": len(cases), "permits": sum(item.verdict == "permit" for item in observed.values()),
            "prohibited_permits": len(prohibited), "prohibited_permit_case_ids": prohibited,
            "gold_positives": len(positives), "gold_positives_permitted": len(permitted),
            "recall": _round(len(permitted) / len(positives)) if positives else None,
            "recall_wilson95": [_round(value) for value in interval] if interval else None,
            "unresolved": sum(item.verdict == "indeterminate" for item in observed.values()),
            "latency_seconds": {"p50": _round(nearest_rank(latencies, 50)), "p95": _round(nearest_rank(latencies, 95))},
            "paraphrase_stability": {"groups": len(variants), "stable_groups": len(variants) - len(unstable),
                                     "rate": _round((len(variants) - len(unstable)) / len(variants)) if variants else None,
                                     "unstable_groups": unstable}}


def score(cases: LoadedCases, observations) -> dict:
    """Join verdicts to gold once every case has exactly one verdict from each arm."""
    known = {case.case_id: case for case in cases.cases}
    seen = {}
    for item in observations:
        if (not isinstance(item, Observation) or item.arm not in (ARM_A, ARM_B) or item.case_id not in known
                or item.verdict not in VERDICTS or (item.case_id, item.arm) in seen
                or type(item.latency_seconds) not in (int, float) or not 0 <= item.latency_seconds < math.inf):
            raise PolicyError("observation_invalid")
        seen[(item.case_id, item.arm)] = item
    if len(seen) != 2 * len(known):
        raise PolicyError("observations_incomplete")
    arms = {arm: _arm(cases.cases, {case_id: seen[(case_id, arm)] for case_id in known}) for arm in (ARM_A, ARM_B)}
    positives = [case_id for case_id, case in known.items() if case.gold.expected_release == "permit"]
    counts = {"both": 0, "b_only": 0, "a_only": 0, "neither": 0}
    for case_id in positives:
        b, a = seen[(case_id, ARM_B)].verdict == "permit", seen[(case_id, ARM_A)].verdict == "permit"
        counts["both" if a and b else "b_only" if b else "a_only" if a else "neither"] += 1
    gap = paired_gap(counts["both"], counts["b_only"], counts["a_only"], counts["neither"])
    return {"version": METRICS_VERSION, "cases_sha256": cases.sha256, "held_out": cases.held_out,
            "synthetic": cases.synthetic, "cases": len(known), "gold_positives": len(positives),
            "gold_withholds": len(known) - len(positives), "arm_a": ARM_A, "arm_b": ARM_B, "arms": arms,
            "recall_gap_b_minus_a_pp": None if gap is None else {
                "point": _round(gap[0] * 100), "ci95": [_round(gap[1][0] * 100), _round(gap[1][1] * 100)],
                "paired_positives": counts},
            "methods": dict(METHODS)}
