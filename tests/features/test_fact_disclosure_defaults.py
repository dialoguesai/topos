"""Both fact lanes default to owner_only when nobody says otherwise.

There are two fact write paths. `DerivationWriter` pins `owner_only` by
construction; `FactStore.assert_fact` defaulted to `scoped`, so "facts are
owner_only" was enforced on one lane and merely conventional on the other.
Measured on the live node 2026-08-26: 15 fact rows carry `scoped`, 1 of them
still active.

Every in-tree caller passes `disclosure` explicitly, so the default is only
reached by a caller that forgot — which is exactly when the safe answer matters.
The read side had the same split: two readers in `reads.py` disagreed about what
an unclassified payload means, so the same row was shareable on one path and
private on the other.

These are fail-closed guards, not behaviour changes.
"""

from __future__ import annotations

import inspect
import json
import sqlite3

import pytest

from topos.features.facts.store import FactStore
from topos.storage.db.migrations import apply_all_migrations


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "f.db"))
    apply_all_migrations(c)
    c.execute(
        "INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name) "
        "VALUES ('ent_owner', 'person', 'Owner', 'owner')"
    )
    c.commit()
    yield c
    c.close()


def test_assert_fact_signature_defaults_to_owner_only():
    """Pinned at the signature, so a future edit trips this rather than shipping."""
    sig = inspect.signature(FactStore.assert_fact)
    assert sig.parameters["disclosure"].default == "owner_only"


def test_a_fact_written_without_a_disclosure_is_owner_only(conn):
    FactStore(conn).assert_fact(
        subject_entity_id="ent_owner",
        predicate="works_on",
        object_value="topos",
        source_refs=[{"table": "t", "record_id": "r1"}],
    )
    payload = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact'"
    ).fetchone()[0]
    assert json.loads(payload)["disclosure"] == "owner_only"


def test_an_explicit_disclosure_is_still_honoured(conn):
    """The default is a floor for the careless caller, not a ceiling for the careful one."""
    FactStore(conn).assert_fact(
        subject_entity_id="ent_owner",
        predicate="works_on",
        object_value="topos",
        source_refs=[{"table": "t", "record_id": "r1"}],
        disclosure="scoped",
    )
    payload = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact'"
    ).fetchone()[0]
    assert json.loads(payload)["disclosure"] == "scoped"


def test_both_readers_agree_on_an_unclassified_payload():
    """The two reads.py defaults must not disagree about what 'absent' means."""
    import pathlib
    import re

    src = pathlib.Path(inspect.getfile(FactStore)).parent / "reads.py"
    defaults = re.findall(r'payload\.get\("disclosure",\s*"([a-z_]+)"\)', src.read_text())
    assert defaults, "no disclosure read-defaults found — did the reader move?"
    assert set(defaults) == {"owner_only"}, defaults
