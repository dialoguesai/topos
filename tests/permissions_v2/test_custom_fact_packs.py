"""Pre-v3 grants stay closed to custom packs, including older facts without provenance."""
from __future__ import annotations

import pytest
import yaml

from topos.features.derivation import custom_packs
from topos.permissions_v2 import expanded_facts, selective_facts as sf
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.share_counts import OUTSIDE, REFUSALS

PACK = "custom.gardening"


@pytest.fixture
def added(tmp_path, monkeypatch):
    home = tmp_path / "fact-packs"
    monkeypatch.setattr(custom_packs, "custom_pack_dir", lambda: home)
    before = sf.catalog()
    source = tmp_path / "my-pack.yaml"
    source.write_text(yaml.safe_dump({
        "pack": PACK, "version": "1.0.0", "title": "My gardening", "sensitivity_class": "personal",
        "role_policy": "authored_only",
        "predicates": [{"name": "custom.plant", "value_type": "structured", "cardinality": "multi",
                        "temporal": "interval", "altitude": "stated", "value_schema": {"name": "string"}}],
        "eval": {"gold": [{"text": "I grow tomatoes"}], "negative_controls": [{"text": "A film about a garden"}]},
        "consumers": ["assistant_context"]}))
    custom_packs.install_pack(source)
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir

    assert PACK in load_packs(bundled_pack_dir())
    return before


def stored_fact():
    """A fact as the derivation writer stores one for the added pack."""
    payload = {"predicate": "custom.plant", "pack": PACK, "altitude": "stated", "disclosure": "owner_only",
               "asserted_by": "owner", "actor_role": "authored", "sensitivity": "personal",
               "extractor": {"pack_version": "1.0.0"}, "value_struct": {"name": "tomatoes"}}
    row = {"object_id": "fact-1", "object_type": "fact", "ontology_id": PACK, "ontology_version": "1.0.0",
           "altitude": "stated", "extractor_version": "derivation:test", "valid_to": None}
    return row, payload


def test_adding_a_pack_extends_only_the_new_catalog_not_old_consent(added):
    assert sf.catalog()["version"] == "selective-facts/v3"
    assert PACK in {item["ontology_id"] for item in sf.catalog()["ontologies"]}
    assert expanded_facts.catalog() == added
    old = sf.selection_v2(["work.career"], assertions=["inferred"])
    assert sf.parse_selection(old).model_dump() == old


def test_v2_selection_cannot_name_an_added_pack(added):
    with pytest.raises(KeyError):
        sf.selection_v2([PACK], assertions=["inferred"])
    forged = sf.selection_v2(["work.career"], assertions=["inferred"])
    forged["ontologies"].append({"ontology_id": PACK, "revision": "0" * 64, "predicates": ["custom.plant"]})
    with pytest.raises(Exception) as refusal:
        sf.parse_selection(forged)
    assert not isinstance(refusal.value, AssertionError)


def test_both_consent_versions_refuse_a_fact_of_an_added_pack(added):
    row, payload = stored_fact()
    for ontology_of in (expanded_facts.ontology_of, sf.catalog_ontology_of, sf.ontology_of):
        with pytest.raises(PolicyError) as refusal:
            ontology_of(row, payload)
        assert refusal.value.code in ("fact_ontology_unsupported", "fact_ontology_unknown")
        assert refusal.value.code in REFUSALS          # a refusal a share already knows how to count
    assert REFUSALS["fact_ontology_unsupported"] == OUTSIDE
