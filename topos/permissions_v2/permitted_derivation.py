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

The journal goal field (IF-5 Lane H1) is a second, model-free step of the same lane
(`JournalGoalFieldPass`): for each journal entry a knowledge grant's own build admits, under a grant
that can release a goal citing it, whose structured goal field clears `journal_goal_field.refusal`
at the write, one `user_goals` row holding the field verbatim, with the lane's lineage. Such a grant
releases the entry itself, whole (`goal_field_grant`, `knowledge_projections.journal_entry_released`),
so the rule runs with its text-form guards set aside, as it does at release under that grant. No owner
command starts it: every index build stores its own grant's fields first (`SearchIndexService.rebuild`).
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
# The owner-socket route that runs the pass (`permissions_v2_permitted_derivation`); off by default.
FLAG = "TOPOS_PERMISSIONS_V2_PERMITTED_DERIVATION"
# Packs whose output this lane can store at all: work.project (work.career), commit.made
# (obligations.commitments), asp.goal (aspirations.goals). Every other enabled pack writes only predicates
# the class table excludes, so running it would spend model time on nothing the lane keeps.
ALLOWED_PACKS = ("work.career", "obligations.commitments", "aspirations.goals")
# aspirations.goals is left out by default: on the 30 Sep census copy its verifier accepted 0 of 4, and it
# cost more model time than the other two together. The goal prompt still runs.
DEFAULT_PACKS = ("work.career", "obligations.commitments")
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


def permitted_journal_entries(resolver, conn, floor, frozen, policy, *, now: int, keep=None) -> dict:
    """The journal entries `SearchIndexService._rebuild_once` admits as members of a knowledge grant (IF-5 §1).

    The same candidates, qualification, policy decision and boundary checks as `permitted_messages`, and the
    build's own journal leaf filters: the grant's tables, the NSFW withhold, and every instant the entry's stated
    day can denote inside the window. Each comes with its qualified review, whose labels the goal-field rule reads.
    `keep`: a test on the entry's row as stored, asked before it is qualified; an entry it refuses is not read
    further (it can only narrow the answer: qualification still decides every entry it keeps).
    """
    from .automatic_message_review import MachineMessageReview
    from .evidence import _key
    from .evidence_families import within
    from .message_evidence import OwnerMessageReview, qualify_automatic_message
    from .release import source_message_decision
    from ..disclosure.content_policy import is_record_nsfw

    tables = set(policy.search.tables)
    lower, upper = (now - policy.search.window.max_age_seconds) * 1_000_000, now * 1_000_000
    boundary = resolver.entity_boundary(conn)
    identities = sorted({_key(review.snapshot.message.identity): review.snapshot.message.identity
                         for fact_id, review in frozen.reviews.items()
                         if isinstance(review, (OwnerMessageReview, MachineMessageReview))
                         and fact_id not in frozen.opt_outs
                         and review.snapshot.message.identity.table == "journal_entries"}.items())
    out = {}
    for _k, identity in identities:
        if keep is not None:
            cursor = conn.execute("SELECT * FROM journal_entries WHERE entry_id=? AND source_id=?",
                                  (identity.record_id, identity.source_id))
            names = [column[0] for column in cursor.description]
            found = [dict(zip(names, values)) for values in cursor.fetchmany(2)]
            if len(found) != 1 or not keep(found[0]):
                continue
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
            if (ident.table != "journal_entries" or ident.table not in tables or is_record_nsfw(row)
                    or not within(ident.table, row, lower, upper)):
                continue
            out[_key(ident)] = (ident, dict(row), qualified)
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


def enabled(env=None) -> bool:
    import os
    env = os.environ if env is None else env
    return str(env.get(FLAG, "")).strip().lower() == "true"


