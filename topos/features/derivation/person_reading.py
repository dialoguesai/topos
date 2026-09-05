"""The reading — a critic's pass over ONE relationship, from cited rows only.

Item 3 of `PLAN_SOCIAL_GRAPH_PERSON_QUALITIES.md`. The card can measure a relationship
(archetype, warmth, drift) and list what the owner wrote about a person; what it could
not do was READ those rows together — the arc, the motifs, the register, the tension, the
role each plays for the other. That is an interpretation, and interpretations are where a
model is most fluent and least honest, so three rules make this criticism rather than
sludge:

1. **Inputs are rows, not vibes.** The model receives a numbered evidence list assembled
   here — the owner's own sentences about the person, the owner's relationship facts, the
   measured shape of the exchange, what they do together, what they have heard about —
   and nothing else. It never sees the person's own messages: this lane is first-party
   by construction (the owner's record of the relationship), which is why it needs no
   outward pack and no consent row.
2. **Every sentence cites, and a checker drops the ones that do not.** `parse_reading`
   keeps a sentence only when it ends with at least one valid `[eN]` reference. Code,
   not prompt: the home-chat lane learned that a rule stated in a prompt is a rule the
   model keeps until it does not.
3. **The subject is the dyad.** "Who this person is to you" — the card's question and the
   privacy boundary. A person profile is the F5 red-team threat; a reading of your own
   relationship is your own record, read back.

Compute rule (owner, 2026-09-05): model time only for people the existing filters already
rank — a measured tie (above the evidence floor) or at least MIN_OWNER_ROWS owner-written
sentences — and only when the evidence hash changed. Readings are stored as
`signal_objects` (`person_reading`, disclosure owner_only) and attached to the person node
at read time; nothing here runs inside a request.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("topos.features.derivation.person_reading")

READING_VERSION = "1"
OBJECT_TYPE = "person_reading"
DIMENSION = "relationships"

#: The evidence list is bounded so a reading of a close friend does not cost a novel's
#: worth of prefill on a local model; the rows are ordered owner-written first, newest
#: first, so what is dropped is the oldest measured filler.
MAX_EVIDENCE_ROWS = 30
#: One row's excerpt. The card's appearance snippets stop at 220 chars, which is right for
#: a list and too short for a reading — a journal sentence about a person is often the
#: second half of a longer one.
EXCERPT_CHARS = 480
#: Eligibility: a person the owner has written about at least this often qualifies even
#: without a messaging tie (a mother written about monthly, a collaborator on a different
#: channel). Measured live: 19 people clear it.
MIN_OWNER_ROWS = 5
#: Per run, so a first pass on a big node is bounded. The debounce re-arms on the next sync.
MAX_PER_RUN = 60
#: Sentences the model is asked for. Fewer reads as a chip, more reads as a dossier.
MIN_SENTENCES, MAX_SENTENCES = 4, 8

_REF = re.compile(r"\[(e\d+(?:\s*,\s*e\d+)*)\]")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'(\[])")


# --------------------------------------------------------------------------- evidence

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _excerpt(text: Any) -> str:
    from ...analytics.person_graph import _collapse_ws, _scrub_identifier_shapes

    body = _scrub_identifier_shapes(_collapse_ws(text))
    return body[:EXCERPT_CHARS]


def assemble_evidence(conn: Any, node: Dict[str, Any], *,
                      relationship: Optional[Dict[str, Any]] = None,
                      archetype: Optional[Dict[str, Any]] = None,
                      warmth: Optional[Dict[str, Any]] = None,
                      drift: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """Numbered rows the model may cite, and nothing it may not.

    Order is the priority under the cap: the owner's own sentences (newest first), then
    the owner's relationship facts, then what they do together / have heard about / share,
    then the measured shape. Every row carries where it came from so the card can open it.
    """
    from ...analytics.person_graph import _load_appearance_record_texts, batch_person_appearances

    rows: List[Dict[str, Any]] = []

    # 1. what the owner wrote about them — the record, in the owner's words
    packed = batch_person_appearances(conn, [node], show=200, fetch=400)
    mentions = (packed.get(str(node.get("node_id") or ""), {}) or {}).get("mentions") or []
    owner_rows = [m for m in mentions if m.get("authored_by_owner") and m.get("text")]
    owner_rows.sort(key=lambda m: str(m.get("at") or ""), reverse=True)
    full = _load_appearance_record_texts(conn, [m.get("record_id") for m in owner_rows if m.get("record_id")])
    for m in owner_rows:
        text = _excerpt(full.get(str(m.get("record_id") or "")) or m.get("text"))
        if not text:
            continue
        rows.append({"kind": "owner_wrote", "text": text, "at": (m.get("at") or "")[:10] or None,
                     "table": m.get("source_label") or m.get("source_id") or "",
                     "record_id": m.get("record_id") or "",
                     # the connector the record came from — what makes the provenance ref
                     # ATTRIBUTED, and therefore the attribution sweep's to trim, never the
                     # drift sweep's to reap (see `provenance_refs`)
                     "source_id": m.get("source_id") or ""})

    # 2. the owner's relationship facts about them
    for f in node.get("facts") or []:
        bits = []
        if f.get("event"):
            bits.append(f"relationship event: {f['event']}")
        if f.get("tier"):
            bits.append(f"closeness tier: {f['tier']} "
                        f"({'you wrote this' if f.get('stated_by_owner') else 'inferred from your data'})")
        if f.get("quote"):
            bits.append(f"\"{_excerpt(f['quote'])}\"")
        if bits:
            rows.append({"kind": "fact", "text": "; ".join(bits), "at": (f.get("at") or "")[:10] or None,
                         "table": f.get("pack") or "facts", "record_id": ""})

    # 3. what you do together, what they have heard about, what you share
    co = node.get("coactivity")
    if co:
        also = ", ".join(f"{a['label']} ({a['sessions']})" for a in (co.get("also") or []))
        rows.append({"kind": "coactivity", "at": (co.get("last_at") or "")[:10] or None,
                     "table": "journal", "record_id": "",
                     "text": (f"you logged {co['sessions']} session(s) together on {co['label']}"
                              + (f"; also {also}" if also else "") + " — declared by you, not inferred")})
    heard = node.get("heard_about")
    if heard and heard.get("items"):
        items = ", ".join(f"{i['label']} ({i['events']}×, last {i.get('last_at') or '?'})"
                          for i in heard["items"])
        rows.append({"kind": "heard_about", "at": None, "table": "your messages", "record_id": "",
                     "text": f"you have told them about your work: {items}"})
    shared = node.get("shared_with_owner")
    if shared and shared.get("examples"):
        rows.append({"kind": "shared", "at": None, "table": "mentions", "record_id": "",
                     "text": (f"you both engage with {shared.get('label') or 'the same subjects'}: "
                              + ", ".join(shared["examples"][:4]))})

    # 4. the measured shape — sentences the engine already composed, cited as measures
    if archetype and archetype.get("sentence"):
        rows.append({"kind": "measure", "at": None, "table": "directed edges", "record_id": "",
                     "text": f"how the exchange behaves: {archetype['sentence']}"})
    if warmth and warmth.get("warmth_band"):
        text = f"warmth: {warmth['warmth_band']}"
        if drift:
            text += f"; running at {round(float(drift.get('drift_ratio') or 0) * 100)}% of its own usual rate"
        rows.append({"kind": "measure", "at": None, "table": "dyad stats", "record_id": "", "text": text})
    if relationship:
        sent, received = int(relationship.get("sent") or 0), int(relationship.get("received") or 0)
        first, last = relationship.get("first_ts"), relationship.get("last_ts")
        rows.append({"kind": "measure", "at": None, "table": "dyad stats", "record_id": "",
                     "text": (f"{sent + received} messages between you, {received} from them, "
                              f"from {str(first or '?')[:10]} to {str(last or '?')[:10]}; "
                              f"{int(relationship.get('reciprocal_streak_weeks') or 0)} weeks "
                              f"both ways at the longest")})

    rows = rows[:MAX_EVIDENCE_ROWS]
    for i, r in enumerate(rows, 1):
        r["id"] = f"e{i}"
    return rows


def evidence_hash(evidence: List[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for r in evidence:
        h.update(f"{r.get('kind')}|{r.get('at')}|{r.get('text')}\n".encode("utf-8"))
    h.update(f"v{READING_VERSION}".encode("utf-8"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- the prompt

PROMPT = """You are a literary critic reading ONE relationship from the owner's seat — who this person is to the owner, as the owner's own record shows it.

