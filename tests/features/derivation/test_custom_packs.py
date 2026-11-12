"""A fact pack the owner adds from a file (`features/derivation/custom_packs`).

protects:
- a pack file is added only when it validates, and a refusal is one plain sentence that never echoes the file;
  a refused add leaves the node's pack directory exactly as it was;
- an added pack starts switched off, and runs only as the definition the owner switched on: a changed, removed
  or re-added file is off again, and nothing reads with the changed definition even before the registry is seeded;
- it cannot shadow, replace or collide with a bundled pack. Its id and predicates live in `custom.`, which no
  bundled pack and no grant vocabulary uses; a file dropped into the directory under a bundled id is left out;
- one bad file in the owner's directory never takes the bundled packs down;
- it goes through the same fact writer as a bundled pack: owner-only facts with the declared sensitivity, and a
  fact about another person is held, not written;
- nothing but the `packs add` / `packs remove` commands can add or remove one.

Synthetic only: an invented gardening pack in a temp directory and an in-memory or temp database.
"""
from __future__ import annotations

import json
import re
import sqlite3
from copy import deepcopy
from pathlib import Path

import pytest
import yaml

from topos.features.derivation import custom_packs
from topos.features.derivation.custom_packs import CustomPackError, install_pack, load_custom_packs
from topos.features.derivation.packs import load_packs
from topos.features.derivation.registry import (bundled_pack_dir, definition_sha, enabled_packs,
                                                runs_as_switched_on, seed_pack_registry)
from topos.storage.db.migrations.pack_registry_v1 import apply_pack_registry_v1_up

PACK = "custom.gardening"
PLANTED = "tpn_" + "planted" + "0123456789abcdef" * 2
BUNDLED = load_packs(bundled_pack_dir(), include_custom=False)


def definition(**changes):
    data = {
        "pack": PACK, "version": "1.0.0", "title": "My gardening",
        "sensitivity_class": "personal", "role_policy": "authored_only", "net_subject": "deny",
        "disclosure_default": "owner_only",
        "routing": {"descriptors": ["Plants the owner grows"], "exemplars": ["I planted basil"]},
        "guidance": {"definitions": f"Plants the owner says they grow. {PLANTED}"},
        "predicates": [{"name": "custom.plant", "value_type": "structured", "cardinality": "multi",
                        "temporal": "interval", "altitude": "stated", "value_schema": {"name": "string"}}],
        "eval": {"gold": [{"text": "I grow tomatoes", "role": "authored", "expect": []}],
                 "negative_controls": [{"text": "A film about a garden", "role": "ambient", "expect": []}]},
        "consumers": ["assistant_context"],
    }
    data.update(changes)
    return data


@pytest.fixture
def home(tmp_path, monkeypatch):
    """This node's own pack directory, in a temp directory."""
    directory = tmp_path / "node" / "fact-packs"
    monkeypatch.setattr(custom_packs, "custom_pack_dir", lambda: directory)
    custom_packs._SAID.clear()
    return directory


def source(tmp_path, data, name="my-pack.yaml") -> Path:
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else yaml.safe_dump(data))
    return path


def add(tmp_path, **changes):
    return install_pack(source(tmp_path, definition(**changes)))


def listing(home):
    return sorted(p.name for p in home.iterdir()) if home.exists() else []


@pytest.fixture
def registry(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "registry.db"))
    conn.execute("CREATE TABLE wiki_schema_migrations (migration_id TEXT PRIMARY KEY)")
    apply_pack_registry_v1_up(conn)
    yield conn
    conn.close()


def row(conn, pack_id=PACK):
    return conn.execute("SELECT enabled, origin, version FROM pack_registry WHERE pack_id=?", (pack_id,)).fetchone()


def switch_on(conn, pack_id=PACK):
    conn.execute("UPDATE pack_registry SET enabled=1 WHERE pack_id=?", (pack_id,))
    conn.commit()


