"""Shared read/write surface for derivation UI (W4) — used by BOTH the message
handlers (core/handlers/derivation.py) and the HTTP routes (api/signal.py), so
the two transports can never drift."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Dict, List


def list_packs(conn: sqlite3.Connection) -> Dict[str, Any]:
    from .packs import load_packs
    from .registry import bundled_pack_dir, seed_pack_registry

    seed_pack_registry(conn, bundled_pack_dir())
    catalog = load_packs(bundled_pack_dir())
    counts = dict(conn.execute(
        "SELECT ontology_id, COUNT(*) FROM signal_objects"
        " WHERE object_type='fact' AND ontology_id IS NOT NULL AND valid_to IS NULL"
        " GROUP BY ontology_id").fetchall())
    conflict_counts = dict(conn.execute(
        "SELECT predicate, COUNT(*) FROM fact_conflicts WHERE status='pending'"
        " GROUP BY predicate").fetchall())
    def _q(sql, args=()):
        try:
            return conn.execute(sql, args).fetchall()
        except sqlite3.OperationalError:
            return []              # pre-migration-66 node
    offers = {}
    for oid, pid_, kind, stats_json, created in _q(
            "SELECT offer_id, pack_id, kind, stats_json, created_at FROM pack_offers"
            " WHERE status='pending' ORDER BY created_at DESC"):
        offers.setdefault(pid_, {"offer_id": oid, "kind": kind, "created_at": created,
                                 "stats": json.loads(stats_json or "{}")})
    yield_30d = {r[0]: {"prefilter_hits": r[1], "llm_calls": r[2], "written": r[3]}
                 for r in _q("SELECT pack_id, SUM(prefilter_hits), SUM(llm_calls), SUM(written)"
                             " FROM pack_yield WHERE day >= date('now','-30 day') GROUP BY pack_id")}
    packs: List[Dict[str, Any]] = []
    for pid, ver, enabled, disclosure, last_run in conn.execute(
            "SELECT pack_id, version, enabled, disclosure_default, last_run_at"
            " FROM pack_registry ORDER BY pack_id"):
        p = catalog.get(pid)
        ns = pid.split(".")[0]
        pending = sum(n for pred, n in conflict_counts.items() if pred.split(".")[0] == ns)
        packs.append({
            "pack_id": pid, "version": ver, "enabled": bool(enabled),
            "disclosure_default": disclosure,
            "title": getattr(p, "title", pid) if p else pid,
            "sensitivity_class": getattr(p, "sensitivity_class", "personal") if p else "personal",
            "fact_count": counts.get(pid, 0),
            "pending_conflicts": pending,
            "last_run_at": last_run,
            "predicates": len(getattr(p, "predicates", {}) or {}) if p else 0,
            "offer": offers.get(pid),
            "yield_30d": yield_30d.get(pid),
        })
    return {"packs": packs, "total_conflicts": sum(conflict_counts.values())}


def set_pack_enabled(conn: sqlite3.Connection, pack_id: str, enabled: bool) -> bool:
    from ...storage.db.write_gate import commit_connection, with_db_write
    with with_db_write():
        cur = conn.execute(
            "UPDATE pack_registry SET enabled=?, updated_at=datetime('now') WHERE pack_id=?",
            (1 if enabled else 0, pack_id))
        commit_connection(conn)
    return bool(cur.rowcount)


def list_conflicts(conn: sqlite3.Connection, limit: int = 100) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fact_conflicts)")}
    has_prov = "pack_id" in cols
    extra = ", pack_id, source_refs_json, quote, about_hint" if has_prov else ""
    for row in conn.execute(
            "SELECT conflict_id, subject_entity_id, predicate, incumbent_object_id,"
            " challenger_value, challenger_confidence, status, created_at" + extra +
            " FROM fact_conflicts WHERE status='pending'"
            " ORDER BY created_at DESC LIMIT ?", (min(int(limit), 500),)):
        cid, subj, pred, incumbent, value, conf, status, created = row[:8]
        pack_id, refs_json, quote, hint = (row[8:12] if has_prov else (None, None, None, None))
        try:
            val = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            val = value
        is_q = str(incumbent).startswith("quarantine:")
        rows.append({"conflict_id": cid, "subject_entity_id": subj, "predicate": pred,
                     "kind": "quarantine" if is_q else "conflict",
                     "reason": str(incumbent)[len("quarantine:"):] if is_q else None,
                     "incumbent_object_id": incumbent, "value": val,
                     "confidence": conf, "created_at": created,
                     "pack_id": pack_id, "quote": quote, "about_hint": hint,
                     "promotable": bool(pack_id)})
    return rows


def resolve_conflict(conn: sqlite3.Connection, conflict_id: str, status: str) -> bool:
    if status not in ("dismissed", "accepted"):
        raise ValueError("status must be dismissed|accepted")
    from ...storage.db.write_gate import commit_connection, with_db_write
    with with_db_write():
        cur = conn.execute("UPDATE fact_conflicts SET status=? WHERE conflict_id=?",
                           (status, conflict_id))
        commit_connection(conn)
    return bool(cur.rowcount)


def resolve_pack_offer(conn: sqlite3.Connection, offer_id: str, action: str) -> Dict[str, Any]:
    """Owner decision on a self-gating offer. accept on an enable_offer enables
    the pack; accept on a disable_nudge disables it; dismiss just records the
    choice (with backoff — a dismissed offer is not re-minted for 30 days)."""
    if action not in ("accept", "dismiss"):
        raise ValueError("action must be accept|dismiss")
    row = conn.execute("SELECT pack_id, kind, status FROM pack_offers WHERE offer_id=?",
                       (offer_id,)).fetchone()
    if not row:
        return {}
    pack_id, kind, status = row
    from ...storage.db.write_gate import commit_connection, with_db_write
    with with_db_write():
        conn.execute("UPDATE pack_offers SET status=?, updated_at=datetime('now')"
                     " WHERE offer_id=?",
                     ("accepted" if action == "accept" else "dismissed", offer_id))
        if action == "accept":
            enable = 1 if kind == "enable_offer" else 0
            conn.execute("UPDATE pack_registry SET enabled=?, updated_at=datetime('now')"
                         " WHERE pack_id=?", (enable, pack_id))
        commit_connection(conn)
    return {"offer_id": offer_id, "pack_id": pack_id, "kind": kind, "action": action}


def run_pack_backfill(conn: sqlite3.Connection, pack_id: str, limit: int = 500) -> Dict[str, Any]:
    """Owner-initiated history backfill for ONE enabled pack (the lens catalog's
    backfill control). Bounded by `limit` prefilter-HIT records per invocation —
    a huge history closes over a few presses (or the drip catch-up finishes it).
    Reuses the ingest pipeline wholesale: guards, multi-vote judging, ladder,
    ledger, yield counters."""
    from ...enrichment.jobs.canonical.derivation_job import (
        _bump_yield, _iter_history, _ledger_write, _verify_mode)
    from .packs import load_packs
    from .prefilter import PackPrefilter
    from . import template as _T
    from .registry import bundled_pack_dir
    from .template import build_prompt, parse_output
    from .verify import (apply_verdict, build_verify_prompt, parse_verdict,
                         verifier_model)
    from .writer import DerivationWriter
    from ...features.facts.llm_extract import _resolved_extraction_model  # type: ignore
    from ...config.settings import settings as _settings
    from ...engine.backends.ollama import OllamaAdapter

    pack_dir = bundled_pack_dir()
    _T.set_pack_dir(pack_dir)
    row = conn.execute("SELECT enabled FROM pack_registry WHERE pack_id=?", (pack_id,)).fetchone()
    if not row or not row[0]:
        raise ValueError(f"pack {pack_id} is not enabled")
    pack = load_packs(pack_dir, only=[pack_id]).get(pack_id)
    if pack is None:
        raise ValueError(f"unknown pack {pack_id}")
    owner_row = conn.execute("SELECT entity_id FROM entities WHERE is_self=1").fetchone()
    if not owner_row:
        raise ValueError("no owner entity")
    owner = owner_row[0]
    model = _resolved_extraction_model(_settings, conn)
    vmodel = verifier_model()
    adapter = OllamaAdapter()

    def llm(m, prompt, n=900):
        out = adapter._generate(m, prompt, num_predict=n, think=False, temperature=0.0,
                                num_ctx=8192, timeout=180)
        return str(out.get("text") or "") if isinstance(out, dict) else str(out or "")

    verify_mode = _verify_mode()
    pf = PackPrefilter(pack)
    writer = DerivationWriter(conn, model=model)
    done = {r[0] for r in conn.execute("SELECT key FROM derivation_progress")}
    stats = {"processed": 0, "assertions": 0, "accepted": 0, "written": 0, "quarantined0": writer.stats.get("quarantined", 0)}
    for rec in _iter_history(conn, limit=20000):
        if stats["processed"] >= limit:
            break
        key = f"{pack_id}@{pack.version}:{rec['table']}:{rec['record_id']}"
        if key in done:
            continue
        if rec["role"] not in pack.allowed_roles() or not pf.passes(rec["text"]):
            conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
            continue
        stats["processed"] += 1
        try:
            raw = llm(model, build_prompt(pack, rec["text"], rec["date"], rec["role"]))
            _bump_yield(conn, pack_id, llm_calls=1, prefilter_hits=1)
        except Exception:  # noqa: BLE001
            continue
        valid, _rej = parse_output(raw, pack, record_text=rec["text"])
        stats["assertions"] += len(valid)
        _bump_yield(conn, pack_id, assertions=len(valid))
        for a in valid:
            if verify_mode != "off":
                try:
                    verdict = parse_verdict(llm(vmodel, build_verify_prompt(
                        rec["text"], rec["role"], rec["date"],
                        a["predicate"], a["value"], a.get("about", "owner")), n=250))
                except Exception:  # noqa: BLE001
                    verdict = None
                a = apply_verdict(a, verdict)
            else:
                a = dict(a); a["verifier_status"] = "skipped"
            if a.get("verifier_status") == "rejected":
                _ledger_write(conn, stage="backfill", pack=pack, rec=rec, a=a,
                              model=model, vmodel=vmodel)
                continue
            stats["accepted"] += 1
            _bump_yield(conn, pack_id, accepted=1)
            out = writer.assert_pack_fact(
                pack=pack, predicate=a["predicate"], subject_entity_id=owner,
                value=a["value"], actor_role=rec["role"],
                source_refs=[{"table": rec["table"], "record_id": rec["record_id"]}],
                confidence=a["confidence"], occurrence=a["occurrence"],
                quote=a.get("quote", ""), about=a.get("about", "owner"),
                event_date=rec["date"] or None)
            stored = out.get("outcome") in ("written", "corroborated", "retelling_merged",
                                            "field_update", "corrected", "superseded")
            if stored:
                stats["written"] += 1
                _bump_yield(conn, pack_id, written=1)
            _ledger_write(conn, stage="backfill", pack=pack, rec=rec, a=a,
                          model=model, vmodel=vmodel,
                          written_id=out.get("object_id") if stored else None)
        conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
        conn.commit()
    conn.commit()
    stats["quarantined"] = writer.stats.get("quarantined", 0) - stats.pop("quarantined0")
    return stats


def predicate_schemas(conn: sqlite3.Connection) -> Dict[str, Any]:
    """Per-predicate field schemas for the Fact Editor (W4.6): enum options,
    person-typed flags, free-text fields — straight from the bundled contracts,
    so the editor can never drift from what the writer accepts."""
    from .packs import load_packs
    from .registry import bundled_pack_dir

    out: Dict[str, Any] = {}
    for pid, pack in load_packs(bundled_pack_dir()).items():
        for pred in pack.predicates.values():
            fields: Dict[str, Any] = {}
            if pred.value_schema:
                for name, spec in pred.value_schema.items():
                    if isinstance(spec, dict) and spec.get("enum"):
                        fields[name] = {"kind": "enum", "options": list(spec["enum"])}
                    elif isinstance(spec, list):
                        fields[name] = {"kind": "enum", "options": list(spec)}
                    elif name in ("person", "member"):
                        fields[name] = {"kind": "person"}
                    else:
                        fields[name] = {"kind": "text"}
            elif pred.values:
                fields["__value__"] = {"kind": "enum", "options": list(pred.values)}
            else:
                fields["__value__"] = {"kind": "text"}
            out[pred.name] = {"pack": pid, "fields": fields,
                              "key_fields": list(pred.key_fields or []),
                              "temporal": pred.temporal}
    return out


def promote_conflict(conn: sqlite3.Connection, conflict_id: str, *,
                     subject_entity_id: str = "", new_person_name: str = "",
                     value: Any = None, to_owner: bool = False) -> Dict[str, Any]:
    """W4.6 — the review queue's 'Edit & add': the owner supplies the identity
    (or corrected fields) the machine refused to guess; the fact writes through
    the FULL writer path and the correction lands in the ledger as GOLD."""
    import json as _json
    import uuid as _uuid

    from .packs import load_packs
    from .registry import bundled_pack_dir
    from .writer import DerivationWriter

    row = conn.execute(
        "SELECT subject_entity_id, predicate, challenger_value, challenger_confidence,"
        " pack_id, source_refs_json, quote, about_hint, status"
        " FROM fact_conflicts WHERE conflict_id=?", (conflict_id,)).fetchone()
    if not row:
        raise ValueError(f"unknown conflict {conflict_id}")
    subj0, predicate, val_json, conf, pack_id, refs_json, quote, hint, status = row
    if status != "pending":
        raise ValueError(f"conflict {conflict_id} already {status}")
    if not pack_id:
        raise ValueError("this row predates provenance capture — dismiss and let re-extraction recreate it")
    pack = load_packs(bundled_pack_dir(), only=[pack_id]).get(pack_id)
    if pack is None:
        raise ValueError(f"unknown pack {pack_id}")
    try:
        stored_value = _json.loads(val_json or "null")
    except (ValueError, TypeError):
        stored_value = val_json
    final_value = value if value is not None else stored_value

    if to_owner:
        subject = conn.execute("SELECT entity_id FROM entities WHERE is_self=1").fetchone()[0]
    elif subject_entity_id:
        ok = conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (subject_entity_id,)).fetchone()
        if not ok:
            raise ValueError(f"unknown entity {subject_entity_id}")
        subject = subject_entity_id
    elif new_person_name.strip():
        subject = f"ent_{_uuid.uuid4().hex[:16]}"
        n = " ".join(new_person_name.strip().split())
        cols = {r[1] for r in conn.execute("PRAGMA table_info(entities)")}
        base = ["entity_id", "entity_type", "canonical_name", "normalized_name", "aliases_json", "is_self"]
        vals = [subject, "person", n, n.lower(), "[]", 0]
        for extra in ("created_at", "updated_at"):
            if extra in cols:
                base.append(extra); vals.append(None)  # filled by SQL below
        placeholders = ", ".join(
            "datetime('now')" if b in ("created_at", "updated_at") else "?" for b in base)
        conn.execute(
            f"INSERT INTO entities ({', '.join(base)}) VALUES ({placeholders})",
            [v for b, v in zip(base, vals) if b not in ("created_at", "updated_at")])
    else:
        raise ValueError("supply subject_entity_id, new_person_name, or to_owner")

    writer = DerivationWriter(conn, model="owner-promote")
    refs = _json.loads(refs_json or "[]")
    out = writer.assert_pack_fact(
        pack=pack, predicate=predicate, subject_entity_id=subject,
        value=final_value, actor_role="authored", source_refs=refs,
        confidence=1.0, quote=quote or "", about="owner")
    from ...storage.db.write_gate import commit_connection, with_db_write
    with with_db_write():
        conn.execute("UPDATE fact_conflicts SET status='accepted', updated_at=datetime('now')"
                     " WHERE conflict_id=?", (conflict_id,))
        try:
            conn.execute(
                """INSERT INTO derivation_training_ledger
                   (ledger_id, stage, pack_id, pack_version, predicate, value_json,
                    about, quote, confidence, vstatus, written_object_id)
                   VALUES (?, 'owner_promote', ?, ?, ?, ?, ?, ?, 1.0, 'accepted', ?)""",
                (f"dtl_{_uuid.uuid4().hex[:16]}", pack_id, pack.version, predicate,
                 _json.dumps(final_value, default=str), f"entity:{subject}",
                 quote or "", out.get("object_id")))
        except sqlite3.OperationalError:
            pass
        commit_connection(conn)
    return {"conflict_id": conflict_id, "outcome": out.get("outcome"),
            "object_id": out.get("object_id"), "subject_entity_id": subject}


def revise_fact(conn: sqlite3.Connection, object_id: str, *,
                value: Any = None, subject_entity_id: str = "",
                evidence_date: str = "", asserted_by: str = "") -> Dict[str, Any]:
    """W4.6 — owner revision of a live pack fact: closes the old row
    (reason=owner_revision, belief clock inherited), re-inserts through keying
    with confidence pinned 1.0. Field/date/subject edits only — a fact's KIND
    never changes here (that is reject + a new fact)."""
    import json as _json
    import uuid as _uuid

    from .packs import load_packs
    from .registry import bundled_pack_dir
    from .writer import DerivationWriter

    row = conn.execute(
        "SELECT object_key, payload_json, valid_from, ontology_id FROM signal_objects"
        " WHERE object_id=? AND object_type='fact' AND valid_to IS NULL", (object_id,)).fetchone()
    if not row:
        raise ValueError(f"no live fact {object_id}")
    key, pj, vf, pack_id = row
    if not pack_id:
        raise ValueError("legacy fact — use the verdict edit action")
    p = _json.loads(pj or "{}")
    predicate = str(p.get("predicate") or "")
    pack = load_packs(bundled_pack_dir(), only=[pack_id]).get(pack_id)
    if pack is None:
        raise ValueError(f"unknown pack {pack_id}")
    old_value = p.get("value_struct") if p.get("value_struct") is not None else p.get("object_value")
    final_value = value if value is not None else old_value
    subject = subject_entity_id or str(p.get("subject_entity_id") or "")
    if subject_entity_id:
        ok = conn.execute("SELECT 1 FROM entities WHERE entity_id=?", (subject_entity_id,)).fetchone()
        if not ok:
            raise ValueError(f"unknown entity {subject_entity_id}")

    from ...storage.db.write_gate import commit_connection, with_db_write
    with with_db_write():
        conn.execute("UPDATE signal_objects SET valid_to=?, updated_at=datetime('now'),"
                     " updated_by='owner_revision' WHERE object_id=?",
                     ((evidence_date or str(vf)), object_id))
        commit_connection(conn)
    writer = DerivationWriter(conn, model="owner-revise")
    out = writer.assert_pack_fact(
        pack=pack, predicate=predicate, subject_entity_id=subject,
        value=final_value, actor_role="authored",
        source_refs=p.get("source_refs") or [], confidence=1.0,
        quote=str(p.get("quote") or ""), about="owner",
        event_date=(evidence_date or str(vf)[:10]) or None)
    with with_db_write():
        try:
            conn.execute(
                """INSERT INTO derivation_training_ledger
                   (ledger_id, stage, pack_id, predicate, value_json, about, confidence,
                    vstatus, vreason, written_object_id)
                   VALUES (?, 'owner_edit', ?, ?, ?, ?, 1.0, 'accepted', ?, ?)""",
                (f"dtl_{_uuid.uuid4().hex[:16]}", pack_id, predicate,
                 _json.dumps(final_value, default=str), f"entity:{subject}",
                 f"revised_from:{object_id}", out.get("object_id")))
        except sqlite3.OperationalError:
            pass
        commit_connection(conn)
    return {"revised_from": object_id, "outcome": out.get("outcome"),
            "object_id": out.get("object_id")}
