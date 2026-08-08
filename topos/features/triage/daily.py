"""The daily attention-triage pass (PLAN_ATTENTION_TRIAGE.md M2).

`load_triage_delta` is the reference implementation of the M4 triage primitive:
one call returning a window's delta pre-joined with vocab, entities and
experience markers. `run_daily_triage` computes the formal instruments,
assigns 2x2 verdicts (+ floors, abstains, seeds), persists them to
`triage_verdicts`, and deposits the anabolic write-back into signal_objects
(dimension "interests": `interest_profile` + `attention_summary`).

The `attention_summary` payload is the digest substrate and MUST honor the
silence invariant: it never references, counts, or alludes to discarded items.
Discards exist only in `triage_verdicts` (the pull-only ledger).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import numpy as np

from ...storage.db.write_gate import batched_writes, commit_connection, with_db_write
from ..entities.declared_mappings import own_project_for_host, own_project_for_terms
from ..signal.signal_object_store import SignalObjectStore
from . import instruments, intents as intents_mod

ROUTINE_VERSION = "triage_v1"
LOOKBACK_DAYS = 30
FLOOR_KEYWORDS = (
    "deadline", "invoice", "payment", "due ", "apply", "application",
    "court", "tax", "legal", "contract", "rent",
)
SEED_CAP = 3


@dataclass
class TriageItem:
    record_id: str
    table: str
    source_id: str
    event_at: str
    day: str
    text: str = ""
    title: str = ""
    vocab: List[str] = field(default_factory=list)
    entities: List[str] = field(default_factory=list)
    authored: bool = False
    experienced: bool = True
    engagement_kind: str = "none"
    floor: bool = False
    floor_reason: str = ""
    visit_count: int = 1
    own_project: Optional[str] = None
    junk: bool = False
    response_expected: bool = False
    intent_sim: float = 0.0
    intent_name: Optional[str] = None
    comp_resid: Optional[float] = None
    comp_novelty: Optional[float] = None
    knn_surprisal: Optional[float] = None
    item_kl: Optional[float] = None
    attachment: Optional[float] = None
    gen_score: Optional[float] = None
    align_mass: Optional[float] = None
    aligned: bool = False
    verdict: str = ""
    grounds: List[str] = field(default_factory=list)
    seed_bond: Optional[str] = None


def _chunks(seq, n=400):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace(" ", "T"))
    except (ValueError, TypeError):
        return None


def load_triage_delta(conn: sqlite3.Connection, start_iso: str, end_iso: str) -> List[TriageItem]:
    """Timeline delta in [start, end), deduped, content-joined, vocab/experience-tagged."""
    rows = conn.execute(
        "SELECT record_id, source_id, canonical_table, event_at FROM timeline "
        "WHERE event_at >= ? AND event_at < ? ORDER BY event_at",
        (start_iso, end_iso),
    ).fetchall()
    all_ids = {r[0] for r in rows}
    items: Dict[str, TriageItem] = {}
    for record_id, source_id, table, event_at in rows:
        if record_id in items:
            continue
        if record_id.endswith("-loc") and record_id[:-4] in all_ids:
            continue
        items[record_id] = TriageItem(
            record_id=record_id, table=table or "", source_id=source_id or "",
            event_at=event_at, day=event_at[:10],
        )

    by_table = defaultdict(list)
    for it in items.values():
        by_table[it.table].append(it.record_id)

    def fetch(table, key, cols, ids):
        got = {}
        for chunk in _chunks(ids):
            q = f"SELECT {key} AS k, {cols} FROM {table} WHERE {key} IN ({','.join('?' * len(chunk))})"
            for row in conn.execute(q, chunk):
                got[row[0]] = row
        return got

    ae = fetch("activity_events", "event_id", "title, url, hostname",
               by_table.get("activity_events", []))
    je = fetch("journal_entries", "entry_id", "content, people, place_name, category",
               by_table.get("journal_entries", []))
    cm = fetch("conversation_messages", "message_id",
               "content, is_from_self, sender_id, conversation_id, event_at",
               by_table.get("conversation_messages", []))
    am = fetch("ai_chat_messages", "message_id", "content, sender_type",
               by_table.get("ai_chat_messages", []))
    le = fetch("location_events", "event_id", "place_name, city",
               by_table.get("location_events", []))

    self_msg_times = defaultdict(list)
    for row in cm.values():
        if row[2]:
            self_msg_times[row[4]].append(row[5])

    for it in items.values():
        if it.table == "activity_events" and it.record_id in ae:
            _, title, url, hostname = ae[it.record_id]
            it.title = (title or url or "")[:140]
            it.text = " ".join(filter(None, [title, hostname]))
            host = hostname or (urlparse(url).netloc if url else None)
            if host:
                host = host.lower().removeprefix("www.")
                it.vocab.append("host:" + host)
                it.own_project = own_project_for_host(host)
                if it.own_project:
                    it.vocab.append("ent:" + it.own_project.lower())
            it.experienced, it.engagement_kind = True, "visited"
            if not it.own_project:
                it.own_project = own_project_for_terms(it.title)  # F4 term bonds
        elif it.table == "journal_entries" and it.record_id in je:
            _, content, people, place_name, category = je[it.record_id]
            it.text = (content or "")[:4000]
            it.title = f"journal ({category or 'entry'}) @ {place_name or '?'}"
            if category:
                it.vocab.append("jcat:" + category.lower())
            if place_name:
                it.vocab.append("place:" + _norm_area(place_name))
            for p in (people or "").split(","):
                if p.strip() and p.strip().lower() != "solo":
                    it.vocab.append("person:" + p.strip().lower())
            it.authored, it.experienced, it.engagement_kind = True, True, "authored"
        elif it.table == "conversation_messages" and it.record_id in cm:
            _, content, is_from_self, _sender, conv_id, ev = cm[it.record_id]
            it.text = (content or "")[:2000]
            it.title = ("me: " if is_from_self else "msg: ") + it.text[:80]
            it.authored = bool(is_from_self)
            it.response_expected = intents_mod.response_expected(it.text)  # F3
            if it.authored:
                it.experienced, it.engagement_kind = True, "authored"
            else:
                base = _parse_ts(ev)
                replied = any(
                    base and (t2 := _parse_ts(t)) and 0 < (t2 - base).total_seconds() < 36 * 3600
                    for t in self_msg_times.get(conv_id, [])
                )
                it.experienced = replied
                it.engagement_kind = "replied" if replied else "none"
            it.floor, it.floor_reason = True, "p2p-message"
        elif it.table == "ai_chat_messages" and it.record_id in am:
            _, content, sender_type = am[it.record_id]
            it.text = (content or "")[:2000]
            it.title = f"ai-chat ({sender_type}): " + it.text[:80]
            it.authored = sender_type in ("user", "human")
            it.experienced, it.engagement_kind = True, ("authored" if it.authored else "visited")
        elif it.table == "location_events" and it.record_id in le:
            _, place_name, city = le[it.record_id]
            it.title = f"location: {place_name or city or '?'}"
            it.text = it.title
            if place_name:
                it.vocab.append("place:" + _norm_area(place_name))
            it.engagement_kind = "ambient"

        if instruments.is_blob(it.text):
            it.junk = True  # BT9: serialized-object noise — abstains, never a quadrant

        if it.authored:
            it.floor, it.floor_reason = True, it.floor_reason or "self-authored"
        low = it.text.lower()
        if any(k in low for k in FLOOR_KEYWORDS):
            it.floor, it.floor_reason = True, (it.floor_reason + "+keyword").lstrip("+")

    for chunk in _chunks(list(items)):
        # WS1.2: resolved support — entity ids (confidence-gated), not surface strings.
        q = ("SELECT m.record_id, e.entity_id, m.confidence "
             "FROM entity_mentions m JOIN entities e ON e.entity_id = m.entity_id "
             f"WHERE m.record_id IN ({','.join('?' * len(chunk))})")
        for rid, eid, confidence in conn.execute(q, chunk):
            it = items.get(rid)
            if it is None or (confidence is not None and confidence < 0.5):
                continue
            it.entities.append(eid)
            it.vocab.append("ent:" + eid)
        try:
            q2 = (f"SELECT record_id, cluster_id FROM topic_cluster_members "
                  f"WHERE record_id IN ({','.join('?' * len(chunk))})")
            for rid, cid in conn.execute(q2, chunk):
                it = items.get(rid)
                if it is not None and cid:
                    it.vocab.append("cluster:" + str(cid))
        except sqlite3.OperationalError:
            pass  # cluster tables absent (minimal DBs) — π works without them
    return sorted(items.values(), key=lambda x: x.event_at)


def _norm_area(place: str) -> str:
    """Area-level place token (WS1.2): 'Williamsburg- Greta's' → 'williamsburg'.
    Collapses the venue fragment and the common '-burgh/-burg' spelling variant."""
    area = str(place or "").split("-")[0].strip().lower()
    if area.endswith("burgh"):
        area = area[:-1]
    return area


def resolve_token_labels(conn, tokens: List[str]) -> Dict[str, str]:
    """Display labels for resolved tokens (ent:/cluster: ids → names)."""
    out: Dict[str, str] = {}
    ent_ids = [t[4:] for t in tokens if t.startswith("ent:")]
    cl_ids = [t[8:] for t in tokens if t.startswith("cluster:")]
    for chunk in _chunks(ent_ids):
        q = f"SELECT entity_id, canonical_name FROM entities WHERE entity_id IN ({','.join('?' * len(chunk))})"
        for eid, name in conn.execute(q, chunk):
            out["ent:" + eid] = "ent:" + (name or eid).lower()
    try:
        for chunk in _chunks(cl_ids):
            q = f"SELECT cluster_id, label FROM topic_clusters WHERE cluster_id IN ({','.join('?' * len(chunk))})"
            for cid, label in conn.execute(q, chunk):
                out["cluster:" + str(cid)] = "topic:" + (label or str(cid)).lower()
    except sqlite3.OperationalError:
        pass
    return out


def set_user_label(conn, day_iso: str, record_id: str, label: str) -> None:
    """WS2: ground-truth label from the verdict-review loop (owner-only)."""
    with with_db_write():
        conn.execute(
            "UPDATE triage_verdicts SET user_label=?, user_labeled_at=datetime('now') "
            "WHERE day=? AND record_id=?",
            (label, day_iso, record_id),
        )
        commit_connection(conn)


def _aggregate_visits(day_items: List[TriageItem]) -> List[TriageItem]:
    out, seen = [], {}
    for i in day_items:
        if i.table != "activity_events":
            out.append(i)
            continue
        key = re.sub(r"^\(\d+\)\s*", "", i.title).strip().lower()
        if key in seen:
            seen[key].visit_count += 1
        else:
            seen[key] = i
            out.append(i)
    return out


def _load_prior_embeddings(conn, start_iso, end_iso, record_ids):
    rows = conn.execute(
        "SELECT record_id, vector_blob FROM signal_embeddings "
        "WHERE event_at >= ? AND event_at < ? AND vector_blob IS NOT NULL",
        (start_iso, end_iso),
    ).fetchall()
    index, vecs = {}, []
    for rid, blob in rows:
        v = np.frombuffer(blob, dtype=np.float32)
        if v.size < 384:
            continue
        v = v[:384].astype(np.float64)
        n = np.linalg.norm(v)
        if n == 0:
            continue
        index[rid] = len(vecs)
        vecs.append(v / n)
    mat = np.vstack(vecs) if vecs else np.zeros((0, 384))
    return index, mat


def _get_fitted_params(conn, day: date, prior_items) -> Dict[str, Any]:
    """WS1.3: fitted half-life via the rent criterion, cached as a signal object
    and refit weekly. Gen weights stay hand-set until labeled ground truth
    exists (WS2) — recorded honestly in the params provenance."""
    store = SignalObjectStore(conn)
    existing = None
    try:
        row = conn.execute(
            "SELECT payload_json FROM signal_objects WHERE object_type='triage_params' "
            "AND valid_to IS NULL ORDER BY valid_from DESC LIMIT 1").fetchone()
        if row:
            existing = json.loads(row[0])
    except Exception:
        existing = None
    if existing:
        fitted = date.fromisoformat(existing.get("fitted_asof", "1970-01-01"))
        if (day - fitted).days < 7:
            return existing
    by_day: Dict[str, List[List[str]]] = defaultdict(list)
    for i in prior_items:
        if i.vocab and not i.junk:
            by_day[i.day].append(i.vocab)
    seq = sorted(by_day.items())
    if len(seq) < 5:
        return {"half_life": instruments.HALF_LIFE_DAYS, "source": "default", "fitted_asof": day.isoformat()}
    best, nlls = instruments.fit_half_life(seq)
    params = {"half_life": best, "nll_by_candidate": {str(k): round(v, 1) for k, v in nlls.items()},
              "source": "rent-criterion fit", "gen_weights": [0.5, 0.3, 0.2],
              "gen_weights_source": "hand-set pending WS2 labels",
              "fitted_asof": day.isoformat()}
    store.upsert_object("interests", "triage_params", "triage_params",
                        {**params, "disclosure": "owner_only"},
                        source_refs=[{"table": "triage_verdicts"}],
                        confidence=0.8, extractor_version=ROUTINE_VERSION)
    return params


def _drift_flag(conn, token: str, day_iso: str) -> bool:
    """WS1.5: batch-backfill signature — an entity whose mentions for MANY
    distinct historical days were created in one burst around `day` is an
    enrichment regime change, not a life event."""
    if not token.startswith("ent:"):
        return False
    row = conn.execute(
        "SELECT COUNT(DISTINCT substr(event_at,1,10)) FROM entity_mentions "
        "WHERE entity_id=? AND created_at >= datetime(?, '-1 day') "
        "AND created_at < datetime(?, '+2 day')",
        (token[4:], day_iso, day_iso)).fetchone()
    return bool(row and row[0] and row[0] >= 5)


def run_daily_triage(conn: sqlite3.Connection, day_iso: str, *,
                     lookback_days: int = LOOKBACK_DAYS) -> Dict[str, Any]:
    """Triage one day's delta; persist verdicts + interest/attention objects."""
    day = date.fromisoformat(day_iso)
    start_hist = (day - timedelta(days=lookback_days)).isoformat()
    day_end = (day + timedelta(days=1)).isoformat()

    window = load_triage_delta(conn, start_hist, day_end)
    day_items = _aggregate_visits([i for i in window if i.day == day_iso])
    prior_items = [i for i in window if i.day < day_iso]
    if not day_items:
        return {"day": day_iso, "items": 0, "quadrants": {}, "day_kl": 0.0}

    params = _get_fitted_params(conn, day, prior_items)
    half_life = float(params.get("half_life", instruments.HALF_LIFE_DAYS))

    clean = lambda seq: (i for i in seq if not i.junk)
    pi_counts = instruments.decayed_counts(
        ((i.day, i.vocab) for i in clean(prior_items)), day, half_life_days=half_life)
    ranked_vocab = sorted(pi_counts.items(), key=lambda kv: -kv[1])
    total_mass = sum(pi_counts.values()) or 1.0
    top_mass, acc = set(), 0.0
    for v, c in ranked_vocab:
        top_mass.add(v)
        acc += c
        if acc / total_mass >= 0.70:
            break

    dict_bytes = " ".join(i.text for i in prior_items[-400:] if i.text and not i.junk
                          ).encode("utf-8", "ignore")
    emb_index, emb_mat = _load_prior_embeddings(conn, start_hist, day_end, None)
    prior_rows = [emb_index[i.record_id] for i in prior_items if i.record_id in emb_index]
    prior_mat = emb_mat[prior_rows] if prior_rows else np.zeros((0, 384))
    ranks = instruments.entity_pagerank(conn)

    day_vocabs = [i.vocab for i in clean(day_items)]
    day_kl, contrib = instruments.bayes_surprise(pi_counts, day_vocabs)

    # WS1.1: permutation null — same trailing world the model saw (D12).
    pool = [i.vocab for i in clean(prior_items) if i.vocab]
    null_kls, envelope = instruments.permutation_null(
        pi_counts, pool, len(day_vocabs), n_perms=1000,
        seed=int(day_iso.replace("-", "")))
    pctl = instruments.surprise_percentile(day_kl, null_kls)

    movers = [(v, c) for v, c in sorted(contrib.items(), key=lambda kv: -kv[1]) if c > 0][:8]
    labels = resolve_token_labels(conn, [v for v, _ in movers] + [v for v, _ in ranked_vocab[:25]])
    movers_meta = []
    for v, c in movers:
        movers_meta.append({
            "token": v, "label": labels.get(v, v), "contribution": round(c, 5),
            "null_pass": bool(c > envelope), "system_drift": _drift_flag(conn, v, day_iso)})
    movers_valid = [m["label"] for m in movers_meta if m["null_pass"] and not m["system_drift"]]

    pins = intents_mod.active_intents(conn)
    for it in day_items:
        it.comp_novelty = instruments.comp_novelty(it.text, dict_bytes)
        if it.record_id in emb_index:
            it.knn_surprisal = instruments.knn_surprisal(emb_mat[emb_index[it.record_id]], prior_mat)
        it.item_kl = instruments.bayes_surprise(pi_counts, [it.vocab])[0]
        it.attachment = sum(ranks.get(e, 0.0) for e in it.entities) or None
        if pins and not it.junk:
            it.intent_sim, it.intent_name = intents_mod.intent_match(
                f"{it.title} {it.text[:400]}", pins)

    # WS1.4: length-residualized compression (fit on prior sample + day).
    sample = [(len(i.text), instruments.comp_novelty(i.text, dict_bytes))
              for i in prior_items[-200:] if i.text and not i.junk]
    sample += [(len(i.text), i.comp_novelty) for i in day_items if i.text]
    resid = instruments.length_residualizer(sample)
    for it in day_items:
        it.comp_resid = resid(len(it.text), it.comp_novelty)

    _assign_verdicts(day_items, pi_counts, top_mass, ranks)
    seeds = _pick_seeds(day_items, ranks, conn)

    with batched_writes(conn):
        for it in day_items:
            conn.execute(
                "INSERT INTO triage_verdicts "
                "(day, record_id, canonical_table, source_id, verdict, aligned, experienced, "
                " engagement_kind, floor_reason, comp_novelty, knn_surprisal, item_kl, attachment, "
                " gen_score, align_mass, visit_count, grounds_json, routine_version, "
                " comp_resid, junk) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(day, record_id) DO UPDATE SET "
                " canonical_table=excluded.canonical_table, source_id=excluded.source_id, "
                " verdict=excluded.verdict, aligned=excluded.aligned, "
                " experienced=excluded.experienced, engagement_kind=excluded.engagement_kind, "
                " floor_reason=excluded.floor_reason, comp_novelty=excluded.comp_novelty, "
                " knn_surprisal=excluded.knn_surprisal, item_kl=excluded.item_kl, "
                " attachment=excluded.attachment, gen_score=excluded.gen_score, "
                " align_mass=excluded.align_mass, visit_count=excluded.visit_count, "
                " grounds_json=excluded.grounds_json, routine_version=excluded.routine_version, "
                " comp_resid=excluded.comp_resid, junk=excluded.junk",
                (day_iso, it.record_id, it.table, it.source_id, it.verdict, int(it.aligned),
                 int(it.experienced), it.engagement_kind, it.floor_reason, it.comp_novelty,
                 it.knn_surprisal, it.item_kl, it.attachment, it.gen_score, it.align_mass,
                 it.visit_count, json.dumps(it.grounds), ROUTINE_VERSION,
                 it.comp_resid, int(it.junk)),
            )

    quadrants = dict(Counter(i.verdict for i in day_items))
    _write_signal_objects(conn, day_iso, day_items, seeds, day_kl, movers,
                          ranked_vocab, quadrants,
                          pctl=pctl, movers_meta=movers_meta,
                          movers_valid=movers_valid, params=params, labels=labels)
    return {"day": day_iso, "items": len(day_items), "quadrants": quadrants,
            "day_kl": day_kl, "day_kl_percentile": pctl,
            "movers_valid": movers_valid[:3], "seeds": len(seeds)}