def node_extractor(conn, *, packs=DEFAULT_PACKS, goals: bool = True):
    """The rules floor plus the node's own model extraction, configured as the node runs it.

    The extraction model is the one the node resolves for derivation (device override, then settings), the
    verifier the pinned derivation verifier unless the node turned verification off. Returns the composed
    extractor and the model extractor, whose `counts` the caller reports.
    """
    from ..config.settings import settings
    from ..engine.backends.ollama import OllamaAdapter
    from ..enrichment.jobs.canonical.derivation_job import _verify_mode
    from ..features.derivation.packs import load_packs
    from ..features.derivation.registry import bundled_pack_dir
    from ..features.derivation.verify import verifier_model
    from ..features.facts.llm_extract import _resolved_extraction_model
    if not set(packs) <= set(ALLOWED_PACKS):
        raise PolicyError("permitted_derivation_pack_unsupported")
    model = str(_resolved_extraction_model(settings, conn) or "").strip()
    if not model:
        raise PolicyError("permitted_derivation_model_unavailable")
    adapter = OllamaAdapter()

    def llm(name, prompt, num_predict):
        out = adapter._generate(name, prompt, num_predict=num_predict, think=False, temperature=0.0,
                                num_ctx=8192, timeout=180)
        return str(out.get("text") or "") if isinstance(out, dict) else str(out or "")
    loaded = load_packs(bundled_pack_dir(), trusted=True)
    model_extractor = ModelExtractor(llm, packs={pid: loaded[pid] for pid in packs}, model=model,
                                     verifier=None if _verify_mode() == "off" else verifier_model(), goals=goals)
    return compose(rules_extractor, model_extractor), model_extractor


def compose(*extractors) -> Callable[[dict, str], list[Spec]]:
    def run(row, table):
        return [spec for extractor in extractors for spec in extractor(row, table)]
    return run


# --- admission and writes -----------------------------------------------------------------------

def refusal(spec: Spec, boundary, *, form: bool = True) -> str | None:
    """Why this lane will not store `spec`. None = store it. Codes only.

    `form=False` (a journal goal field under a grant that releases its entry whole): a goal's shape is not
    judged, since the text is the entry's own paragraph; Off-limits on it still is."""
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
        if not isinstance(value, str) or (form and (not 6 <= len(value) <= MAX_GOAL_CHARS or len(value.split()) < 2
                                                    or "\n" in value or "?" in value)):
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


def write_goal_field(conn, *, identity, row: dict, spec: Spec, now: int) -> str:
    """One `user_goals` row per (entry, goal text), keyed by the node's derived-row identity.

    `record_id` is the entry, `source_id` its source, `goal_text` the field verbatim, and the payload carries the
    lane's lineage. The id is `derived_row_id("user_goals", (entry, text))`, the id the node's own goal extraction
    gives the same goal for the same entry: a rerun finds it ("unchanged"), an edited entry supersedes it in place
    ("superseded"), and the extraction writing the same text later replaces it rather than adding a twin. A row the
    extraction already stored under that id is left as it is ("already_stored": the field rule grounds it as it
    stands); a row whose lineage this code cannot read, or that names another source, is never touched. A row the
    extraction stored for the same entry and text under another id (an older writer's) is "already_stored" too, so
    the lane adds no twin beside it: it carries no lineage, and the field rule grounds it as it stands.
    """
    from ..storage.derived_row_identity import derived_row_id
    lineage = _lineage(identity, row["content"], spec, now)
    goal_id = derived_row_id("user_goals", (identity.record_id, spec.value))
    payload = json.dumps({"lineage": lineage, "confidence": spec.confidence})
    found = conn.execute("SELECT source_id, payload_json FROM user_goals WHERE goal_id=?", (goal_id,)).fetchall()
    if found:
        source_id, payload_json = found[0]
        try:
            old = json.loads(payload_json) if payload_json else {}
        except ValueError:
            old = None
        if source_id != identity.source_id or not isinstance(old, dict):
            return "conflict"
        current = lineage_of(old)
        if current is None:
            return "conflict" if isinstance(old.get("lineage"), dict) else "already_stored"
        if (current.get("message"), current.get("message_revision")) == (lineage["message"],
                                                                         lineage["message_revision"]):
            return "unchanged"
        conn.execute("UPDATE user_goals SET goal_text=?, payload_json=? WHERE goal_id=?",
                     (spec.value, payload, goal_id))
        return "superseded"
    twins = conn.execute("SELECT payload_json FROM user_goals WHERE record_id=? AND source_id=? AND goal_text=?",
                         (identity.record_id, identity.source_id, spec.value)).fetchall()
    for (stored,) in twins:
        try:
            other = json.loads(stored) if stored else {}
        except ValueError:
            continue
        if isinstance(other, dict) and not isinstance(other.get("lineage"), dict):
            return "already_stored"
    columns = {r[1] for r in conn.execute("PRAGMA table_info(user_goals)")}
    values = {"goal_id": goal_id, "record_id": identity.record_id, "source_id": identity.source_id,
              "goal_text": spec.value, "model": None, "provider": None, "payload_json": payload}
    names = [name for name in values if name in columns]
    conn.execute(f"INSERT INTO user_goals ({','.join(names)}) VALUES ({','.join('?' * len(names))})",
                 [values[name] for name in names])
    return "written"


