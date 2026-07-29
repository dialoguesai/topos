"""M0 tests: the black-hole flag store and its notify-first rebuild contract.

Covers eval class C10 (owner fidelity + lifecycle) at the store layer, plus the
D1/D4 contracts the higher layers depend on. The read-path leak batteries
(C1–C9) live in tests/evals/privacy/blackhole/.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.lifecycle.blackhole import (
    TIER_PROVIDERS,
    BlackholeStore,
    blackholed_entity_ids,
    blackholed_name_terms,
    pending_rebuild_names,
    secure_providers_for,
)
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "blackhole.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _seed_entity(
    conn: sqlite3.Connection,
    *,
    entity_id: str = "ent-1",
    canonical_name: str = "Dana Reyes",
    normalized_name: str = "dana reyes",
    aliases_json: str = '["Dana", "D. Reyes"]',
) -> str:
    conn.execute(
        """
        INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json)
        VALUES (?, 'person', ?, ?, ?)
        """,
        (entity_id, canonical_name, normalized_name, aliases_json),
    )
    conn.commit()
    return entity_id


# ------------------------------------------------------------------ basics


def test_blackhole_by_entity_id_flags_and_keeps_the_entity(conn):
    """Additive, not destructive: the owner keeps everything (I3)."""
    _seed_entity(conn)
    store = BlackholeStore(conn)

    result = store.blackhole_entity(entity_ref="ent-1")

    assert result["already_blackholed"] is False
    assert result["entity_id"] == "ent-1"
    assert result["rebuild_state"] == "pending"
    assert store.is_blackholed("ent-1") is True
    # The entity row itself is untouched — this is not an exclusion.
    assert conn.execute("SELECT COUNT(*) FROM entities WHERE entity_id='ent-1'").fetchone()[0] == 1


def test_blackhole_by_name_matches_id_and_name_lookups(conn):
    _seed_entity(conn)
    BlackholeStore(conn).blackhole_entity(entity_ref="Dana Reyes")
    store = BlackholeStore(conn)

    assert store.is_blackholed("ent-1") is True
    assert store.is_blackholed("Dana Reyes") is True
    assert store.is_blackholed("dana reyes") is True
    assert store.is_blackholed("someone else") is False


def test_preemptive_blackhole_of_unminted_name(conn):
    """A name can be protected before the resolver ever mints an entity for it."""
    store = BlackholeStore(conn)
    result = store.blackhole_entity(entity_ref="Nobody Yet")

    assert result["entity_id"] == ""
    assert store.is_blackholed("Nobody Yet") is True
    # No id to join on yet, but the name-scan still protects it.
    assert blackholed_entity_ids(conn) == set()
    assert "nobody yet" in blackholed_name_terms(conn)


def test_bind_entity_id_attaches_a_later_minted_entity(conn):
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="Nobody Yet")

    assert store.bind_entity_id(normalized_name="Nobody Yet", entity_id="ent-9") is True
    assert blackholed_entity_ids(conn) == {"ent-9"}
    # Binding twice does not clobber an already-bound id.
    assert store.bind_entity_id(normalized_name="Nobody Yet", entity_id="ent-other") is False


def test_blackhole_is_idempotent(conn):
    """C10.6 — double-blackhole must not corrupt state or restart a finished rebuild."""
    _seed_entity(conn)
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="ent-1")
    store.mark_rebuild_complete("ent-1")

    again = store.blackhole_entity(entity_ref="ent-1", note="second time")

    assert again["already_blackholed"] is True
    assert again["rebuild_state"] == "complete"
    assert len(store.list()) == 1


def test_unknown_processing_tier_rejected(conn):
    _seed_entity(conn)
    with pytest.raises(ValueError):
        BlackholeStore(conn).blackhole_entity(entity_ref="ent-1", processing_tier="cloud")


# --------------------------------------------------- name terms & aliases


def test_name_terms_include_aliases(conn):
    """C4 — the alias-scan fallback needs every bound alias, normalized."""
    _seed_entity(conn)
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-1")

    terms = blackholed_name_terms(conn)
    assert "dana reyes" in terms
    assert "dana" in terms
    assert "d. reyes" in terms


# ------------------------------------------------- D4: notify-then-rebuild


def test_blackhole_raises_rebuild_needed_notification_first(conn):
    """D4 — the owner is told the hide is incomplete *before* the rebuild runs."""
    _seed_entity(conn)
    store = BlackholeStore(conn)

    result = store.blackhole_entity(entity_ref="ent-1")

    open_notes = store.notifications(state="open")
    assert len(open_notes) == 1
    assert open_notes[0]["kind"] == "rebuild_needed"
    assert open_notes[0]["notification_id"] == result["notification_id"]
    assert "Dana Reyes" in open_notes[0]["message"]


def test_pending_rebuild_withholds_until_complete(conn):
    """I6 — the fail-closed window: pending means withhold, not serve stale."""
    _seed_entity(conn)
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="ent-1")

    assert pending_rebuild_names(conn) == {"dana reyes"}
    assert store.has_pending_rebuild() is True

    store.mark_rebuild_running("ent-1")
    assert pending_rebuild_names(conn) == {"dana reyes"}  # still not safe

    store.mark_rebuild_complete("ent-1")
    assert pending_rebuild_names(conn) == set()
    assert store.has_pending_rebuild() is False


def test_rebuild_complete_resolves_notification_and_announces(conn):
    _seed_entity(conn)
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="ent-1")

    store.mark_rebuild_complete("ent-1")

    kinds = {n["kind"] for n in store.notifications(state="open")}
    assert kinds == {"rebuild_complete"}
    resolved = [n for n in store.notifications(state="resolved")]
    assert [n["kind"] for n in resolved] == ["rebuild_needed"]


def test_rebuild_failure_keeps_withholding(conn):
    """A failed rebuild must not quietly re-open the entity to others."""
    _seed_entity(conn)
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="ent-1")

    store.mark_rebuild_failed("ent-1", reason="digest job crashed")

    assert pending_rebuild_names(conn) == {"dana reyes"}
    open_kinds = {n["kind"] for n in store.notifications(state="open")}
    assert open_kinds == {"rebuild_needed", "rebuild_failed"}
    assert any("digest job crashed" in n["message"] for n in store.notifications(state="open"))


def test_dismiss_notification(conn):
    _seed_entity(conn)
    store = BlackholeStore(conn)
    result = store.blackhole_entity(entity_ref="ent-1")

    assert store.dismiss_notification(result["notification_id"]) is True
    assert store.notifications(state="open") == []
    assert store.dismiss_notification(result["notification_id"]) is False


# ----------------------------------------------------------- un-blackhole


def test_unblackhole_lifts_flag_and_asks_for_reinclusion(conn):
    _seed_entity(conn)
    store = BlackholeStore(conn)
    store.blackhole_entity(entity_ref="ent-1")
    store.mark_rebuild_complete("ent-1")

    result = store.unblackhole_entity(entity_ref="ent-1")

    assert result["removed"] is True
    assert store.is_blackholed("ent-1") is False
    assert blackholed_entity_ids(conn) == set()
    open_kinds = {n["kind"] for n in store.notifications(state="open")}
    assert open_kinds == {"reinclude_needed"}


def test_unblackhole_unknown_entity_is_a_noop(conn):
    assert BlackholeStore(conn).unblackhole_entity(entity_ref="ghost") == {"removed": False}


# ------------------------------------------------- D1: secure model tiers


def test_default_tier_admits_redpill(conn):
    """D1 — Red Pill's TEE counts as secure."""
    _seed_entity(conn)
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-1")

    allowed = secure_providers_for(conn, "ent-1")
    assert allowed == frozenset({"ollama", "huggingface", "redpill"})
    assert "openai" not in allowed
    assert "anthropic" not in allowed


