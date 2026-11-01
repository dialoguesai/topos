"""Routine quota policy against synthetic in-memory data only."""
import json
import sqlite3

import pytest

from topos.config.settings import Settings, settings
from topos.routines import store
from topos.routines.schema import ensure_routines_schema

pytestmark = pytest.mark.public

@pytest.fixture
def conn(monkeypatch):
    monkeypatch.setattr(settings, 'routine_max_runs_per_owner_24h', 100)
    monkeypatch.setattr(settings, 'routine_development_unlimited_runs', False)
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    ensure_routines_schema(db)
    db.execute("INSERT INTO routines (id,owner_user_id,engine_id,enabled,trigger_type,payload_json,updated_at) VALUES ('r','owner','engine',1,'manual','{}',?)", (store._now_iso(),))
    db.commit()
    yield db
    db.close()


def seed(conn, count, *, owner='owner', engine='engine', status='completed'):
    for i in range(count):
        ident=f'{owner}-{engine}-{status}-{i}'
        conn.execute('INSERT INTO routine_runs (id,routine_id,owner_user_id,engine_id,status,payload_json,updated_at) VALUES (?,?,?,?,?,?,?)',
                     (ident,'r',owner,engine,status,json.dumps({'id':ident}),store._now_iso()))
    conn.commit()


def create(conn, **payload):
    return store.create_run(conn,owner_user_id='owner',engine_id='engine',routine_id='r',run_payload=payload)


def test_default_allows_hundredth_and_rejects_hundred_and_first(conn):
    seed(conn, 99)
    run=create(conn)
    store.update_run(conn, run['id'], {'status':'completed'})
    with pytest.raises(ValueError,match='RUN_RATE_LIMIT'):
        create(conn)


def test_development_bypasses_daily_quota_but_keeps_active_run_guard(conn,monkeypatch):
    seed(conn, 125)
    monkeypatch.setattr(settings,'routine_development_unlimited_runs',True)
    run=create(conn)
    assert store.run_quota_policy()['limit'] is None
    with pytest.raises(ValueError,match='ACTIVE_RUN'):
        create(conn)
    store.update_run(conn,run['id'],{'status':'completed'})
    create(conn)


def test_custom_limit_scopes_to_owner_engine_and_counts_failures(conn,monkeypatch):
    monkeypatch.setattr(settings,'routine_max_runs_per_owner_24h',2)
    seed(conn,100,owner='other')
    seed(conn,100,engine='elsewhere')
    seed(conn,2,status='failed')
    with pytest.raises(ValueError,match='RUN_RATE_LIMIT'):
        create(conn)


def test_request_cannot_enable_development_bypass(conn):
    seed(conn,100)
    with pytest.raises(ValueError,match='RUN_RATE_LIMIT'):
        create(conn,development_unlimited=True,limit=None)


def test_old_rows_age_out_and_history_is_preserved(conn):
    seed(conn,100)
    conn.execute("UPDATE routine_runs SET updated_at='2020-01-01T00:00:00+00:00'")
    conn.commit()
    create(conn)
    assert conn.execute('SELECT COUNT(*) FROM routine_runs').fetchone()[0]==101


def test_operator_environment_and_policy(monkeypatch):
    monkeypatch.setenv('TOPOS_ROUTINE_MAX_RUNS_PER_OWNER_24H','250')
    monkeypatch.setenv('TOPOS_ROUTINE_DEVELOPMENT_UNLIMITED_RUNS','true')
    cfg=Settings(_env_file=None)
    assert cfg.routine_max_runs_per_owner_24h==250
    assert cfg.routine_development_unlimited_runs is True
    monkeypatch.setattr(settings,'routine_max_runs_per_owner_24h',250)
    monkeypatch.setattr(settings,'routine_development_unlimited_runs',True)
    assert store.run_quota_policy()=={'contract_version':1,'limit':None,'configured_limit':250,'development_unlimited':True,'window_hours':24,'scope':'owner_engine','timestamp_basis':'updated_at'}


def test_normal_configuration_defaults_to_hundred(monkeypatch):
    monkeypatch.delenv('TOPOS_ROUTINE_MAX_RUNS_PER_OWNER_24H',raising=False)
    monkeypatch.delenv('TOPOS_ROUTINE_DEVELOPMENT_UNLIMITED_RUNS',raising=False)
    cfg=Settings(_env_file=None)
    assert cfg.routine_max_runs_per_owner_24h==100
    assert cfg.routine_development_unlimited_runs is False


def test_development_mode_keeps_idempotency(conn,monkeypatch):
    monkeypatch.setattr(settings,'routine_development_unlimited_runs',True)
    run=create(conn,idempotency_key='same-request')
    store.update_run(conn,run['id'],{'status':'completed'})
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):
        create(conn,idempotency_key='same-request')


@pytest.mark.parametrize('value',['-1','0','invalid'])
def test_invalid_limits_rejected_at_configuration(monkeypatch,value):
    monkeypatch.setenv('TOPOS_ROUTINE_MAX_RUNS_PER_OWNER_24H',value)
    with pytest.raises(ValueError):
        Settings(_env_file=None)
