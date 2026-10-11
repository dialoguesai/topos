#!/usr/bin/env python3
"""Synthetic grant-retrieval and writer evaluation; never opens an owner database.

Run against an explicit checkout with --node-tree. --live opts into the node's
already installed, digest-verified local writer. Keyword utility is a bounded
oracle, not a substitute for semantic review or the six-pair release gate.
"""
import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-tree", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.node_tree.resolve()))
    from topos.permissions_v2.search_index import LoadedIndex, Member, tokenize
    from topos.permissions_v2.search_lanes import rank
    from topos.permissions_v2.answer_generation import build_prompt, post_check_answer, question_lacks_permitted_anchor
    from topos.permissions_v2.knowledge_contract import JournalEntryResult

    class Boundary:
        def mentions_protected(self, text):
            return "secretperson" in text.lower()

    def record(text):
        rid = "r." + "a" * 64
        return JournalEntryResult.parse(dict(kind="journal_entry", record_id=rid, content=text,
            source_ids=["synthetic"], citations=[dict(record_id=rid, source_id="synthetic", content=text)]))

    retrieval_cases = [
        ("What projects?", "I finished a project."), ("Which hobby?", "My hobbies include pottery."),
        ("What classes?", "I enrolled in a class."), ("What boxes?", "I moved a box."),
        ("Which recipe?", "I saved recipes."), ("What studies?", "I completed a study."),
        ("Can you tell me about the kiln?", "The kiln has cooled."),
        # Additional held-out morphology, exact and Unicode topics.
        ("Which sketches?", "I drew a sketch."), ("Which activity?", "Activities included rowing."),
        ("What glass?", "I polished glass."), ("研究", "研究"), ("проект", "проект"),
    ]
    retrieval = []
    for query, text in retrieval_cases:
        texts = [text, "I can tell you about how I could help.", "Unrelated background."]
        members = []
        for n, body in enumerate(texts):
            bag = Counter(tokenize(body))
            members.append(Member(f"r.{n:064x}", 1_000_000, sum(bag.values()), dict(bag), b""))
        index = LoadedIndex({}, None, None, tuple(members), {})
        found = rank(index, query, None, limit=1, lower_us=0, upper_us=2_000_000)
        retrieval.append(dict(query=query, correct=found == [members[0].opaque_id]))

    noise = "I organized the supplies and checked the room. " * 20
    writer_cases = [
        ("What happened with the kiln?", noise + "I switched off the kiln because the temperature sensor failed.",
         ("sensor",), True),
        ("What happened with the orchard?", noise + "I postponed the orchard planting because a frost was forecast.",
         ("frost",), True),
        ("What happened with the telescope?", noise + "I cancelled the telescope session because clouds obscured the sky.",
         ("cloud",), True),
        ("What happened with the rehearsal?", noise + "I delayed the rehearsal because the theatre needed repairs.",
         ("repair",), True),
        ("What happened with the kiln?", "I switched off the kiln because the temperature sensor failed.",
         ("sensor",), True),
        ("What happened with the orchard?", "I postponed the orchard planting because a frost was forecast.",
         ("frost",), True),
        ("What happened with electroplating?", "I prepared for an orchard planting.", (), False),
        ("What did Secretperson do?", "I prepared for an orchard planting.", (), False),
        ("What happened with refrigeration?", "I prepared for an orchard planting.", (), False),
        ("What happened with archaeology?", "I prepared for an orchard planting.", (), False),
        # Short absent subjects pass the eight-character anchor preflight. These
        # exercise the writer and post-checks, not just deterministic abstention.
        ("What happened with the kiln?", "I prepared for an orchard planting.", (), False),
        ("What happened with the cello?", "I prepared for an orchard planting.", (), False),
        ("What happened with the orchard?", "I packed supplies for a telescope session.", (), False),
    ]
    writer = []
    for query, text, expected, answerable in writer_cases:
        records = [record(text)]
        prompt = build_prompt(query, records, precision="none")
        row = dict(query=query, answerable=answerable,
                   context_supported=not question_lacks_permitted_anchor(prompt),
                   complete_detail=text.split(". ")[-1] in prompt.user)
        if args.live:
            from topos.permissions_v2.answer_release import _generate_local
            raw = ""
            started = time.monotonic()
            if not Boundary().mentions_protected(query) and not question_lacks_permitted_anchor(prompt):
                try:
                    raw = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
                except Exception as exc:
                    row["model_failure"] = getattr(exc, "code", type(exc).__name__)
            checked = post_check_answer(raw, records, prompt, mode="with_sources", boundary=Boundary())
            body = getattr(checked.body, "answer", "")
            row.update(seconds=round(time.monotonic() - started, 3), raw=raw,
                       answer=body, outcome=checked.body.outcome, reason=checked.reason,
                       utility=checked.body.outcome == "answered" and all(word in body.lower() for word in expected),
                       citation_fidelity=all(item.record_id == records[0].record_id
                                            for item in getattr(checked.body, "records", ())))
        writer.append(row)
    report = dict(retrieval=dict(correct=sum(row["correct"] for row in retrieval), total=len(retrieval), cases=retrieval),
                  context=dict(supported=sum(row["context_supported"] for row in writer if row["answerable"]),
                               complete_detail=sum(row["complete_detail"] for row in writer if row["answerable"]), total=6),
                  writer_cases=writer, live=args.live)
    if args.live:
        report["writer"] = dict(useful=sum(row["utility"] for row in writer if row["answerable"]), answerable=6,
            unsupported_answers=sum(row["outcome"] == "answered" for row in writer if not row["answerable"]),
            must_abstain=7, model_failures=sum("model_failure" in row for row in writer))
    encoded = json.dumps(report, indent=2) + "\n"
    if args.out:
        args.out.write_text(encoded)
    print(json.dumps({key: value for key, value in report.items() if key != "writer_cases" and key != "retrieval"}
                     | {"retrieval": {key: value for key, value in report["retrieval"].items() if key != "cases"}}))


if __name__ == "__main__":
    main()
