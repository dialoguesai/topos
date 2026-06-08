from __future__ import annotations

import sqlite3

from topos.llm_integrations_storage import (
    CREDENTIALS_TABLE,
    DATA_EXPLORER_HIDDEN_TABLES,
    get_credential,
    get_preferences,
    list_credentials,
    upsert_credential,
    upsert_preferences,
)


def test_engine_llm_integrations_round_trip():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    upsert_credential(
        conn,
        topos_id="topos_local",
        provider="openai",
        ciphertext="cipher",
        key_hint="sk-a…mnop",
        default_model="gpt-4o-mini",
    )
    upsert_preferences(conn, topos_id="topos_local", active_provider="openai")
    creds = list_credentials(conn, topos_id="topos_local")
    assert len(creds) == 1
    assert creds[0]["provider"] == "openai"
    cred = get_credential(conn, topos_id="topos_local", provider="openai")
    assert cred is not None
    assert cred["default_model"] == "gpt-4o-mini"
    prefs = get_preferences(conn, topos_id="topos_local")
    assert prefs is not None
    assert prefs["active_provider"] == "openai"
    assert CREDENTIALS_TABLE in DATA_EXPLORER_HIDDEN_TABLES
