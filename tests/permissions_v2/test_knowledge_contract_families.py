"""IF-5 §2: the knowledge grammar names the journal and browsing-activity tables; nothing else does.

protects: the signed policy's table list is what decides which canonical tables a grant's evidence may come
from. Widening it is the owner's decision (30 Sep 2026: `journal_entries` and `activity_events` join the two
message tables, for the knowledge capability only). What must not follow from it:
  - a message-search grant (p2a, p2c-v1, p2c-v2) listing a journal table: those capabilities release rows as
    messages and keep the two message tables;
  - an existing knowledge grant changing: its bytes and its hash are what the owner signed;
  - a grant that signs a raw kind without the table it comes from (`journal_entry` without `journal_entries`,
    `interest` without `activity_events`): a grant the owner never saw, refused at parse;
  - an interest record that carries anything but a label, a month and a band.
"""
from __future__ import annotations

import copy

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.test_knowledge_search import knowledge_policy
from topos.permissions_v2.canonical import digest
from topos.permissions_v2.knowledge_contract import (InterestResult, JournalEntryResult, KnowledgeMemberBinding,
                                                      KnowledgePolicy, KnowledgeSearchResult)
from topos.permissions_v2.registry import parse_policy

TABLES = ["ai_chat_messages", "conversation_messages", "journal_entries", "activity_events"]
KINDS = ["message", "fact", "goal", "relationship", "journal_entry", "interest"]


def _with(raw, *, tables=None, kinds=None, sources=None):
    raw = copy.deepcopy(raw)
    if tables is not None:
        raw["search"]["tables"] = list(tables)
        for rule in raw["rules"]:
            for form in rule["release"]["forms"]:
                form["tables"] = list(tables)
    if kinds is not None:
        raw["search"]["result_types"] = list(kinds)
    return raw


def test_a_knowledge_grant_can_list_the_journal_and_activity_tables_and_their_kinds():
    policy = parse_policy(_with(knowledge_policy(), tables=TABLES, kinds=KINDS))
    assert isinstance(policy, KnowledgePolicy)
    assert policy.search.tables == TABLES and policy.search.result_types == KINDS


def test_todays_knowledge_grants_keep_their_bytes_and_hash():
    raw = knowledge_policy()
    policy = parse_policy(copy.deepcopy(raw))
    assert policy.model_dump()["search"]["tables"] == raw["search"]["tables"]
    assert policy.model_dump()["search"]["result_types"] == ["message", "fact", "goal", "relationship"]
    # The dump round-trips to the same policy, so its signed hash is unchanged by the wider vocabulary.
    assert digest(parse_policy(policy.model_dump()).model_dump()) == digest(policy.model_dump())


@pytest.mark.parametrize("capability", ["permissions-beta/p2c-v1"])
def test_a_message_search_grant_still_cannot_list_a_journal_table(capability):
    raw = mc.search_policy(max_k=10)
    assert raw["versions"]["capability"] == capability
    for table in ("journal_entries", "activity_events"):
        with pytest.raises(Exception):
            parse_policy(_with(raw, tables=["conversation_messages", table]))


@pytest.mark.parametrize("kinds, tables", [
    (["message", "journal_entry"], ["ai_chat_messages", "conversation_messages"]),
    (["message", "interest"], ["ai_chat_messages", "conversation_messages", "journal_entries"]),
])
def test_a_raw_kind_without_its_table_is_refused_at_parse(kinds, tables):
    from pydantic import ValidationError
    from topos.permissions_v2.canonical import PolicyError
    with pytest.raises(PolicyError) as refused:
        parse_policy(_with(knowledge_policy(), tables=tables, kinds=kinds))
    assert refused.value.code == "schema_invalid"   # the parser names no detail, by design
    with pytest.raises(ValidationError, match="result type without its table"):
        KnowledgePolicy.model_validate(_with(knowledge_policy(), tables=tables, kinds=kinds))
    # ... and it is exactly the missing table: add it and the same grant parses.
    completed = [*tables, *(t for t in ("journal_entries", "activity_events") if t not in tables)]
    parse_policy(_with(knowledge_policy(), tables=completed, kinds=kinds))


@pytest.mark.parametrize("tables", [["journal_entries; DROP"], ["browser_visits"], ["raw_growjournal_ui_stream"],
                                    ["grow_journal_sessions"], ["location_events"]])
def test_no_other_table_can_be_named(tables):
    with pytest.raises(Exception):
        parse_policy(_with(knowledge_policy(), tables=["conversation_messages", *tables]))


def test_a_journal_entry_and_an_interest_have_their_wire_shapes():
    citation = dict(record_id="r." + "a" * 64, source_id="grow_journal", content="An entry.")
    entry = JournalEntryResult.parse(dict(kind="journal_entry", record_id=citation["record_id"], content="An entry.",
                                          source_ids=["grow_journal"], citations=[citation], event_at=1788998400))
    interest = InterestResult.parse(dict(kind="interest", record_id="r." + "b" * 64, content="Trail running",
                                         label="Trail running", month="2026-09", strength="medium",
                                         source_ids=["browser_visits"],
                                         citations=[dict(record_id="r." + "b" * 64, source_id="browser_visits",
                                                         content="Trail running, 2026-09")]))
    page = KnowledgeSearchResult.parse(dict(family="canonical_record", operation="search",
                                            view_id="canonical.knowledge_search.v1",
                                            records=[entry.model_dump(), interest.model_dump()]))
    assert [record.kind for record in page.records] == ["journal_entry", "interest"]


@pytest.mark.parametrize("field, value", [("month", "2026-13"), ("month", "2026-9"), ("strength", "extreme"),
                                          ("url", "https://example.com"), ("title", "A page")])
def test_an_interest_carries_a_label_a_month_and_a_band_and_nothing_else(field, value):
    base = dict(kind="interest", record_id="r." + "b" * 64, content="Trail running", label="Trail running",
                month="2026-09", strength="medium", source_ids=["browser_visits"],
                citations=[dict(record_id="r." + "b" * 64, source_id="browser_visits", content="Trail running, 2026-09")])
    with pytest.raises(Exception):
        InterestResult.parse({**base, field: value})


def test_a_member_binding_can_name_the_new_evidence_tables():
    binding = KnowledgeMemberBinding.parse(dict(kind="journal_entry", record_id="r." + "a" * 64,
        source_ids=["grow_journal"], evidence_tables=["journal_entries"], evidence_revision="0" * 64,
        projection_revision="0" * 64, allow_clause_id="allow-1", member_decision_hash="0" * 64))
    assert binding.evidence_tables == ["journal_entries"]
    with pytest.raises(Exception):
        KnowledgeMemberBinding.parse({**binding.model_dump(), "evidence_tables": ["browser_visits"]})
