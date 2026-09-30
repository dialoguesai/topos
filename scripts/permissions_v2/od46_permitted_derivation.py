"""OD-46 Phase 1: how many typed items (facts, goals, relationships) the messages a p2c-v3 grant
already permits could yield. Owner-local, on a census copy, counts only.

Plan: audits/2026-09-14-permissions/latency-coverage-2026-09-28/OWNER_DECISIONS_2026-09-28.md OD-46.

The permitted messages are the census's own members (`grant_census.run`, keyless: an ephemeral key,
never the grant's). Against them it counts:

  (a) current facts that cite a permitted message, by predicate;
  (b) how many of all current facts and goals cite a permitted message at all;
  (c) the release ceiling for those facts if the supported predicates were widened to the top-N
      predicates the owner's facts actually use (every other gate as the node runs it);
  (d) what a fresh derivation over just the permitted messages would store: rules-only here, and the
      local-model pass by `--derive` on a throwaway clone (never the census copy).

Predicate names are printed only when the engine's own source names them (the fact store's
vocabulary, the derivation packs, the release tables); anything else is counted as free-form and
shown as a count. No content, value, name or identifier is printed or written.

Run (zsh; every flag its own token):
  export TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/od46_permitted_derivation.py \\
      --copy <candidates>/census-copy/<run-id> --out <LC>/runs/<run-id>/od46-measure.json
"""
from __future__ import annotations

import argparse
import collections
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_support as cs  # noqa: E402
import grant_census as gc  # noqa: E402


def code_vocabulary() -> set[str]:
    """Predicate names the engine's source itself defines. Only these are ever printed."""
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    from topos.features.facts.store import KNOWN_PREDICATES, MULTI_VALUED_PREDICATES
    from topos.permissions_v2.evidence import IMPLICIT_LABELS
    from topos.permissions_v2.knowledge_projections import PREDICATE_TEXT
    names = set(KNOWN_PREDICATES) | set(MULTI_VALUED_PREDICATES) | set(IMPLICIT_LABELS) | set(PREDICATE_TEXT)
    for pack in load_packs(bundled_pack_dir(), trusted=True).values():
        names |= set(pack.predicates)
    return names


def label(predicate, vocabulary) -> str:
    if isinstance(predicate, str) and predicate in vocabulary:
        return predicate
    return "free_form"


