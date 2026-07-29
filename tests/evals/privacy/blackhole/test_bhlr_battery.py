"""Black-Hole Leak Rate battery — the hard-0 CI gate (EVAL_ENTITY_BLACKHOLE.md §3).

BHLR = leaked_probes / total_probes across every leak surface × every non-owner
caller class. Target 0.000, absolute: one leak fails the build.

The battery is deliberately three-sided, because a one-sided leak test is easy
to pass and worthless:

1. **no BH token reaches a non-owner** — the leak gate (I1, I5);
2. **every BH token reaches the owner** — non-vacuity (I3). A filter that hides
   everything scores a perfect zero on (1); this is what stops that from
   counting as a pass, and doubles as the coverage check — a surface nobody
   planted a token in shows up here as a failure;
3. **every control token still reaches non-owners** — proves the filter is
   surgical rather than a blanket denial (also I3).
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

from topos.features.lifecycle.blackhole_guard import CallerClass, guard_for
from tests.evals.privacy.blackhole.corpus import (
    BH_ALIAS_A,
    BH_CANONICAL,
    build_blackhole_corpus,
)
from tests.evals.privacy.blackhole.surfaces import SURFACES, read_all, serialize

pytestmark = [pytest.mark.bhlr, pytest.mark.private]

# Every caller that must NOT see a black-holed entity, by the mcp_source the
# gateway resolves. `None` is the unknown caller — it must be filtered too.
NON_OWNER_CALLERS = [
    ("owner_agent_claude", {"mcp_source": "claude_desktop"}),
    ("owner_agent_cursor", {"mcp_source": "cursor"}),
    ("owner_agent_chatgpt", {"mcp_source": "chatgpt"}),
    ("routine", {"mcp_source": "routine_executor"}),
    ("grantee_plugin", {"mcp_source": "plugin_attach"}),
    ("grantee_rpt", {"mcp_source": "rpt"}),
    ("unknown", {"mcp_source": None}),
    ("forged_home_chat", {"mcp_source": "topos_home_chat", "is_grantee_request": True}),
]


@pytest.fixture()
def corpus(tmp_path):
    c = build_blackhole_corpus(str(tmp_path / "bh.db"))
    yield c
    c.conn.close()


def _leaked_tokens(blob: str, tokens: List[str]) -> List[str]:
    lowered = blob.lower()
    return [t for t in tokens if t.lower() in lowered]


# ------------------------------------------------------------- the gate


def test_bhlr_is_zero_across_all_surfaces_and_callers(corpus):
    """The number on the slide: zero leaks across every surface × caller."""
    probes: List[Dict[str, Any]] = []
    for caller_name, kwargs in NON_OWNER_CALLERS:
        guard = guard_for(corpus.conn, **kwargs)
        for surface, reader in SURFACES.items():
            blob = serialize(reader(corpus.conn, guard))
            leaked = _leaked_tokens(blob, corpus.all_bh_tokens)
            probes.append(
                {
                    "probe_id": f"{caller_name}:{surface}",
                    "caller": caller_name,
                    "surface": surface,
                    "leaked_tokens": leaked,
                }
            )

    leaks = [p for p in probes if p["leaked_tokens"]]
    bhlr = len(leaks) / len(probes) if probes else 0.0

    assert bhlr == 0.0, (
        f"BHLR={bhlr:.4f} over {len(probes)} probes; leaked: "
        + "; ".join(f"{p['probe_id']} -> {p['leaked_tokens']}" for p in leaks)
    )


def test_the_entity_name_itself_never_reaches_a_non_owner(corpus):
    """Beyond the surface tokens: the name is the thing being protected."""
    for caller_name, kwargs in NON_OWNER_CALLERS:
        guard = guard_for(corpus.conn, **kwargs)
        blob = serialize(read_all(corpus.conn, guard)).lower()
        assert BH_CANONICAL.lower() not in blob, caller_name
        assert BH_ALIAS_A.lower() not in blob, caller_name


# ------------------------------------------------------- non-vacuity (I3)


def test_owner_recovers_every_planted_token(corpus):
    """Owner recall must be 1.0 — this is what makes the gate above mean anything.

    It is also the coverage check: a surface with no planted token, or a reader
    that drops content for the owner, fails right here.
    """
    guard = guard_for(corpus.conn, mcp_source="topos_home_chat")
    blob = serialize(read_all(corpus.conn, guard))

    missing = [t for t in corpus.all_bh_tokens if t.lower() not in blob.lower()]
    recall = 1 - (len(missing) / len(corpus.all_bh_tokens))

    assert recall == 1.0, f"owner recall {recall:.3f}; unreachable tokens: {missing}"


def test_control_entity_survives_for_every_non_owner(corpus):
    """The filter must be surgical: blocking everything is not a pass."""
    for caller_name, kwargs in NON_OWNER_CALLERS:
        guard = guard_for(corpus.conn, **kwargs)
        blob = serialize(read_all(corpus.conn, guard))
        found = _leaked_tokens(blob, corpus.all_control_tokens)
        assert set(found) == set(corpus.all_control_tokens), (
            f"{caller_name} lost control tokens: "
            f"{set(corpus.all_control_tokens) - set(found)}"
        )


# ------------------------------------------------------ per-surface detail


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_each_surface_is_individually_clean(corpus, surface):
    """Same gate, split per surface so a failure names the leaking surface."""
    guard = guard_for(corpus.conn, mcp_source="rpt")
    blob = serialize(SURFACES[surface](corpus.conn, guard))
    assert _leaked_tokens(blob, corpus.all_bh_tokens) == []


def test_fact_with_protected_entity_as_object_is_blocked(corpus):
    """C5.1 — the case subject-only filtering misses."""
    guard = guard_for(corpus.conn, mcp_source="rpt")
    blob = serialize(SURFACES["facts"](corpus.conn, guard))

    assert corpus.tokens["fact_object_value"] not in blob
    # ...while the visible entity's other facts survive.
    owner_blob = serialize(
        SURFACES["facts"](corpus.conn, guard_for(corpus.conn, mcp_source="topos_home_chat"))
    )
    assert corpus.tokens["fact_object_value"] in owner_blob


def test_neighbour_dossier_is_stripped_not_dropped(corpus):
    """The visible entity's dossier survives; the protected neighbour is removed from it."""
    guard = guard_for(corpus.conn, mcp_source="rpt")
    dossiers = SURFACES["dossiers"](corpus.conn, guard)

    assert dossiers, "the control entity's dossier must survive"
    blob = serialize(dossiers)
    assert corpus.tokens["dossier_neighbour"] not in blob
    assert BH_CANONICAL.lower() not in blob.lower()


