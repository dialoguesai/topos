"""IF-5 on the node: the knowledge grammar widens for journal entries and browsing interests, nothing else.

The file is byte-identical to the control plane's (its test_permissions_v2_fact_wire pins it). Pinned here on
the node's side: the knowledge capability alone takes the family tables and kinds; message-search grammars keep
`contract.Table`; a signed family kind without its table is refused at parse (IF-5 Q2); the wire admits the two
new record kinds and nothing that carries a URL.
"""
from __future__ import annotations

import pytest

from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.contract import Table
from topos.permissions_v2.knowledge_contract import (FAMILY_KIND_TABLES, KnowledgeDeclaration, KnowledgeSearchResult,
                                                     KnowledgeTable, ResultKind)
from topos.permissions_v2.search_contract import SearchDeclaration

WINDOW = {"kind": "rolling", "anchor": "server_request_as_of", "max_age_seconds": 86400,
          "event_time_semantics": "canonical_event_time_v1", "missing_or_ambiguous": "withhold", "future": "withhold"}
DECLARATION = {"view_id": "canonical.knowledge_search.v1", "max_permitted_records": 10, "max_k": 10, "window": WINDOW,
               "time_semantics": "underlying_evidence_time/v1"}


def test_the_vocabularies_widen_for_the_knowledge_capability_only():
    assert set(KnowledgeTable.__args__) == {"conversation_messages", "ai_chat_messages", "journal_entries", "activity_events"}
    assert set(Table.__args__) == {"conversation_messages", "ai_chat_messages"}
    assert set(ResultKind.__args__) >= {"journal_entry", "interest"}
    assert FAMILY_KIND_TABLES == {"journal_entry": "journal_entries", "interest": "activity_events"}
    KnowledgeDeclaration.parse({**DECLARATION, "tables": ["journal_entries", "activity_events"],
                                "result_types": ["journal_entry", "interest"]})
    with pytest.raises(PolicyError):
        SearchDeclaration.parse({"view_id": "canonical.message_search.v1", "tables": ["journal_entries"],
                                 "max_permitted_records": 10, "max_k": 10, "window": WINDOW})


def test_the_wire_takes_journal_entries_and_interests_and_never_a_url():
    rid = "r." + "b" * 64
    cite = lambda source, text: [{"record_id": rid, "source_id": source, "content": text}]  # noqa: E731
    journal = {"kind": "journal_entry", "record_id": rid, "content": "entry", "source_ids": ["grow_journal"],
               "citations": cite("grow_journal", "entry")}
    interest = {"kind": "interest", "record_id": rid, "content": "kayaks", "label": "kayaks", "month": "2026-09",
                "strength": "low", "source_ids": ["browser_visits"], "citations": cite("browser_visits", "kayaks, 2026-09")}
    for item in (journal, interest):
        KnowledgeSearchResult.parse({"family": "canonical_record", "operation": "search",
                                     "view_id": "canonical.knowledge_search.v1", "records": [item]})
    for bad in ({"url": "https://example.invalid"}, {"month": "2026-13"}, {"strength": "very"}):
        with pytest.raises(PolicyError):
            KnowledgeSearchResult.parse({"family": "canonical_record", "operation": "search",
                                         "view_id": "canonical.knowledge_search.v1", "records": [{**interest, **bad}]})


@pytest.mark.parametrize("kind,table", sorted(FAMILY_KIND_TABLES.items()))
def test_a_signed_family_kind_without_its_table_is_refused_at_parse(kind, table):
    """IF-5 Q2: the grammar itself refuses it on the node, as on the control plane, so neither side can sign one."""
    from topos.permissions_v2.knowledge_contract import KnowledgePolicy
    from tests.permissions_v2.test_knowledge_search import knowledge_policy
    raw = knowledge_policy()
    tables = sorted({*raw["search"]["tables"], table})
    raw["search"].update(tables=tables, result_types=[*raw["search"]["result_types"], kind])
    for rule in raw["rules"]:
        for form in rule["release"]["forms"]:
            form["tables"] = tables
    KnowledgePolicy.parse(raw)
    without = [t for t in tables if t != table]
    raw["search"]["tables"] = without
    for rule in raw["rules"]:
        for form in rule["release"]["forms"]:
            form["tables"] = without
    with pytest.raises(PolicyError):
        KnowledgePolicy.parse(raw)
