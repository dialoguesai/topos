"""Typed items derived from the messages a p2c-v3 grant already permits (OD-46).

A p2c-v3 recipient can read a permitted owner message in full, but the node's facts and goals were
derived long before, from other rows, so none of them cited a permitted message (0 of 192 facts and
0 of 3,458 goals on the 30 Sep census copy). This lane derives facts and goals from exactly the
messages the grant's own index build admits, and stores each with the lineage release needs to
judge it:

  * the subject is the one owner-attested `is_self` entity (OD-29), or nothing is written;
  * each item cites ONE message, by its complete identity (table, record, source, dataset);
  * each item carries that message's revision (identity + content hash). Release refuses an item
    whose cited message is not the one in its lineage or has changed since (`check_lineage`);
  * `asserted_by` is the owner: the lane reads only owner-authored rows.

It grants nothing. Every item still goes through `knowledge_projections`: Off-limits, owner-only,
opt-outs, provenance, window, policy labels (with the predicate's own class from
`predicate_classes`), attestation and grounding (fullmatch today; OD-38/OD-45 when they ship).
The lane only decides what gets STORED, and it stores less than release could refuse:

  * only predicates in `predicate_classes.CLASSES` (never a special category, never a third party,
    never an inferred trait), with one atomic scalar value;
  * never a value that names an Off-limits entity (the boundary's own veto, run before the write);
  * never from a message outside the grant's permitted set at selection, or one that changed
    between selection and the write.

Model calls run with no database open. The writes run under the node write gate in one
transaction, and the grant indexes are rebuilt afterwards so the items become searchable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import sqlite3
import time
from typing import Callable, Iterable

from .canonical import PolicyError, digest
from .fact_contract import atomic_label_syntax
from .predicate_classes import CLASSES, excluded_reason

LANE = "od46-permitted-message/v1"
MAX_VALUE_CHARS = 80
MAX_GOAL_CHARS = 300
DEFAULT_BUDGET = 500
FAMILIES = ("fact", "goal", "relationship")


@dataclass(frozen=True)
class Spec:
    kind: str                 # "fact" | "goal"
    predicate: str            # a CLASSES key, or "goal"
    value: str
    confidence: float = 0.6
    extractor: dict = field(default_factory=dict)


def message_revision(identity, content) -> str:
    """The cited message as release will see it: its complete identity and its exact bytes."""
    return digest({"identity": identity.model_dump(),
                   "content": hashlib.sha256(str(content).encode("utf-8")).hexdigest()})


def lineage_of(payload) -> dict | None:
    lineage = payload.get("lineage") if isinstance(payload, dict) else None
    return lineage if isinstance(lineage, dict) and lineage.get("lane") == LANE else None


def check_lineage(payload, sources) -> None:
    """Release side. An item this lane wrote releases only against the message it was derived from,
    unchanged. Items from any other writer are untouched (their own gates apply)."""
    lineage = lineage_of(payload)
    if lineage is None:
        if isinstance(payload, dict) and isinstance(payload.get("lineage"), dict):
            raise PolicyError("lineage_revision_stale")   # a lineage this code cannot read
        return
    from .evidence import _key
    if len(sources) != 1:
        raise PolicyError("lineage_revision_stale")
    qualified, rows = sources[0]
    identity = qualified.snapshot.message.identity
    if (identity.model_dump() != lineage.get("message")
            or message_revision(identity, rows[_key(identity)]["content"]) != lineage.get("message_revision")):
        raise PolicyError("lineage_revision_stale")


# --- which messages -----------------------------------------------------------------------------

def permitted_messages(resolver, conn, floor, frozen, policy, *, now: int) -> dict:
    """The messages `SearchIndexService._rebuild_once` admits as members of a p2c-v3 grant.

    The same candidates (the frozen machine and owner reviews), the same qualification and policy
    decision, the same leaf filters (table, NSFW, dated, window, native time). A message whose
    boundary context cannot be established is left out here, where the build would abort.
    """
    from .automatic_message_review import MachineMessageReview
    from .evidence import _key
    from .fact_eligibility import canonical_utc_microseconds
    from .message_evidence import OwnerMessageReview, qualify_automatic_message
    from .reconciliation_provenance import native_time_within
    from .release import source_message_decision
    from ..disclosure.content_policy import is_record_nsfw

    tables = set(policy.search.tables)
    lower, upper = (now - policy.search.window.max_age_seconds) * 1_000_000, now * 1_000_000
    boundary = resolver.entity_boundary(conn)
    identities = sorted({_key(review.snapshot.message.identity): review.snapshot.message.identity
                         for fact_id, review in frozen.reviews.items()
                         if isinstance(review, (OwnerMessageReview, MachineMessageReview))
                         and fact_id not in frozen.opt_outs}.items())
    out = {}
    for _k, identity in identities:
        try:
            qualified, rows = qualify_automatic_message(resolver, conn, floor, identity, frozen, None)
            if source_message_decision(policy, qualified).verdict != "permit":
                continue
            if boundary.active:
                for version in qualified.snapshot.artifacts + qualified.snapshot.leaves:
                    ident = version.identity
                    boundary.check(table=ident.table, record_id=ident.record_id, source_id=ident.source_id,
                                   dataset_id=ident.dataset_id, row=rows[_key(ident)])
        except PolicyError:
            continue
        for leaf in qualified.snapshot.leaves:
            ident = leaf.identity
            row = rows[_key(ident)]
            stamp = canonical_utc_microseconds(row.get("event_at"))
            if (ident.table not in tables or is_record_nsfw(row) or stamp is None or stamp < lower
                    or not native_time_within(row, lower, upper)):
                continue
            out[_key(ident)] = (ident, dict(row))
    return out


# --- extractors ---------------------------------------------------------------------------------

def rules_extractor(row: dict, table: str) -> list[Spec]:
    """The attested-snapshot lane's rules floor. No model."""
    from .snapshot_message_facts import extract_snapshot_message_facts
    return [Spec("fact", spec["predicate"], spec["object_value"], float(spec.get("confidence") or 0.6),
                 {"kind": "rules", "version": "snapshot-message-facts/v1"})
            for spec in extract_snapshot_message_facts({**row, "_table": table}, None, table=table)]


