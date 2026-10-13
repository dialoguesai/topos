"""Frozen ontology consent vocabulary. Mirrored by the control plane.

An ontology is identity, never proof of ownership or permission. The first
admission is deliberately small: supported stated work predicates only.
"""
import sqlite3
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator

from .canonical import PolicyError, digest
from .contract import Hash, Identifier, StrictModel

VERSION = "selective-facts/v1"
VOCABULARY = "fact-ontology-vocabulary/v1"
LEGACY_MAPPING = "fact-store-work-mapping/v1"
WORK_PREDICATES = ("works_at", "worked_at", "works_on", "work.project")
# Exact identities and versions, not string-prefix inference. Pack additions do
# not change the authorized set. Semantics changes require a new revision.
SEMANTICS = {"work.career": {"revision": "work-career-sharing/v1",
    "pack_versions": ["0.2.3"], "predicates": list(WORK_PREDICATES),
    "legacy": {"producer": "fact_store_v1", "mapping": LEGACY_MAPPING,
               "predicates": ["works_at", "worked_at", "works_on"]}}}
CATALOG_DIGEST = digest({"vocabulary": VOCABULARY, "ontologies": SEMANTICS})


class OntologySelection(StrictModel):
    ontology_id: Identifier
    revision: Identifier
    predicates: Annotated[list[Identifier], Field(min_length=1, max_length=50)]

    @field_validator("predicates")
    @classmethod
    def unique_predicates(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("duplicate predicate")
        return sorted(values)

    @model_validator(mode="after")
    def registered_semantics(self):
        expected = SEMANTICS.get(self.ontology_id)
        if (expected is None or self.revision != expected["revision"]
                or set(self.predicates) != set(expected["predicates"])):
            raise ValueError("unsupported ontology vocabulary")
        return self


class SelectiveFacts(StrictModel):
    version: Literal["selective-facts/v1"]
    vocabulary: Literal["fact-ontology-vocabulary/v1"]
    catalog_digest: Hash
    ontologies: Annotated[list[OntologySelection], Field(min_length=1, max_length=25)]
    excluded_fact_ids: Annotated[list[Identifier], Field(max_length=500)]

    @model_validator(mode="after")
    def exact_catalog(self):
        if self.catalog_digest != CATALOG_DIGEST:
            raise ValueError("unsupported fact catalog")
        if len({item.ontology_id for item in self.ontologies}) != len(self.ontologies):
            raise ValueError("duplicate ontology")
        if len(set(self.excluded_fact_ids)) != len(self.excluded_fact_ids):
            raise ValueError("duplicate fact exclusion")
        return self


def selection(ontology_ids, excluded_fact_ids=()):
    return {"version": VERSION, "vocabulary": VOCABULARY, "catalog_digest": CATALOG_DIGEST,
        "ontologies": [{"ontology_id": oid, "revision": SEMANTICS[oid]["revision"],
                        "predicates": list(SEMANTICS[oid]["predicates"])} for oid in ontology_ids],
        "excluded_fact_ids": list(excluded_fact_ids)}


UNAVAILABLE = {'admin.legal': ('Legal and administration', 'special_category'), 'aspirations.goals': ('Goals and aspirations', 'unsupported_grounding'), 'behavior.habits': ('Habits', 'inferred_or_owner_only'), 'behavior.preferences': ('Preferences', 'inferred_or_owner_only'), 'beliefs.civic': ('Civic beliefs', 'special_category'), 'beliefs.spiritual': ('Spiritual beliefs', 'special_category'), 'communication.style': ('Communication style', 'inferred_or_owner_only'), 'community.membership': ('Community membership', 'special_category'), 'craft.tooling': ('Tools and craft', 'unsupported_grounding'), 'expertise.knowledge': ('Knowledge and expertise', 'unsupported_grounding'), 'finances.economics': ('Finances', 'special_category'), 'health.lifestyle': ('Health and lifestyle', 'special_category'), 'health.mental': ('Mental health', 'special_category'), 'health.physical': ('Physical health', 'special_category'), 'household.pets': ('Household and pets', 'unsupported_grounding'), 'identity.core': ('Identity', 'special_category'), 'interests.taste': ('Interests and taste', 'unsupported_grounding'), 'life.events': ('Life events', 'unsupported_grounding'), 'narrative.self': ('Personal narrative', 'inferred_or_owner_only'), 'net.capability': ('Capabilities', 'inferred_or_owner_only'), 'net.character': ('Character', 'inferred_or_owner_only'), 'obligations.commitments': ('Commitments', 'unsupported_grounding'), 'personality.traits': ('Personality', 'inferred_or_owner_only'), 'places.mobility': ('Places and travel', 'unsupported_grounding'), 'possessions.assets': ('Possessions', 'special_category'), 'relationships.social': ('Social relationships', 'third_party_subject'), 'values.motivation': ('Values and motivations', 'special_category')}


def catalog():
    """Owner-only, data-independent availability. No private count or existence signal."""
    return {"version": VERSION, "vocabulary": VOCABULARY, "catalog_digest": CATALOG_DIGEST,
        "ontologies": [{"ontology_id": "work.career", "title": "Work and career",
            "description": "Explicit employer and work project statements. Owner-only facts stay private.",
            "available": True, "reason": None, "revision": SEMANTICS["work.career"]["revision"],
            "predicates": list(WORK_PREDICATES)}] + [
            {"ontology_id": oid, "title": title, "description": "Unavailable for this release",
             "available": False, "reason": reason, "revision": None, "predicates": []}
            for oid, (title, reason) in UNAVAILABLE.items()]}


def enabled_ontologies(conn):
    """The ontologies the owner has switched on at this node (the derivation registry's ``enabled``).

    A grant's signed consent names an ontology; this is the owner's standing choice about it, and both must hold.
    A registry that is missing or cannot be read names none: a fact shares only while its ontology is provably on."""
    try:
        return frozenset(row[0] for row in conn.execute("SELECT pack_id FROM pack_registry WHERE enabled=1"))
    except sqlite3.Error:
        return frozenset()


def enabled_revision(conn):
    """The admitted ontologies that are on, sorted: what the refresh loop's fact digest reads (`fact_digest`).

    Switching an admitted ontology moves it, which queues a build of every index that could hold its facts while
    the index that is there keeps serving; an ontology outside this release shares nothing either way and never
    moves it. No index basis binds it: an index is never refused because of a switch."""
    enabled = enabled_ontologies(conn)
    return sorted(oid for oid in SEMANTICS if oid in enabled)


def ontology_of(row, payload):
    """Exact, versioned producer mapping; missing or conflicting provenance refuses."""
    if row.get("altitude") not in (None, "stated") or payload.get("altitude") not in (None, "stated"):
        raise PolicyError("fact_not_owner_stated")
    oid, version = row.get("ontology_id"), row.get("ontology_version")
    predicate = payload.get("predicate")
    if oid is None and version is None:
        legacy = SEMANTICS["work.career"]["legacy"]
        if (row.get("extractor_version") != legacy["producer"] or payload.get("pack") is not None
                or payload.get("ontology_id") is not None or payload.get("ontology_version") is not None
                or predicate not in legacy["predicates"]):
            raise PolicyError("fact_ontology_unknown")
        return "work.career"
    spec = SEMANTICS.get(oid)
    if (spec is None or version not in spec["pack_versions"] or payload.get("pack") != oid
            or payload.get("ontology_id", oid) != oid or payload.get("ontology_version", version) != version
            or (payload.get("extractor") if isinstance(payload.get("extractor"), dict) else {}).get("pack_version") != version
            or row.get("altitude") != "stated" or payload.get("altitude") != "stated"
            or predicate not in spec["predicates"]):
        raise PolicyError("fact_ontology_unsupported")
    return oid


def authorize(row, payload, policy):
    declaration = getattr(policy.search, "selective_facts", None)
    if declaration is None:
        raise PolicyError("fact_selection_required")
    if row["object_id"] in declaration.excluded_fact_ids:
        raise PolicyError("fact_excluded_from_grant")
    if payload.get("disclosure") != "scoped":
        raise PolicyError("owner_only")
    if payload.get("asserted_by") != "owner" or payload.get("actor_role", "authored") != "authored":
        raise PolicyError("fact_not_owner_stated")
    # This release admits terminal native source support only. Derived fact
    # dependencies are unavailable until their complete authorization is proven.
    if payload.get("lineage") or payload.get("dependencies"):
        raise PolicyError("fact_lineage_unsupported")
    oid = ontology_of(row, payload)
    if not any(item.ontology_id == oid and payload.get("predicate") in item.predicates
               for item in declaration.ontologies):
        raise PolicyError("fact_ontology_unselected")
    return oid


def require_active_output(output, *, enabled, selective_answer=False):
    """Recheck the kill switch in the task that invokes the final send."""
    raw = output.model_dump() if hasattr(output, "model_dump") else output
    if not isinstance(raw, dict):
        raise PolicyError("permission_denied")
    records = raw.get("records", raw.get("sources", []))
    if not enabled and (any(item.get("kind") == "fact" for item in records)
                        or (selective_answer and raw.get("outcome") == "answered")):
        raise PolicyError("permission_denied")


def validate_output(policy, output):
    """The closed recipient fact shape must match frozen signed consent."""
    declaration = getattr(getattr(policy, "search", None), "selective_facts", None)
    raw = output.model_dump() if hasattr(output, "model_dump") else output
    for fact in raw.get("records", raw.get("sources", [])):
        if fact.get("kind") != "fact":
            continue
        if (declaration is None or fact.get("assertion") != "owner_stated"
                or not any(item.ontology_id == fact.get("ontology_id")
                           and fact.get("predicate") in item.predicates for item in declaration.ontologies)
                or not fact.get("citations")
                or any(citation.get("content") != fact.get("content") for citation in fact["citations"])):
            raise PolicyError("permission_denied")
