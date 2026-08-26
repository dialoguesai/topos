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


def run_derivation_batch(
    conn,
    rows: List[Dict[str, Any]],
    *,
    cancel: Optional[threading.Event] = None,
    stats: Optional[Dict[str, Any]] = None,
) -> int:
    """Sync worker (runs in a thread). Returns facts written+routed."""
    from ....features.derivation import template as _T
    from ....features.derivation.packs import load_packs  # noqa: F401  (registry loads)
    from ....features.derivation.prefilter import PackPrefilter
    from ....features.derivation.registry import (bundled_pack_dir, enabled_packs,
                                                  mark_pack_run, seed_pack_registry)
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
    packs = enabled_packs(conn, pack_dir)
    if not packs:
        st["skipped"] = "no_enabled_packs"
        return 0
    owner_row = conn.execute("SELECT entity_id FROM entities WHERE is_self=1").fetchone()
    if not owner_row:
        st["skipped"] = "no_owner_entity"
        return 0
    owner = owner_row[0]
    model = _resolved_extraction_model(_settings, conn)
    vmodel = verifier_model()
    adapter = OllamaAdapter()

    def llm(m: str, prompt: str, n: int = 900) -> str:
        out = adapter._generate(m, prompt, num_predict=n, think=False,
                                temperature=0.0, num_ctx=None, timeout=180)
        return str(out.get("text") or "") if isinstance(out, dict) else str(out or "")

    verify_mode = _verify_mode()
    if verify_mode == "required":
        try:
            probe = llm(vmodel, "Respond with exactly: ok", n=8)
            if not probe.strip():
                raise RuntimeError("empty verifier probe")
        except Exception as exc:  # noqa: BLE001
            st["deferred"] = f"verifier_unavailable: {exc}"[:120]
            logger.warning("derivation deferred: verifier %s unavailable (%s)", vmodel, exc)
            return 0

    filters = {pid: PackPrefilter(p) for pid, p in packs.items()}
    writer = DerivationWriter(conn, model=model)
    done = {r[0] for r in conn.execute("SELECT key FROM derivation_progress")}
    written = 0
    for row in rows:
        if cancel is not None and cancel.is_set():
            st["cancelled"] = True
            break
        rec = _row_to_record(row)
        if rec is None:
            continue
        for pid, pack in packs.items():
            key = f"{pid}@{pack.version}:{rec['table']}:{rec['record_id']}"
            if key in done:
                continue
            if rec["role"] not in pack.allowed_roles():
                continue
            if not filters[pid].passes(rec["text"]):
                conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
                continue
            try:
                raw = llm(model, build_prompt(pack, rec["text"], rec["date"], rec["role"]))
            except Exception as exc:  # noqa: BLE001
                st["llm_errors"] = st.get("llm_errors", 0) + 1
                logger.debug("derivation llm error on %s: %s", key, exc)
                continue  # no progress mark -> retried next batch
            valid, rejects = parse_output(raw, pack)
            st["parse_rejects"] = st.get("parse_rejects", 0) + rejects
            for a in valid:
                if verify_mode == "off":
                    a = dict(a); a["verifier_status"] = "skipped"
                else:
                    try:
                        verdict = parse_verdict(llm(vmodel, build_verify_prompt(
                            rec["text"], rec["role"], rec["date"],
                            a["predicate"], a["value"], a.get("about", "owner")), n=250))
                    except Exception:  # noqa: BLE001
                        verdict = None
                    a = apply_verdict(a, verdict)
                st[f"v_{a.get('verifier_status')}"] = st.get(f"v_{a.get('verifier_status')}", 0) + 1
                if a.get("verifier_status") == "rejected":
                    continue
                out = writer.assert_pack_fact(
                    pack=pack, predicate=a["predicate"], subject_entity_id=owner,
                    value=a["value"], actor_role=rec["role"],
                    source_refs=[{"table": rec["table"], "record_id": rec["record_id"],
                                  "source_id": rec["source_id"]}],
                    confidence=a["confidence"],
                    occurrence=a["occurrence"],   # NEVER defaulted to record date
                    quote=a.get("quote", ""), about=a.get("about", "owner"),
                    event_date=rec["date"] or None)
                if out.get("outcome") in ("written", "corroborated", "retelling_merged",
                                          "field_update", "corrected", "superseded"):
                    written += 1
            conn.execute("INSERT OR REPLACE INTO derivation_progress (key) VALUES (?)", (key,))
        conn.commit()
    for pid, pack in packs.items():
        mark_pack_run(conn, pid, pack.version)
    conn.commit()
    st["written"] = written
    st.update({f"w_{k}": v for k, v in writer.stats.items() if v})
    return written


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
