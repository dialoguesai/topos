"""Dry run for the shadow audit's local second labeler (confidence program C6).

Shows one sampled release re-scoring to a VERDICT rather than to `labeler_unavailable`, end to end, on text
written in this file.

    uv run python scripts/shadow_labeler_dry_run.py            # the real local model on the loopback Ollama
    uv run python scripts/shadow_labeler_dry_run.py --stub     # no socket at all, canned answers

**Synthetic only.** Every message below was written for this script. It reads no database, opens no node, and
touches nothing under the owner's home. The first exercise against real content is a separate, deliberate run on
the beta stack or a lab copy, in that lane, and it is not this script.

What it prints, per unit: the labels the model returned, whether the policy would still release on those labels,
and the verdict the re-score would file. What it proves, in one line at the end: the same sample that answers
`labeler_unavailable` with nothing registered answers a real verdict once the labeler is bound.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from topos.permissions_v2 import shadow_labeler_local as local  # noqa: E402
from topos.permissions_v2 import shadow_labelers, shadow_rescore  # noqa: E402
from topos.permissions_v2.contract import PolicyV2  # noqa: E402

# Synthetic units. Two a work-only grant should release, two it should not.
UNITS = [
    ("a work note", "Moving the deploy to Thursday so the release notes land first."),
    ("a second work note", "Reviewed the migration plan; the index rebuild is the long pole."),
    ("a hobby note", "Finished the second volume last night; the middle third drags."),
    ("a health note", "The scan came back clear, so the physio starts again on Monday."),
]
CANNED = {
    "a work note": {"domains": ["work", "plans"], "sensitivity": "none"},
    "a second work note": {"domains": ["work"], "sensitivity": "none"},
    "a hobby note": {"domains": ["hobbies"], "sensitivity": "none"},
    "a health note": {"domains": ["health", "plans"], "sensitivity": "special"},
}
WORK_ONLY = {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": ["work"]}


def policy(release_predicate):
    true = {"kind": "all_of", "terms": []}
    return PolicyV2.parse({
        "version": "topos-policy/v2", "policy_version_id": "policy-dry-run",
        "binding": {"environment_id": "beta", "node_id": "node-1", "resource_id": "resource-1",
                    "owner_id": "owner-1", "actor_id": "actor-1", "client_id": "client-1",
                    "grant_id": "grant-1", "assignment_id": "assignment-1"},
        "versions": {"vocabulary": "vocabulary-1", "capability": "permissions-beta/p2a-v1"},
        "validity": {"starts_at": 1000, "expires_at": 5_000_000_000},
        "source_universe": {"universe_id": "sources-1", "revision": 1, "source_ids": ["source-A"]},
        "hard_constraints": {"owner_only": "deny", "unknown_classification": "withhold",
                             "unknown_lineage": "withhold", "cross_rule_derivation": "deny",
                             "capability_growth": "require_consent"},
        "rules": [{"rule_id": "rule-A", "effect": "permit",
                   "evidence_use": {"sources": {"kind": "only", "values": ["source-A"]}, "predicate": true,
                                    "purpose": "reading",
                                    "processors": {"kind": "only", "values": ["owner-engine-local"]},
                                    "new_records": "include_if_predicate"},
                   "release": {"predicate": release_predicate, "ceiling": "raw",
                               "forms": [{"family": "canonical_record", "operation": "read",
                                          "view_id": "canonical.message_disclosure.v1",
                                          "tables": ["conversation_messages"]}]}}],
        "evaluator": {"kind": "hard_rules", "version": "hard-rules/p2a-v1"}, "natural_language": None})


class _Stub:
    def __init__(self):
        self.by_text = {text: CANNED[name] for name, text in UNITS}

    async def label(self, text):
        return json.dumps(self.by_text[text])


def records_for(names):
    by_name = dict(UNITS)
    return [{"record_id": "r.%d" % index, "canonical_table": "conversation_messages", "source_id": "source-A",
             "content": by_name[name]} for index, name in enumerate(names)]


async def label_each(transport):
    out = []
    for name, text in UNITS:
        raw = await transport.label(text)
        out.append((name, text, local.parse_labels(raw), raw))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stub", action="store_true", help="no socket; canned answers")
    arguments = parser.parse_args()

    print("rubric  %s (%d bytes)" % (local.RUBRIC_SHA256[:16], local.RUBRIC_BYTES))
    print("model   %s @ %s" % (local.MODEL, local.MODEL_REVISION[:16]))
    print("labeler %s  family=%s\n" % (local.LABELER_ID, local.FAMILY))

    async def run_once():
        # One loop for the whole exercise: an httpx client belongs to the loop that made it, and a second
        # `asyncio.run` here would hand it a closed one.
        transport = _Stub() if arguments.stub else local.open_transport()
        try:
            if not arguments.stub:
                await transport.verify()
                print("the installed tag matches the reviewed digest\n")
            return await label_each(transport)
        finally:
            client = getattr(transport, "client", None)
            if client is not None:
                await client.aclose()

    try:
        labelled = asyncio.run(run_once())
    except Exception as exc:  # noqa: BLE001
        print("the local model could not be reached or did not match: %s: %s" % (type(exc).__name__, exc))
        print("nothing was scored; every sample would answer unresolved, which is the correct failure.")
        return 2

    for name, _text, labels, raw in labelled:
        shown = json.dumps(labels) if labels else "REFUSED (outside the vocabulary): %r" % (raw,)
        print("  %-20s -> %s" % (name, shown))

    print()
    work_grant = policy(WORK_ONLY)
    for names in (["a work note", "a second work note"], ["a work note", "a health note"], ["a hobby note"]):
        per_record = [dict(labels) for name, _t, labels, _r in labelled if name in names and labels]
        if len(per_record) != len(names):
            print("  %-44s -> unresolved (a record the model would not label)" % (" + ".join(names),))
            continue
        answer = local.policy_verdict(work_grant, per_record)
        print("  %-44s -> policy says %-13s -> %s"
              % (" + ".join(names), answer, local.verdict_of(answer)))

    # The line this script exists for: the same sample, before and after the labeler is bound.
    print()
    request = {"version": shadow_rescore.VERSION, "request_id": "req-dry-run", "grant_id": "grant-1",
               "capability": "permissions-beta/p2a-v1", "output_sha256": "a" * 64, "labeler_mode": "local"}
    shadow_labelers.clear()
    before = shadow_rescore.rescore(object(), request)
    scored = records_for(["a work note", "a second work note"])
    shadow_rescore.resolve_records = lambda runtime, req: scored
    shadow_rescore.resolve_policy = lambda runtime, req: work_grant
    local.register(_Stub() if arguments.stub else None)
    after = shadow_rescore.rescore(object(), request)
    print("  with nothing registered: %s / %s" % (before.verdict, before.reason))
    print("  with the labeler bound:  %s / %s   (labeler=%s family=%s)"
          % (after.verdict, after.reason, after.labeler, after.family))
    return 0 if after.verdict in ("agree", "candidate_miss") else 1


if __name__ == "__main__":
    raise SystemExit(main())
