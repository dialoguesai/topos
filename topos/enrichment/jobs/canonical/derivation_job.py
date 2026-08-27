"""DerivationJob (W2.3): ontology-pack fact derivation over canonical batches.

Per (record, enabled pack): lexical prefilter -> shadow-8 prompt -> extraction
model (qwen3.5-class; recall) -> parse with A1/A2 guards -> A4 verifier
(27B-class; precision, selection-shaped) -> DerivationWriter (A3 routing,
revision ladder, event-identity keying).

Model split is MEASURED, not aesthetic (2026-08-26 battery, 32 records):
extractor alone recurs 19/23 junk classes; verifier-as-extractor loses half the
gold; extract+verify hits ~1/23 junk while keeping recall. See
derivation-packs/pilot/w1_report.md.

Verification policy: TOPOS_DERIVATION_VERIFY=required (default) — if the
verifier model is unavailable the batch DEFERS rather than writing unverified
owner-facts; the junk gate was earned WITH the verifier in the loop. "off"
exists for explicit opt-out (assertions then flagged verifier_status=skipped).

Occurrence dates: an episodic assertion with no stated date stays UNDATED —
defaulting to the record date is how one firing became six events (a July
retelling minted a phantom July firing). The writer's event-identity keying
merges undated retellings into the original.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

from ..base import BaseEnrichmentJob
from ....core.state import get_db_connection

logger = logging.getLogger("topos.enrichment.jobs.derivation")


def derivation_enabled() -> bool:
    return os.environ.get("TOPOS_DERIVATION", "on").strip().lower() not in (
        "0", "false", "off", "no")


def _verify_mode() -> str:
    return os.environ.get("TOPOS_DERIVATION_VERIFY", "required").strip().lower()


def _row_to_record(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    text = str(row.get("content") or row.get("text") or row.get("title") or "")
    if len(text.strip()) <= 15:
        return None
    rid = str(row.get("record_id") or row.get("message_id") or row.get("entry_id")
              or row.get("event_id") or row.get("id") or "")
    if not rid:
        return None
    table = str(row.get("_table") or row.get("canonical_table") or "")
    role = str(row.get("actor_role") or ("authored" if "journal" in table else "observed"))
    date = str(row.get("event_at") or row.get("entry_at") or row.get("occurred_at") or "")[:10]
    return {"record_id": rid, "table": table or "canonical", "text": text,
            "role": role, "date": date,
            "source_id": str(row.get("source_id") or "")}


def _ledger_write(conn, *, stage, pack, rec, a, model, vmodel, written_id=None):
    """Training-data factory: EVERY judged assertion lands here, accepted or not
    (rejects are the hard negatives; quotes+record refs recover spans)."""
    import uuid as _uuid
    from ....features.derivation import template as _T
    conn.execute(
        """INSERT INTO derivation_training_ledger
           (ledger_id, stage, pack_id, pack_version, template_version, extract_model,
            verifier_model, source_table, record_id, actor_role, predicate, value_json,
            about, occurrence, quote, confidence, vstatus, vreason, written_object_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (f"dtl_{_uuid.uuid4().hex[:16]}", stage, pack.pack, pack.version,
         _T.TEMPLATE_VERSION, model, vmodel, rec["table"], rec["record_id"],
         rec["role"], a.get("predicate"), _json.dumps(a.get("value"), default=str),
         a.get("about"), a.get("occurrence"), a.get("quote"), a.get("confidence"),
         a.get("verifier_status"), a.get("verifier_reason"), written_id))


def _bump_yield(conn, pack_id, **cols):
    import datetime as _dt
    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
    conn.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day) VALUES (?, ?)", (pack_id, day))
    for col, inc in cols.items():
        if inc:
            conn.execute(f"UPDATE pack_yield SET {col} = {col} + ? WHERE pack_id=? AND day=?",
                         (int(inc), pack_id, day))


def _yield_window(conn, pack_id, days, col):
    row = conn.execute(
        f"SELECT COALESCE(SUM({col}),0) FROM pack_yield WHERE pack_id=? AND day >= date('now', ?)",
        (pack_id, f"-{int(days)} day")).fetchone()
    return int(row[0] or 0)


