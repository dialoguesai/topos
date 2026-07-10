"""Entity consolidation sweep: propose merge candidates for owner review.

Auto-merge stays conservative (a wrong person-merge is the spine's one
near-irreversible failure); this module finds the *likely* duplicates the
resolver deliberately refused to merge and queues them as review rows the
owner can approve or dismiss in the People tab:

  prefix    — single-token person name that prefixes another person's first
              name ("Jon" -> "Jonathan"); classic nickname/short-form split
  fuzzy     — token-set similarity in the review band [0.80, 0.92)
  embedding — name-embedding cosine >= 0.90 between same-type entities
              (catches variants token metrics miss: "AT & T" vs "ATT")

Embedding similarity runs only here (batch, affordable) — never at
resolve-time, where a per-mention model call would tax every ingest.
Dismissed pairs are never re-proposed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from typing import Any, Dict, List, Optional, Tuple

from .resolver import AUTO_MERGE_SCORE, REVIEW_SCORE, token_set_similarity

logger = logging.getLogger("topos.features.entities.consolidation")

EMBED_MERGE_SCORE = 0.90
_MIN_MENTIONS_FOR_SWEEP = 2  # ignore one-off noise entities as merge *sources*


def _existing_pairs(conn: sqlite3.Connection) -> set:
    """Pairs (in both directions) already queued, approved, or dismissed."""
    pairs = set()
    try:
        rows = conn.execute(
            "SELECT subject_entity_id, candidate_entity_id FROM entity_review WHERE kind='merge'"
        ).fetchall()
    except sqlite3.OperationalError:
        return pairs
    for a, b in rows:
        if a and b:
            pairs.add((str(a), str(b)))
            pairs.add((str(b), str(a)))
    return pairs


def _queue_merge(
    conn: sqlite3.Connection,
    *,
    subject_entity_id: str,
    candidate_entity_id: str,
    subject_name: str,
    score: float,
    reason: str,
) -> None:
    conn.execute(
        """
        INSERT INTO entity_review (
            review_id, surface_text, candidate_entity_id, score,
            kind, subject_entity_id, reason, status
        ) VALUES (?, ?, ?, ?, 'merge', ?, ?, 'pending')
        """,
        (
            f"rev_{uuid.uuid4().hex[:12]}",
            subject_name,
            candidate_entity_id,
            round(float(score), 4),
            subject_entity_id,
            reason,
        ),
    )


def ensure_name_embeddings(conn: sqlite3.Connection, *, limit: int = 2000) -> int:
    """Batch-embed canonical names for entities missing embedding_blob."""
    from ...engine.backends.huggingface import HuggingFaceAdapter

    rows = conn.execute(
        """
        SELECT entity_id, canonical_name FROM entities
        WHERE embedding_blob IS NULL AND mention_count >= ? AND is_self = 0
        ORDER BY mention_count DESC LIMIT ?
        """,
        (_MIN_MENTIONS_FOR_SWEEP, limit),
    ).fetchall()
    if not rows:
        return 0
    adapter = HuggingFaceAdapter()
    from ..signal.vector_codec import encode_f32

    written = 0
    for start in range(0, len(rows), 64):
        batch = rows[start : start + 64]
        out = adapter.run_inference(
            {"texts": [str(r[1]) for r in batch]}, {"subtype": "embedding"}
        )
        vectors = out.get("vectors") or []
        if len(vectors) != len(batch):
            continue
        for (entity_id, _name), vector in zip(batch, vectors):
            conn.execute(
                "UPDATE entities SET embedding_blob=? WHERE entity_id=?",
                (encode_f32([float(x) for x in vector]), entity_id),
            )
            written += 1
    conn.commit()
    return written


def _load_sweep_entities(conn: sqlite3.Connection) -> List[Dict[str, Any]]:
    from ..signal.vector_codec import decode_vector

    rows = conn.execute(
        """
        SELECT entity_id, entity_type, canonical_name, normalized_name,
               mention_count, contact_id, embedding_blob
        FROM entities WHERE is_self = 0
        """
    ).fetchall()
    out = []
    for r in rows:
        vector = None
        if r[6]:
            try:
                vector = decode_vector(r[6], "f32")
            except Exception:
                vector = None
        out.append(
            {
                "entity_id": str(r[0]),
                "entity_type": str(r[1]),
                "name": str(r[2]),
                "normalized": str(r[3]),
                "mentions": int(r[4] or 0),
                "is_contact": r[5] is not None,
                "vector": vector,
            }
        )
    return out


def _merge_direction(a: Dict[str, Any], b: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """(subject=absorbed, candidate=kept).

    Contacts win outright, then fuller names ("Jonathan" keeps, "Jon" is
    absorbed — mention counts transfer in the merge, identity should be the
    richest form), then mention count as the tiebreak.
    """
    def keep_rank(e: Dict[str, Any]) -> tuple:
        return (
            e["is_contact"],
            len(e["normalized"].split()),
            len(e["normalized"]),
            e["mentions"],
        )

    if keep_rank(a) >= keep_rank(b):
        return b, a
    return a, b


def propose_merges(conn: sqlite3.Connection, *, use_embeddings: bool = True) -> Dict[str, int]:
    """Scan for likely duplicates and queue merge-review rows."""
    from ..signal.vector_math import cosine_similarity

    if use_embeddings:
        try:
            embedded = ensure_name_embeddings(conn)
            logger.debug("name embeddings ensured: %d", embedded)
        except Exception as exc:  # noqa: BLE001 — sweep must work without models
            logger.debug("name embedding skipped: %s", exc)

    entities = _load_sweep_entities(conn)
    seen_pairs = _existing_pairs(conn)

    # Block by shared 3-char token prefix to bound comparisons. Must be short
    # enough that nickname pairs share a block ("jon" vs "jonathan" -> "jon").
    blocks: Dict[str, List[int]] = {}
    for idx, entity in enumerate(entities):
        for token in set(entity["normalized"].split()):
            if len(token) >= 3:
                blocks.setdefault(token[:3], []).append(idx)

    proposed = {"prefix": 0, "fuzzy": 0, "embedding": 0}
    queued_now: set = set()

    def consider(a: Dict[str, Any], b: Dict[str, Any], score: float, reason: str) -> None:
        subject, candidate = _merge_direction(a, b)
        key = (subject["entity_id"], candidate["entity_id"])
        if key in seen_pairs or key in queued_now or (key[1], key[0]) in queued_now:
            return
        _queue_merge(
            conn,
            subject_entity_id=subject["entity_id"],
            candidate_entity_id=candidate["entity_id"],
            subject_name=subject["name"],
            score=score,
            reason=reason,
        )
        queued_now.add(key)
        proposed[reason.split(":")[0]] += 1

    for indices in blocks.values():
        if len(indices) < 2 or len(indices) > 200:
            continue
        for i_pos in range(len(indices)):
            for j_pos in range(i_pos + 1, len(indices)):
                a, b = entities[indices[i_pos]], entities[indices[j_pos]]
                if a["entity_type"] != b["entity_type"]:
                    continue
                if a["mentions"] < _MIN_MENTIONS_FOR_SWEEP and b["mentions"] < _MIN_MENTIONS_FOR_SWEEP:
                    continue
                if a["normalized"] == b["normalized"]:
                    continue

                # prefix/nickname: single-token person name prefixing the
                # other's first token ("jon" -> "jonathan smith"/"jonathan")
                if a["entity_type"] == "person":
                    short, longer = (a, b) if len(a["normalized"]) <= len(b["normalized"]) else (b, a)
                    short_tok = short["normalized"]
                    long_first = longer["normalized"].split()[0]
                    if (
                        " " not in short_tok
                        and len(short_tok) >= 3
                        and long_first.startswith(short_tok)
                        and long_first != short_tok
                    ):
                        consider(a, b, 0.85, "prefix:nickname_short_form")
                        continue

                sim = token_set_similarity(a["normalized"], b["normalized"])
                if REVIEW_SCORE <= sim < AUTO_MERGE_SCORE:
                    consider(a, b, sim, f"fuzzy:token_set_{sim:.2f}")
                    continue

                if (
                    use_embeddings
                    and a["vector"] is not None
                    and b["vector"] is not None
                    and sim < REVIEW_SCORE
                ):
                    cos = cosine_similarity(a["vector"], b["vector"])
                    if cos >= EMBED_MERGE_SCORE:
                        consider(a, b, cos, f"embedding:name_cosine_{cos:.2f}")

    conn.commit()
    total = sum(proposed.values())
    return {**proposed, "total": total}


def list_review(
    conn: sqlite3.Connection, *, status: str = "pending", limit: int = 100
) -> List[Dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT r.review_id, r.kind, r.score, r.reason, r.status, r.created_at,
               r.subject_entity_id, r.candidate_entity_id, r.surface_text
        FROM entity_review r
        WHERE r.status = ?
        ORDER BY r.score DESC, r.created_at DESC
        LIMIT ?
        """,
        (status, max(1, min(int(limit), 500))),
    ).fetchall()

    def _entity_summary(entity_id: Optional[str]) -> Optional[Dict[str, Any]]:
        if not entity_id:
            return None
        row = conn.execute(
            "SELECT entity_id, entity_type, canonical_name, mention_count, contact_id IS NOT NULL"
            " FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "entity_id": row[0],
            "entity_type": row[1],
            "canonical_name": row[2],
            "mention_count": row[3],
            "is_contact": bool(row[4]),
        }

    out = []
    for r in rows:
        out.append(
            {
                "review_id": r[0],
                "kind": r[1],
                "score": r[2],
                "reason": r[3],
                "status": r[4],
                "created_at": r[5],
                "subject": _entity_summary(r[6]) or {"canonical_name": r[8]},
                "candidate": _entity_summary(r[7]),
            }
        )
    # Drop rows whose entities vanished (merged elsewhere) — mark them stale.
    return [item for item in out if item.get("candidate")]


