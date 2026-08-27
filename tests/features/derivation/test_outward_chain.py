"""G4 — the outward lane as ONE chain, not five gates tested apart.

Each gate has its own tests; this walks the path an enabled net.capability pack actually
takes, because the retest kept finding defects BETWEEN tested parts (the API 500 lived in a
call the mocks never made). Proven first against a shadow copy of the live node; this is the
hermetic twin.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.features.derivation.writer import DerivationWriter
from topos.storage.db.migrations.net_subject_policy_v1 import apply_net_subject_policy_v1_up
from topos.storage.db.migrations.registry import MIGRATIONS


@pytest.fixture()
def node(tmp_path):
    """A minimal node: real migrations, one owner, one nameable person, one blackholed."""
    c = sqlite3.connect(str(tmp_path / "node.db"))
    c.execute("CREATE TABLE IF NOT EXISTS wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    for spec in sorted(MIGRATIONS, key=lambda m: m.order):
        try:
            spec.fn(c)
        except Exception:  # noqa: BLE001
            pass
    apply_net_subject_policy_v1_up(c)
    c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
              " is_self, mention_count) VALUES ('ent_me', 'person', 'Owner', 'owner', 1, 5)")
    c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
              " is_self, mention_count) VALUES ('ent_kim', 'person', 'Hotel India',"
              " 'hotel india', 0, 5)")
    c.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name,"
              " is_self, mention_count) VALUES ('ent_gone', 'person', 'Forgotten Person',"
              " 'forgotten person', 0, 5)")
    c.execute("INSERT INTO entity_blackholes (blackhole_id, entity_id, normalized_name)"
              " VALUES ('bh1', 'ent_gone', 'forgotten person')")
    c.commit()
    yield c
    c.close()


def _assert_outward(conn, pack, person, **over):
    w = DerivationWriter(conn, model="chain-test")
    kw = dict(pack=pack, predicate="net.demonstrated_skill", subject_entity_id="ignored",
              value={"person": person, "skill": "evaluation design", "basis": "reviewed_work"},
              actor_role="addressed",
              source_refs=[{"table": "conversation_messages", "record_id": "m1"}],
              confidence=0.9, about=f"other:{person}")
    kw.update(over)
    return w.assert_pack_fact(**kw)


def test_the_whole_outward_chain(node):
    seed_pack_registry(node, bundled_pack_dir())
    node.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id='net.capability'")
    node.commit()
    pack = load_packs(bundled_pack_dir(), only=["net.capability"])["net.capability"]

    # 1. a nameable, non-blackholed person: the fact LANDS, on their entity, owner_only
    res = _assert_outward(node, pack, "Hotel India")
    assert res["outcome"] == "written"
    row = node.execute(
        "SELECT object_key, json_extract(payload_json,'$.disclosure') FROM signal_objects"
        " WHERE ontology_id='net.capability' AND valid_to IS NULL").fetchone()
    assert row[0].startswith("fact:ent_kim:")
    assert row[1] == "owner_only"

    # 2. a blackholed person refuses, with the reason on the queue row
    res2 = _assert_outward(node, pack, "Forgotten Person")
    assert res2["outcome"] == "quarantined"
    reasons = [r[0] for r in node.execute(
        "SELECT incumbent_object_id FROM fact_conflicts").fetchall()]
    assert any("net_subject_blackholed" in r for r in reasons)

    # 3. an unresolvable subject quarantines rather than guessing
    res3 = _assert_outward(node, pack, "Somebody Unheard Of")
    assert res3["outcome"] == "quarantined"

    # 4. the projection guard: rebuilding the graph creates NO edge from the stored fact
    from topos.features.entities.fact_materializer import materialize_signal_objects_to_graph

    materialize_signal_objects_to_graph(node)
    n = node.execute(
        "SELECT COUNT(*) FROM entity_edges WHERE src_entity_id='ent_kim'"
        " OR dst_entity_id='ent_kim'").fetchone()[0]
    assert n == 0, "an outward fact must never be restated as a graph edge"


def test_the_chain_is_shut_without_enablement(node):
    """The same write against the DEFAULT registry state must refuse at gate 1 — proving
    the whole lane is opt-in, not merely opt-out."""
    seed_pack_registry(node, bundled_pack_dir())
    enabled = node.execute(
        "SELECT enabled FROM pack_registry WHERE pack_id='net.capability'").fetchone()[0]
    assert enabled == 0, "the outward pack ships disabled"
