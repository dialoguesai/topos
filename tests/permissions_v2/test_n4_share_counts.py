"""The count preview (any-to-any N4; A2A-3 §7.2, A2A-5 §4.2–§4.3; test obligation "Node (N4, N5)" 3).

protects: each seeded item is counted once, under the FIRST reason of A2A-3 §7.2 that applies to it, and a
never-shared item (someone else's message, an AI reply, a row another door wrote, NSFW, owner-only, Off-limits, a
copy) is not counted at all, even when it would also fail a later check; only the policy's kinds, chosen sources and
window are counted; the reply holds counts only. The node's own qualification checks proof before authorship, so a
message someone else sent fails proof first there: the preview must still not count it.

The node is the journal family's synthetic one (``test_journal_family``): a migrated canonical database, the journal
kind on, one journal source installed for the owner. Every person, word and id is invented.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from contextlib import closing

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.test_journal_family import (AFTER_ITS_DAY, OWNER, SOURCE, _db, _entry, _journal_policy,  # noqa: F401
                                                      _off_limits, _publish, _resolver, node, owner)
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.registry import parse_policy
from topos.permissions_v2.share_counts import REASONS, admits_special, count

AI_SOURCE = "chatgpt_file_ingestion"
NOW = AFTER_ITS_DAY
IN_WINDOW = "2026-09-10T08:30:00"


def policy_for(*, kinds=("message", "journal_entry", "goal"), domains=("work", "plans", "hobbies", "home"),
               sensitivities=("none", "personal"), excluded=("finance",), sources=(SOURCE, mc.SOURCE, AI_SOURCE)):
    """A knowledge share as the control plane compiles one: a permit rule over the chosen topics and levels and,
    for an excluded topic, a deny rule (``journey_contract.compile_policy``'s shape)."""
    raw = _journal_policy(kinds=kinds)
    tables = ["ai_chat_messages", "conversation_messages", "journal_entries"]
    atom = mc._atom
    permit = raw["rules"][0]
    predicate = {"kind": "all_of", "terms": [atom("domain", list(domains)), atom("sensitivity", list(sensitivities))]}
    permit["evidence_use"]["sources"] = {"kind": "only", "values": list(sources)}
    permit["evidence_use"]["predicate"] = copy.deepcopy(predicate)
    permit["release"]["predicate"] = copy.deepcopy(predicate)
    for form in permit["release"]["forms"]:
        form["tables"] = tables
    rules = [permit]
    if excluded:
        deny = copy.deepcopy(permit)
        deny.update(rule_id="deny-content", effect="deny")
        deny["evidence_use"]["predicate"] = atom("domain", list(excluded))
        deny["release"]["predicate"] = atom("domain", list(excluded))
        rules.append(deny)
    raw["rules"] = rules
    raw["source_universe"]["source_ids"] = sorted(set(sources))
    raw["search"]["tables"] = tables
    return parse_policy(raw)


@pytest.fixture(autouse=True)
def _no_rows_kept():
    from topos.permissions_v2 import share_counts
    share_counts.forget()
    yield
    share_counts.forget()


def reviews_of(path):
    resolver = _resolver(path)
    with owner():
        return resolver, EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)


def counts(path, policy) -> dict:
    resolver, reviews = reviews_of(path)
    with owner():
        return count(resolver, reviews, policy, now=NOW)


def tally(can_share=0, **held):
    assert set(held) <= set(REASONS)
    return {"can_share": can_share, "held_back": {reason: held.get(reason, 0) for reason in REASONS}}


def _message(path, message_id: str, content: str, *, is_from_self=1):
    with _db(path) as conn:
        mc.insert_message(conn, message_id=message_id, source_id=mc.SOURCE, content=content,
                          event_at=mc._iso(NOW - 3_600), is_from_self=is_from_self)


def _ai_chat(path, message_id: str, content: str, *, sender="user", writer=None, source=AI_SOURCE):
    with _db(path) as conn:
        conn.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, source_id, sender_type, content, "
                     "event_at, writer_class) VALUES (?,?,?,?,?,?,?)",
                     (message_id, "chat-1", source, sender, content, mc._iso(NOW - 3_600), writer))


def _opt_out(path, entry_id: str):
    from topos.permissions_v2.message_evidence import message_key
    resolver, reviews = reviews_of(path)
    with owner():
        reviews.opt_out(message_key(resolver._identity("journal_entries", entry_id, SOURCE)), now=1)


