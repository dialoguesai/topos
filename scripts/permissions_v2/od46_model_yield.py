"""OD-46 Phase 1 (d): what the node's own local-model derivation would store from the messages a
p2c-v3 grant already permits, and how much of it the release guards would pass. Counts only.

It never touches the live node or the census copy. `derive` makes an APFS clone of a census copy
(`cp -c`, copy-on-write) and runs, over just the grant's permitted rows and on the clone only:

  * the derivation packs named (`run_pack_backfill`, the node's own backfill: prefilter, extract,
    27B verifier, writer). A disabled pack may be named as a what-if; it is enabled on the clone;
  * the fact-LLM pass (`extract_owner_facts_llm`, resume off);
  * goal extraction (`GoalExtractionJob.enrich`), stored as `user_goals` rows the way the job's
    writer stores them.

`score` then counts, on the clone, the new items that cite a permitted message and how far each gets
through the release path: predicate allow-list (today's and a what-if class table), special category,
Off-limits, the owner-only split, support (the node's `_support`), and grounding (fullmatch, and
OD-45's sentence-scoped guards from a pinned snapshot of that module when one is given).
`delete` removes the clone. Nothing is printed but counts, codes and predicate names the engine's
source defines.

Run (zsh; every flag its own token):
  export TOPOS_ENV_FILE=<scratch>/topos.env
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/od46_model_yield.py derive \\
      --copy <candidates>/census-copy/<run-id> --clone <scratch>/clone --packs work.career,values.motivation
  ... score --copy <copy> --clone <scratch>/clone [--od45-module <snapshot.py>] --out <json>
  ... delete --clone <scratch>/clone
"""
from __future__ import annotations

import argparse
import asyncio
import collections
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_support as cs  # noqa: E402

RELEASE_PACKS = ("work.career", "values.motivation", "obligations.commitments", "relationships.social")
WHAT_IF_PACKS = ("aspirations.goals",)
BASELINE = "od46-baseline.json"


def _members(copy_root: Path) -> list[tuple[str, str, str]]:
    """The grant's permitted messages, from the census's own keyless run on the untouched copy."""
    import od46_permitted_derivation as od46
    result = od46.measure(copy_root)
    return sorted(result["_members"])


def _rows(conn, members):
    out = []
    for table, source_id, record_id in members:
        cursor = conn.execute(f"SELECT * FROM {table} WHERE message_id=? AND source_id=?", (record_id, source_id))
        names = [c[0] for c in cursor.description]
        found = cursor.fetchall()
        if len(found) == 1:
            out.append({**dict(zip(names, found[0])), "_table": table})
    return out


def _records(rows):
    """The history walk's record shape (`derivation_job._iter_history`), for exactly these rows.

    One difference, measured: the walk takes the role from `actor_role`, which is NULL on every owner
    iMessage, so it reads them as `observed` and every release-relevant pack skips them. These rows
    are owner-authored by provenance (`record_role`), and that is the role given here, as the lane does.
    """
    from topos.features.provenance.roles import record_role
    out = []
    for row in rows:
        text = row.get("content")
        if not isinstance(text, str) or len(text) <= 15:
            continue
        out.append({"table": row["_table"], "record_id": row["message_id"], "text": text[:6000],
                    "date": str(row.get("event_at") or "")[:10], "role": record_role(row, table=row["_table"]),
                    "source_id": "", "speaker": "", "speaker_entity_id": ""})
    return out