def measure(copy_root: Path, *, top_ns=(12, 20, 30, 50)) -> dict:
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.evidence import EvidenceResolver, SHAREABLE_DISCLOSURES, implicit_labels
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    from topos.permissions_v2.identity import ATTESTED_CONTRACT, permit_subjects, restriction_subjects
    from topos.permissions_v2.knowledge_projections import PREDICATE_TEXT, _support, resolve_reference
    from topos.permissions_v2.native_claim_grounding import explicitly_states_claim

    manifest = json.loads((copy_root / "census-copy-manifest.json").read_text())
    if not manifest["consistency"]["consistent"]:
        raise cs.CensusRefused("copy_not_consistent")
    binding = cs.binding_from_config(cs.load_config(copy_root))
    census = gc.run(canonical=copy_root / "database.db", reviews=copy_root / "permissions-v2" / "evidence-reviews.db",
                    ledger=copy_root / "permissions-v2" / "ledger.db",
                    index_root=copy_root / "permissions-v2" / "message-search", keys=None, binding=binding,
                    live_canonical=manifest["live_canonical_path"], now=manifest["copied_at"], keyless=True)
    members = {(o.table, o.source_id, o.record_id) for o in census.members.values() if o.family == "message"}
    member_ids = {m[2] for m in members}
    lower, upper, policy = census.lower_us, census.upper_us, census.policy
    vocabulary = code_vocabulary()
    frozen = gc._frozen(copy_root / "permissions-v2" / "evidence-reviews.db")
    out: dict = {"copy": {"run_id": manifest["run_id"], "copied_at_utc": manifest["copied_at_utc"]},
                 "permitted_messages": len(members),
                 "permitted_by_source": dict(collections.Counter(m[1] for m in members)),
                 "census_members": {k: v for k, v in collections.Counter(o.family for o in census.members.values()).items()}}

    with cs.copy_session(copy_root / "database.db", manifest["live_canonical_path"]):
        resolver = EvidenceResolver(copy_root / "database.db", binding=binding)
        with resolver._read(gated=False) as (conn, floor):
            attested = permit_subjects(conn, contract=ATTESTED_CONTRACT)
            spellings = restriction_subjects(conn)

            def resolved(ref):
                try:
                    identity = resolve_reference(resolver, conn, ref)
                except PolicyError:
                    return None
                row = conn.execute(f"SELECT content, event_at FROM {identity.table} WHERE message_id=? AND source_id=?",
                                   (identity.record_id, identity.source_id)).fetchmany(2)
                if len(row) != 1:
                    return None
                stamp = canonical_utc_microseconds(row[0][1])
                return identity, row[0][0], stamp is not None and lower <= stamp <= upper

            def is_member(identity):
                return (identity.table, identity.source_id, identity.record_id) in members

            # ---- facts -------------------------------------------------------------------------
            all_predicates = collections.Counter()
            cites_member_predicates = collections.Counter()
            in_window_predicates = collections.Counter()
            facts = collections.Counter()
            member_facts = []
            for object_id, payload_json, refs_json in conn.execute(
                    "SELECT object_id,payload_json,source_refs_json FROM signal_objects "
                    "WHERE object_type='fact' AND valid_to IS NULL").fetchall():
                facts["current"] += 1
                try:
                    payload, refs = json.loads(payload_json), json.loads(refs_json)
                except (TypeError, ValueError):
                    facts["malformed"] += 1
                    continue
                refs = refs if isinstance(refs, list) else []
                predicate = payload.get("predicate")
                all_predicates[predicate] += 1
                if any(isinstance(r, dict) and r.get("record_id") in member_ids for r in refs):
                    facts["names_a_permitted_record_id"] += 1
                cited = [c for c in (resolved(r) for r in refs if isinstance(r, dict)) if c]
                if any(c[2] for c in cited):
                    facts["cites_in_window_message"] += 1
                    in_window_predicates[label(predicate, vocabulary)] += 1
                if any(is_member(c[0]) for c in cited):
                    facts["cites_permitted_message"] += 1
                    cites_member_predicates[label(predicate, vocabulary)] += 1
                    member_facts.append((object_id, payload, refs, cited))
            out["facts"] = dict(facts)
            out["fact_predicates_all"] = {"named": {k: v for k, v in all_predicates.items() if k in vocabulary},
                                          "free_form_facts": sum(v for k, v in all_predicates.items() if k not in vocabulary),
                                          "free_form_distinct": sum(1 for k in all_predicates if k not in vocabulary),
                                          "supported_now": sum(v for k, v in all_predicates.items() if k in PREDICATE_TEXT)}
            out["fact_predicates_citing_in_window"] = dict(in_window_predicates)
            out["fact_predicates_citing_permitted"] = dict(cites_member_predicates)

            # (c) ceiling if the allow-list were widened to the top-N predicates in use.
            ranked = [p for p, _ in all_predicates.most_common() if isinstance(p, str)]
            ceilings = {}
            for n in top_ns:
                allowed = set(ranked[:n]) | set(PREDICATE_TEXT) if n > 12 else set(PREDICATE_TEXT)
                gate = collections.Counter()
                for object_id, payload, refs, cited in member_facts:
                    predicate, value = payload.get("predicate"), payload.get("object_value")
                    checks = {"predicate": predicate in allowed,
                              "disclosure": payload.get("disclosure") in SHAREABLE_DISCLOSURES,
                              "value_text": isinstance(value, str),
                              "subject_attested": payload.get("subject_entity_id") in attested,
                              "subject_owner_spelling": payload.get("subject_entity_id") in spellings}
                    domains, sensitivity = implicit_labels(payload, None)
                    checks["implicit_label_not_special"] = sensitivity != "special"
                    try:
                        _support(resolver, conn, floor, frozen, None, refs, policy, lower, upper,
                                 extra_domains=domains, extra_sensitivity=sensitivity)
                        checks["support"] = True
                    except PolicyError as exc:
                        checks["support"] = False
                        gate["support_code:" + gc.public_code(exc.code)] += 1
                    member_rows = [c for c in cited if is_member(c[0])]
                    checks["fullmatch"] = isinstance(value, str) and any(
                        explicitly_states_claim(c[1], predicate, value) for c in member_rows)
                    checks["value_verbatim"] = isinstance(value, str) and any(
                        isinstance(c[1], str) and value.casefold() in c[1].casefold() for c in member_rows)
                    for name, ok in checks.items():
                        gate["pass:" + name] += 1 if ok else 0
                    base = checks["predicate"] and checks["disclosure"] and checks["value_text"] and checks["support"]
                    gate["ceiling:node_rule"] += 1 if base and checks["subject_attested"] and checks["fullmatch"] else 0
                    gate["ceiling:verbatim_upper_bound"] += 1 if base and checks["subject_owner_spelling"] \
                        and checks["value_verbatim"] else 0
                ceilings[f"top_{n}"] = {"allowed_predicates": len(allowed), **dict(gate)}
            out["fact_ceiling_widened"] = ceilings

            # ---- goals and relationships --------------------------------------------------------
            names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            goals = collections.Counter()
            member_goal_ids = set()
            if "user_goals" in names:
                for goal_id, record_id, source_id in conn.execute("SELECT goal_id, record_id, source_id FROM user_goals"):
                    goals["total"] += 1
                    if any((t, source_id, record_id) in members for t in gc.LEAF_TABLES):
                        goals["cites_permitted_message"] += 1
                        member_goal_ids.add(goal_id)
                    elif record_id in member_ids:
                        goals["names_permitted_id_other_source"] += 1
            out["goals"] = dict(goals)
            rels = collections.Counter()
            if "entity_edges" in names:
                for (metadata,) in conn.execute("SELECT metadata_json FROM entity_edges WHERE edge_type='pursues' "
                                                "AND valid_to IS NULL"):
                    rels["pursues_current"] += 1
                    try:
                        source = json.loads(metadata).get("source_object_id")
                    except (TypeError, ValueError, AttributeError):
                        source = None
                    rels["from_a_permitted_goal"] += 1 if source in member_goal_ids else 0
            out["relationships"] = dict(rels)
            out["attested_subjects"] = len(attested)
            out["rules_only"] = rules_only_yield(conn, members)
            out["prior_derivation_coverage"] = prior_coverage(conn, members)
            out["_members"] = members  # in memory only; stripped before anything is written
    return out


