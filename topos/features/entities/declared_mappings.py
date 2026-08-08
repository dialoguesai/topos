"""Declarative entity mappings for STRUCTURED sources (§5a capability 4,
PLAN_CONNECTOR_CATALOG_ROLLOUT.md).

Structured records already KNOW their entities (a github commit row carries its
repo/org in metadata) — NER over their prose misclassifies them (repos became
person nodes). Sources declare which fields mint which entity types and which
edges connect them to the self entity; NER remains the fallback for
unstructured prose and is suppressed for declared sources.

Declaration-shaped registry (same pattern as routines_save.SAVE_CAPABILITIES):
in-code first consumer is github_activity; the shape is designed to move into
source definitions (self-serve) with the other Mode B capabilities.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ...storage.db.write_gate import commit_connection, with_db_write

# Edge type for "the owner worked on this project at this time".
EDGE_WORKED_ON = "worked_on"

# Own-product domains → project entity (BT5, PLAN_ATTENTION_TRIAGE.md):
# frequency-based interest models cannot know a domain is the owner's own
# product; visits to these must never read as distraction. Suffix-matched.
OWN_DOMAIN_PROJECTS: Dict[str, str] = {
    "dialogues.ai": "Dialogues",
    "pitchrotator.vercel.app": "PitchRotator",
    "grow-beta.web.app": "Grow App",
    "localhost": "Dialogues",
    "127.0.0.1": "Dialogues",
}


# Term→project declarations (PLAN_INTENT_STEERING.md F4): owner-declared topic
# terms that bond items to a project the entity graph doesn't know yet.
OWN_TERM_PROJECTS: Dict[str, str] = {
    "witcher": "Witcher Network",
}


def own_project_for_terms(text: str) -> Optional[str]:
    low = str(text or "").lower()
    for term, project in OWN_TERM_PROJECTS.items():
        if term in low:
            return project
    return None


def own_project_for_host(host: str) -> Optional[str]:
    """Project name when `host` (or a parent domain) is an own-product domain."""
    h = str(host or "").strip().lower().removeprefix("www.")
    if not h:
        return None
    h = h.split(":")[0]  # strip port (localhost:3000)
    parts = h.split(".")
    for i in range(len(parts)):
        if ".".join(parts[i:]) in OWN_DOMAIN_PROJECTS:
            return OWN_DOMAIN_PROJECTS[".".join(parts[i:])]
    return OWN_DOMAIN_PROJECTS.get(h)


DECLARED_ENTITY_MAPPINGS: Dict[str, Dict[str, Any]] = {
    "browser_visits": {
        # Own-domain visits mint the project entity (attachment + alignment
        # substrate for attention triage); unknown domains mint nothing.
        "entities": [
            {"path": "hostname", "transform": "own_domain_project", "type": "project"},
        ],
    },
    "browser_events": {
        "entities": [
            {"path": "hostname", "transform": "own_domain_project", "type": "project"},
        ],
    },
    "github_activity": {
        # Commit prose is terse technical text — NER guesses are worse than
        # nothing here (they minted person-type repo nodes).
        "suppress_ner": True,
        "entities": [
            {"path": "metadata_json.repo", "type": "project"},
            # "org" is the spine's slug for organizations (resolver _NER_TYPE_MAP).
            {"path": "metadata_json.repo", "transform": "org_prefix", "type": "org"},
        ],
        # self → worked_on → repo, positioned at the record's event time.
        "self_edges": [{"to_path": "metadata_json.repo", "edge_type": EDGE_WORKED_ON}],
    },
}


def mapping_for(source_id: Optional[str]) -> Optional[Dict[str, Any]]:
    return DECLARED_ENTITY_MAPPINGS.get(str(source_id or "").strip())


def ner_suppressed_source_ids() -> set:
    return {sid for sid, m in DECLARED_ENTITY_MAPPINGS.items() if m.get("suppress_ner")}


def _field(record: Dict[str, Any], dotted_path: str) -> str:
    """Resolve a dotted path; JSON-string columns (metadata_json) are parsed."""
    value: Any = record
    for part in str(dotted_path or "").split("."):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return ""
        if not isinstance(value, dict):
            return ""
        value = value.get(part)
    if isinstance(value, (dict, list)):
        return ""
    return str(value or "").strip()


def _transform(value: str, transform: Optional[str]) -> str:
    if transform == "org_prefix":
        # "dialoguesai/topos-react-app" → "dialoguesai"
        parts = value.split("/")
        return parts[0].strip() if len(parts) >= 2 and parts[0].strip() else ""
    if transform == "own_domain_project":
        # hostname → own project name, or "" (skipped) for third-party domains.
        return own_project_for_host(value) or ""
    return value


def extract_declared_entities(
    record: Dict[str, Any],
    *,
    record_id: Optional[str] = None,
    event_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """NER-result-shaped rows minted from a record's declared mapping.

    provider="declared" marks them so the spine keeps the declared type verbatim
    (map_ner_type must not run on them). self-edge targets carry `self_edge` so
    the spine can attach the owner edge after resolution.

    Callers with an id/time contract (entities_job's record_key/_EVENT_AT_FIELDS)
    should pass record_id/event_at explicitly so the two never drift.
    """
    mapping = mapping_for(record.get("source_id"))
    if not mapping:
        return []
    if record_id is None:
        record_id = str(
            record.get("message_id")
            or record.get("id")
            or record.get("record_id")
            or record.get("event_id")
            or record.get("entry_id")
            or ""
        )
    if event_at is None:
        event_at = (
            record.get("event_at")
            or record.get("ts")
            or record.get("occurred_at")
            or record.get("entry_at")
            or record.get("created_at")
        )
    if not record_id:
        return []
    edge_targets = {
        _field(record, rule.get("to_path", "")): str(rule.get("edge_type") or EDGE_WORKED_ON)
        for rule in mapping.get("self_edges", [])
    }
    out: List[Dict[str, Any]] = []
    seen: set = set()
    for rule in mapping.get("entities", []):
        raw = _field(record, str(rule.get("path") or ""))
        surface = _transform(raw, rule.get("transform"))
        entity_type = str(rule.get("type") or "").strip()
        if not surface or not entity_type:
            continue
        key = (surface.lower(), entity_type)
        if key in seen:
            continue
        seen.add(key)
        row: Dict[str, Any] = {
            "message_id": record_id,
            "record_id": record_id,
            "source_id": record.get("source_id"),
            "event_at": event_at,
            "canonical_table": record.get("_table") or record.get("canonical_table"),
            "entity_text": surface,
            "entity_type": entity_type,
            "confidence": 1.0,
            "provider": "declared",
            "model": None,
        }
        if surface in edge_targets:
            row["self_edge"] = edge_targets[surface]
        out.append(row)
    return out


RECLASSIFY_MIGRATION_SQL = """
UPDATE entities SET entity_type='project'
WHERE entity_type='person'
  AND canonical_name LIKE '%/%'
  AND entity_id IN (SELECT DISTINCT entity_id FROM entity_mentions WHERE record_id LIKE 'github:%');
"""


def reclassify_misdeclared_entities(conn: Any) -> int:
    """One-time repair: repo-shaped person entities minted by NER from github
    records become project entities (idempotent)."""
    with with_db_write():
        cur = conn.execute(RECLASSIFY_MIGRATION_SQL)
        commit_connection(conn)
    return int(cur.rowcount or 0)
