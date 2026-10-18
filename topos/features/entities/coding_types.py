"""Type evidence available from a coding source's own capture contract.

Fixed-label NER can call the capture application a work of art. Correct that
specific case from the source definition, without globally relabeling names
like 'Cursor' or guessing what an arbitrary capitalized phrase means.
"""
from __future__ import annotations


def capture_product_type(surface: str, source_id: str, text: str) -> str | None:
    import re
    from ...ingestion.ai_tool_sync import TOOLS

    source = next((s for s in TOOLS.values() if s.source_id == source_id), None)
    if source is None or surface.casefold().strip() != source.display_name.casefold():
        return None
    # Require software context; a conversation in Codex can still be about
    # an ancient codex, a cursor in typography, or a work titled 'Cursor'.
    if not re.search(r"\b(agent|plugin|coding|software|install|terminal|chat|session|openai)\b", text, re.I):
        return None
    if re.search(r"\b(manuscript|medieval|ancient|novel|painting|poem)\b", text, re.I):
        return None
    return "PRODUCT"


def repair_capture_product_type(conn, entity_id: str, *, surface: str, source_id: str, text: str) -> bool:
    """Preserve identity and lineage while correcting a proven capture product.

    Only the erroneous art/topic label is repairable here; a person, place,
    owner override or contact is never retyped by this source heuristic.
    The caller holds the entity writer's transaction.
    """
    import json

    if capture_product_type(surface, source_id, text) is None:
        return False
    row = conn.execute(
        "SELECT entity_type, metadata_json, contact_id FROM entities WHERE entity_id=?", (entity_id,)
    ).fetchone()
    if not row or row[0] not in ("work_of_art", "topic") or row[2]:
        return False
    metadata = json.loads(row[1] or "{}")
    if metadata.get("owner_override") or metadata.get("type_override"):
        return False
    metadata["type_correction"] = {"from": row[0], "to": "product",
                                   "reason": "capture_application_in_software_context", "source_id": source_id}
    conn.execute("UPDATE entities SET entity_type='product',metadata_json=?,updated_at=datetime('now') WHERE entity_id=?",
                 (json.dumps(metadata), entity_id))
    return True