def test_local_only_tier_excludes_redpill(conn):
    _seed_entity(conn)
    BlackholeStore(conn).blackhole_entity(entity_ref="ent-1", processing_tier="local_only")

    allowed = secure_providers_for(conn, "ent-1")
    assert allowed == frozenset({"ollama", "huggingface"})
    assert "redpill" not in allowed


def test_unprotected_entity_places_no_constraint(conn):
    """Empty set means 'no restriction', which is why callers must not treat it as 'deny all'."""
    _seed_entity(conn)
    assert secure_providers_for(conn, "ent-1") == frozenset()


def test_no_cloud_provider_in_any_tier(conn):
    """The whole point of D1, asserted structurally so a future tier cannot regress it."""
    for tier, providers in TIER_PROVIDERS.items():
        assert "openai" not in providers, tier
        assert "anthropic" not in providers, tier
        assert "grok" not in providers, tier


# ------------------------------------------------------- failure posture


def test_missing_tables_report_no_blackholes(conn):
    """A DB predating the migration genuinely has none — empty is the right answer."""
    conn.execute("DROP TABLE entity_blackholes")
    conn.commit()

    assert blackholed_entity_ids(conn) == set()
    assert blackholed_name_terms(conn) == set()
    assert pending_rebuild_names(conn) == set()
    assert BlackholeStore(conn).is_blackholed("ent-1") is False


def test_sick_database_fails_closed_rather_than_reporting_no_blackholes(conn):
    """A broken DB must raise, never silently answer 'nothing is protected'.

    Reporting an empty set on an unwell database would fail open — precisely the
    failure this feature exists to prevent.
    """

    class SickConn:
        def execute(self, *_args, **_kwargs):
            raise sqlite3.OperationalError("database disk image is malformed")

    with pytest.raises(sqlite3.OperationalError):
        BlackholeStore(SickConn()).blackholed_entity_ids()
    with pytest.raises(sqlite3.OperationalError):
        BlackholeStore(SickConn()).is_blackholed("ent-1")