GOAL_PACK_KEYS = {"asp.goal": "goal"}


class ModelExtractor:
    """The node's own pack extraction (prefilter, extract, verifier) and goal prompt, over one row.

    `llm(model, prompt, num_predict) -> str` is injected, so tests never reach a model. A pack
    assertion is kept only when the verifier accepted it as about the owner, and only through the
    one scalar field its class names. Anything else is dropped here and counted by the caller.
    """

    def __init__(self, llm: Callable[[str, str, int], str], *, packs: dict, model: str, verifier: str | None,
                 goals: bool = True):
        self.llm, self.packs, self.model, self.verifier, self.goals = llm, packs, model, verifier, goals
        from ..features.derivation.prefilter import PackPrefilter
        self.filters = {pid: PackPrefilter(pack) for pid, pack in packs.items()}
        self.counts: dict = {}

    def _count(self, name: str) -> None:
        self.counts[name] = self.counts.get(name, 0) + 1

    def __call__(self, row: dict, table: str) -> list[Spec]:
        from ..features.derivation.template import build_prompt, parse_output
        from ..features.derivation.verify import (apply_verdict, build_verify_prompt, label_note_for,
                                                  lens_note_for, parse_verdict)
        text, date = str(row.get("content") or ""), str(row.get("event_at") or "")[:10]
        rec = {"table": table, "record_id": row.get("message_id"), "text": text, "date": date, "role": "authored"}
        out: list[Spec] = []
        for pid, pack in self.packs.items():
            if "authored" not in pack.allowed_roles() or not self.filters[pid].passes(text):
                continue
            self._count("pack_calls")
            try:
                valid, _rejects = parse_output(self.llm(self.model, build_prompt(pack, text, date, "authored"), 900),
                                               pack, record_text=text)
            except Exception:  # noqa: BLE001 -- an unreachable or malformed model yields nothing
                self._count("pack_errors")
                continue
            for assertion in valid:
                self._count("pack_assertions")
                if self.verifier:
                    try:
                        verdict = parse_verdict(self.llm(self.verifier, build_verify_prompt(
                            text, "authored", date, assertion["predicate"], assertion["value"],
                            assertion.get("about", "owner"), lens_note=lens_note_for(pack),
                            label_note=label_note_for(rec)), 250))
                    except Exception:  # noqa: BLE001
                        verdict = None
                    assertion = apply_verdict(assertion, verdict)
                    if assertion.get("verifier_status") != "accepted":
                        self._count("verifier_" + str(assertion.get("verifier_status")))
                        continue
                if str(assertion.get("about") or "owner") != "owner":
                    self._count("not_about_owner")
                    continue
                predicate, value = assertion.get("predicate"), assertion.get("value")
                meta = {"kind": "pack", "pack": pid, "pack_version": pack.version, "model": self.model,
                        "verifier": self.verifier}
                if predicate in GOAL_PACK_KEYS:
                    goal = value.get(GOAL_PACK_KEYS[predicate]) if isinstance(value, dict) else value
                    if isinstance(goal, str):
                        out.append(Spec("goal", "goal", goal, float(assertion.get("confidence") or 0.6), meta))
                    continue
                klass = CLASSES.get(predicate)
                if klass is None:
                    self._count("predicate_unclassed")
                    continue
                scalar = value.get(klass.key) if isinstance(value, dict) and klass.key else value
                if isinstance(scalar, str):
                    out.append(Spec("fact", predicate, scalar, float(assertion.get("confidence") or 0.6), meta))
        if self.goals:
            from ..engine.backends.generative_prompts import build_generative_prompt
            from ..engine.backends.generative_response import parse_json_object
            self._count("goal_calls")
            try:
                parsed = parse_json_object(self.llm(self.model, build_generative_prompt("goal_extraction",
                                                                                        {"text": text}), 512))
            except Exception:  # noqa: BLE001
                self._count("goal_errors")
                parsed = {}
            for goal in (parsed.get("goals") if isinstance(parsed, dict) else None) or []:
                if isinstance(goal, dict) and isinstance(goal.get("text"), str):
                    out.append(Spec("goal", "goal", goal["text"].strip(), 0.6,
                                    {"kind": "goal_extraction", "model": self.model}))
        return out