def _owner_says_quote(path, entry_id: str):
    """The owner's own correction of an entry: not their original words (so not to share)."""
    from topos.permissions_v2.message_evidence import preview_message, record_message_review
    resolver, reviews = reviews_of(path)
    identity = resolver._identity("journal_entries", entry_id, SOURCE)
    with owner():
        preview = preview_message(resolver, reviews, identity)
        record_message_review(resolver, reviews, review_id="owner-review-1", expected_snapshot=preview["snapshot"],
                              classification=dict(evidence=preview["snapshot"]["message"], domains=["work"],
                                                  sensitivity="personal", authorship="owner_authored",
                                                  speech="third_party_quote", independent_copies="none_known",
                                                  protected_content="none"),
                              expected_current_review_revision=preview["current_review_revision"], reviewed_at=2)


def _seed(path):
    """One item per class, and every never-shared kind of item; each also fails a later check where it can."""
    _off_limits(path, "Quillon Marsh")
    # journal entries (proven by the owner's own import door unless said otherwise)
    _entry(path, "e-ok", "Shipped the release notes before lunch.", entry_at=IN_WINDOW)
    _entry(path, "e-special", "Picked up the new prescription from the clinic.", entry_at=IN_WINDOW)
    _entry(path, "e-family", "Called my sister about the holiday plans.", entry_at=IN_WINDOW)
    _entry(path, "e-finance", "Moved the rent money into the joint account.", entry_at=IN_WINDOW)
    _entry(path, "e-unassessed", "Repotted the basil on the balcony.", entry_at=IN_WINDOW)
    _entry(path, "e-optout", "Sketched the shed plans again.", entry_at=IN_WINDOW)
    _entry(path, "e-corrected", "Copied a paragraph from the team handbook.", entry_at=IN_WINDOW)
    _entry(path, "e-long", "Long draft. " + "word " * 1_700, entry_at=IN_WINDOW)              # over 8,000 characters
    _entry(path, "e-unproven", "Written before the node recorded writers.", entry_at=IN_WINDOW, writer_class=None,
           dataset=None)
    # never shared, each of which would also fail something later
    _entry(path, "e-relay", "Sent in through the relay by someone.", entry_at=IN_WINDOW, writer_class="cp_relay")
    _entry(path, "e-nsfw", "An entry the NSFW rule withholds.", entry_at=IN_WINDOW, content_nsfw=1)
    _entry(path, "e-owner-only", "Kept to myself, and never assessed.", entry_at=IN_WINDOW)
    _entry(path, "e-offlimits", "Lunch with Quillon Marsh near the harbour.", entry_at=IN_WINDOW, writer_class=None,
           dataset=None)
    _entry(path, "e-copy", "The same words in two journals.", entry_at=IN_WINDOW)
    _entry(path, "o-copy", "The same words in two journals.", entry_at=IN_WINDOW, source="other_journal")
    with _db(path) as conn:
        conn.execute("INSERT INTO owner_only_records (canonical_table, record_id, created_at, updated_at) "
                     "VALUES ('journal_entries','e-owner-only','t','t')")
    # outside what the share covers: another source, and long before the window
    _entry(path, "o-other", "From a journal the share does not choose.", entry_at=IN_WINDOW, source="other_journal")
    _entry(path, "e-old", "From long before the window.", entry_at="2026-01-05T08:30:00")
    # messages: someone else's (never, though it is also unproven), and the owner's own with no proof
    _message(path, "imessage:901", "See you at the trailhead at nine.", is_from_self=0)
    _message(path, "imessage:902", "Running ten minutes late, sorry.")
    # AI chats: a reply (never), a prompt another door wrote (never), the owner's prompt with no proof
    _ai_chat(path, "chat-reply", "Here is a summary of your notes.", sender="assistant")
    _ai_chat(path, "chat-relay", "A prompt that came through the relay.", writer="cp_relay")
    _ai_chat(path, "chat-mine", "Draft a packing list for the weekend.")
    _publish(path, "e-ok", domains=["work"])
    _publish(path, "e-special", domains=["home"], sensitivity="special")
    _publish(path, "e-family", domains=["family"])
    _publish(path, "e-finance", domains=["finance"])
    _publish(path, "e-optout", domains=["hobbies"])
    _opt_out(path, "e-optout")
    _publish(path, "e-corrected", domains=["work"])
    _owner_says_quote(path, "e-corrected")