def _assign_verdicts(day_items, pi_counts, top_mass, ranks) -> None:
    z_comp = instruments.zscorer(
        (i.comp_resid if i.comp_resid is not None else i.comp_novelty) for i in day_items)
    z_kl = instruments.zscorer(i.item_kl for i in day_items)
    z_att = instruments.zscorer(i.attachment for i in day_items)
    masses = [sum(pi_counts.get(v, 0.0) for v in i.vocab) for i in day_items if i.vocab]
    mass_median = float(np.median(masses)) if masses else 0.0
    comps = [i.comp_novelty for i in day_items if i.comp_novelty is not None]
    comp_median = float(np.median(comps)) if comps else None

    for it in day_items:
        it.align_mass = sum(pi_counts.get(v, 0.0) for v in it.vocab)
        if it.junk:
            it.verdict = "abstain"   # BT9: junk never reaches a quadrant
            it.grounds = ["junk-blob"]
            it.gen_score = 0.0
            continue
        if it.own_project:
            # BT5: own products are never distraction, whatever frequency says.
            it.aligned = True
            it.grounds = [f"own-project:{it.own_project}"]
        elif it.vocab:
            it.aligned = bool(set(it.vocab) & top_mass) or (
                mass_median > 0 and it.align_mass >= mass_median)
        elif it.comp_novelty is not None and comp_median is not None:
            it.aligned = it.comp_novelty <= comp_median
            if it.aligned:
                it.grounds = [f"comp-familiar {it.comp_novelty:.2f}<=med {comp_median:.2f}"]
        else:
            it.verdict = "abstain"
        if it.intent_sim >= 0.5 and it.intent_name:
            # F2: proximity to a declared future counts as nutritive — and the
            # grounds must name the pin that fired.
            it.aligned = True
            it.grounds = [f'intent:"{it.intent_name[:50]}" sim={it.intent_sim:.2f}'] + it.grounds
        it.aligned = it.aligned or it.authored
        comp_for_gen = it.comp_resid if it.comp_resid is not None else it.comp_novelty
        it.gen_score = float(max(z_comp(comp_for_gen), 0) * 0.5 +
                             max(z_kl(it.item_kl), 0) * 0.3 + max(z_att(it.attachment), 0) * 0.2)
        if it.verdict == "abstain":
            pass
        elif it.aligned and it.experienced:
            it.verdict = "signal"
        elif it.aligned and not it.experienced:
            # F3: surface is for asks — warmth without a response-expected
            # signal is kept (floor), never pushed.
            if it.table == "conversation_messages" and not it.response_expected \
                    and "keyword" not in (it.floor_reason or ""):
                it.verdict = "floor-hold"
                it.floor_reason = (it.floor_reason + "+warmth-no-ask").lstrip("+")
            else:
                it.verdict = "surface"
        elif not it.aligned and it.experienced:
            it.verdict = "distraction"
        else:
            it.verdict = "floor-hold" if it.floor else "discard"
        it.grounds = it.grounds or (sorted(set(it.vocab) & top_mass)[:4] or it.vocab[:3])