# --- adding ---------------------------------------------------------------------------------------------------------
def test_an_added_pack_loads_beside_the_bundled_ones_and_starts_switched_off(home, tmp_path, registry):
    added = add(tmp_path)
    assert added == {"pack_id": PACK, "version": "1.0.0", "title": "My gardening", "replaced": None,
                     "unchanged": False, "path": str(home / f"{PACK}.yaml")}
    assert listing(home) == [f"{PACK}.yaml"]
    catalog = load_packs(bundled_pack_dir())
    assert set(catalog) == set(BUNDLED) | {PACK}
    assert catalog[PACK].first_party is False and catalog[PACK].net_subject == "deny"
    assert set(load_packs(bundled_pack_dir(), include_custom=False)) == set(BUNDLED)
    assert set(load_packs(bundled_pack_dir(), only=[PACK])) == {PACK}

    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry) == (0, "third_party", "1.0.0")
    assert PACK not in enabled_packs(registry, bundled_pack_dir())
    switch_on(registry)
    assert PACK in enabled_packs(registry, bundled_pack_dir())
    seed_pack_registry(registry, bundled_pack_dir())            # seeding again leaves the owner's switch alone
    assert row(registry)[0] == 1


def test_only_the_bundled_catalog_composes_the_owners_packs(home, tmp_path):
    add(tmp_path)
    elsewhere = tmp_path / "another-catalog"
    elsewhere.mkdir()
    assert load_packs(elsewhere) == {}


MALFORMED = [
    ("not a mapping", "- just\n- a list\n", "YAML mapping"),
    ("not yaml", "pack: [unclosed\n", "not valid YAML"),
    ("outside the namespace", lambda d: d.update(pack="gardening.mine"), "must be an id that starts with `custom.`"),
    ("a path as an id", lambda d: d.update(pack="../escape"), "must be an id that starts with `custom.`"),
    ("a bundled id", lambda d: d.update(pack="work.career"), "must be an id that starts with `custom.`"),
    ("a number for a version", lambda d: d.update(version=1.0), "`version` must be text"),
    ("no title", lambda d: d.pop("title"), "`title` must be a short name"),
    ("an unknown sensitivity", lambda d: d.update(sensitivity_class="secret"), "`sensitivity_class` must be one of"),
    ("an unknown role policy", lambda d: d.update(role_policy="everything"), "`role_policy` must be one of"),
    ("describes other people", lambda d: d.update(net_subject="allow"), "can only describe you"),
    ("wider disclosure", lambda d: d.update(disclosure_default="public"), "must be owner_only"),
    ("a lens", lambda d: d.update(synthesis=[{"kind": "pattern", "predicate": "custom.plant"}]),
     "`synthesis` is not supported"),
    ("no predicates", lambda d: d.update(predicates=[]), "`predicates` must list 1 to 50"),
    ("a bundled predicate", lambda d: d["predicates"][0].update(name="work.project"), "must start with `custom.`"),
    ("a predicate twice", lambda d: d["predicates"].append(deepcopy(d["predicates"][0])), "declared twice"),
    ("an inferred predicate", lambda d: d["predicates"][0].update(altitude="inferred"), "`altitude` must be stated"),
    ("an unknown value type", lambda d: d["predicates"][0].update(value_type="blob"), "`value_type` must be"),
    ("an unknown field type", lambda d: d["predicates"][0].update(value_schema={"name": "executable"}),
     "has a type Topos does not know"),
    ("an unknown scale", lambda d: d["predicates"][0].update(value_schema={"often": {"scale": "invented"}}),
     "names a scale Topos does not ship"),
    ("a structured value with no fields", lambda d: d["predicates"][0].pop("value_schema"), "needs `value_schema`"),
    ("an enum with no values", lambda d: d["predicates"][0].update(value_type="enum", value_schema=None),
     "must list 1 to 100 different text values"),
    ("no gold examples", lambda d: d["eval"].update(gold=[]), "`eval.gold` must list at least one example"),
    ("an example with no text", lambda d: d["eval"].update(negative_controls=[{"expect": []}]),
     "`eval.negative_controls` must list at least one example"),
    ("no consumers", lambda d: d.pop("consumers"), "`consumers` must list at least one name"),
    ("a bad cardinality", lambda d: d["predicates"][0].update(cardinality="several"), "`cardinality` must be one of"),
]


