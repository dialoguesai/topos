"""The grantee DDR pipeline, and the taint feed that activates CP Gate C.

`_build_summary_items` is where every loader's output converges before it
leaves the engine, so it is where the black hole has to bite for grantees. The
same choke point does double duty: for the owner it keeps the items and stamps
them, which is the only way the control plane can learn that a prompt it is
about to route contains protected content.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.blackhole import BlackholeStore
from topos.query.retrieval import _blackhole_policy_for_summary
from topos.storage.db.migrations import apply_all_migrations

pytestmark = [pytest.mark.bhlr, pytest.mark.private]

PROTECTED = "Dana Qx71reyes"
ALIAS = "Dqx72nickname"
VISIBLE = "Sam Ok91okoye"

GRANTEE_TIERS = ("scoped", "summary", "inference", "default")


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "retrieval.db"))
    apply_all_migrations(c)
    c.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json)
        VALUES ('ent-bh', 'person', ?, ?, ?)
        """,
        (PROTECTED, PROTECTED.lower(), f'["{ALIAS}"]'),
    )
    c.commit()
    BlackholeStore(c).blackhole_entity(entity_ref="ent-bh")
    yield c
    c.close()


def _items():
    return [
        {"summary_text": f"dinner with {PROTECTED} on tuesday"},
        {"summary_text": f"standup with {VISIBLE}"},
        {"content": f"note about {ALIAS}"},
        {"topic": "quarterly planning"},
    ]


# ------------------------------------------------------------- grantee view


@pytest.mark.parametrize("tier", GRANTEE_TIERS)
def test_grantee_never_receives_protected_items(conn, tier):
    kept = _blackhole_policy_for_summary(_items(), conn=conn, disclosure_tier=tier)

    blob = str(kept)
    assert PROTECTED not in blob
    assert ALIAS not in blob


@pytest.mark.parametrize("tier", GRANTEE_TIERS)
def test_grantee_keeps_everything_else(conn, tier):
    """Surgical: only the protected items go."""
    kept = _blackhole_policy_for_summary(_items(), conn=conn, disclosure_tier=tier)

    assert len(kept) == 2
    assert any(VISIBLE in str(i) for i in kept)
    assert any("quarterly planning" in str(i) for i in kept)


def test_grantee_items_are_dropped_not_stamped(conn):
    """A stamped-but-present item would still carry the name into the response."""
    kept = _blackhole_policy_for_summary(_items(), conn=conn, disclosure_tier="scoped")

    assert not any(i.get("blackhole_protected") for i in kept)


# --------------------------------------------------- owner view + taint feed


def test_owner_keeps_protected_items(conn):
    kept = _blackhole_policy_for_summary(_items(), conn=conn, disclosure_tier="owner_raw")

    assert len(kept) == len(_items())
    assert any(PROTECTED in str(i) for i in kept)


def test_owner_items_are_stamped_so_gate_c_can_fire(conn):
    """The stamp is the control plane's only way to know: it has no entity store."""
    kept = _blackhole_policy_for_summary(_items(), conn=conn, disclosure_tier="owner_raw")

    stamped = [i for i in kept if i.get("blackhole_protected")]
    unstamped = [i for i in kept if not i.get("blackhole_protected")]

    assert len(stamped) == 2  # the canonical name and the alias
    assert all(PROTECTED not in str(i) and ALIAS not in str(i) for i in unstamped)


def test_stamp_does_not_mutate_the_original_item(conn):
    """Loaders may hold references; stamping must not leak into their copies."""
    items = _items()
    _blackhole_policy_for_summary(items, conn=conn, disclosure_tier="owner_raw")

    assert not any("blackhole_protected" in i for i in items)


# --------------------------------------------------------- inert conditions


def test_no_blackholes_is_a_passthrough(tmp_path):
    c = sqlite3.connect(str(tmp_path / "empty.db"))
    apply_all_migrations(c)
    try:
        items = _items()
        assert _blackhole_policy_for_summary(items, conn=c, disclosure_tier="scoped") == items
    finally:
        c.close()


def test_no_connection_is_a_passthrough(conn):
    items = _items()
    assert _blackhole_policy_for_summary(items, conn=None, disclosure_tier="scoped") == items


def test_empty_items_short_circuit(conn):
    assert _blackhole_policy_for_summary([], conn=conn, disclosure_tier="scoped") == []


# -------------------------------------------------------- failure posture


def test_unreadable_store_refuses_to_serve_a_grantee(conn):
    """Failing to read the flags must not become "nothing is protected"."""

    class SickConn:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database disk image is malformed")

    with pytest.raises(sqlite3.OperationalError):
        _blackhole_policy_for_summary(_items(), conn=SickConn(), disclosure_tier="scoped")


def test_unreadable_store_does_not_break_the_owner(conn):
    """The owner is not a leak risk, so their own view degrades gracefully."""

    class SickConn:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database disk image is malformed")

    items = _items()
    assert (
        _blackhole_policy_for_summary(items, conn=SickConn(), disclosure_tier="owner_raw")
        == items
    )