def _pick_seeds(day_items, ranks, conn) -> List[TriageItem]:
    cands = [i for i in day_items
             if (i.gen_score and i.gen_score > 0.8 and i.entities
                 and not (i.aligned and (i.align_mass or 0) > 1.0))
             or (i.intent_sim >= 0.5 and not i.junk)]  # F2: intent-steered seeds
    seen, seeds = set(), []
    for i in sorted(cands, key=lambda x: -(x.gen_score or 0)):
        key = i.vocab[0] if i.vocab else i.record_id
        if key in seen:
            continue
        seen.add(key)
        if i.intent_sim >= 0.5 and i.intent_name:
            i.seed_bond = f'your pin: "{i.intent_name[:60]}"'
        else:
            known = [e for e in i.entities if ranks.get(e, 0) > 0]
            if known:
                best = max(known, key=lambda e: ranks.get(e, 0))
                row = conn.execute(
                    "SELECT canonical_name FROM entities WHERE entity_id=?", (best,)).fetchone()
                i.seed_bond = row[0] if row else None
        seeds.append(i)
        if len(seeds) == SEED_CAP:
            break
    return seeds


def _write_signal_objects(conn, day_iso, day_items, seeds, day_kl, movers,
                          ranked_vocab, quadrants, *, pctl=None, movers_meta=None,
                          movers_valid=None, params=None, labels=None) -> None:
    store = SignalObjectStore(conn)
    refs = [{"table": "triage_verdicts", "day": day_iso}]
    labels = labels or {}

    store.upsert_object(
        "interests", "interest_profile", f"interest_profile:{day_iso}",
        {
            "asof": day_iso,
            "half_life_days": (params or {}).get("half_life", instruments.HALF_LIFE_DAYS),
            "params_source": (params or {}).get("source", "default"),
            "top_vocab": [[labels.get(v, v), round(c, 4)] for v, c in ranked_vocab[:25]],
            "day_kl": round(day_kl, 5),
            "day_kl_percentile": None if pctl is None else round(pctl, 3),
            "movers": [[labels.get(v, v), round(c, 5)] for v, c in movers],
            "disclosure": "owner_only",
        },
        source_refs=refs,
        confidence=min(1.0, len(day_items) / 50.0 + 0.3),
        extractor_version=ROUTINE_VERSION,
    )

    # Digest substrate — silence invariant: no discard references of any kind.
    dgroups = Counter()
    for i in day_items:
        if i.verdict == "distraction":
            key = next((v for v in i.vocab if v.startswith("host:")), i.table)
            dgroups[key] += i.visit_count
    surface = [i for i in day_items if i.verdict == "surface"]
    signal_top = sorted((i for i in day_items if i.verdict == "signal"),
                        key=lambda x: -(x.align_mass or 0))[:10]
    store.upsert_object(
        "interests", "attention_summary", f"attention_summary:{day_iso}",
        {
            "day": day_iso,
            "day_kl": round(day_kl, 5),
            "day_kl_percentile": None if pctl is None else round(pctl, 3),
            "movers": [[labels.get(v, v), round(c, 5)] for v, c in movers],
            "movers_valid": movers_valid or [],
            "movers_meta": movers_meta or [],
            "params": {k: (params or {}).get(k) for k in ("half_life", "source")},
            "flux_by_lane": dict(Counter(i.table for i in day_items)),
            "surface": [{"title": i.title, "feeds": i.grounds} for i in surface[:8]],
            "signal": [{"title": i.title, "grounds": i.grounds,
                        "count": i.visit_count} for i in signal_top],
            "seeds": [{"title": s.title, "gen_score": round(s.gen_score or 0, 2),
                       "bond": s.seed_bond,
                       "via_intent": s.intent_name if s.intent_sim >= 0.5 else None}
                      for s in seeds],
            "active_intents": [p.get("intent_text") for p in
                               intents_mod.active_intents(conn)][:5],
            "distraction_patterns": [
                {"group": k.removeprefix("host:"), "count": c}
                for k, c in dgroups.most_common(3)],
            "disclosure": "owner_only",
        },
        source_refs=refs,
        confidence=0.7,
        extractor_version=ROUTINE_VERSION,
    )