@pytest.mark.parametrize("label,change,says", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_a_malformed_pack_is_refused_in_a_plain_sentence_and_nothing_is_written(home, tmp_path, label, change, says):
    add(tmp_path)                                    # one good pack is there already
    before = (home / f"{PACK}.yaml").read_bytes()
    if callable(change):
        data = definition()
        change(data)
        bad = source(tmp_path, data, "bad.yaml")
    else:
        bad = source(tmp_path, change, "bad.yaml")
    with pytest.raises(CustomPackError) as refusal:
        install_pack(bad)
    message = str(refusal.value)
    assert says in message, message
    assert PLANTED not in message and "\n" not in message and len(message) < 400
    assert listing(home) == [f"{PACK}.yaml"] and (home / f"{PACK}.yaml").read_bytes() == before


def test_a_file_too_large_or_not_text_is_refused(home, tmp_path):
    big = tmp_path / "big.yaml"
    big.write_text("pack: custom.big\n" + "# padding\n" * 30_000)
    with pytest.raises(CustomPackError, match="larger than 256 KB"):
        install_pack(big)
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00pack")
    with pytest.raises(CustomPackError, match="not UTF-8 text"):
        install_pack(binary)
    assert listing(home) == []


def test_a_changed_pack_needs_a_new_version_and_the_same_file_again_changes_nothing(home, tmp_path):
    add(tmp_path)
    again = add(tmp_path)
    assert again["unchanged"] is True and again["replaced"] == "1.0.0"
    with pytest.raises(CustomPackError, match="Give the changed pack a new `version`"):
        add(tmp_path, title="Renamed")
    newer = add(tmp_path, title="Renamed", version="1.1.0")
    assert newer["replaced"] == "1.0.0" and newer["unchanged"] is False
    assert load_custom_packs()[PACK].title == "Renamed" and listing(home) == [f"{PACK}.yaml"]


def test_two_added_packs_cannot_declare_the_same_predicate(home, tmp_path):
    add(tmp_path)
    with pytest.raises(CustomPackError, match="already declared by custom.gardening"):
        add(tmp_path, pack="custom.orchard")
    assert listing(home) == [f"{PACK}.yaml"]


def test_a_node_holds_a_bounded_number_of_added_packs(home, tmp_path, monkeypatch):
    monkeypatch.setattr(custom_packs, "MAX_PACKS", 2)
    for n in range(2):
        add(tmp_path, pack=f"custom.p{n}", predicates=[{**definition()["predicates"][0], "name": f"custom.p{n}.thing"}])
    with pytest.raises(CustomPackError, match="already holds 2 packs"):
        add(tmp_path, pack="custom.p2", predicates=[{**definition()["predicates"][0], "name": "custom.p2.thing"}])
    assert len(listing(home)) == 2


# --- it cannot take a bundled pack's place -------------------------------------------------------------------------------
def test_nothing_topos_ships_or_grants_uses_the_custom_namespace():
    """The whole no-shadowing argument: a custom id or predicate starts with `custom.`, and nothing else does."""
    from topos.permissions_v2 import expanded_facts, selective_facts
    from topos.permissions_v2.fact_catalog_v2 import PACKS

    names = set(BUNDLED) | {name for pack in BUNDLED.values() for name in pack.predicates}
    names |= set(PACKS) | {name for spec in PACKS.values() for name in spec["predicates"]}
    names |= set(expanded_facts.LEGACY) | set(expanded_facts.LEGACY.values())
    names |= set(selective_facts.SEMANTICS) | set(selective_facts.UNAVAILABLE)
    assert len(names) > 250
    assert not [name for name in names if name == "custom" or name.startswith("custom.")]


def test_a_file_dropped_in_under_a_bundled_id_is_left_out_and_the_bundled_pack_stands(home, tmp_path, registry):
    home.mkdir(parents=True)
    bundled_id = "work.career"
    impostor = definition(pack=bundled_id, title="Not the shipped one")
    (home / f"{bundled_id}.yaml").write_text(yaml.safe_dump(impostor))
    # and one that keeps a custom id but claims a bundled predicate
    claimed = sorted(BUNDLED[bundled_id].predicates)[0]
    thief = definition(pack="custom.thief")
    thief["predicates"][0]["name"] = claimed
    (home / "custom.thief.yaml").write_text(yaml.safe_dump(thief))

    catalog = load_packs(bundled_pack_dir())
    assert set(catalog) == set(BUNDLED)
    assert catalog[bundled_id].first_party is True and catalog[bundled_id].title == BUNDLED[bundled_id].title
    assert set(custom_packs.custom_pack_problems()) == {f"{bundled_id}.yaml", "custom.thief.yaml"}
    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry, bundled_id)[1] == "first_party" and row(registry, "custom.thief") is None