def compose(*extractors) -> Callable[[dict, str], list[Spec]]:
    def run(row, table):
        return [spec for extractor in extractors for spec in extractor(row, table)]
    return run


# --- admission and writes -----------------------------------------------------------------------

def refusal(spec: Spec, boundary) -> str | None:
    """Why this lane will not store `spec`. None = store it. Codes only."""
    if spec.kind == "fact":
        if excluded_reason(spec.predicate):
            return "predicate_" + excluded_reason(spec.predicate)
        klass = CLASSES.get(spec.predicate)
        if klass is None:
            return "predicate_unclassed"
        if klass.sensitivity not in ("none", "personal") or "health" in klass.domains:
            return "predicate_special_category"
        value = spec.value
        if not isinstance(value, str) or not 1 <= len(value) <= MAX_VALUE_CHARS:
            return "value_shape"
        try:
            atomic_label_syntax(value)
        except (TypeError, ValueError):
            return "value_not_atomic"
    elif spec.kind == "goal":
        value = spec.value
        if (not isinstance(value, str) or not 6 <= len(value) <= MAX_GOAL_CHARS or len(value.split()) < 2
                or "\n" in value or "?" in value):
            return "goal_shape"
    else:
        return "kind_unsupported"
    try:
        if boundary is not None and boundary.active and boundary.legacy_veto(
                "signal_objects", {"payload_json": json.dumps({"object_value": value})}):
            return "entity_protected"
    except PolicyError:
        return "entity_boundary_unavailable"
    return None


