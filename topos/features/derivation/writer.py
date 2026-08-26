"""Derivation fact writer — the assertion ladder for ontology-pack facts.

Implements (shadow-pilot scope of plan F1.4/F1.6/F1.7 + §2.5 Tier 1-2):
  - D6 provenance columns (ontology_id, ontology_version, altitude) — idempotent migration
  - registry-driven object keys: single / multi / episodic (F1.7 — a 2nd medication must
    NOT supersede the 1st; episodes never supersede each other)
  - F1.6 identifier guard as a REJECTING validator on every value leaf
  - resolution ladder: NOOP / CORROBORATE / CORRECT / SUPERSEDE / CONFLICT
      * same value, overlapping sources  -> CORROBORATE (extraction audit trail, no revision row)
      * diff value, overlapping sources  -> CORRECT (closed_reason=correction; belief clock
                                            inherited — the world didn't change, our reading did)
      * diff value, new sources          -> SUPERSEDE (the world changed; history kept)
      * much weaker challenger           -> CONFLICT queue, incumbent kept
      * re-statement within noop window  -> NOOP
  - Tier-2 declarative `revision:` rules from the pack: closes / supersedes_on / noop_when
"""
from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from topos.features.facts.store import FactStore, normalize_predicate, CONFLICT_CONFIDENCE_MARGIN
from .guard import find_identifiers
from .packs import Pack, Predicate

TEMPLATE_VERSION = "shadow-1"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm(v: Any) -> str:
    return " ".join(str(v or "").strip().lower().split())


# Free-text values arrive with cosmetic variation the raw string cannot see through:
# "browser plugin"/"browser-plugin" became two project facts, and "tired"/"tiredness"/
# "very tired" became three symptom facts (two from ONE sentence). Keys are normalized so
# those collapse onto one revision chain. Deliberately conservative — punctuation,
# intensifiers, token order and a few English suffixes only. No fuzzy matching: on personal
# data an over-merge silently destroys a real distinction, which is worse than a duplicate.
_INTENSIFIERS = {"very", "really", "super", "extremely", "quite", "somewhat", "slightly",
                 "a", "an", "the", "some", "bit", "little", "mild", "severe", "constant"}


def _stem(word: str) -> str:
    for suf in ("ness", "ing", "ies", "ed", "es", "s"):
        if len(word) > len(suf) + 2 and word.endswith(suf):
            return word[: -len(suf)]
    return word


def _key_norm(v: Any) -> str:
    """Normalized form used for KEYING only (never for display or equality of record)."""
    text = re.sub(r"[^a-z0-9\s]+", " ", str(v or "").lower())
    toks = [_stem(w) for w in text.split() if w not in _INTENSIFIERS]
    return " ".join(sorted(toks))


def _value_leaves(value: Any) -> List[str]:
    if isinstance(value, dict):
        out: List[str] = []
        for v in value.values():
            out.extend(_value_leaves(v))
        return out
    if isinstance(value, list):
        out = []
        for v in value:
            out.extend(_value_leaves(v))
        return out
    return [str(value)] if value is not None else []


def _canon_value(value: Any) -> str:
    """Stable normalized representation for keying + equality (dicts key-sorted)."""
    if isinstance(value, dict):
        return _norm(json.dumps({k: value[k] for k in sorted(value)}, default=str))
    return _norm(value)


class DerivationSchemaMissing(RuntimeError):
    """The provenance columns are absent — the database has not been migrated."""


def assert_derivation_schema(conn: sqlite3.Connection) -> None:
    """Verify the provenance columns exist; never create them.

    They arrive via migration ``derivation_provenance_v1`` (registry order 64), which
    stamps ``user_version``. This function previously ran the ALTER TABLE itself from
    the writer's constructor — a feature quietly reshaping a live schema behind the
    migration ledger's back, which is precisely how a database gets stamped ahead of
    the engine that opens it. Fail loudly instead: an unmigrated database is an
    install problem with a known fix, not something an extraction pass should paper
    over mid-run.
    """
    cols = {r[1] for r in conn.execute("PRAGMA table_info(signal_objects)")}
    missing = [c for c in ("ontology_id", "ontology_version", "altitude") if c not in cols]
    if missing:
        raise DerivationSchemaMissing(
            f"signal_objects is missing {missing}; run migrations "
            "(derivation_provenance_v1) before writing pack facts"
        )


