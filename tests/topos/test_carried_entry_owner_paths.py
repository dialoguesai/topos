"""Third fix round, ruling P.2: every path that serves the owner himself behaves exactly as before the upgrade while
an entry the upgrade carried still waits; and P.3: from the owner's act it is an ordinary entry on every such path.

The re-check (REVIEW_R1_NODE, R2-H1) measured what one excluded contact with a short or ordinary alias did to the
owner's own tools right after the upgrade step, with no clean-up: the username "al" dropped every query item for his
outside AI client, moved four model calls in five, and stamped every item his own app retrieved. Each test here takes
one owner-serving path, reads it BEFORE the step, right AFTER it (must be the same answer) and after the owner's act
(must differ: otherwise the test would pass with nothing protected at all).

protects, one path each:
  - the query pipeline's exit filter, for the owner's outside client (through the control plane, and at the node's own
    door) and for his app (nothing dropped, nothing stamped);
  - a real retrieval for the outside client, through the function a turn calls (nothing emptied);
  - the pipeline's row filter over a conversation with the carried contact;
  - the model gate and the two answers the control plane routes a home-chat turn on;
  - the read-time guard as the owner's outside client holds it (no summary withheld);
  - the producers of the owner's own derived text and graph (nothing refused, no fingerprint moved).
Every person, handle and id here is invented; the short ordinary names are the ones this round is about.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.topos.test_carried_entry_waits import EXOTIC, ORDINARY, excluded
from tests.topos.test_carry_step_review_r1 import cid, conn  # noqa: F401 (conn: fixture)
from topos.features.lifecycle.blackhole import OWNER, BlackholeStore, start_waiting_clean_up
from topos.features.lifecycle.blackhole_guard import BlackholeGuard, CallerClass, guard_from_message
from topos.features.lifecycle.blackhole_llm import evaluate
from topos.features.lifecycle.contact_excludes import carry_contact_excludes
from topos.principal import OWNER_APP, THIRD_PARTY, Principal, reset_principal, set_principal
from topos.query.retrieval import _blackhole_policy_for_summary, _blackhole_policy_for_clusters

pytestmark = pytest.mark.public

OWNER_ID = "owner-user-1"
#: The owner's own callers, as the node's doors verify them.
APP = Principal(cls=OWNER_APP, channel="cp_relay", acting_user=OWNER_ID)
OUTSIDE_CLIENT = Principal(cls=THIRD_PARTY, channel="cp_relay", client_id="mcp", acting_user=OWNER_ID)
LOCAL_CLIENT = Principal(cls=THIRD_PARTY, channel="local_http", client_id="enrolled")

#: Text that has nothing to do with the excluded contact and holds each alias's letters inside other words.
UNRELATED = [
    "We also finished the normal walk before the usual rain.",
    "Edited the homework and pushed the fixed branch.",
    "The network was slow all week, so the samples came late.",
    "Just a short note in the journal about the project.",
    "Same plan as before: jam session on Thursday.",
]
#: Text that names the contact by each alias as a word of its own.
NAMING = {"username al": "Lunch with Al went late.", "alias Ed": "Ed rang about the boiler.",
          "saved as J": "J has the spare keys.", "saved as Sam": "Sam is bringing the ladder.",
          "handle work": "Left a message for work about Friday."}


def items(label):
    """Summary items shaped as `retrieval._canonical_row_to_item` returns them (the re-check's shape)."""
    return [{"topic": text[:120], "summary_text": text, "record_id": f"m-{i}", "source_id": "src",
             "relevance_score": 0.5, "retrieval_source": "canonical:conversation_messages"}
            for i, text in enumerate([*UNRELATED, NAMING[label]])]


def as_caller(principal, fn, *args, **kwargs):
    token = set_principal(principal)
    try:
        return fn(*args, **kwargs)
    finally:
        reset_principal(token)


@pytest.fixture(autouse=True)
def the_node_knows_its_owner(monkeypatch):
    """As every real node does (`core.handlers._relay_owner_id`): the control plane's stamp for the owner's outside
    client names this id."""
    import topos.core.handlers as hub

    monkeypatch.setattr(hub, "_relay_owner_id", lambda: OWNER_ID)


def the_owner_acts(c):
    store = BlackholeStore(c)
    for entry in store.list():
        start_waiting_clean_up(store, entry["blackhole_id"], processing_tier="secure", note=None)


# ------------------------------------------------------------------------------------- the query pipeline's exit

@pytest.mark.parametrize("label", list(ORDINARY))
@pytest.mark.parametrize("caller", [OUTSIDE_CLIENT, LOCAL_CLIENT], ids=["through_the_control_plane", "at_the_nodes_own_door"])
def test_the_outside_client_gets_every_query_item_while_the_entry_waits(conn, label, caller):
    """Rule: the exit filter reads the request's own view (`retrieval._off_limits_view`). Read every entry there and
    "al" drops all six items for the owner's outside client again (the key `retrieval_source` holds the letters)."""
    excluded(conn, ORDINARY[label])
    given = items(label)
    before = as_caller(caller, _blackhole_policy_for_summary, given, conn=conn, disclosure_tier="default_disclosure")
    carry_contact_excludes(conn)
    conn.commit()
    waiting = as_caller(caller, _blackhole_policy_for_summary, given, conn=conn, disclosure_tier="default_disclosure")
    assert before == given and waiting == given                           # nothing dropped: 0 of 6
    the_owner_acts(conn)
    acted = as_caller(caller, _blackhole_policy_for_summary, given, conn=conn, disclosure_tier="default_disclosure")
    assert given[-1] not in acted and len(acted) < len(given)             # an ordinary entry now: the naming item goes


@pytest.mark.parametrize("label", list(ORDINARY))
def test_the_owners_app_gets_no_item_stamped_while_the_entry_waits(conn, label):
    """The stamp is what the control plane routes the owner's turn on: a stamped item moves his model call."""
    excluded(conn, ORDINARY[label])
    given = items(label)
    carry_contact_excludes(conn)
    conn.commit()
    waiting = as_caller(APP, _blackhole_policy_for_summary, given, conn=conn, disclosure_tier="owner_raw")
    assert waiting == given and not any(item.get("blackhole_protected") for item in waiting)
    clusters = [{"label": text, "centroid_preview": text} for text in (*UNRELATED, NAMING[label])]
    assert as_caller(APP, _blackhole_policy_for_clusters, clusters, conn=conn, disclosure_tier="owner_raw") == clusters
    the_owner_acts(conn)
    acted = as_caller(APP, _blackhole_policy_for_summary, given, conn=conn, disclosure_tier="owner_raw")
    assert len(acted) == len(given) and acted[-1].get("blackhole_protected") is True


def test_a_real_retrieval_for_the_outside_client_is_the_same_packet_while_the_entry_waits(tmp_path):
    """Through `DefaultSignalRetrievalAdapter.retrieve`, the function a turn calls. Before this round ANY entry
    emptied every summary-mode query for a caller who is not the owner's app (`retrieval`'s derived-mode floor reads
    whether the guard is active), so one carried contact, whatever its names, left the owner's outside client with
    nothing: the re-check measured the exit filter alone and did not see it."""
    from tests.evals.privacy.blackhole.corpus import OK_CANONICAL, SOURCE_ID, build_blackhole_corpus
    from topos.query.manifest_validation import resolve_scope_manifest
    from topos.query.retrieval import DefaultSignalRetrievalAdapter
    from topos.query.types import RetrievalRequest
    from topos.storage.adapters.factory import AdapterFactory
    from topos.storage.canonical import ConversationsTablesManager

    c = build_blackhole_corpus(str(tmp_path / "corpus.db")).conn
    ConversationsTablesManager(c).ensure_tables()
    store = BlackholeStore(c)
    for entry in store.list():                                            # a home with no Off-limits entry at all
        store.unblackhole_entity(entity_ref=entry["blackhole_id"])
    c.commit()

    def packet():
        adapter = DefaultSignalRetrievalAdapter(AdapterFactory.create("local_database", conn=c))
        found = as_caller(LOCAL_CLIENT, adapter.retrieve, RetrievalRequest(
            manifest=resolve_scope_manifest("messages:read"), access_mode="summary",
            query_text=f"what happened with the {OK_CANONICAL} thread", installed_source_ids=[SOURCE_ID],
            owner_mode=False, disclosure_tier="default_disclosure")).context_packet
        return json.loads(json.dumps(found, sort_keys=True, default=str))

    before = packet()
    assert len(before["summaries"]) >= 3, "control: the outside client is answered before the step"
    excluded(c, ORDINARY["username al"])
    assert carry_contact_excludes(c)["carried"] == 1
    c.commit()
    assert packet() == before                                             # exactly as before the upgrade
    the_owner_acts(c)
    c.commit()
    # Until BL-112 (the owner's ruling of 8 Oct 2026) the derived-mode floor emptied this for an entry he made. Now
    # the entry hides its items and releases the rest: nothing here names the person, so nothing is withheld.
    assert packet() == before


def test_the_row_filter_keeps_a_conversation_with_the_carried_contact_for_the_outside_client(conn):
    """The pipeline's canonical rows pass the share boundary's own veto (`filter_observed_canonical_rows`). For the
    owner's own client that boundary is built without what waits: a contact row of the carried person, and a row
    that names them, are still his to read."""
    excluded(conn, ORDINARY["saved as Sam"])
    rows = [{"contact_id": cid("0a"), "display_name": "Sam"}, {"contact_id": cid("zz"), "display_name": "Perrin Ashgrove"}]
    carry_contact_excludes(conn)
    conn.commit()
    mine = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE, view=OWNER)
    assert mine.filter_observed_canonical_rows(rows, canonical_table="contacts") == rows
    assert not mine.active and mine.blocked_record_ids() == set()
    the_owner_acts(conn)
    acted = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE, view=OWNER)
    assert acted.filter_observed_canonical_rows(rows, canonical_table="contacts") == rows[1:] and acted.active


