import sqlite3
from copy import deepcopy

import pytest

from topos.routines.permissions import validate_query_permissions
from topos.routines import store


def payload():
    return {"name": "Weekly work", "prompt": "Summarize my work updates", "trigger_type": "manual", "targets": [
        {"topos_id": "topos-1", "access_mode": "raw_access", "query_path": "grant", "filters": {},
         "grant_permissions": {"version": "uma-policy-draft/v1", "profile": "knowledge_search", "client_id": "topos-frontend",
            "source_ids": ["chat"], "domains": ["work"], "excluded_domains": ["health"], "sensitivities": ["none"],
            "lookback_days": 30, "ceiling": "raw", "release_event_time": "day", "result_types": ["message"],
            "questions_per_day": 10, "answers": "only", "writer_disclosure": "model", "expires_at": 2000000000}}]}


def test_local_storage_round_trip_and_path_change():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    row = store.create_routine(conn, owner_user_id="owner", engine_id="engine", payload=payload())
    assert row["targets"] == payload()["targets"]
    stored = store.get_routine(conn, owner_user_id="owner", routine_id=row["id"])
    assert stored["targets"][0]["grant_permissions"]["questions_per_day"] == 10
    target = {**row["targets"][0], "query_path": "owner", "filters": {"hide_pii": True}}
    changed = store.patch_routine(conn, owner_user_id="owner", routine_id=row["id"], payload={"targets": [target]})
    assert changed["targets"][0]["access_mode"] == "raw_access"
    assert changed["targets"][0]["filters"] == {}
    assert "grant_permissions" not in changed["targets"][0]
    assert store.get_routine(conn, owner_user_id="someone-else", routine_id=row["id"]) is None


def test_legacy_restrictions_are_preserved():
    target = {"topos_id": "a", "access_mode": "summary_only", "filters": {"hide_pii": True}}
    assert validate_query_permissions({"targets": [target]}) == {"targets": [target]}


@pytest.mark.parametrize("mutate", [
    lambda p: p["targets"][0].update(query_path="other"),
    lambda p: p["targets"][0].pop("grant_permissions"),
    lambda p: p["targets"][0]["grant_permissions"].update(source_ids=[]),
    lambda p: p["targets"][0]["grant_permissions"].update(extra_rule=True),
    lambda p: p.update(save_to_topos=True),
    lambda p: p.update(targets=None),
    lambda p: p.update(targets=[None]),
    lambda p: p["targets"][0]["grant_permissions"].update(answers=[]),
    lambda p: p.update(prompt="x" * 4001),
])
def test_invalid_permissions_are_rejected(mutate):
    body = deepcopy(payload())
    mutate(body)
    with pytest.raises(ValueError, match="INVALID_QUERY_PERMISSIONS"):
        validate_query_permissions(body)


def test_selective_fact_consent_round_trip_and_invalid_vocabulary():
    from topos.permissions_v2.selective_facts import selection
    body = payload()
    draft = body['targets'][0]['grant_permissions']
    draft.update(result_types=['fact'], selective_facts=selection(['work.career'], ['private-fact']))
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    row = store.create_routine(conn, owner_user_id='owner', engine_id='engine', payload=body)
    assert row['targets'][0]['grant_permissions']['selective_facts'] == draft['selective_facts']
    draft['selective_facts']['ontologies'][0]['ontology_id'] = 'health.physical'
    with pytest.raises(ValueError, match='INVALID_QUERY_PERMISSIONS'):
        validate_query_permissions(body)