def resolve_review(
    conn: sqlite3.Connection, review_id: str, *, action: str
) -> Dict[str, Any]:
    """approve -> merge subject into candidate; dismiss -> never re-propose.

    Two review kinds are approvable:
      * merge      — consolidation-sweep pair (subject + candidate ids);
      * resolution — resolver-queued surface match (surface_text + candidate
        only). Approving confirms "this surface IS that entity": the surface's
        own entity (created at ingest) merges into the candidate, or — when no
        separate entity exists — the surface becomes an alias.
    """
    row = conn.execute(
        "SELECT kind, subject_entity_id, candidate_entity_id, status, surface_text "
        "FROM entity_review WHERE review_id=?",
        (review_id,),
    ).fetchone()
    if row is None:
        raise LookupError(f"review not found: {review_id}")
    kind, subject_id, candidate_id, status, surface_text = row
    if status != "pending":
        raise ValueError(f"review already {status}")

    if action == "dismiss":
        conn.execute(
            "UPDATE entity_review SET status='dismissed' WHERE review_id=?", (review_id,)
        )
        conn.commit()
        return {"review_id": review_id, "status": "dismissed"}

    if action != "approve":
        raise ValueError(f"unknown action: {action}")
    if not candidate_id:
        raise ValueError("review has no candidate entity to merge into")

    from .dossier import refresh_dossiers
    from .resolver import EntityResolver, normalize_name

    resolver = EntityResolver(conn)

    if kind == "resolution" or (kind != "merge" and not subject_id):
        surface = str(surface_text or "").strip()
        if not surface:
            raise ValueError("resolution review has no surface text")
        # The resolver created a separate entity for this surface at ingest —
        # find it by normalized name (never the candidate itself).
        hit = conn.execute(
            "SELECT entity_id FROM entities WHERE normalized_name=? AND entity_id != ? LIMIT 1",
            (normalize_name(surface), str(candidate_id)),
        ).fetchone()
        subject_id = hit[0] if hit else None
        if subject_id is None:
            # No separate entity — the confirmation is just an alias.
            resolver._add_alias(str(candidate_id), surface)
            conn.execute(
                "UPDATE entity_review SET status='approved' WHERE review_id=?", (review_id,)
            )
            conn.commit()
            refresh_dossiers(conn)
            return {
                "review_id": review_id,
                "status": "approved",
                "kept": str(candidate_id),
                "alias_added": surface,
            }
    elif kind != "merge" or not subject_id:
        raise ValueError("only merge/resolution reviews can be approved")

    resolver.merge_entities(str(candidate_id), str(subject_id))
    conn.execute(
        "UPDATE entity_review SET status='approved' WHERE review_id=?", (review_id,)
    )
    # Any other pending reviews touching the absorbed entity are now stale.
    conn.execute(
        """
        UPDATE entity_review SET status='stale'
        WHERE status='pending'
          AND (subject_entity_id=? OR candidate_entity_id=?)
        """,
        (subject_id, subject_id),
    )
    conn.commit()
    refresh_dossiers(conn)
    return {"review_id": review_id, "status": "approved", "kept": str(candidate_id), "absorbed": str(subject_id)}