def derive(copy_root: Path, clone: Path, packs: list[str], *, fact_llm: bool, goals: bool, limit: int | None) -> dict:
    if clone.exists():
        raise cs.CensusRefused("clone_exists")
    members = _members(copy_root)
    if limit:
        members = members[:limit]
    clone.mkdir(mode=0o700, parents=True)
    subprocess.run(["cp", "-c", str(copy_root / "database.db"), str(clone / "database.db")], check=True)
    os.chmod(clone / "database.db", 0o600)
    # Any incidental open of "the database" by engine code lands on the clone, never elsewhere.
    os.environ["TOPOS_DATABASE_PATH"] = str(clone / "database.db")
    conn = sqlite3.connect(clone / "database.db")
    conn.row_factory = sqlite3.Row
    baseline = {"facts": [r[0] for r in conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'")],
                "goals": [r[0] for r in conn.execute("SELECT goal_id FROM user_goals")],
                "ledger": conn.execute("SELECT COUNT(*) FROM derivation_training_ledger").fetchone()[0],
                "members": members}
    (clone / BASELINE).write_text(json.dumps(baseline))
    rows = _rows(conn, members)
    records = _records(rows)
    report: dict = {"members": len(members), "rows": len(rows), "records_over_15_chars": len(records), "packs": {},
                    "actor_role_null": sum(1 for r in rows if not r.get("actor_role")),
                    "record_roles": dict(collections.Counter(r["role"] for r in records))}
    from topos.features.derivation.surfaces import run_pack_backfill
    for pack in packs:
        started = time.monotonic()
        before = conn.execute("SELECT COUNT(*) FROM derivation_training_ledger").fetchone()[0]
        enabled = conn.execute("SELECT enabled FROM pack_registry WHERE pack_id=?", (pack,)).fetchone()
        if enabled is None:
            report["packs"][pack] = {"error": "unknown_pack"}
            continue
        if not enabled[0]:
            conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id=?", (pack,))
            conn.commit()
        try:
            stats = run_pack_backfill(conn, pack, limit=len(records), records=records, use_prefilter=True)
        except Exception as exc:  # noqa: BLE001 -- counted, never hidden
            stats = {"error": type(exc).__name__}
        ledger = collections.Counter(r[0] for r in conn.execute(
            "SELECT COALESCE(vstatus,'none') FROM derivation_training_ledger ORDER BY rowid LIMIT -1 OFFSET ?", (before,)))
        report["packs"][pack] = {**{k: v for k, v in stats.items() if isinstance(v, (int, float, str))},
                                 "what_if_enabled": not enabled[0], "verifier": dict(ledger),
                                 "seconds": round(time.monotonic() - started, 1)}
        print(json.dumps({"pack": pack, **report["packs"][pack]}), file=sys.stderr, flush=True)
    if fact_llm:
        from topos.features.facts.llm_extract import extract_owner_facts_llm
        started, stats = time.monotonic(), {}
        try:
            written = extract_owner_facts_llm(conn, rows, resume=False, stats=stats, concurrency=1)
        except Exception as exc:  # noqa: BLE001
            written, stats = 0, {"error": type(exc).__name__}
        report["fact_llm"] = {"written": written, **{k: v for k, v in stats.items() if isinstance(v, (int, float, bool))},
                              "seconds": round(time.monotonic() - started, 1)}
        print(json.dumps({"fact_llm": report["fact_llm"]}), file=sys.stderr, flush=True)
    if goals:
        from topos.enrichment.jobs.canonical.goal_extraction_job import GoalExtractionJob
        started = time.monotonic()
        try:
            found = asyncio.run(GoalExtractionJob().enrich(rows))
        except Exception as exc:  # noqa: BLE001
            found = [{"_error": type(exc).__name__}]
        stored = 0
        for goal in found:
            if goal.get("_deferred") or goal.get("_error"):
                continue
            digest = hashlib.sha256(json.dumps([goal["message_id"], goal["goal_text"]]).encode()).hexdigest()[:24]
            conn.execute("INSERT OR IGNORE INTO user_goals (goal_id, record_id, source_id, goal_text, model, provider, "
                         "payload_json) VALUES (?,?,?,?,?,?,?)",
                         ("od46_" + digest, goal["message_id"], goal.get("source_id"), goal["goal_text"],
                          goal.get("model"), goal.get("provider"), json.dumps({"lane": "od46-measure"})))
            stored += 1
        conn.commit()
        report["goals"] = {"returned": len(found), "stored": stored,
                           "deferred": sum(1 for g in found if g.get("_deferred")),
                           "errors": sum(1 for g in found if g.get("_error")),
                           "seconds": round(time.monotonic() - started, 1)}
        print(json.dumps({"goals": report["goals"]}), file=sys.stderr, flush=True)
    conn.close()
    return report


def _load_od45(path: Path | None):
    if path is None:
        return None
    spec = importlib.util.spec_from_file_location("topos.permissions_v2._od45_snapshot", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module   # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


def score(copy_root: Path, clone: Path, *, od45: Path | None, classes: dict) -> dict:
    """New items on the clone that cite a permitted message, through the release gates. Counts only."""
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.evidence import EvidenceResolver, SHAREABLE_DISCLOSURES, _json, implicit_labels
    from topos.permissions_v2.identity import ATTESTED_CONTRACT, permit_subjects
    from topos.permissions_v2.knowledge_projections import (_goal_stated, _support, _unrestricted,
                                                             resolve_reference)
    from topos.permissions_v2.native_claim_grounding import explicitly_states_claim
    from topos.permissions_v2.predicate_classes import excluded_reason, scalar
    import grant_census as gc
    import od46_permitted_derivation as od46

    baseline = json.loads((clone / BASELINE).read_text())
    members = {tuple(m) for m in baseline["members"]}
    old_facts, old_goals = set(baseline["facts"]), set(baseline["goals"])
    manifest = json.loads((copy_root / "census-copy-manifest.json").read_text())
    binding = cs.binding_from_config(cs.load_config(copy_root))
    ledger = copy_root / "permissions-v2" / "ledger.db"
    lconn = cs.ro(ledger, immutable=True)
    try:
        _grant_id, _authority, policy = gc._grant(lconn, manifest["copied_at"], None)
    finally:
        lconn.close()
    upper = manifest["copied_at"] * 1_000_000
    lower = upper - policy.search.window.max_age_seconds * 1_000_000
    frozen = gc._frozen(copy_root / "permissions-v2" / "evidence-reviews.db")
    vocabulary = od46.code_vocabulary()
    guards = _load_od45(od45)
    out: dict = {"facts": {}, "goals": {}}
    # The clone stands at the live path for this session, exactly as the copy does for the census.
    with cs.copy_session(clone / "database.db", manifest["live_canonical_path"]):
        resolver = EvidenceResolver(clone / "database.db", binding=binding)
        with resolver._read(gated=False) as (conn, floor):
            attested = permit_subjects(conn, contract=ATTESTED_CONTRACT)
            # OD-45's "an attested owner subject exists" is the ledger's attestation of an is_self entity (the lane's
            # own rule, permitted_derivation._attested_self), never the literal "self" that permit_subjects adds.
            from topos.permissions_v2.permitted_derivation import _attested_self
            owner_attested = _attested_self(conn) is not None
            boundary = resolver.entity_boundary(conn)

            class Protected:
                # OD-38's EntityBoundary.mentions_protected (not on main): the boundary's own term match on each text.
                @staticmethod
                def mentions_protected(*texts):
                    return boundary.active and any(boundary._hits({"content": t}) for t in texts if isinstance(t, str))
            facts, by_predicate = collections.Counter(), collections.defaultdict(collections.Counter)
            for row in conn.execute("SELECT * FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL").fetchall():
                row = dict(row)
                if row["object_id"] in old_facts:
                    continue
                payload, refs = _json(row["payload_json"], dict), _json(row["source_refs_json"], list)
                cited = []
                for ref in refs:
                    try:
                        identity = resolve_reference(resolver, conn, ref)
                    except PolicyError:
                        continue
                    if (identity.table, identity.source_id, identity.record_id) in members:
                        content = conn.execute(f"SELECT content FROM {identity.table} WHERE message_id=? AND source_id=?",
                                               (identity.record_id, identity.source_id)).fetchone()[0]
                        cited.append((identity, content))
                facts["new"] += 1
                if not cited:
                    facts["new_not_citing_permitted"] += 1
                    continue
                predicate = payload.get("predicate")
                value = scalar(predicate, payload)
                name = predicate if predicate in vocabulary else "free_form"
                tally = by_predicate[name]
                tally["stored"] += 1
                tally["extractor:" + str(row.get("extractor_version") or "")[:40].split(":")[0]] += 1
                tally["asserted_by_owner"] += 1 if payload.get("asserted_by") == "owner" else 0
                tally["pack_sensitivity:" + str(payload.get("sensitivity"))] += 1 if payload.get("pack") else 0
                tally["value_is_text"] += 1 if isinstance(payload.get("object_value"), str) else 0
                tally["scalar_under_class"] += 1 if value is not None else 0
                tally["excluded:" + str(excluded_reason(predicate))] += 1 if excluded_reason(predicate) else 0
                tally["subject_attested"] += 1 if payload.get("subject_entity_id") in attested else 0
                tally["disclosure_shareable"] += 1 if payload.get("disclosure") in SHAREABLE_DISCLOSURES else 0
                domains, sensitivity = implicit_labels(payload, row.get("signal_dimension"))
                klass = classes.get(predicate)
                tally["class_today:" + sensitivity] += 1
                tally["class_proposed:" + (klass.sensitivity if klass else "unclassed")] += 1
                try:
                    _unrestricted(resolver, conn, frozen, None, "signal_objects", row["object_id"], row)
                    tally["pass:unrestricted"] += 1
                except PolicyError as exc:
                    tally["veto:" + exc.code] += 1
                    continue
                if klass is None:
                    continue
                try:
                    _support(resolver, conn, floor, frozen, None,
                             [{"table": i.table, "record_id": i.record_id, "source_id": i.source_id,
                               **({"dataset_id": i.dataset_id} if i.dataset_id else {})} for i, _ in cited],
                             policy, lower, upper, extra_domains=klass.domains, extra_sensitivity=klass.sensitivity)
                    tally["pass:support_proposed_class"] += 1
                except PolicyError as exc:
                    tally["support:" + gc.public_code(exc.code)] += 1
                    continue
                if not isinstance(value, str):
                    continue
                tally["grounded:fullmatch"] += 1 if any(explicitly_states_claim(c, predicate, value) for _i, c in cited) \
                    else 0
                tally["value_verbatim"] += 1 if any(value.casefold() in c.casefold() for _i, c in cited) else 0
                if guards is not None:
                    first_person = dict(getattr(guards, "FIRST_PERSON", {}))
                    first_person.update({p: k.first_person for p, k in classes.items() if k.first_person})
                    guards.FIRST_PERSON = first_person
                    claim = guards.fact_claim(predicate, value)
                    codes = [guards.guard_failure(claim, c, author_is_owner=True,
                                                  subject_attested=payload.get("subject_entity_id") in attested,
                                                  boundary=Protected, env={"TOPOS_PERMISSIONS_V2_ENTAILMENT_SENTENCE_REPORTING": "true"})
                             for _i, c in cited]
                    passed = any(code is None for code in codes)
                    tally["od45:guards_pass"] += 1 if passed else 0
                    if not passed and codes:
                        tally["od45:" + str(codes[0])] += 1
            out["facts"] = {**dict(facts), "by_predicate": {k: dict(v) for k, v in by_predicate.items()}}
            goals = collections.Counter()
            for row in conn.execute("SELECT * FROM user_goals").fetchall():
                row = dict(row)
                if row["goal_id"] in old_goals:
                    continue
                goals["new"] += 1
                hit = [t for t in gc.LEAF_TABLES if (t, row.get("source_id"), row.get("record_id")) in members]
                if not hit:
                    continue
                goals["cites_permitted"] += 1
                content = conn.execute(f"SELECT content FROM {hit[0]} WHERE message_id=? AND source_id=?",
                                       (row["record_id"], row["source_id"])).fetchone()[0]
                try:
                    _unrestricted(resolver, conn, frozen, None, "user_goals", row["goal_id"], row)
                    goals["pass:unrestricted"] += 1
                except PolicyError as exc:
                    goals["veto:" + exc.code] += 1
                    continue
                try:
                    _support(resolver, conn, floor, frozen, None,
                             [{"table": hit[0], "record_id": row["record_id"], "source_id": row["source_id"]}],
                             policy, lower, upper, extra_domains=("plans",))
                    goals["pass:support"] += 1
                except PolicyError as exc:
                    goals["support:" + gc.public_code(exc.code)] += 1
                    continue
                goals["grounded:fullmatch"] += 1 if _goal_stated(content, row.get("goal_text")) else 0
                goals["goal_text_verbatim"] += 1 if isinstance(row.get("goal_text"), str) and \
                    row["goal_text"].casefold() in content.casefold() else 0
                if guards is not None:
                    code = guards.guard_failure(guards.goal_claim(row.get("goal_text")), content, author_is_owner=True,
                                                subject_attested=owner_attested, boundary=Protected,
                                                env={"TOPOS_PERMISSIONS_V2_ENTAILMENT_SENTENCE_REPORTING": "true"})
                    goals["od45:guards_pass" if code is None else "od45:" + str(code)] += 1
                    waivable = frozenset(getattr(guards, "OWNER_WAIVABLE", ()) or (
                        "entailment_too_long", "entailment_value_not_atomic", "entailment_question_or_quote"))
                    waived = guards.guard_failure(guards.goal_claim(row.get("goal_text")), content, author_is_owner=True,
                                                  subject_attested=owner_attested, boundary=Protected, waive=waivable,
                                                  env={"TOPOS_PERMISSIONS_V2_ENTAILMENT_SENTENCE_REPORTING": "true"})
                    goals["od38_owner_confirm:candidate" if waived is None else "od38_owner_confirm:" + str(waived)] += 1
            out["goals"] = {**dict(goals), "owner_subject_attested": owner_attested}
    out["od45_module_sha256"] = hashlib.sha256(od45.read_bytes()).hexdigest() if od45 else None
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=("derive", "score", "delete"))
    parser.add_argument("--copy", type=Path)
    parser.add_argument("--clone", type=Path, required=True)
    parser.add_argument("--packs", default=",".join(RELEASE_PACKS + WHAT_IF_PACKS))
    parser.add_argument("--no-fact-llm", action="store_true")
    parser.add_argument("--no-goals", action="store_true")
    parser.add_argument("--limit", type=int, help="a smoke run over the first N permitted rows")
    parser.add_argument("--od45-module", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    clone = cs.refuse_live(args.clone.expanduser().absolute())
    if args.action == "delete":
        shutil.rmtree(clone)
        print(json.dumps({"deleted": not clone.exists()}))
        return 0
    cs.require_scratch_environment()
    copy_root = cs.refuse_live(args.copy.expanduser().absolute())
    if args.action == "derive":
        result = derive(copy_root, clone, [p for p in args.packs.split(",") if p],
                        fact_llm=not args.no_fact_llm, goals=not args.no_goals, limit=args.limit)
    else:
        from topos.permissions_v2.predicate_classes import CLASSES
        result = score(copy_root, clone, od45=args.od45_module, classes=CLASSES)
    text = json.dumps(result, sort_keys=True, indent=1)
    if args.out is not None:
        cs.refuse_live(args.out.expanduser().absolute()).write_text(text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except cs.CensusRefused as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        sys.exit(2)