def test_a_bundled_pack_wins_even_if_a_custom_pack_slips_past_validation(home, tmp_path, monkeypatch):
    """The loader's own last check, with the validation taken away: same id, or a predicate a bundled pack has."""
    bundled_id = "work.career"
    fake = deepcopy(BUNDLED[bundled_id])
    fake.first_party, fake.title = False, "Not the shipped one"
    other = deepcopy(BUNDLED[bundled_id])
    other.pack, other.first_party = "custom.other", False
    monkeypatch.setattr(custom_packs, "load_custom_packs", lambda: {bundled_id: fake, "custom.other": other})
    catalog = load_packs(bundled_pack_dir())
    assert set(catalog) == set(BUNDLED) and catalog[bundled_id].title == BUNDLED[bundled_id].title


# --- a bad file never takes the fact layer down ----------------------------------------------------------------------
def test_a_broken_file_is_skipped_said_once_and_every_other_pack_still_loads(home, tmp_path, caplog):
    add(tmp_path)
    (home / "custom.broken.yaml").write_text(f"pack: [unclosed {PLANTED}\n")
    (home / "custom.wrongname.yaml").write_text(yaml.safe_dump(definition(
        pack="custom.other", predicates=[{**definition()["predicates"][0], "name": "custom.other.thing"}])))
    (home / "custom.linked.yaml").symlink_to(home / f"{PACK}.yaml")
    with caplog.at_level("WARNING"):
        first = load_packs(bundled_pack_dir())
        second = load_packs(bundled_pack_dir())
    assert set(first) == set(second) == set(BUNDLED) | {PACK}
    problems = custom_packs.custom_pack_problems()
    assert set(problems) == {"custom.broken.yaml", "custom.wrongname.yaml", "custom.linked.yaml"}
    assert "not valid YAML" in problems["custom.broken.yaml"]
    assert "must be named custom.other.yaml" in problems["custom.wrongname.yaml"]
    assert "not a link" in problems["custom.linked.yaml"]
    said = [r.getMessage() for r in caplog.records if "custom fact pack" in r.getMessage()]
    assert len(said) == 3 and not any(PLANTED in line for line in said)        # once each, never the content


def test_no_directory_means_no_custom_packs(home):
    assert not home.exists()
    assert load_custom_packs() == {} and set(load_packs(bundled_pack_dir())) == set(BUNDLED)
    assert custom_packs.custom_pack_problems() == {}


def test_the_bundled_directory_is_never_treated_as_the_owners(monkeypatch):
    monkeypatch.setattr(custom_packs, "custom_pack_dir", bundled_pack_dir)
    assert load_custom_packs() == {} and set(load_packs(bundled_pack_dir())) == set(BUNDLED)