# -------------------------------------------------------------------------------------------- the model gate

@pytest.mark.parametrize("label", list(ORDINARY))
def test_no_model_call_is_moved_or_blocked_while_the_entry_waits(conn, label):
    """Rule: the gate reads the owner's own view (`blackhole_llm._blackhole_rows`). Read every entry and a carried
    "al" moves the owner's enrichment and chat turns off the model he chose."""
    excluded(conn, ORDINARY[label])
    carry_contact_excludes(conn)
    conn.commit()
    texts = [*UNRELATED, NAMING[label], "\n".join(UNRELATED)]
    for text in texts:
        verdict = evaluate(conn, {"prompt": text}, provider="openai")
        assert (verdict.tainted, verdict.provider, verdict.redirected, verdict.blocked) == (False, "openai", False, False)
    the_owner_acts(conn)
    named = evaluate(conn, {"prompt": NAMING[label]}, provider="openai")
    assert named.tainted and named.provider != "openai"


def test_the_control_plane_is_told_nothing_is_protected_while_the_entry_waits(conn, monkeypatch):
    """`blackhole_status` and `blackhole_check_text` are what the control plane routes a home-chat turn on."""
    import topos.core.handlers as hub
    from topos.core.handlers.signal_features import handle_blackhole_check_text, handle_blackhole_status

    monkeypatch.setattr(hub, "get_db_connection", lambda: conn)
    excluded(conn, ORDINARY["username al"])
    status = lambda: asyncio.run(handle_blackhole_status({"id": "s"}))["payload"]                      # noqa: E731
    check = lambda text: asyncio.run(handle_blackhole_check_text({"id": "c", "payload": {"text": text}}))["payload"]  # noqa: E731
    before = (status(), check(UNRELATED[0]), check(NAMING["username al"]))
    carry_contact_excludes(conn)
    conn.commit()
    assert (status(), check(UNRELATED[0]), check(NAMING["username al"])) == before
    assert before[0] == {"has_blackholes": False, "pending_rebuild": False} and before[1]["protected"] is False
    the_owner_acts(conn)
    assert status() == {"has_blackholes": True, "pending_rebuild": True}
    assert check(NAMING["username al"])["protected"] is True and check(UNRELATED[0])["protected"] is False