def _ref(identity) -> dict:
    ref = {"table": identity.table, "record_id": identity.record_id, "source_id": identity.source_id}
    if identity.dataset_id is not None:
        ref["dataset_id"] = identity.dataset_id
    return ref


def _lineage(identity, content, spec: Spec, now: int) -> dict:
    return {"lane": LANE, "message": identity.model_dump(), "message_revision": message_revision(identity, content),
            "extractor": spec.extractor, "derived_at": now}


def _norm(value: str) -> str:
    return " ".join(value.lower().split())


def write_fact(conn, *, subject: str, identity, row: dict, spec: Spec, now: int) -> str:
    klass = CLASSES[spec.predicate]
    lineage = _lineage(identity, row["content"], spec, now)
    key = "od46:" + digest({"subject": subject, "predicate": spec.predicate, "message": identity.model_dump(),
                            "value": _norm(spec.value)})[:32]
    current = conn.execute("SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact' "
                           "AND object_key=? AND valid_to IS NULL", (key,)).fetchall()
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    for object_id, payload_json in current:
        old = lineage_of(json.loads(payload_json))
        if old is not None and old.get("message_revision") == lineage["message_revision"]:
            return "unchanged"
        conn.execute("UPDATE signal_objects SET valid_to=?, updated_at=? WHERE object_id=?", (stamp, stamp, object_id))
    payload = {"subject_entity_id": subject, "predicate": spec.predicate, "object_value": spec.value,
               "object_entity_id": None, "confidence": round(float(spec.confidence), 3),
               "disclosure": "owner_only", "asserted_by": "owner", "lineage": lineage}
    object_id = "od46_" + digest({"key": key, "revision": lineage["message_revision"]})[:32]
    conn.execute("INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key, payload_json, "
                 "confidence, source_refs_json, valid_from, valid_to, extractor_version, created_at, updated_at, "
                 "created_by) VALUES (?,?,'fact',?,?,?,?,?,NULL,?,?,?,'system')",
                 (object_id, klass.domains[0], key, json.dumps(payload), float(spec.confidence),
                  json.dumps([_ref(identity)]), str(row.get("event_at") or stamp), LANE, stamp, stamp))
    return "superseded" if current else "written"


