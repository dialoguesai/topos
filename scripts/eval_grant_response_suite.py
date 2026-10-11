#!/usr/bin/env python3
"""Frozen synthetic suite: scoped lexical retrieval and real, checked local answers.

The corpus file is independent of implementation and each case has an authored
expectation. This is not a production-population sample. Scope selection in this
harness models an already-permitted index; signed consent is tested separately.
No owner DB, shared rig, grant, embedding server or hosted model is used.
"""
import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import inspect
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace


class Boundary:
    def mentions_protected(self, text):
        return "secretperson" in text.lower()


def aggregate(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row["split"], row["family"])].append(row)
    result = []
    for (split, family), cases in sorted(groups.items()):
        result.append(dict(split=split, family=family, cases=len(cases),
            answerable=sum(c["answerable"] for c in cases),
            answered=sum(c["outcome"] == "answered" for c in cases),
            useful_by_oracle=sum(c["useful_by_oracle"] for c in cases),
            abstained=sum(c["outcome"] == "no_answer" for c in cases),
            negative_answered=sum(not c["answerable"] and c["outcome"] == "answered" for c in cases),
            model_calls=sum(c["model_called"] for c in cases),
            model_errors=sum(bool(c.get("model_error")) for c in cases)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-tree", type=Path, required=True)
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--split", choices=("development", "heldout", "all"), default="development")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    encoded = args.corpus.read_bytes()
    corpus = json.loads(encoded)
    digest = hashlib.sha256(encoded).hexdigest()
    source_digest = hashlib.sha256()
    for path in sorted((args.node_tree / "topos/permissions_v2").glob("*.py")):
        source_digest.update(path.name.encode())
        source_digest.update(path.read_bytes())
    implementation = source_digest.hexdigest()
    sys.path.insert(0, str(args.node_tree.resolve()))
    from topos.permissions_v2.search_index import LoadedIndex, Member, tokenize
    from topos.permissions_v2.search_lanes import rank
    from topos.permissions_v2.knowledge_contract import KnowledgeSearchResult
    from topos.permissions_v2 import answer_generation as generation
    from topos.permissions_v2.answer_generation import build_prompt, post_check_answer, question_lacks_permitted_anchor

    def selected(case):
        return args.split == "all" or case["split"] == args.split

    report = dict(corpus_sha256=digest, corpus_version=corpus["version"], live=args.live,
                  implementation_sha256=implementation, node_tree=str(args.node_tree.resolve()),
                  retrieval=[], answers=[], limits=corpus["limits"])
    if args.resume and args.out.exists():
        prior = json.loads(args.out.read_text())
        if (prior["corpus_sha256"] != digest or prior["node_tree"] != report["node_tree"]
                or prior["live"] != args.live or prior["implementation_sha256"] != implementation):
            raise ValueError("Resume requires the same corpus, checkout path and model mode")
        report["answers"] = prior["answers"]
    for case in corpus["retrieval"]:
        if not selected(case):
            continue
        # Source/domain/sensitivity filtering happens before the production rank
        # call. Production rank additionally filters the effective event window.
        members = []
        for n, doc in enumerate(corpus["corpora"][case["corpus_id"]]):
            if not doc["permitted"]:
                continue
            bag = Counter(tokenize(doc["text"]))
            members.append(Member(f"r.{n:064x}", doc["event_us"], sum(bag.values()), dict(bag), b""))
        index = LoadedIndex({}, None, None, tuple(members), {})
        found = rank(index, case["question"], None, limit=case["k"], lower_us=0, upper_us=2_000_000)
        expected = {f"r.{n:064x}" for n in case["expected_indices"]}
        recall = len(expected & set(found)) / len(expected) if expected else None
        report["retrieval"].append(dict(id=case["id"], split=case["split"], group=case["group"],
            family=case["family"], scope=case["scope"], k=case["k"], recall_at_k=recall,
            top1_relevant=bool(found and found[0] in expected),
            outside_expected=bool(set(found) - expected), retrieved=len(found)))

    def save():
        report["summary"] = aggregate(report["answers"])
        args.out.write_text(json.dumps(report, indent=2) + "\n")

    done = {row["id"] for row in report["answers"]}
    for case in corpus["answers"]:
        if not selected(case) or case["id"] in done:
            continue
        records = KnowledgeSearchResult.parse(dict(family="canonical_record", operation="search",
            view_id="canonical.knowledge_search.v1", records=case["records"])).records
        if not records:
            row = {key: case[key] for key in ("id", "group", "split", "family", "answerable", "question")}
            row.update(model_called=False, outcome="no_answer", reason="no_evidence", context_text_chars=0,
                       answer="", raw="", seconds=0, cited_record_ids=[], expected_oracle=case["oracle"], useful_by_oracle=False)
            report["answers"].append(row)
            save()
            continue
        prompt_options = {"mode": case["mode"]} if "mode" in inspect.signature(build_prompt).parameters else {}
        prompt = build_prompt(case["question"], records, **prompt_options, precision=case["precision"],
                              item_domains=case.get("domains"), owner_words=frozenset(case.get("owner_words", ())))
        row = {key: case[key] for key in ("id", "group", "split", "family", "answerable", "question")}
        row.update(model_called=False, outcome="not_run", context_text_chars=sum(map(len, prompt.raw_texts)))
        if args.live:
            from topos.permissions_v2.answer_release import _generate_local
            started = time.monotonic()
            raw = ""
            calls = []
            if not Boundary().mentions_protected(case["question"]) and not question_lacks_permitted_anchor(prompt):
                row["model_called"] = True
                from topos.permissions_v2 import shadow_labeler_local as local
                original = local.open_transport
                def traced_transport(**kwargs):
                    transport = original(**kwargs)
                    class Client:
                        async def post(self, url, **request):
                            response = await transport.client.post(url, **request)
                            body = response.json()
                            calls.append(dict(stage="verification" if request["json"].get("format") else "rewrite" if request["json"]["messages"][0]["content"] == getattr(generation, "REWRITE_SYSTEM_PROMPT", None) else "writer",
                                content=(body.get("message") or {}).get("content"), done=body.get("done"),
                                model=body.get("model"), eval_count=body.get("eval_count")))
                            return response
                        async def aclose(self):
                            await transport.client.aclose()
                    return SimpleNamespace(client=Client(), base_url=transport.base_url, verify=transport.verify)
                local.open_transport = traced_transport
                try:
                    raw = asyncio.run(_generate_local(prompt, deadline=int(time.time()) + 110))
                except Exception as exc:
                    row["model_error"] = getattr(exc, "code", type(exc).__name__)
                finally:
                    local.open_transport = original
            checked = post_check_answer(raw, records, prompt, mode=case["mode"], boundary=Boundary())
            answer = getattr(checked.body, "answer", "")
            folded = answer.casefold()
            row.update(raw=raw, answer=answer, outcome=checked.body.outcome, reason=checked.reason, model_responses=calls,
                seconds=round(time.monotonic()-started, 3),
                checks={key:value for key,value in vars(checked).items() if key != "body"},
                cited_record_ids=[item.record_id for item in getattr(checked.body, "records", ())],
                expected_oracle=case["oracle"],
                useful_by_oracle=bool(case["answerable"] and checked.body.outcome == "answered"
                    and all(any(word in folded for word in group) for group in case["oracle"]["required_any"])
                    and not any(word in folded for word in case["oracle"].get("forbidden", []))))
        else:
            row["useful_by_oracle"] = False
        report["answers"].append(row)
        save()
        print(json.dumps({key: row[key] for key in ("id", "family", "outcome", "model_called", "useful_by_oracle")}), flush=True)
    save()
    print(json.dumps(dict(corpus_sha256=digest, retrieval_cases=len(report["retrieval"]), summary=report["summary"])), flush=True)


if __name__ == "__main__":
    main()
