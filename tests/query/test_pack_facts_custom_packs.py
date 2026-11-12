"""An app's facts read (``pack_facts``, DR-61, DR-66) over a pack the owner added from a file.

protects: a pack the owner added obeys the read's rules exactly as a bundled pack does. An app gets its facts only
when its grant names the pack and the owner switched the pack on; a special-class fact is held back from an app and
not from the owner; a fact that touches an Off-limits person or record is left out by absence. A pack whose file
is gone has no definition, so an app gets none of its facts, while the bundled packs are served as before and the
owner still sees everything.

Synthetic only: the fixtures of tests/query/test_pack_facts.py (a migrated temp database with invented people) and
an invented gardening pack in a temp directory.
"""
from __future__ import annotations

import pytest
import yaml

from tests.query.test_pack_facts import (OWNER, add_fact, by_id, day, dispatch, enable, node, protect,  # noqa: F401
                                         ref, world)
from tests.truth_lane_support import add_journal
from topos.features.derivation import custom_packs
from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
from topos.mcp_clients import AppGrant
from topos.principal import Principal

PACK = "custom.gardening"
PRIVATE = "custom.clinic"


def definition(pack, predicate, sensitivity="personal"):
    return {
        "pack": pack, "version": "1.0.0", "title": pack, "sensitivity_class": sensitivity,
        "role_policy": "authored_only", "net_subject": "deny",
        "predicates": [{"name": predicate, "value_type": "structured", "cardinality": "multi",
                        "temporal": "interval", "altitude": "stated",
                        "value_schema": {"name": "string", "with": "person_ref"}}],
        "eval": {"gold": [{"text": "I grow tomatoes"}], "negative_controls": [{"text": "A film about a garden"}]},
        "consumers": ["assistant_context"],
    }


def app(*packs):
    return Principal(cls="owner_agent", channel="uds", client_id="agent-reader", app_grant=AppGrant(packs=packs))


@pytest.fixture
def garden(world, tmp_path, monkeypatch):
    """The invented owner of test_pack_facts, plus two added packs (one special-class), both switched on."""
    home = tmp_path / "fact-packs"
    monkeypatch.setattr(custom_packs, "custom_pack_dir", lambda: home)
    for pack, predicate, sensitivity in ((PACK, "custom.plant", "personal"), (PRIVATE, "custom.visit", "special")):
        path = tmp_path / f"{pack}-source.yaml"
        path.write_text(yaml.safe_dump(definition(pack, predicate, sensitivity)))
        custom_packs.install_pack(path)
    conn = world.conn
    seed_pack_registry(conn, bundled_pack_dir())
    assert [r[0] for r in conn.execute("SELECT enabled FROM pack_registry WHERE pack_id IN (?, ?)",
                                       (PACK, PRIVATE))] == [0, 0]
    add_journal(conn, "j-garden", "Planted tomatoes by the fence this morning.", at=day(6))
    add_journal(conn, "j-garden-wren", "Wren helped me stake the beans.", at=day(3))
    conn.commit()
    world.ids.plant = add_fact(conn, PACK, "custom.plant", {"name": "tomatoes", "description": "tomatoes by the fence"},
                               at=day(6), refs=ref("journal_entries", "j-garden", 6), quote="Planted tomatoes")
    world.ids.beans = add_fact(conn, PACK, "custom.plant", {
        "name": "beans", "with": "id:ent_0a1b2c3d4e5f6071", "description": "beans, staked"},
        at=day(3), refs=ref("journal_entries", "j-garden-wren", 3))
    world.ids.visit = add_fact(conn, PRIVATE, "custom.visit", {"name": "a check-up", "description": "a check-up"},
                               at=day(2), refs=ref("journal_entries", "j-garden", 2), sensitivity="special")
    world.home = home
    return world


def test_an_added_pack_that_is_switched_off_gives_an_app_nothing(garden):
    out = dispatch(app(PACK, "moments.joy"))["payload"]
    assert out["packs_enabled"] == ["moments.joy"] and out["packs_missing"] == [PACK]
    assert {f["pack"] for f in out["facts"]} == {"moments.joy"}


def test_an_app_reads_an_added_pack_only_through_a_grant_that_names_it(garden):
    enable(garden.conn, PACK, PRIVATE)
    found = by_id(dispatch(app(PACK))["payload"])
    assert set(found) == {garden.ids.plant, garden.ids.beans}
    assert found[garden.ids.plant]["value"]["name"] == "tomatoes" and found[garden.ids.plant]["pack"] == PACK
    assert found[garden.ids.beans]["value"]["with"] == "Wren Example"          # a person as a display name, as ever
    # a grant that does not name it: refused when asked for, absent otherwise
    other = app("moments.joy")
    assert dispatch(other, {"packs": [PACK]})["error"] == "invalid_pack_facts_request"
    assert PACK not in {f["pack"] for f in dispatch(other)["payload"]["facts"]}


def test_a_special_class_added_pack_is_held_back_from_an_app_and_not_from_the_owner(garden):
    enable(garden.conn, PACK, PRIVATE)
    out = dispatch(app(PACK, PRIVATE))["payload"]
    assert out["packs_enabled"] == [PACK, PRIVATE]
    assert garden.ids.visit not in by_id(out) and garden.ids.plant in by_id(out)
    assert {garden.ids.visit, garden.ids.plant, garden.ids.beans} <= set(by_id(dispatch(OWNER)["payload"]))


def test_off_limits_takes_an_added_packs_facts_out_of_an_apps_read(garden):
    from topos.features.lifecycle.record_protection import RecordProtectionStore

    enable(garden.conn, PACK)
    protect(garden.conn, "ent_0a1b2c3d4e5f6071")
    agent = by_id(dispatch(app(PACK))["payload"])
    assert garden.ids.beans not in agent and garden.ids.plant in agent        # the person it binds is protected
    RecordProtectionStore(garden.conn).protect(canonical_table="journal_entries", record_id="j-garden")
    garden.conn.commit()
    assert by_id(dispatch(app(PACK))["payload"]) == {}                         # and the record the other rests on
    assert {garden.ids.beans, garden.ids.plant} <= set(by_id(dispatch(OWNER)["payload"]))


def test_a_pack_whose_file_is_gone_gives_an_app_nothing_and_the_rest_is_served(garden):
    enable(garden.conn, PACK)
    assert garden.ids.plant in by_id(dispatch(app(PACK, "moments.joy"))["payload"])
    custom_packs.remove_pack(PACK)
    out = dispatch(app(PACK, "moments.joy"))
    assert out["status"] == "ok"
    found = by_id(out["payload"])
    assert {f["pack"] for f in found.values()} == {"moments.joy"} and garden.ids.peak in found
    assert garden.ids.plant in by_id(dispatch(OWNER)["payload"])               # the owner's own read is whole