GOAL_FORMS = re.compile(r"\A(?:My goal is to|I want to|I plan to|I intend to|I aim to) (\S.*?)[.!]?\Z", re.I | re.S)
SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def member_rows(conn, members):
    for table, source_id, record_id in sorted(members):
        cursor = conn.execute(f"SELECT * FROM {table} WHERE message_id=? AND source_id=?", (record_id, source_id))
        names = [c[0] for c in cursor.description]
        found = cursor.fetchall()
        if len(found) == 1:
            yield table, {**dict(zip(names, found[0])), "_table": table}


def rules_only_yield(conn, members) -> dict:
    """(d), rules floor: what the attested-snapshot lane's own extractor would assert from these rows. In memory."""
    from topos.features.facts.extract import _is_owner_authored
    from topos.permissions_v2.fact_contract import atomic_label_syntax
    from topos.permissions_v2.knowledge_projections import _goal_stated
    from topos.permissions_v2.native_claim_grounding import explicitly_states_claim
    from topos.permissions_v2.snapshot_message_facts import extract_snapshot_message_facts
    tally = collections.Counter()
    predicates = collections.Counter()
    for table, row in member_rows(conn, members):
        content = row.get("content") if isinstance(row.get("content"), str) else ""
        tally["rows"] += 1
        owner = _is_owner_authored(row, table)
        tally["owner_authored"] += 1 if owner else 0
        tally["has_question_mark"] += 1 if "?" in content else 0
        tally["chars_le_120"] += 1 if len(content) <= 120 else 0
        sentences = [x.strip() for x in SENTENCE.split(content) if x.strip()]
        tally["sentences"] += len(sentences)
        tally["sentences_in_goal_form"] += sum(1 for x in sentences if GOAL_FORMS.match(x))
        whole = GOAL_FORMS.match(content)
        tally["messages_goal_stated_whole"] += 1 if whole and _goal_stated(content, whole.group(1)) else 0
        for spec in extract_snapshot_message_facts(row, conn, table=table):
            tally["fact_specs"] += 1
            predicates[spec["predicate"]] += 1
            try:
                atomic_label_syntax(spec["object_value"])
            except (TypeError, ValueError):
                tally["fact_specs_value_not_atomic"] += 1
                continue
            tally["fact_specs_fullmatch"] += 1 if explicitly_states_claim(content, spec["predicate"], spec["object_value"]) else 0
    return {**dict(tally), "fact_spec_predicates": dict(predicates)}


def prior_coverage(conn, members) -> dict:
    """Did the node's own derivation already visit these rows? Pack ledger keys and fact-LLM progress marks."""
    from topos.features.facts.llm_extract import _FACT_LLM_PROGRESS_TYPE, _progress_source_ref
    from topos.features.signal.extraction.artifact_store import source_ref_hash
    packs = {pack: version for pack, enabled, version in
             conn.execute("SELECT pack_id, enabled, version FROM pack_registry") if enabled}
    done = {r[0] for r in conn.execute("SELECT key FROM derivation_progress")}
    marks = {r[0] for r in conn.execute("SELECT source_ref_hash FROM extraction_artifacts WHERE artifact_type=?",
                                        (_FACT_LLM_PROGRESS_TYPE,))}
    out = {"enabled_packs": sorted(packs), "pack_visited": {}, "pack_visited_any_version": {}}
    for pack, version in packs.items():
        out["pack_visited"][pack] = sum(1 for t, _s, r in members if f"{pack}@{version}:{t}:{r}" in done)
        out["pack_visited_any_version"][pack] = sum(
            1 for t, _s, r in members if any(k.startswith(pack + "@") and k.endswith(f":{t}:{r}") for k in done))
    out["fact_llm_pass_marked"] = sum(
        1 for t, s, r in members if source_ref_hash(_FACT_LLM_PROGRESS_TYPE, [_progress_source_ref(t, r, s)]) in marks)
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    cs.require_scratch_environment()
    copy_root = cs.refuse_live(args.copy.expanduser().absolute())
    started = time.monotonic()
    result = measure(copy_root)
    result.pop("_members", None)
    result["seconds"] = round(time.monotonic() - started, 1)
    text = json.dumps(result, sort_keys=True, indent=1)
    if args.out is not None:
        out = cs.refuse_live(args.out.expanduser().absolute())
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except cs.CensusRefused as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        sys.exit(2)