def _has_recent_offer(conn, pack_id, kind, days=30) -> bool:
    row = conn.execute(
        "SELECT 1 FROM pack_offers WHERE pack_id=? AND kind=? AND"
        " (status='pending' OR updated_at >= datetime('now', ?)) LIMIT 1",
        (pack_id, kind, f"-{int(days)} day")).fetchone()
    return row is not None


def _mint_offer(conn, pack_id, kind, stats):
    import uuid as _uuid
    conn.execute(
        "INSERT INTO pack_offers (offer_id, pack_id, kind, stats_json) VALUES (?,?,?,?)",
        (f"ofr_{_uuid.uuid4().hex[:12]}", pack_id, kind, _json.dumps(stats, default=str)))
    logger.info("pack offer minted: %s %s %s", kind, pack_id, stats)


#: self-gating knobs — deliberately conservative; the node OFFERS, the owner decides
TRIAL_MIN_HITS_14D = 25
TRIAL_SAMPLE_CAP = 30
TRIAL_MIN_ACCEPTED = 3
TRIAL_MIN_ACCEPT_RATE = 0.4
DRYWELL_MIN_CALLS_30D = 50


def run_derivation_batch(
    conn,
    rows: List[Dict[str, Any]],
    *,
    cancel: Optional[threading.Event] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> int:
    """Sync worker (runs in a thread). Returns facts written+routed."""
    from ....features.derivation import template as _T
    from ....features.derivation.prefilter import PackPrefilter
    from ....features.derivation.registry import (bundled_pack_dir, enabled_packs,
                                                  mark_pack_run, seed_pack_registry)
    from ....features.derivation.packs import load_packs
    from ....features.derivation.template import build_prompt, parse_output
    from ....features.derivation.verify import (build_verify_prompt, parse_verdict,
                                                apply_verdict, verifier_model)
    from ....features.derivation.writer import DerivationWriter
    from ....features.facts.llm_extract import _resolved_extraction_model
    from ....config.settings import settings as _settings
    from ....engine.backends.ollama import OllamaAdapter

    st = stats if stats is not None else {}
    pack_dir = bundled_pack_dir()
    _T.set_pack_dir(pack_dir)
    seed_pack_registry(conn, pack_dir)
    all_packs = load_packs(pack_dir)
    packs = enabled_packs(conn, pack_dir)
    from ....features.entities.owner import owner_entity_id
    _o = owner_entity_id(conn)
    owner_row = (_o,) if _o else None
    if not owner_row:
        st["skipped"] = "no_owner_entity"
        return 0
    owner = owner_row[0]
    model = _resolved_extraction_model(_settings, conn)
    vmodel = verifier_model()
    adapter = OllamaAdapter()

    def llm(m: str, prompt: str, n: int = 900) -> str:
        out = adapter._generate(m, prompt, num_predict=n, think=False,
                                temperature=0.0, num_ctx=8192, timeout=180)
        return str(out.get("text") or "") if isinstance(out, dict) else str(out or "")

    from ....features.derivation.router import router_mode
    _router_hybrid = router_mode() == "hybrid"
    verify_mode = _verify_mode()
    if verify_mode == "required" and packs:
        try:
            probe = llm(vmodel, "Respond with exactly: ok", n=8)
            if not probe.strip():
                raise RuntimeError("empty verifier probe")
        except Exception as exc:  # noqa: BLE001
            st["deferred"] = f"verifier_unavailable: {exc}"[:120]
            logger.warning("derivation deferred: verifier %s unavailable (%s)", vmodel, exc)
            return 0

    filters = {pid: PackPrefilter(pk) for pid, pk in all_packs.items()}
    writer = DerivationWriter(conn, model=model)
    done = {r[0] for r in conn.execute("SELECT key FROM derivation_progress")}

    HARM_ROLES = {"partner", "spouse", "child", "sibling", "ex_partner"}
    HARM_EVENTS = {"loss", "conflict", "reconnected", "ended", "estranged"}

    def _is_high_harm(a):
        v = a.get("value") if isinstance(a.get("value"), dict) else {}
        if a.get("predicate") == "rel.relationship" and str(v.get("role")) in HARM_ROLES:
            return True
        if a.get("predicate") == "rel.relationship_event" and str(v.get("event")) in HARM_EVENTS:
            return True
        return False

    def judge(pack, rec, a, stage):
        """Verify one assertion; high-harm predicate classes get 3-vote majority
        (measured 2026-08-26: single quantized-verifier votes are variance-exposed
        exactly where errors hurt most). Everything else keeps one vote."""
        if verify_mode == "off":
            a = dict(a); a["verifier_status"] = "skipped"
            return a
        votes_needed = 3 if _is_high_harm(a) else 1
        accepts = 0
        last_verdict = None
        for _ in range(votes_needed):
            try:
                verdict = parse_verdict(llm(vmodel, build_verify_prompt(
                    rec["text"], rec["role"], rec["date"],
                    a["predicate"], a["value"], a.get("about", "owner")), n=250))
            except Exception:  # noqa: BLE001
                verdict = None
            last_verdict = verdict or last_verdict
            if verdict and verdict["supported"] and verdict["fields_ok"]:
                accepts += 1
        if votes_needed > 1 and accepts < 2:
            a = dict(a); a["verifier_status"] = "rejected"
            a["verifier_reason"] = f"majority {accepts}/{votes_needed} against"
            return a
        return apply_verdict(a, last_verdict)

    written = 0
    for row in rows:
        if cancel is not None and cancel.is_set():
            st["cancelled"] = True
            break
        rec = _row_to_record(row)
        if rec is None:
            continue
        _rec_vec = None
        if _router_hybrid:
            from ....features.derivation.router import embed_record
            _rec_vec = embed_record(rec["text"])
        for pid, pack in all_packs.items():
            hit = rec["role"] in pack.allowed_roles() and filters[pid].passes(rec["text"])
            if not hit and _router_hybrid and rec["role"] in pack.allowed_roles():
                from ....features.derivation.router import semantic_passes
                hit = semantic_passes(pack, _rec_vec)
            if hit:
                _bump_yield(conn, pid, prefilter_hits=1)
            if pid not in packs:
                continue                     # disabled: counted for self-gating, never run
            key = f"{pid}@{pack.version}:{rec['table']}:{rec['record_id']}"
            if key in done or not hit:
                if hit:
                    conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
                continue
            try:
                raw = llm(model, build_prompt(pack, rec["text"], rec["date"], rec["role"]))
                _bump_yield(conn, pid, llm_calls=1)
            except Exception as exc:  # noqa: BLE001
                st["llm_errors"] = st.get("llm_errors", 0) + 1
                logger.debug("derivation llm error on %s: %s", key, exc)
                continue
            valid, rejects = parse_output(raw, pack, record_text=rec["text"])
            st["parse_rejects"] = st.get("parse_rejects", 0) + rejects
            _bump_yield(conn, pid, assertions=len(valid))
            for a in valid:
                a = judge(pack, rec, a, "ingest")
                st[f"v_{a.get('verifier_status')}"] = st.get(f"v_{a.get('verifier_status')}", 0) + 1
                if a.get("verifier_status") == "rejected":
                    _ledger_write(conn, stage="ingest", pack=pack, rec=rec, a=a,
                                  model=model, vmodel=vmodel)
                    continue
                _bump_yield(conn, pid, accepted=1)
                out = writer.assert_pack_fact(
                    pack=pack, predicate=a["predicate"], subject_entity_id=owner,
                    value=a["value"], actor_role=rec["role"],
                    source_refs=[{"table": rec["table"], "record_id": rec["record_id"],
                                  "source_id": rec["source_id"]}],
                    confidence=a["confidence"],
                    occurrence=a["occurrence"],   # NEVER defaulted to record date
                    quote=a.get("quote", ""), about=a.get("about", "owner"),
                    event_date=rec["date"] or None)
                stored = out.get("outcome") in ("written", "corroborated", "retelling_merged",
                                                "field_update", "corrected", "superseded")
                if stored:
                    written += 1
                    _bump_yield(conn, pid, written=1)
                _ledger_write(conn, stage="ingest", pack=pack, rec=rec, a=a,
                              model=model, vmodel=vmodel,
                              written_id=out.get("object_id") if stored else None)
            conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
        conn.commit()

    _drip_catchup(conn, packs=packs, filters=filters, llm=llm, judge=judge,
                  writer=writer, owner=owner, done=done, st=st, cancel=cancel)

    _self_gate(conn, all_packs=all_packs, enabled=set(packs), filters=filters,
               llm=llm, judge=judge, model=model, vmodel=vmodel, st=st,
               cancel=cancel)

    for pid, pack in packs.items():
        mark_pack_run(conn, pid, pack.version)
    conn.commit()
    st["written"] = written
    st.update({f"w_{k}": v for k, v in writer.stats.items() if v})
    return written


#: history records drip-processed per batch — the dark delta self-heals without
#: a scheduler; a fresh enablement's history closes over days, or immediately
#: via the owner's backfill control.
CATCHUP_PER_BATCH = 25


def _iter_history(conn, limit=2000):
    import sqlite3 as _sqlite3
    def _rows(sql):
        try:
            return conn.execute(sql).fetchall()
        except _sqlite3.OperationalError:
            return []
    out = []
    for tbl, rid, text, at, role in _rows(
            f"SELECT 'journal_entries', entry_id, content, entry_at, 'authored'"
            f" FROM journal_entries WHERE content IS NOT NULL AND LENGTH(content)>15"
            f" ORDER BY entry_at DESC LIMIT {int(limit)}"):
        out.append({"table": tbl, "record_id": rid, "text": (text or "")[:6000],
                    "date": str(at or "")[:10], "role": role or "authored", "source_id": ""})
    for tbl, rid, text, at, role in _rows(
            f"SELECT 'conversation_messages', message_id, content, event_at, actor_role"
            f" FROM conversation_messages WHERE content IS NOT NULL AND LENGTH(content)>15"
            f" ORDER BY event_at DESC LIMIT {int(limit)}"):
        out.append({"table": tbl, "record_id": rid, "text": (text or "")[:6000],
                    "date": str(at or "")[:10], "role": role or "observed", "source_id": ""})
    return out


def _drip_catchup(conn, *, packs, filters, llm, judge, writer, owner, done, st, cancel):
    """Process up to CATCHUP_PER_BATCH unprocessed HISTORY (record, pack) pairs
    per batch. Newest-first: recent history answers queries soonest."""
    if not packs:
        return
    from ....features.derivation.template import build_prompt, parse_output
    from ....features.facts.llm_extract import _resolved_extraction_model
    from ....config.settings import settings as _settings
    model = _resolved_extraction_model(_settings, conn)
    budget = CATCHUP_PER_BATCH
    for rec in _iter_history(conn):
        if budget <= 0 or (cancel is not None and cancel.is_set()):
            return
        for pid, pack in packs.items():
            key = f"{pid}@{pack.version}:{rec['table']}:{rec['record_id']}"
            if key in done:
                continue
            if rec["role"] not in pack.allowed_roles() or not filters[pid].passes(rec["text"]):
                conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
                done.add(key)
                continue
            budget -= 1
            try:
                raw = llm(model, build_prompt(pack, rec["text"], rec["date"], rec["role"]))
                _bump_yield(conn, pid, llm_calls=1, prefilter_hits=1)
            except Exception:  # noqa: BLE001
                st["llm_errors"] = st.get("llm_errors", 0) + 1
                continue
            valid, _rej = parse_output(raw, pack, record_text=rec["text"])
            _bump_yield(conn, pid, assertions=len(valid))
            for a in valid:
                a = judge(pack, rec, a, "catchup")
                if a.get("verifier_status") == "rejected":
                    _ledger_write(conn, stage="catchup", pack=pack, rec=rec, a=a,
                                  model=model, vmodel="(job)", )
                    continue
                _bump_yield(conn, pid, accepted=1)
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
                    _bump_yield(conn, pid, written=1)
                    st["catchup_written"] = st.get("catchup_written", 0) + 1
                _ledger_write(conn, stage="catchup", pack=pack, rec=rec, a=a,
                              model=model, vmodel="(job)",
                              written_id=out.get("object_id") if stored else None)
            conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
            done.add(key)
            if budget <= 0:
                break
        conn.commit()


def _self_gate(conn, *, all_packs, enabled, filters, llm, judge, model, vmodel, st, cancel):
    """Per-node self-gating (owner design, 2026-08-26): the node OFFERS, never
    switches. Enable offers come from a capped shadow trial on a disabled pack
    whose prefilter keeps hitting; disable nudges come from an enabled pack
    burning calls with zero yield. Special-class packs (health, beliefs) are
    NEVER auto-trialed — extracting their content requires the owner's opt-in
    first (consent precedes computation)."""
    from ....features.derivation.template import build_prompt, parse_output

    for pid, pack in all_packs.items():
        if cancel is not None and cancel.is_set():
            return
        if pid in enabled:
            calls = _yield_window(conn, pid, 30, "llm_calls")
            wrote = _yield_window(conn, pid, 30, "written")
            if calls >= DRYWELL_MIN_CALLS_30D and wrote == 0                     and not _has_recent_offer(conn, pid, "disable_nudge"):
                _mint_offer(conn, pid, "disable_nudge",
                            {"calls_30d": calls, "written_30d": 0})
            continue
        if getattr(pack, "sensitivity_class", "personal") == "special":
            continue
        hits = _yield_window(conn, pid, 14, "prefilter_hits")
        if hits < TRIAL_MIN_HITS_14D or _has_recent_offer(conn, pid, "enable_offer"):
            continue
        # sample recent prefilter-hit records for the trial
        sample = []
        import sqlite3 as _sqlite3
        def _rows(sql):
            try:
                return conn.execute(sql).fetchall()
            except _sqlite3.OperationalError:
                return []          # canonical table absent on this node — fine
        recent = _rows(
            """SELECT 'conversation_messages', message_id, content, event_at, actor_role
               FROM conversation_messages WHERE content IS NOT NULL AND LENGTH(content)>15
               AND event_at >= datetime('now','-14 day') ORDER BY event_at DESC LIMIT 400""")
        recent += _rows(
            """SELECT 'journal_entries', entry_id, content, entry_at, 'authored'
               FROM journal_entries WHERE content IS NOT NULL AND LENGTH(content)>15
               AND entry_at >= datetime('now','-14 day') ORDER BY entry_at DESC LIMIT 100""")
        for tbl, rid, text, at, role in recent:
            role = role or "observed"
            if role in pack.allowed_roles() and filters[pid].passes(text):
                sample.append({"table": tbl, "record_id": rid, "text": (text or "")[:6000],
                               "date": str(at or "")[:10], "role": role, "source_id": ""})
            if len(sample) >= TRIAL_SAMPLE_CAP:
                break
        if len(sample) < 5:
            continue
        n_assert = n_acc = 0
        examples = []
        for rec in sample:
            if cancel is not None and cancel.is_set():
                return
            try:
                raw = llm(model, build_prompt(pack, rec["text"], rec["date"], rec["role"]))
            except Exception:  # noqa: BLE001
                continue
            valid, _rej = parse_output(raw, pack, record_text=rec["text"])
            for a in valid:
                a = judge(pack, rec, a, "trial")
                n_assert += 1
                _ledger_write(conn, stage="trial", pack=pack, rec=rec, a=a,
                              model=model, vmodel=vmodel)
                if a.get("verifier_status") in ("accepted", "rerouted"):
                    n_acc += 1
                    if len(examples) < 3:
                        examples.append({"predicate": a["predicate"],
                                         "value": a["value"], "quote": a.get("quote", "")})
        conn.commit()
        st[f"trial_{pid}"] = {"sampled": len(sample), "assertions": n_assert, "accepted": n_acc}
        if n_acc >= TRIAL_MIN_ACCEPTED and n_assert and (n_acc / n_assert) >= TRIAL_MIN_ACCEPT_RATE:
            _mint_offer(conn, pid, "enable_offer",
                        {"sampled": len(sample), "assertions": n_assert,
                         "accepted": n_acc, "examples": examples})


class DerivationJob(BaseEnrichmentJob):
    def get_derived_table(self) -> str:
        return ""  # writes signal_objects via DerivationWriter

    def get_job_name(self) -> str:
        return "derivation"

    def should_run(self, canonical_messages: List[Dict[str, Any]]) -> bool:
        return bool(canonical_messages) and derivation_enabled()

    async def enrich(
        self,
        canonical_messages: List[Dict[str, Any]],
        progress_callback: Optional[Callable[[int, int], None]] = None,
    ) -> List[Dict[str, Any]]:
        conn = get_db_connection()
        if conn is None:
            return [{"_deferred": True, "error": "database_unavailable"}]
        cancel = threading.Event()
        stats: Dict[str, Any] = {}
        try:
            written = await asyncio.to_thread(
                run_derivation_batch, conn, canonical_messages,
                cancel=cancel, stats=stats)
        except asyncio.CancelledError:
            cancel.set()
            raise
        logger.info("derivation batch: %s written, stats=%s", written, stats)
        return [{"derivation_written": written, **stats}]