def test_each_item_counts_once_under_the_first_reason_that_applies(node):
    _seed(node)
    result = counts(node, policy_for())
    assert result["version"] == "topos-share-counts/v1" and result["as_of"] == NOW
    assert result["kinds"] == {
        "messages": tally(not_proven_yours=1),
        "ai_chats": tally(not_proven_yours=1),
        "journal_entries": tally(can_share=1, highly_sensitive=1, outside_your_choices=2, not_checked_yet=1,
                                 you_held_back=2, could_not_check=1, not_proven_yours=1),
        "goals": tally(),
    }


def test_the_counts_follow_the_share_and_hold_no_text(node):
    _seed(node)
    # Highly sensitive included: the special entry can be shared; family chosen: so can the family entry.
    wider = counts(node, policy_for(domains=("work", "plans", "hobbies", "home", "family"),
                                    sensitivities=("none", "personal", "special")))
    assert wider["kinds"]["journal_entries"] == tally(can_share=3, outside_your_choices=1, not_checked_yet=1,
                                                      you_held_back=2, could_not_check=1, not_proven_yours=1)
    # Journal entries alone, and only the messages source: no AI chats, no messages, no goals.
    journal = counts(node, policy_for(kinds=("journal_entry",), sources=(SOURCE,)))
    assert set(journal["kinds"]) == {"journal_entries"}
    # A source the share does not choose counts nothing of its own.
    elsewhere = counts(node, policy_for(kinds=("journal_entry",), sources=("other_journal",)))
    assert elsewhere["kinds"]["journal_entries"]["can_share"] == 0
    text = json.dumps(wider)
    for word in ("release", "prescription", "sister", "rent", "basil", "shed", "Quillon", "trailhead"):
        assert word not in text


def test_a_goal_counts_under_its_own_kind_and_inherits_its_rows_class(node):
    with _db(node) as conn:
        found = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='user_goals'").fetchone()
        if found is None:
            conn.execute("CREATE TABLE user_goals(goal_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT, "
                         "goal_text TEXT, payload_json TEXT)")
    # A goal is grounded when its entry states it in one of the fixed forms (``knowledge_projections._goal_stated``).
    _entry(node, "e-goal", "I plan to finish the garden shed by spring.", entry_at=IN_WINDOW)
    _entry(node, "e-goal-unproven", "I want to learn the harbour tide tables.", entry_at=IN_WINDOW, writer_class=None,
           dataset=None)
    _publish(node, "e-goal", domains=["home"])
    with _db(node) as conn:
        columns = [row[1] for row in conn.execute("PRAGMA table_info(user_goals)")]
        for goal_id, entry_id, text in (("goal-1", "e-goal", "finish the garden shed by spring"),
                                        ("goal-2", "e-goal-unproven", "learn the harbour tide tables")):
            values = {"goal_id": goal_id, "record_id": entry_id, "source_id": SOURCE, "goal_text": text,
                      "payload_json": "{}"}
            kept = {key: value for key, value in values.items() if key in columns}
            conn.execute(f"INSERT INTO user_goals ({','.join(kept)}) VALUES ({','.join('?' * len(kept))})",
                         list(kept.values()))
    result = counts(node, policy_for(kinds=("journal_entry", "goal")))
    assert result["kinds"]["goals"] == tally(can_share=1, not_proven_yours=1)
    assert result["kinds"]["journal_entries"] == tally(can_share=1, not_proven_yours=1)


def test_the_highly_sensitive_level_is_read_from_the_rules():
    assert not admits_special(policy_for())
    assert admits_special(policy_for(sensitivities=("personal", "special")))
    assert not admits_special(policy_for(sensitivities=("none",), excluded=()))



# --- browsing interests ----------------------------------------------------------------------------------------