# ----------------------------------------------------------------------------------------- the read-time guard

def test_no_summary_is_withheld_from_the_owners_outside_client_while_the_entry_waits(conn):
    """The re-check's run (R2-H3): an unrelated summary was kept for the owner's app and withheld from his outside
    client for as long as an entry waited, which without a control in the app was for ever."""
    excluded(conn, ORDINARY["saved as Sam"])
    artifacts = [{"text": "The week went to the compiler and the bouldering trip."}, {"text": NAMING["saved as Sam"]}]

    def kept(caller_class):
        return BlackholeGuard(conn, caller_class=caller_class).filter_name_string_artifacts(artifacts, text_keys=("text",))

    before = {cls: kept(cls) for cls in (CallerClass.OWNER_UI, CallerClass.OWNER_AGENT)}
    carry_contact_excludes(conn)
    conn.commit()
    assert {cls: kept(cls) for cls in before} == before == {cls: artifacts for cls in before}
    agent = BlackholeGuard(conn, caller_class=CallerClass.OWNER_AGENT)
    assert not agent.withhold_pending_rebuild() and not agent.active and not agent.text_mentions_blackholed("Sam")
    the_owner_acts(conn)
    assert kept(CallerClass.OWNER_AGENT) == [] and kept(CallerClass.OWNER_UI) == artifacts   # his clean-up is owed now