def write_goal(conn, *, identity, row: dict, spec: Spec, now: int) -> str:
    lineage = _lineage(identity, row["content"], spec, now)
    goal_id = "od46_" + digest({"message": identity.model_dump(), "goal": _norm(spec.value),
                                "revision": lineage["message_revision"]})[:32]
    if conn.execute("SELECT 1 FROM user_goals WHERE goal_id=?", (goal_id,)).fetchone():
        return "unchanged"
    columns = {r[1] for r in conn.execute("PRAGMA table_info(user_goals)")}
    values = {"goal_id": goal_id, "record_id": identity.record_id, "source_id": identity.source_id,
              "goal_text": spec.value, "model": spec.extractor.get("model"), "provider": "ollama",
              "payload_json": json.dumps({"lineage": lineage, "confidence": spec.confidence})}
    names = [name for name in values if name in columns]
    conn.execute(f"INSERT INTO user_goals ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                 [values[name] for name in names])
    return "written"


# --- the pass -----------------------------------------------------------------------------------

def _attested_self(conn):
    from .identity import attested_subjects, self_entity_ids
    try:
        subjects = attested_subjects(conn) & self_entity_ids(conn)
    except Exception:  # noqa: BLE001 -- unreadable identity state is "unattested", never a guess
        return None
    return next(iter(subjects)) if len(subjects) == 1 else None


class PermittedDerivationPass:
    """Owner-only. Derive, store and index the typed items of every active p2c-v3 grant's permitted messages.

    Returns counts and codes only.
    """

    def __init__(self, service, *, extractor: Callable[[dict, str], Iterable[Spec]], budget: int = DEFAULT_BUDGET):
        # `budget`: at most this many messages go to the extractor per pass (each may cost model calls).
        self.service, self.extractor, self.budget = service, extractor, budget

    def _selected(self, now: int) -> dict:
        from .search_contract import CAPABILITY_KNOWLEDGE_SEARCH
        service = self.service
        selected: dict = {}
        for grant_id in service._search_grants(now):
            with service.ledger._transaction() as db:
                try:
                    authority, policy = service.ledger._authority(db, grant_id, now)
                except PolicyError:
                    continue
            if (policy.versions.capability != CAPABILITY_KNOWLEDGE_SEARCH
                    or not set(FAMILIES) & set(policy.search.result_types)):
                continue
            frozen, floor, _clock = service._freeze()
            if floor != authority.protection_revision:
                continue   # the grant's own requests refuse until the owner syncs it; so does this
            with service.resolver._read(gated=False) as (conn, snapshot_floor):
                if snapshot_floor != floor:
                    continue
                selected.update(permitted_messages(service.resolver, conn, floor, frozen, policy, now=now))
        return selected

    def run(self, *, now: int | None = None) -> dict:
        from . import entity_boundary
        from ..features.facts.extract import _is_owner_authored
        from ..storage.db.write_gate import with_db_write
        service = self.service
        service._require_owner(service.resolver.binding)
        now = int(time.time()) if now is None else now
        counts: dict = {}

        def count(name, n=1):
            counts[name] = counts.get(name, 0) + n

        selected = self._selected(now)
        count("permitted_messages", len(selected))
        # Extraction: no database is open while a model runs.
        derived, extracted = [], 0
        for key, (identity, row) in sorted(selected.items()):
            if not _is_owner_authored({**row, "_table": identity.table}, identity.table):
                count("refused:not_owner_authored")
                continue
            if extracted >= self.budget:
                count("over_budget")
                continue
            extracted += 1
            revision = message_revision(identity, row["content"])
            for spec in self.extractor(row, identity.table):
                derived.append((key, identity, revision, spec))
        count("specs", len(derived))
        written = 0
        with with_db_write():
            conn = sqlite3.connect(service.resolver.path)
            conn.isolation_level = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                subject = _attested_self(conn)
                if subject is None:
                    conn.execute("ROLLBACK")
                    count("refused:owner_subject_unattested", len(derived))
                    return counts
                try:
                    boundary = entity_boundary.EntityBoundary(conn)   # read at write time, in this transaction
                except PolicyError:
                    # Off-limits cannot be checked, so nothing is written.
                    conn.execute("ROLLBACK")
                    count("refused:entity_boundary_unavailable", len(derived))
                    return counts
                has_goals = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_goals'").fetchone()
                for key, identity, revision, spec in derived:
                    if spec.kind == "goal" and not has_goals:
                        count("refused:goal_store_missing")
                        continue
                    found = conn.execute(f"SELECT * FROM {identity.table} WHERE message_id=? AND source_id=?",
                                         (identity.record_id, identity.source_id)).fetchall()
                    names = [c[0] for c in conn.execute(f"SELECT * FROM {identity.table} LIMIT 0").description]
                    rows = [dict(zip(names, r)) for r in found]
                    if len(rows) != 1 or message_revision(identity, rows[0].get("content")) != revision:
                        count("refused:message_changed")
                        continue
                    code = refusal(spec, boundary)
                    if code is not None:
                        count("refused:" + code)
                        continue
                    if spec.kind == "fact":
                        outcome = write_fact(conn, subject=subject, identity=identity, row=rows[0], spec=spec, now=now)
                    else:
                        outcome = write_goal(conn, identity=identity, row=rows[0], spec=spec, now=now)
                    count(f"{spec.kind}:{outcome}")
                    count(f"{spec.kind}:{outcome}:{spec.predicate}")
                    written += outcome != "unchanged"
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        if written:
            count("rebuilt", len(service.rebuild_all(now=now)))
        return counts