# --- it runs only as the definition the owner switched on -------------------------------------------------------------
def test_a_changed_pack_is_switched_off_and_reads_nothing_until_switched_on_again(home, tmp_path, registry):
    add(tmp_path)
    seed_pack_registry(registry, bundled_pack_dir())
    switch_on(registry)
    assert PACK in enabled_packs(registry, bundled_pack_dir())

    add(tmp_path, version="1.1.0", guidance={"definitions": "Now anything green."})
    # before any seeding: the row still says on, and nothing runs with the new definition
    assert row(registry)[0] == 1
    assert PACK not in enabled_packs(registry, bundled_pack_dir())
    assert runs_as_switched_on(registry, load_custom_packs()[PACK]) is False
    from topos.features.derivation.surfaces import run_pack_backfill
    with pytest.raises(ValueError, match="is not enabled"):
        run_pack_backfill(registry, PACK, 5)

    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry) == (0, "third_party", "1.1.0")
    switch_on(registry)
    assert PACK in enabled_packs(registry, bundled_pack_dir())
    assert enabled_packs(registry, bundled_pack_dir())[PACK].version == "1.1.0"


def test_a_file_edited_in_place_is_a_changed_pack_too(home, tmp_path, registry):
    add(tmp_path)
    seed_pack_registry(registry, bundled_pack_dir())
    switch_on(registry)
    path = home / f"{PACK}.yaml"
    path.write_text(path.read_text() + "\n# a comment is not a change\n")
    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry)[0] == 1
    edited = yaml.safe_load(path.read_text())
    edited["role_policy"] = "any_with_label"
    path.write_text(yaml.safe_dump(edited))
    assert PACK not in enabled_packs(registry, bundled_pack_dir())
    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry)[0] == 0


@pytest.mark.parametrize("how", ["removed", "broken"])
def test_a_pack_whose_file_is_gone_or_broken_is_switched_off_and_adding_it_again_starts_off(home, tmp_path, registry,
                                                                                            how):
    add(tmp_path)
    seed_pack_registry(registry, bundled_pack_dir())
    switch_on(registry)
    bundled_on = [r[0] for r in registry.execute("SELECT pack_id FROM pack_registry WHERE enabled=1 AND origin='first_party'")]
    assert bundled_on
    if how == "removed":
        assert custom_packs.remove_pack(PACK) is True and custom_packs.remove_pack(PACK) is False
    else:
        (home / f"{PACK}.yaml").write_text("pack: [unclosed\n")
    assert PACK not in enabled_packs(registry, bundled_pack_dir())
    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry)[0] == 0
    assert [r[0] for r in registry.execute(
        "SELECT pack_id FROM pack_registry WHERE enabled=1 AND origin='first_party'")] == bundled_on
    add(tmp_path)
    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry)[0] == 0 and PACK not in enabled_packs(registry, bundled_pack_dir())


def test_only_a_pack_the_owner_added_can_be_removed(home, tmp_path):
    for pack_id in ("work.career", "../escape", "custom", "custom/../x"):
        with pytest.raises(CustomPackError, match="Only a pack you added"):
            custom_packs.remove_pack(pack_id)


def test_a_bundled_pack_has_no_recorded_definition_and_always_runs_as_switched_on(registry):
    seed_pack_registry(registry, bundled_pack_dir())
    pack = BUNDLED["work.career"]
    assert definition_sha(pack) is None and runs_as_switched_on(registry, pack) is True
    assert registry.execute("SELECT COUNT(*) FROM pack_registry WHERE definition_sha IS NOT NULL").fetchone()[0] == 0


def test_seeding_another_directory_does_not_switch_off_the_owners_packs(home, tmp_path, registry):
    add(tmp_path)
    seed_pack_registry(registry, bundled_pack_dir())
    switch_on(registry)
    empty = tmp_path / "another-catalog"
    empty.mkdir()
    seed_pack_registry(registry, empty)
    assert row(registry)[0] == 1