@pytest.fixture()
def interests(tmp_path, monkeypatch):
    """The interest family's synthetic browsing: one topic with a closed August and the open September."""
    from tests.permissions_v2 import interest_fixtures as fx
    from tests.permissions_v2.test_interest_index import LABEL, assess
    from topos.permissions_v2 import interest_index
    from topos.permissions_v2.protection_clock import ensure_protection_clock
    monkeypatch.setenv(interest_index.FLAG, "1")
    path = tmp_path / "interests.db"
    conn = fx.open_db(path)
    fx.install(conn)
    fx.attest_app(conn)
    fx.cluster(conn, "tc_hobby", LABEL)
    fx.month_of_visits(conn, 0, 5, [3, 9, 17])
    fx.month_of_visits(conn, 100, 16, [1, 5, 19], month=9)
    # Never an interest: four visits are below the threshold.
    fx.cluster(conn, "tc_small", "chess openings / endgames / puzzles")
    fx.month_of_visits(conn, 200, 4, [2, 6, 10], cluster_id="tc_small")
    # Private-window browsing: enough visits, but every one of them in a private window.
    fx.cluster(conn, "tc_private", "camping gear / tents / stoves")
    for n in range(5):
        fx.visit(conn, 300 + n, fx.at(8, (2, 9, 16)[n % 3]), cluster_id="tc_private", incognito=1)
    # Not proven: written through the relay, not by the owner's plugin.
    fx.cluster(conn, "tc_relay", "bird watching / binoculars / field guides")
    for n in range(5):
        fx.visit(conn, 400 + n, fx.at(8, (4, 11, 18)[n % 3]), cluster_id="tc_relay", writer="cp_relay")
    conn.commit()
    assess(conn)
    conn.close()
    ensure_protection_clock(path, owner_id=fx.OWNER)
    return path


def interest_counts(path, **policy_options):
    from contextlib import contextmanager
    from tests.permissions_v2 import interest_fixtures as fx
    from tests.permissions_v2.test_interest_index import policy as interest_policy
    from topos.permissions_v2.evidence import EvidenceBinding, EvidenceResolver
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal

    @contextmanager
    def as_owner():
        token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user=fx.OWNER))
        try:
            yield
        finally:
            reset_principal(token)
    resolver = EvidenceResolver(path, binding=EvidenceBinding(environment_id="permissions-beta-test",
                                                              node_id="node-1", resource_id=fx.RESOURCE,
                                                              owner_id=fx.OWNER))
    with as_owner():
        reviews = EvidenceReviewStore(path.parent / "reviews.db", resolver=resolver)
        return count(resolver, reviews, interest_policy(**policy_options), now=fx.NOW_US // 1_000_000)


def test_an_interest_counts_per_topic_month_and_private_browsing_is_never_counted(interests):
    result = interest_counts(interests)
    assert result["kinds"]["interests"] == tally(can_share=2, not_proven_yours=1)
    # A share that releases no dates sees whole months only: the open month is outside its choices.
    whole_months = interest_counts(interests, release_event_time="none")
    assert whole_months["kinds"]["interests"] == tally(can_share=1, outside_your_choices=1, not_proven_yours=1)


# --- rows kept between counts ----------------------------------------------------------------------------------

def test_a_second_count_reuses_the_rows_until_what_they_were_read_from_moves(node, monkeypatch):
    from topos.permissions_v2 import message_evidence, share_counts
    _seed(node)
    calls = []
    real = message_evidence.qualify_automatic_message

    def counted(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)
    monkeypatch.setattr(message_evidence, "qualify_automatic_message", counted)
    first = counts(node, policy_for())
    assert calls
    # Another draft over the same rows (other topics and levels): its own classes, and no row qualified again.
    calls.clear()
    wider_policy = policy_for(domains=("work", "plans", "hobbies", "home", "family"),
                              sensitivities=("none", "personal", "special"))
    wider = counts(node, wider_policy)
    assert calls == []
    assert wider["kinds"]["journal_entries"]["can_share"] == 3
    share_counts.forget()
    assert counts(node, wider_policy) == wider                  # the same as a count that kept nothing
    # The owner's decisions move (an opt-out in the review store): the rows are read again.
    calls.clear()
    _opt_out(node, "e-ok")
    after_opt_out = counts(node, policy_for())
    assert calls
    assert after_opt_out["kinds"]["journal_entries"]["can_share"] == first["kinds"]["journal_entries"]["can_share"] - 1
    # The canonical database moves (an owner-only mark): read again; the marked entry is never shared.
    calls.clear()
    with _db(node) as conn:
        conn.execute("INSERT INTO owner_only_records (canonical_table, record_id, created_at, updated_at) "
                     "VALUES ('journal_entries','e-special','t','t')")
    after_mark = counts(node, policy_for())
    assert calls
    assert after_mark["kinds"]["journal_entries"]["held_back"]["highly_sensitive"] == 0
