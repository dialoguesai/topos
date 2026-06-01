from topos.core.table_layers import layer_for_category, layer_kind_labels


def test_layer_for_category_mapping():
    assert layer_for_category("system") == ("system", "Topos system")
    assert layer_for_category("enrichment_system") == ("system", "Topos system")
    assert layer_for_category("raw_retention") == ("raw", "Raw")
    assert layer_for_category("canonical") == ("canonical", "Canonical")
    assert layer_for_category("canonical_enrichment") == ("enrichment", "Enrichment")
    assert layer_for_category("unknown_bucket") == ("raw", "Raw")


def test_layer_kind_labels_has_four_kinds():
    labels = layer_kind_labels()
    assert set(labels.keys()) == {"system", "raw", "enrichment", "canonical"}


def test_raw_enrichment_category_maps_to_enrichment_layer():
    assert layer_for_category("raw_enrichment") == ("enrichment", "Enrichment")
