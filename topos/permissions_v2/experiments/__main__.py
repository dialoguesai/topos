"""Synthetic offline metadata demo; semantic transport is absent by default."""
import argparse
import asyncio
import json

from .harness import ExperimentHarness
from .synthetic import DeterministicTestTransport, config, fixture


async def run_demo(deterministic_test_transport=False):
    policy, snapshot, context = fixture()
    result = {"synthetic_only": True, "live_classifier_evaluation": False, "execution_enabled": False, "arms": {}}
    for arm in ("rules_v2", "semantic_v1"):
        harness = ExperimentHarness(policy, config(arm), provider=lambda: snapshot, clock=lambda: 1100,
            transport=DeterministicTestTransport() if deterministic_test_transport else None)
        outcome = await harness.run(context)
        result["arms"][arm] = {"verdict": outcome.verdict, "evidence_reason": outcome.evidence.reason_code,
                               "output_reason": outcome.output.reason_code if outcome.output else None}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--synthetic", required=True, action="store_true")
    parser.add_argument("--deterministic-test-transport", action="store_true", help="Exercise orchestration with a fixed answer; not a classifier")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_demo(args.deterministic_test_transport)), indent=2))