Below is a numbered list of evidence rows. Every row is something the owner wrote, recorded, or measured about this relationship. It is the ONLY material you may use.

Write a reading of {min_s} to {max_s} sentences, in plain English, about the person named "{label}".
Structure it as a critic would: the ARC (how it began, where it is heading), the MOTIFS (what keeps coming up), the REGISTER (how the exchange is conducted), the TENSION (conflict, silence, imbalance — only if the evidence shows it), and the ROLE (what each is for the other).

Rules, all of them hard:
- EVERY sentence must end with the citations that support it, in the form [e3] or [e2, e5]. A sentence with no citation will be deleted.
- Never state a feeling, motive, diagnosis or trait the cited rows do not show. If the evidence is thin, write fewer sentences.
- Refer to the person as "{label}" or "they". Address the owner as "you".
- No headings, no bullet points, no preamble — sentences only.

Evidence:
{evidence}

Reading:"""


def build_prompt(label: str, evidence: List[Dict[str, Any]]) -> str:
    lines = []
    for r in evidence:
        when = f" ({r['at']})" if r.get("at") else ""
        lines.append(f"[{r['id']}] {r['kind']}{when}: {r['text']}")
    return PROMPT.format(label=label, min_s=MIN_SENTENCES, max_s=MAX_SENTENCES,
                         evidence="\n".join(lines))


# --------------------------------------------------------------------------- the checker

def parse_reading(text: str, valid_ids: List[str]) -> Dict[str, Any]:
    """Keep only sentences that cite real evidence. Returns sentences + how many were dropped.

    This is the rule that makes the output a reading rather than a story: a fluent
    sentence with no row behind it is exactly the kind of claim this feature must not
    make, and the model will make it whenever the evidence runs out.
    """
    valid = set(valid_ids)
    body = str(text or "").strip()
    # strip a leaked heading or preamble line
    body = re.sub(r"^(reading|here is .*?)[:\-]\s*", "", body, flags=re.IGNORECASE)
    body = body.replace("\n", " ")
    pieces = [p.strip() for p in _SENTENCE_SPLIT.split(body) if p.strip()]
    kept: List[Dict[str, Any]] = []
    dropped = 0
    for piece in pieces:
        refs: List[str] = []
        for m in _REF.finditer(piece):
            for tok in m.group(1).split(","):
                tok = tok.strip()
                if tok in valid and tok not in refs:
                    refs.append(tok)
        clean = _REF.sub("", piece).strip()
        clean = re.sub(r"\s+([.,;!?])", r"\1", clean)
        clean = re.sub(r"\s{2,}", " ", clean)
        if not refs or not clean or len(clean) < 12:
            dropped += 1
            continue
        kept.append({"text": clean, "refs": refs})
    return {"sentences": kept, "dropped": dropped}


# --------------------------------------------------------------------------- the model

def choose_model(configured: str, loaded: List[str]) -> str:
    """Use what is warm. Measured 2026-09-05: the home-chat 27B held 15.7GB of VRAM, and
    asking Ollama for the configured 9B extraction model on top of it timed out at 240s
    before the first token — the swap cost the reading AND the chat. When the configured
    model is not resident and another one is, the reading rides the resident model and
    says so in `model`; when nothing is resident, the configured model loads as usual."""
    if configured in loaded or not loaded:
        return configured
    return loaded[0]


def _loaded_models(base_url: str) -> List[str]:
    import urllib.request

    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/api/ps", timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8") or "{}")
        return [str(m.get("name") or "") for m in data.get("models", []) if m.get("name")]
    except Exception:  # noqa: BLE001 — unknown is "nothing resident"
        return []


def _default_llm(conn: Any) -> Callable[[str], str]:
    from ...config.settings import settings as _settings
    from ...engine.backends.ollama import OllamaAdapter
    from ...features.facts.llm_extract import _resolved_extraction_model

    configured = _resolved_extraction_model(_settings, conn)
    adapter = OllamaAdapter()
    model = choose_model(configured, _loaded_models(adapter.base_url if hasattr(adapter, "base_url")
                                                    else _settings.engine_ollama_base_url))
    if model != configured:
        logger.info("person readings: %s is resident, using it instead of %s", model, configured)

    def llm(prompt: str) -> str:
        # 420s: a 30-row reading measured 68s on the resident 27B with the GPU shared with
        # home chat, and 126s under contention; the timeout is a ceiling, not a budget.
        out = adapter._generate(model, prompt, num_predict=700, think=False, temperature=0.2,
                                num_ctx=8192, timeout=420)
        return str(out.get("text") or "") if isinstance(out, dict) else str(out or "")

    llm.model_name = model  # type: ignore[attr-defined]
    return llm


# --------------------------------------------------------------------------- the run

def eligible(node: Dict[str, Any], owner_rows: int) -> Optional[str]:
    """Why this node gets a reading, or None. The reasons are the compute rule."""
    if node.get("is_owner") or node.get("dismissed"):
        return None
    if node.get("needs_name"):
        return None  # D-F: a bare identifier is not a nameable subject
    if node.get("closeness") is not None:
        return "measured_tie"
    if owner_rows >= MIN_OWNER_ROWS:
        return "written_about"
    return None


def load_person_readings(conn: Any) -> Dict[str, Dict[str, Any]]:
    """node_id -> stored reading payload, active rows only."""
    try:
        rows = conn.execute(
            "SELECT object_key, payload_json FROM signal_objects"
            " WHERE object_type=? AND valid_to IS NULL", (OBJECT_TYPE,)).fetchall()
    except sqlite3.Error:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    for key, payload in rows:
        try:
            data = json.loads(payload or "{}")
        except (TypeError, ValueError):
            continue
        nid = str(data.get("node_id") or str(key).replace("reading:", "", 1))
        out[nid] = data
    return out


def provenance_refs(node: Dict[str, Any], evidence: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Where the reading came from, in the shape the lifecycle sweeps understand.

    THE BUG this replaces: the first live pass wrote `{"table": "person_graph",
    "record_id": "<node id>"}` — a synthetic table the derived-drift sweep could not find
    a record in, so 32 minutes after they were written all twelve readings were judged
    to have lost their evidence and closed. Two rules from `close_dangling_facts`, read
    rather than guessed: a ref carrying `source_id` is the ATTRIBUTION sweep's to trim
    (when that connector scrubs) and counts as live here; a spine id (`ent_…`) resolves
    against `entities` whatever table the ref names. So every record-backed evidence row
    becomes an attributed ref, and an entity-keyed person adds the spine ref. A reading
    with neither keeps one unverifiable ref, which the sweep treats as alive on purpose —
    "closing a real fact is worse than keeping a stale one an extra sweep".
    """
    refs: List[Dict[str, Any]] = []
    seen = set()
    for r in evidence:
        rid = str(r.get("record_id") or "")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        refs.append({"table": str(r.get("table") or ""), "record_id": rid,
                     "source_id": str(r.get("source_id") or ""), "kind": r.get("kind")})
    eid = str(node.get("entity_id") or "")
    if eid:
        refs.append({"table": "entities", "record_id": eid, "kind": "subject"})
    if not refs:
        refs.append({"table": "", "record_id": "", "note": "person_graph node "
                     + str(node.get("node_id") or ""), "kind": "unverifiable"})
    return refs


