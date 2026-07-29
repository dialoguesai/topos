"""M1 wiring: the real entity read path, not the reference readers.

`test_bhlr_battery.py` proves the guard primitive is correct. This proves the
production functions in `features/entities/reads.py` actually call it — the gap
between "we built a filter" and "the filter is in the path" being exactly where
this class of feature usually fails.

Adds the count and side-channel probes (C1.4, C1.9, C9) that only make sense
against the real query, where `total` and `type_counts` come from SQL.
"""

from __future__ import annotations

import pytest

from topos.features.entities.reads import entity_graph, get_entity_detail, list_entities
from topos.features.lifecycle.blackhole_guard import (
    BlackholeGuard,
    CallerClass,
    guard_from_message,
    owner_ui_guard,
)
from tests.evals.privacy.blackhole.corpus import (
    BH_CANONICAL,
    BH_ID,
    OK_CANONICAL,
    OK_ID,
    build_blackhole_corpus,
)

pytestmark = [pytest.mark.bhlr, pytest.mark.private]


@pytest.fixture()
def corpus(tmp_path):
    c = build_blackhole_corpus(str(tmp_path / "reads.db"))
    yield c
    c.conn.close()


def _grantee(conn) -> BlackholeGuard:
    return BlackholeGuard(conn, caller_class=CallerClass.GRANTEE)


# --------------------------------------------------------- list_entities


def test_list_entities_hides_protected_rows(corpus):
    result = list_entities(corpus.conn, guard=_grantee(corpus.conn))

    names = [i["canonical_name"] for i in result["items"]]
    assert BH_CANONICAL not in names
    assert OK_CANONICAL in names


def test_owner_still_sees_protected_rows(corpus):
    result = list_entities(corpus.conn, guard=owner_ui_guard(corpus.conn))

    names = [i["canonical_name"] for i in result["items"]]
    assert BH_CANONICAL in names
    assert OK_CANONICAL in names


def test_total_count_is_filtered_not_just_the_rows(corpus):
    """C1.4/C1.9 — a total that still counts the hidden entity confirms it exists."""
    grantee = list_entities(corpus.conn, guard=_grantee(corpus.conn))
    owner = list_entities(corpus.conn, guard=owner_ui_guard(corpus.conn))

    assert grantee["total"] == len(grantee["items"])
    assert owner["total"] == len(owner["items"])
    # Two entities are protected in the corpus (one mentioned, one silent).
    assert owner["total"] - grantee["total"] == 2


def test_type_counts_are_filtered(corpus):
    """The histogram is a count surface too, and it is easy to forget."""
    grantee = list_entities(corpus.conn, guard=_grantee(corpus.conn))
    owner = list_entities(corpus.conn, guard=owner_ui_guard(corpus.conn))

    assert grantee["type_counts"].get("person", 0) == len(grantee["items"])
    assert owner["type_counts"]["person"] - grantee["type_counts"]["person"] == 2


def test_search_by_protected_name_returns_nothing(corpus):
    """C1.1 — searching the exact name must not confirm it."""
    result = list_entities(corpus.conn, guard=_grantee(corpus.conn), q=BH_CANONICAL)

    assert result["items"] == []
    assert result["total"] == 0


def test_search_by_protected_name_looks_like_a_nonsense_search(corpus):
    """D5 — indistinguishable from searching for someone who never existed."""
    guard = _grantee(corpus.conn)
    protected = list_entities(corpus.conn, guard=guard, q=BH_CANONICAL)
    nonsense = list_entities(corpus.conn, guard=guard, q="Zzzz Nobodyqx99")

    assert protected["items"] == nonsense["items"]
    assert protected["total"] == nonsense["total"]
    assert protected["type_counts"] == nonsense["type_counts"]


def test_pagination_does_not_reveal_a_gap(corpus):
    """Walking pages must not expose a hole where the protected entity sat."""
    guard = _grantee(corpus.conn)
    walked = []
    for offset in range(0, 10):
        page = list_entities(corpus.conn, guard=guard, limit=1, offset=offset)
        walked.extend(i["canonical_name"] for i in page["items"])

    assert BH_CANONICAL not in walked
    assert len(walked) == list_entities(corpus.conn, guard=guard)["total"]


