"""An app's facts read (``pack_facts``, DR-61, DR-66) over a pack the owner added from a file.

protects: a pack the owner added obeys the read's rules exactly as a bundled pack does. An app gets its facts only
when its grant names the pack and the owner switched the pack on; a special-class fact is held back from an app and
not from the owner; a fact that touches an Off-limits person or record is left out by absence. A pack whose file
is gone has no definition, so an app gets none of its facts, while the bundled packs are served as before. A pack is
on for this read by the test the ingest job uses: the registry row AND the definition the owner switched on. After
`packs remove` then `packs add` of the same id, or a new version, the row still says on until the next seeding;
in that window no reader gets the facts the pack wrote while it was on.

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
    assert out["payload"]["packs_missing"] == [PACK]
    # The pack is no longer on (its file is gone), so this read lists it for no one; the fact itself is kept.
    owner = dispatch(OWNER)["payload"]
    assert PACK not in owner["packs_enabled"] and garden.ids.plant not in by_id(owner)
    assert garden.conn.execute("SELECT COUNT(*) FROM signal_objects WHERE object_id=? AND valid_to IS NULL",
                               (garden.ids.plant,)).fetchone()[0] == 1


@pytest.mark.parametrize("how", ["removed and added again", "a new version"])
def test_between_a_re_add_and_the_next_seeding_an_app_reads_nothing_of_the_pack(garden, tmp_path, how):
    """Found in the second review. `packs add` has just said "It is switched off", and the registry row still
    says on until something seeds it (an ingest batch, a Fact packs page view; none while the node is stopped).
    The read asked the row alone, so an app whose grant names the pack still read what it wrote while it was on."""
    enable(garden.conn, PACK)
    assert garden.ids.plant in by_id(dispatch(app(PACK))["payload"])
    source = tmp_path / "again.yaml"
    if how == "a new version":
        changed = definition(PACK, "custom.plant")
        changed["version"] = "2.0.0"
        changed["predicates"].append({"name": "custom.harvest", "value_type": "string", "cardinality": "multi",
                                      "temporal": "interval", "altitude": "stated"})
        source.write_text(yaml.safe_dump(changed))
    else:
        source.write_text(yaml.safe_dump(definition(PACK, "custom.plant")))
        assert custom_packs.remove_pack(PACK) is True
    assert custom_packs.install_pack(source)["unchanged"] is False
    # nothing has seeded: the row still says on
    assert garden.conn.execute("SELECT enabled FROM pack_registry WHERE pack_id=?", (PACK,)).fetchone()[0] == 1

    out = dispatch(app(PACK, "moments.joy"))["payload"]
    assert out["packs_enabled"] == ["moments.joy"] and out["packs_missing"] == [PACK]
    assert {f["pack"] for f in out["facts"]} == {"moments.joy"}
    assert dispatch(app(PACK), {"fact_ids": [garden.ids.plant]})["payload"]["facts"] == []
    assert PACK not in dispatch(OWNER)["payload"]["packs_enabled"]
    # the seeding switches it off; the owner switching it on again is what opens the read
    seed_pack_registry(garden.conn, bundled_pack_dir())
    assert garden.conn.execute("SELECT enabled FROM pack_registry WHERE pack_id=?", (PACK,)).fetchone()[0] == 0
    assert dispatch(app(PACK))["payload"]["packs_missing"] == [PACK]
    enable(garden.conn, PACK)
    assert garden.ids.plant in by_id(dispatch(app(PACK))["payload"])


def test_a_shipped_pack_is_on_by_its_registry_row_alone_as_before(garden):
    """The definition test is asked of added packs only: a shipped pack's switch is its row, unchanged."""
    from topos.query.pack_facts import enabled_packs

    on = enabled_packs(garden.conn)
    rows = sorted(r[0] for r in garden.conn.execute("SELECT pack_id FROM pack_registry WHERE enabled=1"))
    assert on == rows and "moments.joy" in on and PACK not in on
    enable(garden.conn, PACK)
    assert enabled_packs(garden.conn) == sorted(rows + [PACK])


# --- the owner's reaction and rank on an added pack's fact (DR-72, `fact_feedback`) -------------------------------------
FEEDBACK_NOT_AVAILABLE = {"id": "r1", "status": "error", "code": 404, "error": "fact_not_available"}


def recorder(*packs, feedback=None):
    """An app that reads these packs and may record on `feedback` (all of them when not given)."""
    return Principal(cls="owner_agent", channel="uds", client_id="agent-recorder",
                     app_grant=AppGrant(packs=packs, feedback=tuple(packs if feedback is None else feedback)))


def feedback(principal, fact_id, **fields):
    return dispatch(principal, {"fact_id": fact_id, **fields}, "fact_feedback")


def test_an_app_records_on_an_added_packs_fact_only_with_a_feedback_grant_that_names_the_pack(garden):
    """The same door and the same rules as a shipped pack's fact: `--feedback facts:custom.gardening` lets the app
    record, a grant that only reads the pack does not, nor one that may record on another pack only."""
    enable(garden.conn, PACK)
    assert feedback(recorder(PACK, feedback=()), garden.ids.plant, reaction="love") == {
        "id": "r1", "status": "error", "code": 403, "error": "owner_agent_not_allowed"}
    assert feedback(recorder(PACK, "moments.joy", feedback=("moments.joy",)), garden.ids.plant,
                    reaction="love") == FEEDBACK_NOT_AVAILABLE
    out = feedback(recorder(PACK), garden.ids.plant, reaction="love", rank=2)
    assert out["status"] == "ok", out
    assert by_id(dispatch(app(PACK))["payload"])[garden.ids.plant]["reaction"] == "love"


def test_feedback_never_reaches_an_added_packs_fact_an_app_could_not_read(garden):
    """Switched off, special-class, or re-added and not yet switched on again: the same refusal as for a fact that
    does not exist, and nothing is stored."""
    assert feedback(recorder(PACK), garden.ids.plant, reaction="love") == FEEDBACK_NOT_AVAILABLE    # off
    enable(garden.conn, PACK, PRIVATE)
    assert feedback(recorder(PACK, PRIVATE), garden.ids.visit, reaction="love") == FEEDBACK_NOT_AVAILABLE
    assert custom_packs.remove_pack(PACK) is True                                               # the row still says on
    assert feedback(recorder(PACK), garden.ids.plant, reaction="love") == FEEDBACK_NOT_AVAILABLE
    assert garden.conn.execute("SELECT name FROM sqlite_master WHERE name='fact_feedback'").fetchone() is None
