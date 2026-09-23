"""Atomic first-owner creation without merging existing self identities."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
import threading

import pytest

from topos.features.facts.extract import _owner_entity_id
from topos.storage.db.migrations.wiki_entities_v1 import apply_wiki_entities_v1_up


@pytest.fixture
def database(tmp_path):
    path=tmp_path/'owner-initialization.db'
    with closing(sqlite3.connect(path)) as conn:
        conn.execute('CREATE TABLE wiki_schema_migrations(migration_id TEXT PRIMARY KEY)')
        apply_wiki_entities_v1_up(conn)
        conn.execute('CREATE TABLE signal_objects(object_id TEXT PRIMARY KEY,object_type TEXT,object_key TEXT)')
        conn.commit()
    return path


def test_two_connections_observing_no_owner_create_only_one(database):
    # Force both real connections past their initial empty SELECT before either
    # can enter the creation gate. No sleeps or probabilistic stress loop.
    barrier=threading.Barrier(2,timeout=5)
    class FirstReadTogether(sqlite3.Connection):
        owner_reads=0
        def execute(self,sql,*args,**kwargs):
            cursor=super().execute(sql,*args,**kwargs)
            if sql.startswith('SELECT entity_id FROM entities WHERE is_self=1'):
                self.owner_reads+=1
                if self.owner_reads==1:barrier.wait()
            return cursor
    def initialize():
        with closing(sqlite3.connect(database,factory=FirstReadTogether)) as conn:
            return _owner_entity_id(conn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(initialize);second=pool.submit(initialize)
        identities=[first.result(timeout=10),second.result(timeout=10)]
    with closing(sqlite3.connect(database)) as conn:
        rows=conn.execute('SELECT entity_id FROM entities WHERE is_self=1').fetchall()
    assert len(rows)==1
    assert identities==[rows[0][0],rows[0][0]]


def test_existing_fact_bearing_selector_and_all_self_rows_remain_unchanged(database):
    with closing(sqlite3.connect(database)) as conn:
        conn.executemany('INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name,is_self) VALUES(?,?,?,?,1)',
            [('self-empty','person','Synthetic empty','synthetic empty'),
             ('self-facts','person','Synthetic owner','synthetic owner')])
        conn.execute("INSERT INTO signal_objects VALUES('fact-1','fact','fact:self-facts:prefers')")
        conn.commit()
        before=conn.execute('SELECT * FROM entities ORDER BY entity_id').fetchall()
        assert _owner_entity_id(conn)=='self-facts'
        assert conn.execute('SELECT * FROM entities ORDER BY entity_id').fetchall()==before
        assert conn.execute('SELECT count(*) FROM signal_objects').fetchone()[0]==1


def test_initial_creation_is_reused_without_another_entity(database):
    with closing(sqlite3.connect(database)) as first:
        identity=_owner_entity_id(first)
    with closing(sqlite3.connect(database)) as second:
        assert _owner_entity_id(second)==identity
        assert second.execute('SELECT count(*) FROM entities WHERE is_self=1').fetchone()[0]==1
