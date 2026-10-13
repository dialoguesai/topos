"""Owner-only grant-specific candidates. Release reuses the same qualifier."""
from .canonical import PolicyError
from .evidence import _json, _key
from .knowledge_projections import candidates, fact_projection
from .predicate_classes import scalar
from .selective_facts import ontology_of

REASONS = {
    "eligible": "Passes this grant's current protections",
    "owner_only": "Kept private by its producer or owner",
    "fact_ontology_unselected": "This ontology is not selected",
    "fact_ontology_off": "This ontology is turned off on your Topos",
    "fact_excluded_from_grant": "Excluded from this grant",
    "fact_ontology_unknown": "Missing or conflicting ontology provenance",
    "fact_ontology_unsupported": "Ontology or predicate is outside this release",
    "fact_not_grounded": "No supported owner-stated source sentence",
    "fact_lineage_unsupported": "Fact dependencies are outside this release",
    "fact_not_owner_stated": "The owner did not state this fact",
    "entity_protected": "Off-limits protection applies",
    "owner_opted_out": "You held this item back globally",
    "evidence_not_permitted": "Supporting evidence fails this grant's choices",
    "selective_facts_disabled": "Fact sharing is disabled",
    "could_not_check": "Could not verify every protection",
}


def preview(walk, identities):
    items = []
    for table, fid in sorted(set(candidates(walk.conn, identities, ["fact"]))):
        rows = walk.conn.execute("SELECT * FROM signal_objects WHERE object_id=?", (fid,)).fetchmany(2)
        if len(rows) != 1:
            continue
        row = dict(rows[0])
        try:
            payload = _json(row["payload_json"], dict)
        except PolicyError:
            continue
        try:
            oid = ontology_of(row, payload)
        except PolicyError:
            oid = row.get("ontology_id") or "unregistered"
        code, sources, projected = "eligible", [], None
        try:
            cached = walk.projection_results.get((table, fid))
            if cached is not None:
                projected, refusal = cached
                if refusal is not None:
                    raise PolicyError(refusal)
            else:
                projected = fact_projection(walk.resolver, walk.conn, walk.floor, walk.frozen, None,
                                           row, walk.policy, walk.lower_us, walk.upper_us)
            for qualified, native in projected.sources:
                identity = qualified.snapshot.message.identity
                sources.append({"source_id": identity.source_id, "record_id": identity.record_id,
                                "excerpt": native[_key(identity)]["content"][:240]})
        except PolicyError as exc:
            code = exc.code if exc.code in REASONS else "could_not_check"
        value = scalar(payload.get("predicate"), payload)
        items.append({"fact_id": fid, "ontology_id": oid, "predicate": payload.get("predicate") or "unknown",
            "statement": projected.content if code == "eligible" else value or "Unsupported structured fact", "assertion": "inferred" if
                payload.get("altitude") in ("inferred", "predicted") or row.get("altitude") in ("inferred", "predicted") else "owner_stated",
            "eligible": code == "eligible", "reason": code, "reason_text": REASONS[code], "sources": sources})
    return items