# ------------------------------------------------------------ D2: routines


def test_local_only_routine_sees_protected_content(corpus):
    """D2 — the carve-out, asserted positively so the gate above is not just
    'routines are blocked'."""
    guard = guard_for(corpus.conn, mcp_source="routine_executor", routine_local_only=True)
    blob = serialize(read_all(corpus.conn, guard))

    assert corpus.tokens["brief"] in blob
    assert guard.caller_class == CallerClass.ROUTINE


# ------------------------------------------------- I6: rebuild window


def test_pending_rebuild_withholds_prose_from_non_owners(tmp_path):
    """D4 — during the window, prose artifacts are withheld wholesale, not served stale."""
    corpus = build_blackhole_corpus(str(tmp_path / "pending.db"), rebuild_complete=False)
    try:
        guard = guard_for(corpus.conn, mcp_source="rpt")
        assert guard.withhold_pending_rebuild() is True
        assert SURFACES["briefs"](corpus.conn, guard) == []

        # The owner is never degraded by the window.
        owner = guard_for(corpus.conn, mcp_source="topos_home_chat")
        assert SURFACES["briefs"](corpus.conn, owner)
    finally:
        corpus.conn.close()


def test_pending_rebuild_still_blocks_everything_else(tmp_path):
    """The window must not be a hole: id-joinable surfaces stay filtered throughout."""
    corpus = build_blackhole_corpus(str(tmp_path / "pending2.db"), rebuild_complete=False)
    try:
        guard = guard_for(corpus.conn, mcp_source="rpt")
        blob = serialize(read_all(corpus.conn, guard))
        assert _leaked_tokens(blob, corpus.all_bh_tokens) == []
    finally:
        corpus.conn.close()


# ------------------------------------------- D5: indistinguishability


def test_protected_and_never_existed_are_indistinguishable(corpus):
    """A probe for the protected-but-unmentioned entity must look exactly like a
    probe for an entity that was never in the store."""
    guard = guard_for(corpus.conn, mcp_source="rpt")

    protected = guard.blocks_entity_id(corpus.quiet_entity_id)
    never_existed = guard.blocks_entity_id("ent-does-not-exist")

    # The store answers differently internally...
    assert protected is True
    assert never_existed is False
    # ...but what the caller receives is identical: nothing, with no reason given.
    rows_protected = guard.filter_rows([{"entity_id": corpus.quiet_entity_id}])
    assert rows_protected == []
    assert serialize(rows_protected) == serialize([])