class DerivationWriter:
    def __init__(self, conn: sqlite3.Connection, model: str) -> None:
        self.conn = conn
        self.model = model
        self.store = FactStore(conn)  # low-level helpers only; the ladder lives here
        assert_derivation_schema(conn)
        self.stats: Dict[str, int] = {k: 0 for k in (
            "written", "corroborated", "corrected", "superseded", "noop",
            "conflicts", "guard_rejects", "role_rejects", "updated_fields")}
        self.guard_hits: List[Dict[str, str]] = []

    # ---------------------------------------------------------------- keying
    def object_key(self, subject: str, pred: Predicate, value: Any, occurrence: Optional[str]) -> str:
        base = f"fact:{subject}:{normalize_predicate(pred.name)}"
        ident = value
        if isinstance(value, dict) and pred.key_fields:
            # identity fields key the instance; everything else is state that revises IN PLACE
            ident = {k: value.get(k) for k in pred.key_fields}
        if isinstance(ident, dict):
            ikey = _key_norm(" ".join(str(ident.get(k) or "") for k in sorted(ident)))
        else:
            ikey = _key_norm(ident)
        if pred.temporal == "episodic":
            occ = (occurrence or "undated")[:10]
            return f"{base}:{ikey[:48]}:{occ}"
        if pred.cardinality == "multi":
            return f"{base}:{ikey[:48]}"
        return base

    # ---------------------------------------------------------------- ladder
    def assert_pack_fact(
        self,
        *,
        pack: Pack,
        predicate: str,
        subject_entity_id: str,
        value: Any,
        actor_role: str,
        source_refs: List[Dict[str, Any]],
        confidence: float = 0.7,
        object_entity_id: Optional[str] = None,
        occurrence: Optional[str] = None,   # episodic anchor (event date)
        quote: str = "",
    ) -> Dict[str, Any]:
        pred = pack.predicates.get(predicate)
        if pred is None:
            return {"outcome": "schema_reject", "reason": f"unknown predicate {predicate}"}
        synth_preds = {q for e in pack.synthesis for q in (e.get("predicate") if isinstance(e.get("predicate"), list) else [e.get("predicate")])}
        if actor_role == "synthesis":
            # synthesis facts derive from AGGREGATES, not a single record's role; allowed only
            # for inferred-altitude predicates the pack declares a synthesis job for
            if pred.altitude != "inferred" or predicate not in synth_preds:
                self.stats["role_rejects"] += 1
                return {"outcome": "role_reject", "reason": "synthesis role only for declared inferred predicates"}
        elif actor_role not in pack.allowed_roles():
            self.stats["role_rejects"] += 1
            return {"outcome": "role_reject", "reason": f"{actor_role} not allowed for {pack.role_policy}"}
        # F1.6 guard — every leaf of the value
        for leaf in _value_leaves(value):
            kinds = find_identifiers(leaf)
            if kinds:
                self.stats["guard_rejects"] += 1
                self.guard_hits.append({"predicate": predicate, "kinds": ",".join(kinds)})
                return {"outcome": "guard_reject", "reason": f"identifier ({kinds}) in value"}

        key = self.object_key(subject_entity_id, pred, value, occurrence)
        incumbent = self.store._active_fact_by_key(key)
        now = _now_iso()

        if incumbent is not None:
            inc_payload = incumbent["payload"]
            inc_refs = incumbent.get("source_refs") or []
            same_value = _canon_value(inc_payload.get("value_struct") or inc_payload.get("object_value")) == _canon_value(value)
            overlap = self._sources_overlap(inc_refs, source_refs)

            if same_value:
                if self._within_noop_window(pack, incumbent):
                    self.stats["noop"] += 1
                    return {"outcome": "noop", "object_id": incumbent["object_id"]}
                self._corroborate(incumbent, confidence, source_refs)
                self.stats["corroborated"] += 1
                return {"outcome": "corroborated", "object_id": incumbent["object_id"]}

            # different value — Tier-2 supersedes_on: a change in a non-superseding field
            # of a structured value is a field update, not a revision
            sup_on = set((pack.revision.get("supersedes_on") or {}).get(predicate, [])) if isinstance(pack.revision.get("supersedes_on"), dict) else set(pack.revision.get("supersedes_on") or [])
            if isinstance(value, dict) and isinstance(inc_payload.get("value_struct"), dict) and sup_on:
                changed = {k for k in set(value) | set(inc_payload["value_struct"])
                           if _norm(json.dumps(value.get(k), default=str)) != _norm(json.dumps(inc_payload["value_struct"].get(k), default=str))}
                if changed and not (changed & sup_on):
                    self._update_fields(incumbent, value, confidence, source_refs)
                    self.stats["updated_fields"] += 1
                    return {"outcome": "field_update", "object_id": incumbent["object_id"], "changed": sorted(changed)}

            if float(confidence) < float(inc_payload.get("confidence") or 0.0) - CONFLICT_CONFIDENCE_MARGIN:
                self.store._queue_conflict(subject_entity_id, normalize_predicate(predicate),
                                           incumbent["object_id"], _canon_value(value), confidence)
                self.stats["conflicts"] += 1
                return {"outcome": "conflict_queued", "object_id": incumbent["object_id"]}

            if overlap:
                # CORRECTION: same evidence, new reading — inherit the belief clock
                self._close(incumbent, reason="correction", at=now)
                row = self._insert(pack, pred, subject_entity_id, value, actor_role, source_refs,
                                   confidence, object_entity_id, occurrence, quote,
                                   valid_from=incumbent.get("valid_from") or now, key=key)
                self.stats["corrected"] += 1
                return {"outcome": "corrected", "object_id": row}
            # SUPERSESSION: new evidence, the world changed
            self._close(incumbent, reason="superseded", at=now)
            row = self._insert(pack, pred, subject_entity_id, value, actor_role, source_refs,
                               confidence, object_entity_id, occurrence, quote, valid_from=now, key=key)
            self.stats["superseded"] += 1
            return {"outcome": "superseded", "object_id": row}

        row = self._insert(pack, pred, subject_entity_id, value, actor_role, source_refs,
                           confidence, object_entity_id, occurrence, quote, valid_from=now, key=key)
        self.stats["written"] += 1
        # Tier-2 `closes`: a value transition that closes OTHER active rows of this predicate
        self._apply_closes_rules(pack, pred, subject_entity_id, value)
        return {"outcome": "written", "object_id": row}

    # ---------------------------------------------------------------- pieces
    @staticmethod
    def _sources_overlap(a: List[Dict[str, Any]], b: List[Dict[str, Any]]) -> bool:
        ka = {(r.get("table"), str(r.get("record_id"))) for r in a or []}
        kb = {(r.get("table"), str(r.get("record_id"))) for r in b or []}
        return bool(ka & kb)

    def _within_noop_window(self, pack: Pack, incumbent: Dict[str, Any]) -> bool:
        rule = str(pack.revision.get("noop_when") or "")
        days = 7 if "7d" in rule or not rule else 7  # shadow: fixed 7d window
        upd = str(incumbent.get("updated_at") or incumbent.get("valid_from") or "")
        try:
            ts = datetime.fromisoformat(upd.replace("Z", "+00:00"))
        except ValueError:
            return False
        same_model = (incumbent["payload"].get("extractor") or {}).get("model") == self.model
        return same_model and (datetime.now(timezone.utc) - ts) < timedelta(days=days)

    def _corroborate(self, incumbent: Dict[str, Any], confidence: float, refs: List[Dict[str, Any]]) -> None:
        payload = dict(incumbent["payload"])
        hist = list(payload.get("extractions") or [])
        hist.append({"model": self.model, "template": TEMPLATE_VERSION, "ts": _now_iso(),
                     "agrees": True})
        payload["extractions"] = hist[-12:]
        payload["confidence"] = round(max(float(payload.get("confidence") or 0), float(confidence)), 3)
        merged = list(incumbent.get("source_refs") or [])
        for r in refs or []:
            if r not in merged:
                merged.append(r)
        self.conn.execute(
            "UPDATE signal_objects SET payload_json=?, confidence=?, source_refs_json=?, updated_at=? WHERE object_id=?",
            (json.dumps(payload, default=str), payload["confidence"], json.dumps(merged, default=str),
             _now_iso(), incumbent["object_id"]))
        self.conn.commit()

    def _update_fields(self, incumbent: Dict[str, Any], value: Dict[str, Any],
                       confidence: float, refs: List[Dict[str, Any]]) -> None:
        payload = dict(incumbent["payload"])
        struct = dict(payload.get("value_struct") or {})
        struct.update(value)
        payload["value_struct"] = struct
        payload["object_value"] = _canon_value(struct)[:160]
        self.conn.execute(
            "UPDATE signal_objects SET payload_json=?, updated_at=? WHERE object_id=?",
            (json.dumps(payload, default=str), _now_iso(), incumbent["object_id"]))
        self.conn.commit()

    def _close(self, incumbent: Dict[str, Any], *, reason: str, at: str) -> None:
        payload = dict(incumbent["payload"])
        payload["closed_reason"] = reason
        self.conn.execute(
            "UPDATE signal_objects SET valid_to=?, payload_json=?, updated_at=? WHERE object_id=?",
            (at, json.dumps(payload, default=str), _now_iso(), incumbent["object_id"]))
        self.conn.commit()

    def _apply_closes_rules(self, pack: Pack, pred: Predicate, subject: str, value: Any) -> None:
        rules = pack.revision.get("closes") or []
        if not isinstance(value, dict):
            return
        for rule in rules:
            on = rule.get("on_value") or {}
            if all(_norm(value.get(k)) == _norm(v) for k, v in on.items()) and rule.get("closes") == "same_object":
                # value itself encodes an end-state; the inserted row IS the closed state.
                # Close any OTHER active rows for this predicate+subject that don't carry the end-state.
                like = f"fact:{subject}:{normalize_predicate(pred.name)}%"
                now = _now_iso()
                for r in self.conn.execute(
                        "SELECT object_id, payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL AND object_key LIKE ?",
                        (like,)).fetchall():
                    p = json.loads(r[1])
                    if not all(_norm((p.get("value_struct") or {}).get(k)) == _norm(v) for k, v in on.items()):
                        p["closed_reason"] = f"closed_by_rule:{json.dumps(on, default=str)[:60]}"
                        self.conn.execute(
                            "UPDATE signal_objects SET valid_to=?, payload_json=?, updated_at=? WHERE object_id=?",
                            (now, json.dumps(p, default=str), now, r[0]))
                self.conn.commit()

    def _insert(self, pack: Pack, pred: Predicate, subject: str, value: Any, actor_role: str,
                refs: List[Dict[str, Any]], confidence: float, object_entity_id: Optional[str],
                occurrence: Optional[str], quote: str, *, valid_from: str, key: str) -> str:
        oid = str(uuid.uuid4())
        payload = {
            "subject_entity_id": subject,
            "predicate": normalize_predicate(pred.name),
            "object_value": _canon_value(value)[:160] if isinstance(value, dict) else str(value).strip(),
            "object_entity_id": object_entity_id,
            "confidence": round(float(confidence), 3),
            "disclosure": "owner_only",
            "asserted_by": "owner" if actor_role == "authored" else f"extracted:{actor_role}",
            "actor_role": actor_role,
            "pack": pack.pack,
            "altitude": pred.altitude,
            "sensitivity": pack.effective_sensitivity(pred.name),
            "extractor": {"model": self.model, "pack_version": pack.version,
                          "template": TEMPLATE_VERSION, "ts": _now_iso()},
        }
        if isinstance(value, dict):
            payload["value_struct"] = value
        if quote:
            payload["quote"] = quote[:200]
        now = _now_iso()
        self.conn.execute(
            """INSERT INTO signal_objects (object_id, signal_dimension, object_type, object_key,
                 payload_json, confidence, source_refs_json, valid_from, created_at, updated_at,
                 extractor_version, ontology_id, ontology_version, altitude, period_start)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (oid, "profile", "fact", key, json.dumps(payload, default=str), float(confidence),
             json.dumps(refs or [], default=str), valid_from, now, now,
             f"derivation:{TEMPLATE_VERSION}", pack.pack, pack.version, pred.altitude,
             occurrence))
        self.conn.commit()
        return oid