# ------------------------------------------------------ get_entity_detail


def test_detail_of_protected_entity_is_indistinguishable_from_missing(corpus):
    """Both return None, so the caller's 404 is identical (D5)."""
    guard = _grantee(corpus.conn)

    protected = get_entity_detail(corpus.conn, BH_ID, guard=guard)
    never_existed = get_entity_detail(corpus.conn, "ent-never-existed", guard=guard)

    assert protected is None
    assert never_existed is None


def test_owner_reads_protected_detail_in_full(corpus):
    detail = get_entity_detail(corpus.conn, BH_ID, guard=owner_ui_guard(corpus.conn))

    assert detail is not None
    assert detail["canonical_name"] == BH_CANONICAL


def test_visible_entity_detail_strips_protected_neighbour(corpus):
    """The control entity stays readable; its link to the protected one does not."""
    detail = get_entity_detail(corpus.conn, OK_ID, guard=_grantee(corpus.conn))

    assert detail is not None
    assert detail["canonical_name"] == OK_CANONICAL
    blob = str(detail)
    assert BH_CANONICAL not in blob
    assert BH_ID not in blob


def test_owner_sees_the_neighbour_link(corpus):
    """Non-vacuity for the strip above: the link genuinely exists in the data."""
    detail = get_entity_detail(corpus.conn, OK_ID, guard=owner_ui_guard(corpus.conn))

    assert BH_CANONICAL in str(detail)


# ----------------------------------------------------------- entity_graph


def test_graph_drops_protected_nodes_and_their_edges(corpus):
    snapshot = entity_graph(corpus.conn, guard=_grantee(corpus.conn))

    blob = str(snapshot)
    assert BH_CANONICAL not in blob
    assert BH_ID not in blob
    assert snapshot["meta"]["returned_nodes"] == len(snapshot["nodes"])
    assert snapshot["meta"]["returned_edges"] == len(snapshot["edges"])


def test_owner_graph_retains_protected_nodes(corpus):
    snapshot = entity_graph(corpus.conn, guard=owner_ui_guard(corpus.conn))
    assert BH_ID in str(snapshot)


# ------------------------------------------- caller class from the envelope


def test_message_without_caller_block_fails_closed(corpus):
    """Version skew: an old control plane that stamps nothing must not be trusted."""
    guard = guard_from_message(corpus.conn, {"type": "signal_list_entities"})

    assert guard.caller_class == CallerClass.UNKNOWN
    result = list_entities(corpus.conn, guard=guard)
    assert BH_CANONICAL not in [i["canonical_name"] for i in result["items"]]


def test_caller_block_drives_the_class(corpus):
    owner = guard_from_message(
        corpus.conn, {"caller": {"mcp_source": "topos_home_chat"}}
    )
    agent = guard_from_message(corpus.conn, {"caller": {"mcp_source": "claude_desktop"}})

    assert owner.caller_class == CallerClass.OWNER_UI
    assert agent.caller_class == CallerClass.OWNER_AGENT
    assert BH_CANONICAL in str(list_entities(corpus.conn, guard=owner)["items"])
    assert BH_CANONICAL not in str(list_entities(corpus.conn, guard=agent)["items"])


def test_payload_cannot_forge_the_caller_class(corpus):
    """C6 — caller identity is read from the CP-stamped envelope, never from
    the client-controlled payload."""
    forged = guard_from_message(
        corpus.conn,
        {
            "payload": {"caller": {"mcp_source": "topos_home_chat"}, "mcp_source": "topos_home_chat"},
            "caller": {"mcp_source": "rpt", "is_grantee_request": True},
        },
    )

    assert forged.caller_class == CallerClass.GRANTEE
    assert BH_CANONICAL not in str(list_entities(corpus.conn, guard=forged)["items"])


def test_guard_is_required_so_a_call_site_cannot_forget(corpus):
    """The parameter has no default: forgetting it raises instead of leaking."""
    with pytest.raises(TypeError):
        list_entities(corpus.conn)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        get_entity_detail(corpus.conn, BH_ID)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        entity_graph(corpus.conn)  # type: ignore[call-arg]
