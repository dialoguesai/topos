#!/usr/bin/env python3
"""Fresh grouped challenge for follow-up development; authored before changes.

Development groups and held-out groups are separate. Related variants and task
families remain correlated. This is a challenge set, not a production estimate.
"""
import argparse
import hashlib
import json
from pathlib import Path


def record(n, text, kind="journal_entry", source="journal", **fields):
    rid = f"r.{n:064x}"
    return dict(kind=kind, record_id=rid, content=text, source_ids=[source],
                citations=[dict(record_id=rid, source_id=source, content=text)], **fields)


def build():
    data = dict(version="synthetic-grant-followup/v1", limits=[
        "Fresh authored challenge, not a random sample of users or grants.",
        "Variants and task families are correlated; heldout is by scenario group.",
        "Writer evaluation starts with already-permitted records, not signed retrieval.",
        "Keyword oracle is triage only; semantic review remains necessary."],
        corpora={}, retrieval=[], answers=[])
    n = 1000
    def pair(family, group, index, question, records, answerable, required, variant, domains=None):
        for mode in ("only", "with_sources"):
            case = dict(id=f"follow-{family}-{index}-{variant}-{mode}", group=group, family=family,
                split="development" if index < 4 else "heldout", question=question, records=records,
                answerable=answerable, precision="none", mode=mode,
                oracle=dict(required_any=required, forbidden=[]))
            if domains is not None:
                case["domains"] = domains
            data["answers"].append(case)
    events = [
        ("screenprint", "I postponed the screenprint demonstration because the exposure lamp failed.", ["lamp", "exposure"]),
        ("ceramicist", "I stopped the ceramicist apprenticeship because the kiln ventilation was damaged.", ["ventilation", "kiln"]),
        ("bookbinding", "I cancelled the bookbinding class because the instructor was ill.", ["ill", "sick", "instructor"]),
        ("navigation", "I delayed the navigation exercise because the compass was missing.", ["compass"]),
        ("hydroponics", "I halted the hydroponics experiment because the nutrient pump broke.", ["pump"]),
        ("astrometry", "I postponed the astrometry measurements because cloud covered the reference stars.", ["cloud", "star"]),
    ]
    filler = "I arranged the equipment. I put the notes away. I checked the empty room. "
    for i, (topic, text, cause) in enumerate(events):
        for variant, body in (("direct", text), ("long", filler*14+text)):
            n += 1
            pair("event", topic, i, f"What happened with {topic}?", [record(n, body)], True, [cause], variant)
    attributes = [
        ("linocut", "What did the linocut cost?", "I spent 47 dollars on the linocut materials.", "I delayed the linocut after the paper ran out.", [["47", "forty-seven"], ["dollar", "usd", "$"]]),
        ("campanology", "Who paid for campanology?", "I paid for the campanology session myself.", "I cancelled campanology because the hall was unavailable.", [["owner", "speaker", "they", "author"]]),
        ("origami", "How long did the origami take?", "The origami took me nine days.", "I paused the origami after the paper tore.", [["nine", "9"], ["day"]]),
        ("basketry", "What was the date of basketry?", "I finished the basketry on 2026-07-12.", "I stopped the basketry because a reed snapped.", [["july", "07"], ["12", "twelfth"]]),
        ("apiculture", "How many apiculture kits were donated?", "I donated four apiculture kits.", "I prepared the apiculture kits for a donation.", [["four", "4"]]),
        ("photogrammetry", "Why did the photogrammetry stop?", "I stopped the photogrammetry because the camera overheated.", "I stopped the photogrammetry session.", [["overheat", "hot"]]),
    ]
    for i, (topic, question, stated, missing, required) in enumerate(attributes):
        for variant, text, available in (("stated", stated, True), ("missing", missing, False)):
            n += 1
            pair("attribute", topic, i, question, [record(n, text, "message", "signal")], available,
                 required if available else [], variant)
    for i, topic in enumerate(("bonsai", "lacemaking", "falconry", "potterywheel", "cartography", "calligraphytools")):
        n += 1
        item = record(n, f"Owner prefers a quiet setting for {topic}.", "fact", assertion="inferred")
        item["citations"][0]["content"] = f"I chose a quiet setting for {topic} twice."
        pair("inference", topic, i, f"What have they shared about {topic}?", [item], True,
             [["quiet"], ["infer", "suggest", "likely", "appear", "may", "seem", "perhaps"]], "preference")
        pair("inference", topic, i, f"Why do they prefer {topic} in quiet settings?", [item], False, [], "missing_cause")
    for i, topic in enumerate(("harmonium", "trombone", "tambourine", "mandolin", "oboe", "glockenspiel")):
        n += 1
        item = record(n, f"A volunteer said, 'I repaired the {topic}.' I booked the {topic} studio.")
        pair("attribution", topic, i, f"What did the owner book for the {topic}?", [item], True,
             [["studio", "room"]], "owner_act")
        pair("attribution", topic, i, f"What did the owner repair on the {topic}?", [item], False, [], "other_act")
    categories = [
        ("work", "insulation", "I am drafting an insulation proposal for my employer.", "work and projects", [["insulation"], ["draft", "propos"]]),
        ("plans", "rafting", "I intend to take a rafting trip next summer.", "plans and outings", [["rafting"], ["plan", "intend", "summer", "trip"]]),
        ("hobbies", "juggling", "I practiced juggling patterns in my free time.", "hobbies and interests", [["juggl"], ["practic", "pattern"]]),
        ("family", "reunion", "I attended a reunion with my cousins.", "family events", [["reunion"], ["cousin"]]),
        ("relationships", "volunteers", "I collaborate with volunteers to restore park railings.", "the people they spend time with", [["volunteer"], ["railing", "restor", "collaborat"]]),
        ("home", "boardinghouse", "I am staying in a boardinghouse near the river.", "home and where they are staying", [["boardinghouse"], ["river", "stay"]]),
    ]
    for i, (domain, topic, text, query, required) in enumerate(categories):
        n += 1
        item = record(n, text, "message", "whatsapp")
        for variant, records, domains in (("single", [item], [[domain]]),
                ("mixed", [record(n+100, "I adjusted the microscope focus."), item,
                           record(n+200, "I put away the spare folders.")], [["hobbies"], [domain], ["work"]])):
            pair("category", topic, i, f"What has the owner shared about {query}?", records, True, required,
                 variant, domains)
    states = [("radiorestoration", "completed", ["complet", "finish", "done"]),
              ("glassengraving", "abandoned", ["abandon", "drop", "stopped"]),
              ("flowerarranging", "active", ["active", "progress", "ongoing", "working"]),
              ("stampcollecting", "stated_intention", ["intend", "plan", "want", "hope", "intention"]),
              ("bridgebuilding", "completed", ["complet", "finish", "done"]),
              ("kitemaking", "abandoned", ["abandon", "drop", "stopped"])]
    for i, (topic, state, required) in enumerate(states):
        n += 1
        item = record(n, f"Owner wants to improve the {topic} setup.", "goal", status=state)
        item["citations"][0]["content"] = f"I want to improve the {topic} setup."
        for variant, records in (("single", [item]), ("mixed", [item, record(n+300, "I organized the spare parts.")])):
            pair("state", topic, i, f"What are the plans for {topic}?", records, True, [required], variant)
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    payload = (json.dumps(build(), indent=2, ensure_ascii=False)+"\n").encode()
    args.out.write_bytes(payload)
    print(json.dumps(dict(sha256=hashlib.sha256(payload).hexdigest(), cases=len(build()["answers"]))))


if __name__ == "__main__":
    main()