# --- the pass -----------------------------------------------------------------------------------

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
        from .identity import attested_self
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
                subject = attested_self(conn)   # OD-29: one attested is_self entity, or nothing is written
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


def _carries_a_goal(row) -> bool:
    """A journal row that carries a goal field or a Goal paragraph at all, stated or not (a mismatch included)."""
    from .journal_goal_field import field_state
    return field_state(row)[1] != "goal_field_absent"


def goal_field_grant(policy) -> bool:
    """A grant whose index can hold a journal entry's goal field: a knowledge grant that signs `journal_entry`
    (it releases the entry itself, whole, as a record) and `goal` or `relationship`. The lane's write, the index
    build's own step and the refresh loop all ask this one function."""
    from .search_contract import CAPABILITY_KNOWLEDGE_SEARCH
    kinds = set(policy.search.result_types)
    return (policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH and "journal_entry" in kinds
            and bool({"goal", "relationship"} & kinds))


class JournalGoalFieldPass:
    """Owner-only, no model: the structured goal field of every qualifying journal entry, stored (IF-5 Lane H1).

    A qualifying entry is a journal member that carries a goal field or a Goal paragraph (only those are qualified
    again here) and that some active knowledge grant's own index build admits, under a grant that can release a
    goal citing it (`goal_field_grant`: `goal` or `relationship`, and `journal_entry`). That grant
    releases the entry itself, whole (`knowledge_projections.journal_entry_released`, the function release asks,
    here over the lane's own selection and again on the row as it is at the write), so its goal field is the
    entry's own released paragraph and clears `journal_goal_field.refusal` with the text-form guards set aside:
    in the write's own transaction, against the row as it is then, with the owner's attested self and the
    Off-limits boundary read there too. A stored goal grants nothing: every grant decides again at release, and
    one that does not release the entry whole applies every guard. Returns counts and codes only. Safe to run
    again: a goal already stored for the same entry and text is left as it is. Relationships follow the goals at
    the next graph rebuild, as for every stored goal.

    `run(grant_id=...)` reads one grant's members only (an index build stores its own grant's fields before it
    builds, `SearchIndexService.rebuild`); `rebuild=False` leaves the index to the caller. With nothing selected
    it opens no write.
    """

    def __init__(self, service):
        self.service = service

    def _selected(self, now: int, grant_id: str | None = None) -> dict:
        from .knowledge_projections import journal_entry_released
        service = self.service
        selected: dict = {}
        for candidate in service._search_grants(now):
            if grant_id is not None and candidate != grant_id:
                continue
            with service.ledger._transaction() as db:
                try:
                    authority, policy = service.ledger._authority(db, candidate, now)
                except PolicyError:
                    continue
            if not goal_field_grant(policy):
                continue
            frozen, floor, _clock = service._freeze()
            if floor != authority.protection_revision:
                continue   # the grant's own requests refuse until the owner syncs it; so does this
            window = ((now - policy.search.window.max_age_seconds) * 1_000_000, now * 1_000_000)
            with service.resolver._read(gated=False) as (conn, snapshot_floor):
                if snapshot_floor != floor:
                    continue
                # Only an entry that carries a goal field or a Goal paragraph can give a goal; the rest (most
                # members) are not qualified again here, so a build pays only for the few that do.
                admitted = permitted_journal_entries(service.resolver, conn, floor, frozen, policy, now=now,
                                                     keep=_carries_a_goal)
                for member, (identity, row, qualified) in admitted.items():
                    # The build admits it; release's own question, asked here as release asks it.
                    if journal_entry_released(policy, qualified, {member: row}, *window):
                        selected.setdefault(member, (identity, row, qualified, (policy, *window)))
        return selected

    def run(self, *, now: int | None = None, grant_id: str | None = None, rebuild: bool = True) -> dict:
        from . import entity_boundary, journal_goal_field
        from .entailment_grounding import author_of
        from .evidence_families import family
        from .identity import attested_self
        from .knowledge_projections import journal_entry_released
        from ..storage.db.write_gate import with_db_write
        service = self.service
        service._require_owner(service.resolver.binding)
        if not journal_goal_field.enabled() or not family("journal_entries").enabled():
            raise PolicyError("journal_goal_field_disabled")
        now = int(time.time()) if now is None else now
        counts: dict = {}

        def count(name, n=1):
            counts[name] = counts.get(name, 0) + n

        selected = self._selected(now, grant_id)
        count("journal_members", len(selected))
        if not selected:
            return counts   # nothing to store: no write is opened (every build of every grant asks)
        written = 0
        with with_db_write():
            conn = sqlite3.connect(service.resolver.path)
            conn.isolation_level = None
            try:
                conn.execute("BEGIN IMMEDIATE")
                refused = None
                if attested_self(conn) is None:
                    refused = "refused:owner_subject_unattested"   # OD-29: the goal's subject is the attested self
                elif not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' "
                                      "AND name='user_goals'").fetchone():
                    refused = "refused:goal_store_missing"
                try:
                    boundary = entity_boundary.EntityBoundary(conn)   # read at write time, in this transaction
                except PolicyError:
                    refused = refused or "refused:entity_boundary_unavailable"
                if refused is not None:
                    conn.execute("ROLLBACK")
                    count(refused, len(selected))
                    return counts
                names = [c[0] for c in conn.execute("SELECT * FROM journal_entries LIMIT 0").description]
                for member, (identity, row, qualified, grant) in sorted(selected.items(), key=lambda item: item[0]):
                    found = conn.execute("SELECT * FROM journal_entries WHERE entry_id=? AND source_id=?",
                                         (identity.record_id, identity.source_id)).fetchall()
                    fresh = [dict(zip(names, r)) for r in found]
                    if (len(fresh) != 1 or message_revision(identity, fresh[0].get("content"))
                            != message_revision(identity, row.get("content"))):
                        count("refused:message_changed")
                        continue
                    if not journal_entry_released(grant[0], qualified, {member: fresh[0]}, grant[1], grant[2]):
                        count("refused:entry_not_released")   # no longer released whole, on the row as it is now
                        continue
                    field = journal_goal_field.structured_field(fresh[0])
                    code = journal_goal_field.refusal(
                        field, fresh[0], boundary=boundary, author_is_owner=author_of(qualified),
                        subject_attested=True, sensitivity=qualified.classifications[0].sensitivity,
                        entry_released=True)
                    spec = Spec("goal", "goal", field or "", 1.0, {"kind": "journal_goal_field",
                                                                   "version": journal_goal_field.VERSION})
                    code = code or refusal(spec, boundary, form=False)
                    if code is not None:
                        count("refused:" + code)
                        continue
                    outcome = write_goal_field(conn, identity=identity, row=fresh[0], spec=spec, now=now)
                    count("goal:" + outcome)
                    written += outcome in ("written", "superseded")
                conn.execute("COMMIT")
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
        if written and rebuild:
            count("rebuilt", len(service.rebuild_all(now=now)))
        return counts