def split_surface(conn: sqlite3.Connection, entity_id: str, surface: str) -> Dict[str, Any]:
    """Owner unbind: split a surface's mentions OUT of an entity, permanently.

    The reverse of an accidental merge/bind (e.g. the resolver's unique-contact
    tier binding "Claire" to the contact "Romeo Tango"):

      * mentions whose surface_text normalizes to ``surface`` move to a fresh
        entity named after the surface (created only when mentions exist);
      * a matching alias is removed from the source entity;
      * a permanent ``no_bind`` guard row is written — the resolver consults it
        so this surface can never resolve back to the source entity (token-
        subset names score 1.0, so every tier would otherwise re-merge them);
      * both mention counts recount. Edges/dossiers correct on the next graph
        rebuild.
    """
    from .resolver import EntityResolver, normalize_name

    row = conn.execute(
        "SELECT canonical_name, normalized_name, entity_type, aliases_json "
        "FROM entities WHERE entity_id=?",
        (str(entity_id),),
    ).fetchone()
    if row is None:
        raise LookupError(f"entity not found: {entity_id}")
    canonical_name, normalized_name, entity_type, aliases_json = row

    normalized = normalize_name(surface)
    if not normalized:
        raise ValueError(f"unresolvable surface: {surface!r}")
    if normalized == str(normalized_name):
        raise ValueError(
            "surface is the entity's own name — use exclude to stop tracking it entirely"
        )

    # Move matching mentions to a fresh entity.
    mention_rows = conn.execute(
        "SELECT mention_id, surface_text FROM entity_mentions WHERE entity_id=?",
        (str(entity_id),),
    ).fetchall()
    moved_ids = [
        str(mid) for mid, stext in mention_rows if normalize_name(str(stext or "")) == normalized
    ]

    new_entity_id: Optional[str] = None
    if moved_ids:
        resolver = EntityResolver(conn)
        new_entity_id = resolver._create_entity(str(surface).strip(), str(entity_type))
        placeholders = ",".join("?" for _ in moved_ids)
        conn.execute(
            f"UPDATE entity_mentions SET entity_id=? WHERE mention_id IN ({placeholders})",
            (new_entity_id, *moved_ids),
        )

    # Remove a matching alias from the source entity.
    alias_removed = False
    try:
        aliases = [a for a in (json.loads(aliases_json or "[]")) if isinstance(a, str)]
    except json.JSONDecodeError:
        aliases = []
    kept_aliases = [a for a in aliases if normalize_name(a) != normalized]
    if len(kept_aliases) != len(aliases):
        alias_removed = True
        conn.execute(
            "UPDATE entities SET aliases_json=?, updated_at=datetime('now') WHERE entity_id=?",
            (json.dumps(kept_aliases), str(entity_id)),
        )

    # Permanent guard: this surface never binds to the source entity again.
    conn.execute(
        """
        INSERT INTO entity_review
            (review_id, surface_text, candidate_entity_id, score, record_id,
             status, created_at, kind, subject_entity_id, reason)
        VALUES (?, ?, ?, 1.0, NULL, 'approved', datetime('now'), 'no_bind', ?, 'owner split')
        """,
        (f"rev_{uuid.uuid4().hex[:16]}", normalized, str(entity_id), new_entity_id),
    )

    # Recount both sides.
    for eid in filter(None, [str(entity_id), new_entity_id]):
        conn.execute(
            "UPDATE entities SET mention_count = ("
            " SELECT COUNT(*) FROM entity_mentions m WHERE m.entity_id = entities.entity_id"
            "), updated_at=datetime('now') WHERE entity_id=?",
            (eid,),
        )
    conn.commit()

    return {
        "entity_id": str(entity_id),
        "surface": str(surface).strip(),
        "new_entity_id": new_entity_id,
        "mentions_moved": len(moved_ids),
        "alias_removed": alias_removed,
    }