# --- the same fact writer ---------------------------------------------------------------------------------------------
@pytest.fixture
def node_db():
    from topos.storage.canonical.conversations_tables import ensure_all_tables
    from topos.storage.db.migrations import apply_all_migrations

    conn = sqlite3.connect(":memory:")
    ensure_all_tables(conn)
    apply_all_migrations(conn)
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) "
                 "VALUES('self-owner','person','Owner','owner',1)")
    conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) "
                 "VALUES('ent-neighbour','person','Rowan Example','rowan example',0)")
    conn.commit()
    yield conn
    conn.close()


@pytest.mark.parametrize("sensitivity", ["personal", "special"])
def test_the_writer_stores_an_added_packs_fact_owner_only_with_its_declared_sensitivity(home, tmp_path, node_db,
                                                                                         sensitivity):
    from topos.features.derivation.writer import DerivationWriter

    add(tmp_path, sensitivity_class=sensitivity)
    pack = load_packs(bundled_pack_dir(), only=[PACK])[PACK]
    out = DerivationWriter(node_db, "synthetic-model").assert_pack_fact(
        pack=pack, predicate="custom.plant", subject_entity_id="self-owner", value={"name": "tomatoes"},
        actor_role="authored", source_refs=[{"table": "conversation_messages", "record_id": "synthetic-source"}],
        quote="I grow tomatoes", event_date="2026-11-01")
    assert out["outcome"] == "written", out
    oid, version, payload = node_db.execute(
        "SELECT ontology_id, ontology_version, payload_json FROM signal_objects WHERE object_id=?",
        (out["object_id"],)).fetchone()
    payload = json.loads(payload)
    assert (oid, version) == (PACK, "1.0.0")
    assert payload["value_struct"] == {"name": "tomatoes"} and payload["pack"] == PACK
    assert payload["disclosure"] == "owner_only" and payload["sensitivity"] == sensitivity


def test_a_fact_about_another_person_is_held_not_written(home, tmp_path, node_db):
    from topos.features.derivation.writer import DerivationWriter

    add(tmp_path)
    pack = load_packs(bundled_pack_dir(), only=[PACK])[PACK]
    out = DerivationWriter(node_db, "synthetic-model").assert_pack_fact(
        pack=pack, predicate="custom.plant", subject_entity_id="self-owner", value={"name": "roses"},
        actor_role="authored", source_refs=[{"table": "conversation_messages", "record_id": "synthetic-source"}],
        quote="Rowan grows roses", about="other:Rowan Example", event_date="2026-11-01")
    assert out["outcome"] == "quarantined", out
    assert node_db.execute("SELECT COUNT(*) FROM signal_objects WHERE ontology_id=?", (PACK,)).fetchone()[0] == 0


# --- nothing else adds one ---------------------------------------------------------------------------------------------
def test_only_the_packs_command_can_add_or_remove_a_pack():
    """An app reaches the node through handlers, routes and tools. None of them may add a pack, so the functions
    that write the owner's pack directory are named in exactly two modules: where they live and the terminal
    command that asks a person first."""
    root = Path(custom_packs.__file__).resolve().parents[2]
    writers = re.compile(r"\b(install_pack|remove_pack)\b")
    found = sorted(str(path.relative_to(root)) for path in root.rglob("*.py") if writers.search(path.read_text()))
    assert found == ["cli/packs_cmd.py", "features/derivation/custom_packs.py"]
    # and the directory itself is written nowhere else
    users = sorted(str(path.relative_to(root)) for path in root.rglob("*.py")
                   if re.search(r"\bcustom_pack_dir\b", path.read_text()))
    assert users == ["features/derivation/custom_packs.py", "query/pack_facts.py"]


def test_the_example_in_the_docs_is_a_pack_that_can_be_added(home):
    example = Path(__file__).resolve().parents[3] / "docs" / "examples" / "custom.gardening.yaml"
    added = install_pack(example)
    assert added["pack_id"] == "custom.gardening" and listing(home) == ["custom.gardening.yaml"]
