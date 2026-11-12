"""A fact pack the owner adds from a file (`features/derivation/custom_packs`).

protects:
- a pack file is added only when it validates, and a refusal is one plain sentence that never echoes the file;
  a refused add leaves the node's pack directory exactly as it was;
- a file the checks accept is one the engine can run: every part the engine reads (routing, guidance, revision,
  key fields, event identity) is held to its shape, so no accepted file can stop the ingest batch of every pack;
- the title is one printable line: it is the first line of what `packs add` asks the owner to approve;
- an added pack starts switched off, and runs only as the definition the owner switched on: a changed, removed
  or re-added file is off again, and nothing reads with the changed definition even before the registry is seeded
  (a pack removed and added again with no seeding in between included);
- while it is off the ingest job reads nothing for it: no shadow trial, no prefilter count, no ledger row;
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
from types import SimpleNamespace

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
    """The pack files. The stamp beside each (`.custom.name.added`) is in `snapshot`."""
    return sorted(p.name for p in home.iterdir() if not p.name.startswith(".")) if home.exists() else []


def snapshot(home):
    """Every file in the directory and its bytes, stamps included."""
    return {p.name: p.read_bytes() for p in sorted(home.iterdir())} if home.exists() else {}


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
    # every part the engine reads is held to its shape: the ingest job builds a prefilter for every loaded pack
    ("routing descriptors that are a number", lambda d: d.update(routing={"descriptors": 5}),
     "`routing.descriptors` must be a list"),
    ("routing exemplars that are a number", lambda d: d.update(routing={"exemplars": 5}),
     "`routing.exemplars` must be a list"),
    ("routing exemplars that are not text", lambda d: d.update(routing={"exemplars": [5, ["nested"]]}),
     "`routing.exemplars` must be a list"),
    ("routing that is a list", lambda d: d.update(routing=["plants"]), "`routing` must be a mapping"),
    ("guidance that is text", lambda d: d.update(guidance=PLANTED), "`guidance` must be a mapping"),
    ("abstention that is a number", lambda d: d.update(guidance={"abstention": 5}),
     "`guidance.abstention` must be a list"),
    ("definitions that are a list", lambda d: d.update(guidance={"definitions": [PLANTED]}),
     "`guidance.definitions` must be text"),
    ("key fields that are a number", lambda d: d["predicates"][0].update(key_fields=5),
     "`key_fields` must list fields"),
    ("a key field the value does not have", lambda d: d["predicates"][0].update(key_fields=[PLANTED]),
     "`key_fields` must list fields"),
    ("required fields that are text", lambda d: d["predicates"][0].update(required_fields="name"),
     "`required_fields` must list fields"),
    ("qualifiers that are a list", lambda d: d["predicates"][0].update(qualifiers=[PLANTED]),
     "`qualifiers` must be a mapping"),
    ("an event identity Topos does not know", lambda d: d["predicates"][0].update(event_identity=f"never\n{PLANTED}"),
     "`event_identity` must be once, dated or windowed"),
    ("a note that is a list", lambda d: d["predicates"][0].update(note=[PLANTED]), "`note` must be text"),
    ("revision that is a number", lambda d: d.update(revision=5), "`revision` must be a mapping"),
    ("a revision rule a pack you add cannot hold", lambda d: d.update(revision={"closes": [{"on_value": 5}]}),
     "`revision` must be a mapping that holds only"),
    ("supersedes_on for a predicate it does not declare",
     lambda d: d.update(revision={"supersedes_on": {"custom.other": ["name"]}}), "`revision.supersedes_on` must map"),
    ("supersedes_on that is a number", lambda d: d.update(revision={"supersedes_on": 5}),
     "`revision.supersedes_on` must map"),
    # the title is printed above what the owner approves
    ("a title of two lines", lambda d: d.update(title=f"My gardening\n  reads: nothing {PLANTED}"),
     "`title` must be a short name for the pack on one line"),
    ("a title with a terminal escape", lambda d: d.update(title="My gardening\x1b[2K\rsensitivity: none"),
     "`title` must be a short name for the pack on one line"),
    ("a title with a text-direction override", lambda d: d.update(title="My gardening \u202e enon :ytivitisnes"),
     "`title` must be a short name for the pack on one line"),
    ("a title that is only a tab", lambda d: d.update(title="\t"), "`title` must be a short name"),
    # a refusal never carries text from the file that did not pass its own check
    ("a predicate name that is a sentence", lambda d: d["predicates"][0].update(name=f"custom.x {PLANTED}\nreads"),
     "The name of predicate 1 must start with `custom.`"),
    ("a very long predicate name", lambda d: d["predicates"][0].update(name="custom." + "a" * 5000),
     "The name of predicate 1 must start with `custom.`"),
    ("a field name that is a sentence", lambda d: d["predicates"][0].update(value_schema={f"N {PLANTED}": "string"}),
     "the name of field 1 must use lowercase letters"),
    # plain YAML only: the registry digest and the engine walk the parsed file on every load
    ("a key that is not text", lambda d: d.update({5: "five"}), "Every key in the file must be text"),
    ("a nested key that is not text", lambda d: d["guidance"].update({True: "yes"}),
     "Every key in the file must be text"),
    ("an anchor repeated by alias",
     yaml.safe_dump(definition()) + "extra_a: &part [one, two]\nextra_b: *part\n", "YAML anchor or alias"),
    ("a value that is not plain", yaml.safe_dump(definition()) + "extra: !!binary aGVsbG8=\n",
     "a YAML type a pack cannot hold"),
    ("nesting without end", yaml.safe_dump(definition()) + "extra: " + "[" * 40 + "]" * 40 + "\n",
     "nests more than 12 levels"),
]


@pytest.mark.parametrize("label,change,says", MALFORMED, ids=[m[0] for m in MALFORMED])
def test_a_malformed_pack_is_refused_in_a_plain_sentence_and_nothing_is_written(home, tmp_path, label, change, says):
    add(tmp_path)                                    # one good pack is there already
    before = snapshot(home)
    assert sorted(before) == [f".{PACK}.added", f"{PACK}.yaml"]
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
    assert PLANTED not in message and message.isprintable() and len(message) < 400
    assert snapshot(home) == before
    # the check that runs when the directory is loaded says the same, so a file dropped in by hand is left out
    with pytest.raises(CustomPackError):
        custom_packs.parse_definition(bad.read_bytes())


def test_a_file_too_large_or_not_text_is_refused(home, tmp_path):
    big = tmp_path / "big.yaml"
    big.write_text("pack: custom.big\n" + "# padding\n" * 30_000)
    with pytest.raises(CustomPackError, match="larger than 256 KB"):
        install_pack(big)
    binary = tmp_path / "binary.yaml"
    binary.write_bytes(b"\xff\xfe\x00pack")
    with pytest.raises(CustomPackError, match="not UTF-8 text"):
        install_pack(binary)
    assert snapshot(home) == {}


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
    assert sorted(snapshot(home)) == [f".{PACK}.added", f"{PACK}.yaml"]


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


def test_a_pack_removed_and_added_again_with_no_seeding_in_between_starts_off(home, tmp_path, registry):
    """The node was stopped, or simply idle: no ingest batch and no Fact packs page view ran between the remove
    and the add, so the registry row still says on and the file is byte for byte the one it was switched on for.
    The stamp `packs add` and `packs remove` renew is what tells them apart."""
    add(tmp_path)
    seed_pack_registry(registry, bundled_pack_dir())
    switch_on(registry)
    file_before = (home / f"{PACK}.yaml").read_bytes()
    stamp_before = (home / f".{PACK}.added").read_bytes()
    assert PACK in enabled_packs(registry, bundled_pack_dir())

    assert custom_packs.remove_pack(PACK) is True
    assert listing(home) == [] and (home / f".{PACK}.added").read_bytes() != stamp_before
    added = add(tmp_path)
    assert added["unchanged"] is False and (home / f"{PACK}.yaml").read_bytes() == file_before
    assert row(registry)[0] == 1                                    # nothing has seeded: the row still says on
    assert PACK not in enabled_packs(registry, bundled_pack_dir())  # and it does not run
    assert runs_as_switched_on(registry, load_custom_packs()[PACK]) is False
    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry)[0] == 0 and PACK not in enabled_packs(registry, bundled_pack_dir())
    switch_on(registry)
    assert PACK in enabled_packs(registry, bundled_pack_dir())


def test_adding_the_same_file_again_leaves_the_stamp_and_the_switch_alone(home, tmp_path, registry):
    add(tmp_path)
    seed_pack_registry(registry, bundled_pack_dir())
    switch_on(registry)
    before = snapshot(home)
    assert add(tmp_path)["unchanged"] is True and snapshot(home) == before
    seed_pack_registry(registry, bundled_pack_dir())
    assert row(registry)[0] == 1 and PACK in enabled_packs(registry, bundled_pack_dir())


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


# --- what the checks accept, the engine can run -----------------------------------------------------------------------------
_WRONG = (5, "x", [5], {"a": 5}, [["x"]], True, 1.5, [], {})
_ENGINE_READS = [
    ("routing",), ("routing", "descriptors"), ("routing", "exemplars"),
    ("guidance",), ("guidance", "definitions"), ("guidance", "abstention"),
    ("revision",), ("revision", "supersedes_on"), ("revision", "noop_when"), ("revision", "closes"),
    ("revision", "exclusive_with"), ("revision", "supersedes_on", "custom.plant"),
    ("predicates", 0, "key_fields"), ("predicates", 0, "required_fields"), ("predicates", 0, "qualifiers"),
    ("predicates", 0, "event_identity"), ("predicates", 0, "note"), ("predicates", 0, "values"),
    ("predicates", 0, "value_schema"), ("predicates", 0, "value_schema", "name"), ("predicates", 0, "sensitivity"),
    ("eval",), ("eval", "gold"), ("eval", "gold", 0), ("eval", "gold", 0, "text"), ("consumers",),
    ("synthesis",), ("title",), ("version",), ("sensitivity_class",), ("role_policy",),
]
#: Shapes a pack may well use. Each must be accepted, and then run.
_ALLOWED = [
    (("predicates", 0, "key_fields"), ["name"]), (("predicates", 0, "required_fields"), ["name"]),
    (("predicates", 0, "event_identity"), "once"), (("predicates", 0, "event_identity"), "windowed:30"),
    (("predicates", 0, "temporal"), "episodic"), (("predicates", 0, "cardinality"), "single"),
    (("predicates", 0, "qualifiers"), {"season": "text"}), (("predicates", 0, "note"), "Only plants in the ground."),
    (("predicates", 0, "value_schema"), {"name": "string", "kind": ["herb", "fruit"], "since": "date_or_approx"}),
    (("predicates", 0, "sensitivity"), "special"), (("routing",), None), (("guidance",), None), (("revision",), None),
    (("guidance", "abstention"), ["Not plants in fiction."]), (("routing", "exemplars"), []),
]


def _mutated(path, value):
    data = definition(revision={"supersedes_on": {"custom.plant": ["name"]}, "noop_when": "same value"})
    node = data
    for step in path[:-1]:
        if isinstance(node, dict) and not isinstance(node.get(step), (dict, list)):
            node[step] = {}
        node = node[step]
    node[path[-1]] = value
    return data


def test_a_file_the_checks_accept_is_one_the_engine_can_run(home, node_db, monkeypatch):
    """A wrong shape at every place the engine reads a pack: each is refused, or the pack runs end to end.

    Found in review: `routing: {descriptors: 5}` was accepted, and the ingest job, which builds a prefilter for
    every loaded pack on or off, then raised on every batch for every bundled pack."""
    from topos.features.derivation import router
    from topos.features.derivation.prefilter import PackPrefilter
    from topos.features.derivation.template import build_prompt, parse_output
    from topos.features.derivation.verify import lens_note_for
    from topos.features.derivation.writer import DerivationWriter

    monkeypatch.setattr(router, "_embed", lambda text: None)       # no embedding model in a test
    home.mkdir(parents=True)
    reply = json.dumps({"assertions": [{"predicate": "custom.plant", "value": {"name": "tomatoes"}, "about": "owner",
                                        "occurrence_date": None, "confidence": 0.9, "quote": "I grow tomatoes"}]})
    accepted = refused = 0
    cases = [(path, value, False) for path in _ENGINE_READS for value in _WRONG]
    for path, value, must_pass in cases + [(path, value, True) for path, value in _ALLOWED]:
        if True:
            data = _mutated(path, value)
            where = f"{'.'.join(map(str, path))} = {value!r}"
            try:
                custom_packs.check_definition(data)
            except CustomPackError:
                assert not must_pass, f"a shape a pack may use was refused: {where}"
                refused += 1
                continue
            accepted += 1
            (home / f"{PACK}.yaml").write_text(yaml.safe_dump(data))
            pack = load_custom_packs().get(PACK)
            assert pack is not None, f"accepted by the check but not loaded: {where}"
            try:
                assert definition_sha(pack)
                PackPrefilter(pack).passes("I grow tomatoes in my garden")
                router.pack_centroid(pack)
                build_prompt(pack, "I grow tomatoes", "2026-11-01", "authored")
                lens_note_for(pack)
                valid, _rejects = parse_output(reply, pack, record_text="I grow tomatoes")
                writer = DerivationWriter(node_db, "synthetic-model")
                for assertion in valid:
                    for plant in ("tomatoes", "basil"):
                        writer.assert_pack_fact(
                            pack=pack, predicate=assertion["predicate"], subject_entity_id="self-owner",
                            value={"name": plant}, actor_role="authored", quote=f"I grow {plant}",
                            source_refs=[{"table": "conversation_messages", "record_id": "synthetic-source"}],
                            event_date="2026-11-01")
            except Exception as exc:  # noqa: BLE001
                raise AssertionError(f"accepted by the check, then the engine raised: {where}: {exc!r}") from exc
    assert refused > 150 and accepted >= len(_ALLOWED) + 10, (refused, accepted)


@pytest.mark.parametrize("routing", [{"descriptors": 5}, {"exemplars": 5}])
def test_the_shape_that_stopped_every_batch_is_refused_and_left_out_at_load(home, tmp_path, routing):
    with pytest.raises(CustomPackError, match="must be a list"):
        add(tmp_path, routing=routing)
    home.mkdir(parents=True, exist_ok=True)
    (home / f"{PACK}.yaml").write_text(yaml.safe_dump(definition(routing=routing)))     # dropped in by hand
    assert set(load_packs(bundled_pack_dir())) == set(BUNDLED)
    assert "must be a list" in custom_packs.custom_pack_problems()[f"{PACK}.yaml"]


def test_the_engine_loaders_own_refusal_is_not_passed_on(home, tmp_path, monkeypatch):
    """The loader quotes the value it refused. Whatever it says, the owner and the node log get a fixed sentence."""
    from topos.features.derivation.packs import PackValidationError

    def refuse(path, **kwargs):
        raise PackValidationError(f"pack custom.gardening: bad thing {PLANTED!r}\nreads: nothing")

    monkeypatch.setattr(custom_packs, "load_pack", refuse)
    with pytest.raises(CustomPackError) as refusal:
        add(tmp_path)
    assert str(refusal.value) == "The file is not a pack Topos can read."


def test_the_prompt_lines_are_printable_and_hold_only_checked_text(home, tmp_path):
    add(tmp_path)
    lines = custom_packs.describe(load_custom_packs()[PACK])
    assert lines[0] == f"{PACK}  1.0.0  My gardening" and len(lines) == 4
    assert all(line.isprintable() and PLANTED not in line for line in lines)


# --- while it is off, the ingest job reads nothing for it -------------------------------------------------------------------
GARDEN_ROW = {"content": "I planted basil and tomatoes in the raised bed today", "message_id": "garden-1",
              "_table": "conversation_messages", "actor_role": "authored", "event_at": "2026-11-01",
              "source_id": "synthetic"}


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    """The ingest batch on a temp database, with a stubbed model that records every prompt it is sent. No model
    is loaded."""
    from topos.engine.backends import ollama
    from topos.enrichment.jobs.canonical.derivation_job import run_derivation_batch
    from topos.storage.db.migrations import apply_all_migrations

    node_db = sqlite3.connect(tmp_path / "ingest.db")
    apply_all_migrations(node_db)
    node_db.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, is_self)"
                    " VALUES ('self-owner','person','Owner','owner','[]',1)")
    node_db.execute("CREATE TABLE IF NOT EXISTS conversation_messages (message_id TEXT PRIMARY KEY, content TEXT,"
                    " event_at TEXT, actor_role TEXT, source_id TEXT)")
    prompts = []
    extract = json.dumps({"assertions": [{"predicate": "custom.plant", "value": {"name": "basil"}, "about": "owner",
                                          "occurrence_date": None, "confidence": 0.9, "quote": "I planted basil"}]})
    verify = json.dumps({"supported": True, "about": "owner", "fields_ok": True, "reason": "stated"})

    def generate(self, model, prompt, **kwargs):
        if "Respond with exactly" in prompt:
            return {"text": "ok"}
        prompts.append(prompt)
        return {"text": verify if "strict fact-checker" in prompt else extract}

    monkeypatch.setattr(ollama.OllamaAdapter, "_generate", generate)
    monkeypatch.setattr("topos.features.facts.llm_extract._resolved_extraction_model", lambda s, c: "stub-model")
    # what the shadow trial needs before it samples: prefilter hits on record, and recent records that match
    node_db.execute("INSERT OR IGNORE INTO pack_yield (pack_id, day, prefilter_hits) VALUES (?, date('now'), 40)",
                    (PACK,))
    for n in range(12):
        node_db.execute(
            "INSERT INTO conversation_messages (message_id, content, event_at, actor_role, source_id)"
            " VALUES (?, ?, datetime('now'), 'authored', 'synthetic')",
            (f"recent-{n}", "I planted basil in the garden again and I grow tomatoes too"))
    node_db.commit()

    def run(rows=(GARDEN_ROW,)):
        stats = {}
        written = run_derivation_batch(node_db, list(rows), stats=stats)
        return written, stats

    def about_the_pack():
        return [prompt for prompt in prompts if "My gardening" in prompt]

    def count(sql, *args):
        return node_db.execute(sql, args).fetchone()[0]

    yield SimpleNamespace(run=run, prompts=prompts, about_the_pack=about_the_pack, count=count, db=node_db)
    node_db.close()


def test_a_pack_that_was_never_switched_on_is_not_trialled_counted_or_read(home, tmp_path, ingest):
    """Found in review: the ingest job's shadow trial runs every pack that is off, and nothing left out an added
    one: 12 records went to the model under its prompt and 12 rows landed in the training ledger."""
    add(tmp_path)
    _written, stats = ingest.run()
    assert ingest.about_the_pack() == []
    assert f"trial_{PACK}" not in stats
    assert ingest.count("SELECT COUNT(*) FROM derivation_training_ledger WHERE pack_id=?", PACK) == 0
    assert ingest.count("SELECT COUNT(*) FROM pack_offers WHERE pack_id=?", PACK) == 0
    assert ingest.count("SELECT COALESCE(SUM(prefilter_hits),0) FROM pack_yield WHERE pack_id=?", PACK) == 40
    assert ingest.count("SELECT COUNT(*) FROM signal_objects WHERE ontology_id=?", PACK) == 0
    assert ingest.count("SELECT enabled FROM pack_registry WHERE pack_id=?", PACK) == 0


def test_a_file_dropped_in_by_hand_is_not_trialled_either(home, ingest):
    home.mkdir(parents=True)
    (home / f"{PACK}.yaml").write_text(yaml.safe_dump(definition()))
    _written, stats = ingest.run()
    assert ingest.about_the_pack() == [] and f"trial_{PACK}" not in stats
    assert ingest.count("SELECT COUNT(*) FROM derivation_training_ledger WHERE pack_id=?", PACK) == 0


def test_a_pack_switched_off_because_its_file_changed_is_not_trialled_with_the_changed_definition(home, tmp_path,
                                                                                                  ingest):
    add(tmp_path)
    seed_pack_registry(ingest.db, bundled_pack_dir())
    switch_on(ingest.db)
    add(tmp_path, version="1.1.0", title="My gardening, changed")
    _written, stats = ingest.run()                # this batch is the seeding that switches the changed pack off
    assert ingest.about_the_pack() == [] and f"trial_{PACK}" not in stats
    assert ingest.count("SELECT enabled FROM pack_registry WHERE pack_id=?", PACK) == 0
    assert ingest.count("SELECT COUNT(*) FROM derivation_training_ledger WHERE pack_id=?", PACK) == 0
    assert ingest.count("SELECT COUNT(*) FROM signal_objects WHERE ontology_id=?", PACK) == 0


def test_once_the_owner_switches_it_on_the_ingest_job_reads_for_it(home, tmp_path, ingest):
    add(tmp_path)
    ingest.run()                                               # seeds the registry row, off
    switch_on(ingest.db)
    written, _stats = ingest.run([{**GARDEN_ROW, "message_id": "garden-2"}])
    assert written >= 1 and ingest.about_the_pack()
    assert ingest.count("SELECT COUNT(*) FROM signal_objects WHERE ontology_id=?", PACK) == 1
    assert ingest.count("SELECT COUNT(*) FROM derivation_training_ledger WHERE pack_id=? AND stage='trial'", PACK) == 0


def test_an_added_pack_no_prefilter_can_be_built_for_is_left_out_and_the_batch_still_runs(home, tmp_path, ingest,
                                                                                         monkeypatch, caplog):
    """The second line of defence: whatever shape gets past the checks, the packs Topos ships still run."""
    from topos.features.derivation import prefilter

    add(tmp_path)
    ingest.run()
    switch_on(ingest.db)
    build = prefilter.PackPrefilter.__init__

    def init(self, pack):
        if pack.pack == PACK:
            raise TypeError("'int' object is not iterable")
        build(self, pack)

    monkeypatch.setattr(prefilter.PackPrefilter, "__init__", init)
    before = len(ingest.prompts)
    both = {**GARDEN_ROW, "message_id": "garden-2",
            "content": "Signed the offer, starting as Staff Engineer in March. I planted basil to celebrate."}
    with caplog.at_level("WARNING"):
        _written, stats = ingest.run([both])
    assert "skipped" not in stats and "deferred" not in stats
    assert len(ingest.prompts) > before                       # a pack Topos ships read the record
    assert [p for p in ingest.prompts[before:] if "My gardening" in p] == []
    assert any(PACK in record.getMessage() and "left out" in record.getMessage() for record in caplog.records)


# --- the Fact packs page and the direct-answer lane -------------------------------------------------------------------
def test_each_added_pack_shows_only_its_own_pending_conflicts(home, tmp_path, node_db):
    from topos.features.derivation.surfaces import list_packs

    add(tmp_path)
    add(tmp_path, pack="custom.orchard", predicates=[{**definition()["predicates"][0], "name": "custom.tree"}])
    seed_pack_registry(node_db, bundled_pack_dir())
    for n, predicate in enumerate(["custom.plant", "custom.plant", "custom.tree"]):
        node_db.execute(
            "INSERT INTO fact_conflicts (conflict_id, subject_entity_id, predicate, incumbent_object_id,"
            " challenger_value, status) VALUES (?, 'self-owner', ?, 'synthetic-object', 'synthetic', 'pending')",
            (f"synthetic-conflict-{n}", predicate))
    node_db.commit()
    shown = {p["pack_id"]: p["pending_conflicts"] for p in list_packs(node_db)["packs"]}
    assert shown[PACK] == 2 and shown["custom.orchard"] == 1
    custom_packs.remove_pack("custom.orchard")                # a row whose file is gone claims none
    shown = {p["pack_id"]: p["pending_conflicts"] for p in list_packs(node_db)["packs"]}
    assert shown[PACK] == 2 and shown["custom.orchard"] == 0


def test_the_direct_answer_lane_indexes_only_the_predicates_topos_ships(home, tmp_path, monkeypatch):
    """Its index is built once per process and matches a question on a predicate's last word. An added pack's
    predicate stays out: `custom.plant` would otherwise take every question holding "plant" away from the model,
    only after a restart, and still after the pack was removed."""
    from topos.query import facts_direct

    add(tmp_path)
    monkeypatch.setattr(facts_direct, "_generic_index_cache", None)
    names = {name for _pattern, name, _special in facts_direct._generic_predicate_index()}
    assert len(names) > 100 and not [name for name in names if name.startswith("custom.")]
    monkeypatch.setattr(facts_direct, "_generic_index_cache", None)


# --- nothing else adds one ---------------------------------------------------------------------------------------------
def test_only_the_packs_command_can_add_or_remove_a_pack():
    """An app reaches the node through handlers, routes and tools. None of them may add a pack, so the functions
    that write the owner's pack directory are named in exactly two modules: where they live and the terminal
    command that asks a person first."""
    root = Path(custom_packs.__file__).resolve().parents[2]
    writers = re.compile(r"\b(install_pack|install_content|remove_pack|_renew_stamp|_write_beside)\b")
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