def build_reading(node: Dict[str, Any], evidence: List[Dict[str, Any]], llm: Callable[[str], str],
                  *, reason: str, model_name: str = "") -> Dict[str, Any]:
    label = str(node.get("label") or "this person")
    raw = llm(build_prompt(label, evidence)) if evidence else ""
    parsed = parse_reading(raw, [r["id"] for r in evidence])
    counts: Dict[str, int] = {}
    for r in evidence:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return {
        "version": READING_VERSION,
        "node_id": node.get("node_id"),
        "label": label,
        "subject": "dyad",
        "disclosure": "owner_only",
        "reason": reason,
        "model": model_name,
        "computed_at": _now(),
        "evidence_hash": evidence_hash(evidence),
        "sentences": parsed["sentences"],
        "dropped_uncited": parsed["dropped"],
        "evidence": evidence,
        "coverage": {
            "rows": len(evidence), "by_kind": counts,
            "basis": ("read from the owner's own sentences, relationship facts and measured "
                      "shape — never from the person's own messages; every sentence cites "
                      "the rows it was read from and uncited sentences were deleted"),
        },
    }


def refresh_person_readings(conn: Any, dataset_id: str, *, llm: Optional[Callable[[str], str]] = None,
                            nodes: Optional[List[Dict[str, Any]]] = None,
                            signals: Optional[Dict[str, Any]] = None,
                            relationships: Optional[Dict[str, Dict[str, Any]]] = None,
                            limit: int = MAX_PER_RUN, force: bool = False) -> Dict[str, Any]:
    """Write a reading for every eligible person whose evidence changed. Deferred lane only."""
    from ...features.lifecycle.blackhole import BlackholeStore
    from ...features.signal.signal_object_store import SignalObjectStore

    if nodes is None:
        from ...analytics.relationship_reads import read_person_graph
        nodes = read_person_graph(conn, dataset_id=dataset_id).get("nodes") or []
    if signals is None or relationships is None:
        from ...analytics.relationship_reads import read_relationship_signals, read_relationships
        try:
            signals = signals or read_relationship_signals(conn, dataset_id=dataset_id, signal="all")
            rel = read_relationships(conn, dataset_id=dataset_id, limit=500)
            relationships = relationships or {r["peer_key"]: r for r in rel.get("relationships", [])}
        except Exception as exc:  # noqa: BLE001 — a reading without measures is still a reading
            logger.debug("readings: signals unavailable: %s", exc)
            signals, relationships = signals or {}, relationships or {}
    by_peer = {}
    for k in ("archetypes", "warmth", "drift_alarms"):
        for r in (signals or {}).get(k, []) or []:
            by_peer.setdefault(r["peer_key"], {})[k] = r

    llm = llm or _default_llm(conn)
    model_name = str(getattr(llm, "model_name", "") or "")
    existing = load_person_readings(conn)
    store = SignalObjectStore(conn)
    try:
        blackholes: Optional[BlackholeStore] = BlackholeStore(conn)
    except Exception:  # noqa: BLE001
        blackholes = None

    stats = {"considered": 0, "eligible": 0, "written": 0, "unchanged": 0, "skipped_blackholed": 0,
             "abstained": 0, "errors": 0}
    written = 0
    # The per-run budget goes to real ties first: closest measured relationship, then the
    # most written-about. A run that stops at `limit` has read the people who matter most.
    nodes = sorted(nodes, key=lambda n: (-(n.get("closeness") if n.get("closeness") is not None else -1.0),
                                         -int(n.get("mention_count") or 0)))
    for node in nodes:
        stats["considered"] += 1
        # evidence is loaded before eligibility because "written about" IS an evidence count
        try:
            peer = (node.get("messenger_keys") or [None])[0]
            per = by_peer.get(peer, {}) if peer else {}
            evidence = assemble_evidence(
                conn, node,
                relationship=(relationships or {}).get(peer) if peer else None,
                archetype=per.get("archetypes"), warmth=per.get("warmth"), drift=per.get("drift_alarms"))
        except Exception as exc:  # noqa: BLE001
            logger.debug("readings: evidence failed for %s: %s", node.get("node_id"), exc)
            stats["errors"] += 1
            continue
        owner_rows = sum(1 for r in evidence if r["kind"] == "owner_wrote")
        reason = eligible(node, owner_rows)
        if not reason:
            continue
        stats["eligible"] += 1
        if blackholes is not None and node.get("entity_id"):
            try:
                if blackholes.is_blackholed(str(node["entity_id"])):
                    stats["skipped_blackholed"] += 1
                    continue
            except Exception:  # noqa: BLE001
                pass
        prior = existing.get(str(node.get("node_id")))
        if prior and not force and prior.get("evidence_hash") == evidence_hash(evidence) \
                and prior.get("version") == READING_VERSION:
            stats["unchanged"] += 1
            continue
        if written >= limit:
            break
        try:
            payload = build_reading(node, evidence, llm, reason=reason, model_name=model_name)
        except Exception as exc:  # noqa: BLE001 — one person's failure never ends the run
            logger.warning("readings: model failed for %s: %s", node.get("node_id"), exc)
            stats["errors"] += 1
            continue
        if not payload["sentences"]:
            stats["abstained"] += 1
        try:
            store.upsert_object(
                DIMENSION, OBJECT_TYPE, f"reading:{node.get('node_id')}", payload,
                source_refs=provenance_refs(node, evidence),
                confidence=min(1.0, len(payload["sentences"]) / MAX_SENTENCES),
                extractor_version=f"person_reading_v{READING_VERSION}",
            )
            written += 1
            stats["written"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("readings: store failed for %s: %s", node.get("node_id"), exc)
            stats["errors"] += 1
    stats["model"] = model_name
    stats["ran_at"] = _now()
    return stats


# --------------------------------------------------------------------------- scheduling

_LOCK = threading.Lock()
_TIMER: Optional[threading.Timer] = None
_RUNNING = False
_LAST: Dict[str, Any] = {}


def _enabled() -> bool:
    return os.getenv("TOPOS_PERSON_READINGS", "on").strip().lower() not in {"off", "0", "false"}


def _run_now(dataset_id: str) -> None:
    global _RUNNING
    from ...core.state import close_thread_db_connection, get_db_connection

    with _LOCK:
        if _RUNNING:
            return
        _RUNNING = True
    try:
        conn = get_db_connection()
        if conn is None:
            return
        _LAST.update(refresh_person_readings(conn, dataset_id))
    except Exception as exc:  # noqa: BLE001 — a deferred lane never breaks its caller
        logger.warning("person readings failed: %s", exc)
        _LAST["error"] = str(exc)[:200]
    finally:
        with _LOCK:
            _RUNNING = False
        close_thread_db_connection()


def mark_readings_due(dataset_id: str) -> None:
    """Messaging analytics were recomputed — schedule the debounced readings pass.

    A long debounce on purpose (default 10 minutes): the pass spends model time, a sync
    is often followed by an enrichment walk that changes the evidence again, and a
    reading computed on a half-enriched record would be re-read minutes later anyway.
    """
    global _TIMER
    if not _enabled() or not dataset_id:
        return
    delay = float(os.getenv("TOPOS_READINGS_DEBOUNCE_S", "600") or 600)
    with _LOCK:
        if _TIMER is not None:
            _TIMER.cancel()
        _TIMER = threading.Timer(delay, _run_now, args=(dataset_id,))
        _TIMER.daemon = True
        _TIMER.name = "topos-person-readings-debounce"
        _TIMER.start()


def readings_status() -> Dict[str, Any]:
    with _LOCK:
        return {"enabled": _enabled(), "pending": _TIMER is not None, "running": _RUNNING, **_LAST}