def test_a_request_the_node_verified_as_the_owners_reads_his_view_at_the_guard_too(conn):
    """`guard_from_message` still filters a caller who is not the owner's app, and reads the request's own view."""
    excluded(conn, ORDINARY["saved as Sam"])
    carry_contact_excludes(conn)
    conn.commit()
    assert as_caller(OUTSIDE_CLIENT, guard_from_message, conn, {}).view == OWNER
    assert not as_caller(OUTSIDE_CLIENT, guard_from_message, conn, {}).active
    assert as_caller(APP, guard_from_message, conn, {}).sees_everything


# ------------------------------------------------------------------------------------------------ the producers

def test_the_owners_own_derivation_and_graph_are_as_before_while_the_entry_waits(conn):
    """The producers of his own derived text read the owner's own view: nothing is refused on account of a person
    who was only ever excluded from sharing, and the graph's input fingerprint does not move (no rebuild)."""
    from topos.features.derivation.net_subject_policy import ALLOW, may_write_about, set_subject_policy
    from topos.features.entities.graph_inputs import _off_limits_part
    from topos.features.lifecycle.blackhole import blackholed_entity_ids
    from topos.features.lifecycle.off_limits_view import for_own_processing
    from topos.features.signal.cluster_labels import label_mentions_protected
    from topos.features.signal.topic_clustering import _protected_name_terms

    excluded(conn, ORDINARY["alias Ed"])
    set_subject_policy(conn, "ent-0a", ALLOW)

    def read():
        decision = may_write_about(conn, "ent-0a", pack_allows_net_subject=True)
        return {"may_derive": (decision.allowed, decision.reason),
                "label_refused": label_mentions_protected("Ed and the boiler", _protected_name_terms(conn)),
                "left_out_of_the_graph": blackholed_entity_ids(conn, view=for_own_processing()),
                "fingerprint": _off_limits_part(conn)}

    before = read()
    assert before["may_derive"] == (True, "net_subject_allowed") and not before["label_refused"]
    carry_contact_excludes(conn)
    conn.commit()
    assert read() == before
    the_owner_acts(conn)
    acted = read()
    assert (acted["may_derive"], acted["label_refused"]) == ((False, "net_subject_blackholed"), True)
    assert acted["left_out_of_the_graph"] == {"ent-0a"} and acted["fingerprint"] != before["fingerprint"]


def test_the_graph_fingerprint_is_byte_for_byte_the_old_one_where_nothing_waits(conn):
    """So the upgrade itself moves no node's fingerprint: the part is what `_table_part` returned for this table."""
    from topos.features.entities.graph_inputs import _off_limits_part, _table_part

    BlackholeStore(conn).blackhole_entity(entity_ref=EXOTIC)
    old = _table_part(conn, "entity_blackholes", ("entity_id", "normalized_name", "canonical_name", "aliases_json"))
    assert _off_limits_part(conn) == old
    excluded(conn, ORDINARY["saved as Sam"], tail="0b")
    carry_contact_excludes(conn)                                          # adds the column and one waiting entry
    conn.commit()
    assert _off_limits_part(conn) == old
