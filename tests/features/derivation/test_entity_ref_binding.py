"""`entity_ref` is a type the packs declare and the pipeline honours.

48 declarations across 12 packs used the entity-ref family, and no code matched the
token. The prompt builder recognised only enum notations, so every one of those fields
was advertised to the model as free text — it was TOLD to write prose, wrote prose, and
the materialiser resolved that prose as a topic. Every relationship fact on the live node
pointed at a topic rather than at a person.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from topos.features.derivation import packs as P
from topos.features.derivation import template as T
from topos.features.derivation.registry import bundled_pack_dir


@pytest.fixture(scope="module")
def packs_dir():
    d = Path(bundled_pack_dir())
    T.set_pack_dir(d)
    return d


@pytest.fixture()
def work_pack(packs_dir):
    return P.load_pack(packs_dir / "work.career.yaml")


class TestTheTypeIsAdvertised:
    def test_a_person_field_asks_for_a_known_person(self, packs_dir):
        pack = P.load_pack(packs_dir / "relationships.social.yaml")
        line = next(l for l in T._predicate_menu(pack).splitlines()
                    if l.startswith("- rel.relationship:"))
        assert "person's name from the list above" in line
        assert "person(free text)" not in line

    def test_a_LIST_of_people_asks_for_an_array(self, work_pack):
        line = next(l for l in T._predicate_menu(work_pack).splitlines()
                    if l.startswith("- work.project"))
        assert "collaborators(JSON array of person names" in line
        assert "collaborators(free text)" not in line

    def test_an_entity_ref_that_is_NOT_a_person_is_left_alone(self, work_pack):
        """`entity_ref` says "a thing this archive knows" and not which kind — the same
        type declares org, venue and project. Pointing those at the known-PEOPLE list
        would instruct the model to answer a project field with somebody's name."""
        line = next(l for l in T._predicate_menu(work_pack).splitlines()
                    if l.startswith("- work.project"))
        assert "project(free text)" in line

    def test_the_person_field_set_comes_from_the_schema(self, work_pack):
        fields = T.person_fields_for(work_pack)
        assert "collaborators" in fields, "the field this milestone exists for"
        assert "person" in fields
        assert "venue" not in fields and "org" not in fields


class TestTheTypeIsEnforced:
    def _parse(self, pack, value):
        raw = json.dumps({"assertions": [
            {"predicate": "work.project", "value": value, "quote": "worked with Adaline and Bowen"}]})
        return T.parse_output(raw, pack, record_text="worked with Adaline and Bowen on Helios")

    def test_a_comma_string_becomes_a_list(self, work_pack):
        """The model returns "A, B" when it ignores the menu, and the one live value
        naming two people was stored as a single name that resolved to nobody."""
        got, _ = self._parse(work_pack, {"project": "Helios", "collaborators": "Adaline, Bowen"})
        assert got, "the assertion must survive"
        assert got[0]["value"]["collaborators"] == ["Adaline", "Bowen"]

    def test_a_json_array_is_kept(self, work_pack):
        got, _ = self._parse(work_pack, {"project": "Helios", "collaborators": ["Adaline", "Bowen"]})
        assert got[0]["value"]["collaborators"] == ["Adaline", "Bowen"]

    def test_a_pronoun_in_the_list_is_rejected(self, work_pack):
        """The same blocklist a singular person field gets — it simply never ran here."""
        got, rejects = self._parse(work_pack, {"project": "Helios", "collaborators": "him, her"})
        assert not got and rejects >= 1

    def test_a_name_not_in_the_record_is_rejected(self, work_pack):
        got, rejects = self._parse(
            work_pack, {"project": "Helios", "collaborators": "Adaline, Zorbo Nobody"})
        assert not got and rejects >= 1

    def test_the_NEW_marker_is_stripped_and_flagged(self, work_pack):
        got, _ = self._parse(work_pack, {"project": "Helios", "collaborators": ["NEW:Adaline", "Bowen"]})
        assert got[0]["value"]["collaborators"] == ["Adaline", "Bowen"]
        assert got[0].get("new_person") is True


class TestTheLoaderRefusesAnUnhonouredType:
    def test_every_bundled_pack_loads(self, packs_dir):
        for f in sorted(packs_dir.glob("*.yaml")):
            if f.name.startswith("_"):
                continue
            P.load_pack(f)

    def test_an_unknown_type_fails_the_pack(self, tmp_path, packs_dir):
        """A pack asking for something the pipeline cannot do should fail where a human is
        reading the message, not at extraction time where the only symptom is a worse fact.
        Two unclassified types were found the moment this went in."""
        src = (packs_dir / "work.career.yaml").read_text()
        broken = src.replace("collaborators: person_refs", "collaborators: telepathy_ref", 1)
        assert broken != src
        path = tmp_path / "work.career.yaml"
        path.write_text(broken)

        with pytest.raises(Exception) as err:
            P.load_pack(path, trusted=True)
        assert "telepathy_ref" in str(err.value)

    def test_prose_and_reference_types_are_separately_declared(self):
        """"We decided this is prose" and "nobody has looked at this" must not read the
        same in the source."""
        assert "entity_ref" in P.ENTITY_REF_VALUE_TYPES
        assert "person_ref" in P.PERSON_REF_VALUE_TYPES
        assert "date_or_approx" in P.PROSE_VALUE_TYPES
        assert not (P.PROSE_VALUE_TYPES & P.ENTITY_REF_VALUE_TYPES)
        # A person ref and a generic entity ref are handled differently — a venue must
        # never be offered the known-PEOPLE list — so the sets must not overlap.
        assert not (P.PERSON_REF_VALUE_TYPES & P.ENTITY_REF_VALUE_TYPES)