def merge_entity_pair(
    conn: sqlite3.Connection, keep_id: str, absorb_id: str
) -> Dict[str, Any]:
    """Owner link: merge absorb_id into keep_id (drawer-driven inverse of split).

    Reuses EntityResolver.merge_entities for mentions/aliases/edges, then clears
    any no_bind guards between the pair so an explicit re-link after Unlink
    works. Pending reviews that touched the absorbed entity are marked stale.
    """
    from .dossier import refresh_dossiers
    from .resolver import EntityResolver

    keep_id = str(keep_id).strip()
    absorb_id = str(absorb_id).strip()
    if not keep_id or not absorb_id:
        raise ValueError("keep_id and absorb_id are required")
    if keep_id == absorb_id:
        raise ValueError("cannot merge an entity into itself")

    keep_row = conn.execute(
        "SELECT entity_type, canonical_name FROM entities WHERE entity_id=?",
        (keep_id,),
    ).fetchone()
    absorb_row = conn.execute(
        "SELECT entity_type, canonical_name FROM entities WHERE entity_id=?",
        (absorb_id,),
    ).fetchone()
    if keep_row is None:
        raise LookupError(f"entity not found: {keep_id}")
    if absorb_row is None:
        raise LookupError(f"entity not found: {absorb_id}")
    if str(keep_row[0]) != str(absorb_row[0]):
        raise ValueError(
            f"entity types must match (keep={keep_row[0]!r}, absorb={absorb_row[0]!r})"
        )

    mentions_moved = int(
        conn.execute(
            "SELECT COUNT(*) FROM entity_mentions WHERE entity_id=?",
            (absorb_id,),
        ).fetchone()[0]
    )
    absorb_name = str(absorb_row[1] or "")

    # Clear no_bind guards between this pair (either direction) so the owner
    # can reverse a prior Unlink by explicitly linking again.
    conn.execute(
        """
        DELETE FROM entity_review
        WHERE kind='no_bind'
          AND (
            (candidate_entity_id=? AND subject_entity_id=?)
            OR (candidate_entity_id=? AND subject_entity_id=?)
          )
        """,
        (keep_id, absorb_id, absorb_id, keep_id),
    )
    # Pending reviews that referenced the absorbed entity are now stale.
    conn.execute(
        """
        UPDATE entity_review SET status='stale'
        WHERE status='pending'
          AND (subject_entity_id=? OR candidate_entity_id=?)
        """,
        (absorb_id, absorb_id),
    )

    resolver = EntityResolver(conn)
    resolver.merge_entities(keep_id, absorb_id)

    # Also drop any leftover no_bind rows that still point at the deleted
    # absorb id as the blocked candidate (surface → absorb).
    conn.execute(
        "DELETE FROM entity_review WHERE kind='no_bind' AND candidate_entity_id=?",
        (absorb_id,),
    )
    conn.commit()
    refresh_dossiers(conn)

    return {
        "kept": keep_id,
        "absorbed": absorb_id,
        "absorbed_name": absorb_name,
        "mentions_moved": mentions_moved,
    }
