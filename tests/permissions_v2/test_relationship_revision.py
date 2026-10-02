"""A relationship's revision pins what its release and eligibility read, and nothing a graph rebuild rewrites (1.4.4).

On 1.4.3 a `pursues` relationship's revision digested its whole edge row and its whole goal-node row. The node's
graph refresh, about every 2.5 minutes, rewrites `updated_at` on every `pursues` edge and goal node (and the
counters, the edge's display statement and the node's variant list when a goal recurs) with nothing released
changing, so every grant index holding a relationship went stale (`stale (projection)`) and refused searches.

Every column of the two graph tables is classified (knowledge_projections.EDGE_COLUMNS, ENTITY_COLUMNS). Each pinned
column has a case below that changes it and sees the member go stale; each volatile one has a case that changes it
and sees the member stay current and still release. The Off-limits scan reads the volatile columns too, so it is
run again on the current rows at every currency check: a protected name written into one of them stales the member.
"""
import json
import logging
import sqlite3

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.permissions_v2.canonical import PolicyError, digest
from topos.permissions_v2.entity_boundary import rows_revision
from topos.permissions_v2.knowledge_projections import EDGE_COLUMNS, ENTITY_COLUMNS, current_revision

GOAL = 'finish the compiler at work by Friday'
QUERY = 'compiler Friday'
LABELS = {'domains': ['work', 'plans']}
PROTECTED = 'Quillon Vexmoor'
OLD = '2026-09-30 12:00:00'


def _connect(legacy):
    conn = sqlite3.connect(legacy[0].resolver.path)
    conn.row_factory = sqlite3.Row
    return conn


def materialize(legacy):
    """The node's own graph rebuild of the goals (graph_enrichers._materialize_goals), as the refresh runs it."""
    from topos.features.entities.graph_enrichers import _materialize_goals
    conn = _connect(legacy)
    try:
        _materialize_goals(conn, 'owner-entity')
        conn.commit()
    finally:
        conn.close()


def goal_graph(legacy, *, protected=False):
    """A goal stored for the owner's message, and its goal node and `pursues` edge as the graph rebuild makes them.

    Their rebuild timestamps are set back to an earlier day, so the next rebuild moves them, as on a live node."""
    from topos.storage.db.migrations.entity_edges_validity_v1 import apply_entity_edges_validity_v1_up
    conn = _connect(legacy)
    apply_entity_edges_validity_v1_up(conn)
    conn.execute("CREATE TABLE user_goals(goal_id TEXT PRIMARY KEY, record_id TEXT, source_id TEXT, goal_text TEXT, "
                 "model TEXT, provider TEXT, payload_json TEXT NOT NULL, created_at TEXT NOT NULL "
                 "DEFAULT (datetime('now')), spec_version INTEGER)")
    conn.execute("INSERT INTO user_goals(goal_id,record_id,source_id,goal_text,payload_json,created_at) "
                 "VALUES('goal-1','imessage:1','imessage',?,'{}',?)", (GOAL, OLD))
    if protected:
        # Off-limits on: someone the owner protected, and the message's conversation, which its context then reads.
        from topos.storage.canonical.conversations_tables import (ensure_contact_identifiers_table,
            ensure_contacts_table, ensure_conversation_participants_table, ensure_conversations_table)
        for create in (ensure_contacts_table, ensure_contact_identifiers_table, ensure_conversations_table,
                       ensure_conversation_participants_table):
            create(conn)
        conversation, source, dataset = conn.execute("SELECT conversation_id, source_id, dataset_id FROM "
                                                     "conversation_messages WHERE message_id='imessage:1'").fetchone()
        conn.execute('INSERT INTO conversations(conversation_id,dataset_id,source_id) VALUES(?,?,?)',
                     (conversation, dataset, source))
        conn.execute("INSERT INTO entity_blackholes(blackhole_id,entity_id,canonical_name,normalized_name,rebuild_state) "
                     "VALUES('bh','',?,?,'complete')", (PROTECTED, PROTECTED.lower()))
    conn.commit()
    conn.close()
    materialize(legacy)
    conn = _connect(legacy)
    conn.execute("UPDATE entity_edges SET created_at=?, updated_at=? WHERE edge_type='pursues'", (OLD, OLD))
    conn.execute("UPDATE entities SET created_at=?, updated_at=? WHERE entity_type='goal'", (OLD, OLD))
    conn.commit()
    conn.close()


def ids(legacy):
    conn = _connect(legacy)
    try:
        edge, node = conn.execute("SELECT edge_id, dst_entity_id FROM entity_edges WHERE edge_type='pursues'").fetchone()
        return edge, node
    finally:
        conn.close()


def indexed(legacy, tmp_path, monkeypatch, *, protected=False):
    goal_graph(legacy, protected=protected)
    node, _identity = node_for(legacy, tmp_path, monkeypatch, labels=LABELS)
    with owner():
        assert node.index.rebuild('grant-search', now=node.now[0])['state'] == 'ready'
    assert kinds(node) == {'message', 'goal', 'relationship'}
    return node


def kinds(node):
    output, refused = node.search_request(QUERY, k=10)
    assert refused is None
    return {record['kind'] for record in output['records']}


def edit(legacy, sql, args=()):
    conn = _connect(legacy)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def sweep(node, caplog):
    """(files the daemon sweep removed, the stale stages it logged)."""
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger='topos.permissions_v2.search_index'):
        removed = node.index.sweep(now=node.now[0])
    return removed, [record.getMessage() for record in caplog.records if 'stale' in record.getMessage()]


def old_revision(legacy):
    """1.4.3's relationship revision: both graph rows whole. Proves a case below changed what 1.4.3 pinned."""
    conn = _connect(legacy)
    try:
        edge = dict(conn.execute("SELECT * FROM entity_edges WHERE edge_type='pursues'").fetchone())
        endpoint = dict(conn.execute('SELECT * FROM entities WHERE entity_id=?', (edge['dst_entity_id'],)).fetchone())
        goal = dict(conn.execute("SELECT * FROM user_goals WHERE goal_id='goal-1'").fetchone())
        return digest({'rows': rows_revision([[edge, endpoint]]), 'source': rows_revision([[goal]])})
    finally:
        conn.close()


def revision(legacy, edge=None, **kwargs):
    edge = edge or ids(legacy)[0]
    conn = _connect(legacy)
    try:
        return current_revision(conn, 'entity_edges', edge, **kwargs)
    finally:
        conn.close()


# -- the regression: a graph refresh that changes nothing ------------------------------------------------------------

@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_graph_refresh_that_changes_nothing_keeps_the_index_current(legacy, tmp_path, monkeypatch, caplog):
    node = indexed(legacy, tmp_path, monkeypatch)
    before_old, before = old_revision(legacy), revision(legacy)
    materialize(legacy)                                      # the next refresh: same goals, same graph
    # The graph no longer rewrites unchanged rows (fix/graph-refresh-no-churn), so stamp the volatile columns the way
    # any graph write still can: a real change elsewhere moves updated_at, centrality and community stamps.
    edit(legacy, "UPDATE entity_edges SET updated_at=datetime('now','+1 minute') WHERE edge_type='pursues'")
    edit(legacy, "UPDATE entities SET updated_at=datetime('now','+1 minute') "
                 "WHERE entity_id IN (SELECT dst_entity_id FROM entity_edges WHERE edge_type='pursues')")
    assert old_revision(legacy) != before_old                 # 1.4.3 saw a change here (updated_at) and went stale
    assert revision(legacy) == before
    assert sweep(node, caplog) == (0, [])
    assert kinds(node) == {'message', 'goal', 'relationship'}


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_recurring_goal_moves_counters_not_the_relationship(legacy, tmp_path, monkeypatch, caplog):
    """Re-extraction stores the same goal again under a fresh id: the refresh rewrites the edge's weight, count,
    statement and the node's occurrences. Nothing a recipient receives changes."""
    node = indexed(legacy, tmp_path, monkeypatch)
    before_old, before = old_revision(legacy), revision(legacy)
    edit(legacy, "INSERT INTO user_goals(goal_id,record_id,source_id,goal_text,payload_json,created_at) "
                 "VALUES('goal-again','imessage:1','imessage',?,'{}',?)", (GOAL, OLD))
    materialize(legacy)
    conn = _connect(legacy)
    edge = dict(conn.execute("SELECT * FROM entity_edges WHERE edge_type='pursues'").fetchone())
    conn.close()
    assert edge['evidence_count'] == 2 and '×2' in json.loads(edge['metadata_json'])['statement']
    assert old_revision(legacy) != before_old
    assert revision(legacy) == before
    assert sweep(node, caplog) == (0, [])
    assert 'relationship' in kinds(node)


# -- every pinned column stales; every volatile column does not -----------------------------------------------------

def _twin_node(conn, node):
    row = dict(conn.execute('SELECT * FROM entities WHERE entity_id=?', (node,)).fetchone())
    row['entity_id'] = node + '-twin'
    conn.execute(f"INSERT INTO entities({','.join(row)}) VALUES({','.join('?' * len(row))})", list(row.values()))
    return row['entity_id']


PINNED_EDGE = {
    'edge_id': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET edge_id='edge-renamed' WHERE edge_id=?", (edge,)),
    'src_entity_id': lambda conn, edge, node: (
        conn.execute("INSERT INTO entities(entity_id,entity_type,canonical_name,normalized_name) "
                     "VALUES('someone-else','person','Person','person')"),
        conn.execute("UPDATE entity_edges SET src_entity_id='someone-else' WHERE edge_id=?", (edge,))),
    # The same names on another node: the endpoint's identity alone is pinned.
    'dst_entity_id': lambda conn, edge, node: conn.execute(
        'UPDATE entity_edges SET dst_entity_id=? WHERE edge_id=?', (_twin_node(conn, node), edge)),
    'edge_type': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET edge_type='relates_to' WHERE edge_id=?", (edge,)),
    'valid_from': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET valid_from='2026-01-01T00:00:00+00:00' WHERE edge_id=?", (edge,)),
    'valid_to': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET valid_to='2026-10-01T00:00:00+00:00' WHERE edge_id=?", (edge,)),
    # Another stored goal with the same text and message: the link alone is pinned.
    'metadata_json': lambda conn, edge, node: (
        conn.execute("INSERT INTO user_goals(goal_id,record_id,source_id,goal_text,payload_json,created_at) "
                     "VALUES('goal-twin','imessage:1','imessage',?,'{}',?)", (GOAL, OLD)),
        conn.execute("UPDATE entity_edges SET metadata_json=json_set(metadata_json,'$.source_object_id','goal-twin') "
                     "WHERE edge_id=?", (edge,))),
}
PINNED_ENDPOINT = {
    'entity_id': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET entity_id='goal-node-renamed' WHERE entity_id=?", (node,)),
    'entity_type': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET entity_type='project' WHERE entity_id=?", (node,)),
    'canonical_name': lambda conn, edge, node: conn.execute(
        'UPDATE entities SET canonical_name=? WHERE entity_id=?', (GOAL.upper(), node)),
    'normalized_name': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET normalized_name=normalized_name||' again' WHERE entity_id=?", (node,)),
    'aliases_json': lambda conn, edge, node: conn.execute(
        """UPDATE entities SET aliases_json='["the compiler"]' WHERE entity_id=?""", (node,)),
    'identifiers_json': lambda conn, edge, node: conn.execute(
        """UPDATE entities SET identifiers_json='["compiler-handle"]' WHERE entity_id=?""", (node,)),
    'contact_id': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET contact_id='contact-1' WHERE entity_id=?", (node,)),
    'is_self': lambda conn, edge, node: conn.execute(
        'UPDATE entities SET is_self=1 WHERE entity_id=?', (node,)),
}
# The goal stays its whole row (what OD-38's stored verdicts are keyed by); two columns stand for all of them.
GOAL_ROW = {
    'goal_text': lambda conn, edge, node: conn.execute(
        "UPDATE user_goals SET goal_text=goal_text||'!' WHERE goal_id='goal-1'"),
    'model': lambda conn, edge, node: conn.execute("UPDATE user_goals SET model='another-model' WHERE goal_id='goal-1'"),
}
VOLATILE_EDGE = {
    'weight': lambda conn, edge, node: conn.execute('UPDATE entity_edges SET weight=weight+1.25 WHERE edge_id=?', (edge,)),
    'evidence_count': lambda conn, edge, node: conn.execute(
        'UPDATE entity_edges SET evidence_count=evidence_count+3 WHERE edge_id=?', (edge,)),
    'last_event_at': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET last_event_at='2026-10-02 14:28:26' WHERE edge_id=?", (edge,)),
    'created_at': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET created_at=datetime('now') WHERE edge_id=?", (edge,)),
    'updated_at': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET updated_at=datetime('now') WHERE edge_id=?", (edge,)),
    # Every key but the goal link: the display statement, the role, a key a later rebuild adds.
    'metadata_json': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET metadata_json=json_set(metadata_json,'$.statement','pursues: it (×7)',"
        "'$.actor_role','observed','$.refreshed',1) WHERE edge_id=?", (edge,)),
}
VOLATILE_ENDPOINT = {
    'embedding_blob': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET embedding_blob=x'00112233' WHERE entity_id=?", (node,)),
    'first_seen': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET first_seen='2025-01-01T00:00:00+00:00' WHERE entity_id=?", (node,)),
    'last_seen': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET last_seen='2026-10-02T14:28:26+00:00' WHERE entity_id=?", (node,)),
    'mention_count': lambda conn, edge, node: conn.execute(
        'UPDATE entities SET mention_count=mention_count+5 WHERE entity_id=?', (node,)),
    'metadata_json': lambda conn, edge, node: conn.execute(
        """UPDATE entities SET metadata_json='{"mz": 1, "goal_variants": ["finish it"], "occurrences": 4}' """
        'WHERE entity_id=?', (node,)),
    'created_at': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET created_at=datetime('now') WHERE entity_id=?", (node,)),
    'updated_at': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET updated_at=datetime('now') WHERE entity_id=?", (node,)),
}


def test_every_column_of_the_graph_tables_is_classified():
    """A migration that adds a column to `entities` or `entity_edges` fails here until it is classified as pinned
    (it changes what is released or whether it may be) or volatile (a rebuild rewrites it and only the Off-limits
    scan reads it). An unclassified column is pinned meanwhile (knowledge_projections._pinned)."""
    from topos.storage.db.migrations import apply_all_migrations
    conn = sqlite3.connect(':memory:')
    try:
        apply_all_migrations(conn)
        for table, classified in (('entity_edges', EDGE_COLUMNS), ('entities', ENTITY_COLUMNS)):
            columns = {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}
            assert columns == set(classified), (table, sorted(columns ^ set(classified)))
    finally:
        conn.close()
    assert set(EDGE_COLUMNS.values()) == {'pinned', 'goal_link', 'volatile'}
    assert set(ENTITY_COLUMNS.values()) == {'pinned', 'volatile'}


def test_every_classified_column_has_its_case_here():
    assert set(PINNED_EDGE) == {name for name, kind in EDGE_COLUMNS.items() if kind != 'volatile'}
    # The goal link's column has a case on each side: its link stales, its other keys do not.
    assert set(VOLATILE_EDGE) == {name for name, kind in EDGE_COLUMNS.items() if kind in ('volatile', 'goal_link')}
    assert set(PINNED_ENDPOINT) == {name for name, kind in ENTITY_COLUMNS.items() if kind != 'volatile'}
    assert set(VOLATILE_ENDPOINT) == {name for name, kind in ENTITY_COLUMNS.items() if kind == 'volatile'}


def _apply(legacy, change):
    edge, node = ids(legacy)
    conn = _connect(legacy)
    try:
        change(conn, edge, node)
        conn.commit()
    finally:
        conn.close()


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
@pytest.mark.parametrize('family,name', [pytest.param(family, name, id=f'{family}-{name}')
    for family, cases in (('edge', PINNED_EDGE), ('endpoint', PINNED_ENDPOINT), ('goal', GOAL_ROW)) for name in cases])
def test_a_pinned_column_change_stales_the_relationship(legacy, tmp_path, monkeypatch, caplog, family, name):
    node = indexed(legacy, tmp_path, monkeypatch)
    edge, _node = ids(legacy)
    before = revision(legacy, edge)
    _apply(legacy, {'edge': PINNED_EDGE, 'endpoint': PINNED_ENDPOINT, 'goal': GOAL_ROW}[family][name])
    try:
        after = revision(legacy, edge)
    except PolicyError:
        after = None                                           # the row the member names is gone
    assert after != before
    removed, stages = sweep(node, caplog)
    assert removed == 1
    # A projection is what moved. Marking a node as the owner's self also moves the protection clock (identity
    # coverage), which the basis sees first.
    assert stages == ['message search index stale (' + ('basis' if name == 'is_self' else 'projection') + ')']


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
@pytest.mark.parametrize('family,name', [pytest.param(family, name, id=f'{family}-{name}')
    for family, cases in (('edge', VOLATILE_EDGE), ('endpoint', VOLATILE_ENDPOINT)) for name in cases])
def test_a_volatile_column_change_keeps_the_relationship_current(legacy, tmp_path, monkeypatch, caplog, family, name):
    node = indexed(legacy, tmp_path, monkeypatch)
    before_old, before = old_revision(legacy), revision(legacy)
    _apply(legacy, {'edge': VOLATILE_EDGE, 'endpoint': VOLATILE_ENDPOINT}[family][name])
    assert old_revision(legacy) != before_old                 # the case changed a value 1.4.3 pinned
    assert revision(legacy) == before
    assert sweep(node, caplog) == (0, [])
    assert kinds(node) == {'message', 'goal', 'relationship'}  # the release's own revision check agrees


# -- Off-limits still reads the volatile columns --------------------------------------------------------------------

VETOED = {
    'edge statement': lambda conn, edge, node: conn.execute(
        "UPDATE entity_edges SET metadata_json=json_set(metadata_json,'$.statement',?) WHERE edge_id=?",
        (f'pursues: lunch with {PROTECTED}', edge)),
    'edge timestamp': lambda conn, edge, node: conn.execute(
        'UPDATE entity_edges SET last_event_at=? WHERE edge_id=?', (PROTECTED, edge)),
    'node variants': lambda conn, edge, node: conn.execute(
        'UPDATE entities SET metadata_json=? WHERE entity_id=?',
        (json.dumps({'mz': 1, 'goal_variants': [f'finish the compiler with {PROTECTED}'], 'occurrences': 2}), node)),
    'node timestamp': lambda conn, edge, node: conn.execute(
        'UPDATE entities SET last_seen=? WHERE entity_id=?', (PROTECTED, node)),
    # With Off-limits on, an endpoint carrying binary content cannot be scanned, so it cannot release.
    'node embedding': lambda conn, edge, node: conn.execute(
        "UPDATE entities SET embedding_blob=x'00112233' WHERE entity_id=?", (node,)),
}


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
@pytest.mark.parametrize('name', sorted(VETOED))
def test_off_limits_is_decided_again_on_the_volatile_columns(legacy, tmp_path, monkeypatch, caplog, name):
    node = indexed(legacy, tmp_path, monkeypatch, protected=True)
    before = revision(legacy)
    _apply(legacy, VETOED[name])
    assert revision(legacy) == before                          # not pinned...
    from topos.permissions_v2.entity_boundary import EntityBoundary
    conn = _connect(legacy)
    try:
        with pytest.raises(PolicyError):                       # ...but scanned on the current rows
            current_revision(conn, 'entity_edges', ids(legacy)[0], boundary=EntityBoundary(conn))
    finally:
        conn.close()
    assert sweep(node, caplog) == (1, ['message search index stale (projection)'])


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_volatile_change_that_names_no_one_stays_current_under_off_limits(legacy, tmp_path, monkeypatch, caplog):
    """The control for the case above: Off-limits on, the same columns rewritten with nothing protected in them."""
    node = indexed(legacy, tmp_path, monkeypatch, protected=True)
    for change in (VOLATILE_EDGE['metadata_json'], VOLATILE_EDGE['last_event_at'], VOLATILE_ENDPOINT['metadata_json'],
                   VOLATILE_ENDPOINT['last_seen'], VOLATILE_EDGE['updated_at'], VOLATILE_ENDPOINT['updated_at']):
        _apply(legacy, change)
    materialize(legacy)
    assert sweep(node, caplog) == (0, [])
    assert kinds(node) == {'message', 'goal', 'relationship'}


# -- the revision's own rules ------------------------------------------------------------------------------------------

EDGE = {'edge_id': 'e', 'src_entity_id': 'o', 'dst_entity_id': 'g', 'edge_type': 'pursues', 'weight': 1.0,
        'evidence_count': 1, 'last_event_at': None, 'valid_from': None, 'valid_to': None,
        'metadata_json': '{"source_object_id": "goal-1", "statement": "pursues: it"}', 'created_at': OLD,
        'updated_at': OLD}
ENDPOINT = {'entity_id': 'g', 'entity_type': 'goal', 'canonical_name': GOAL, 'normalized_name': GOAL,
            'aliases_json': '[]', 'identifiers_json': '[]', 'embedding_blob': None, 'is_self': 0, 'contact_id': None,
            'first_seen': OLD, 'last_seen': OLD, 'mention_count': 0, 'metadata_json': '{"mz": 1}',
            'created_at': OLD, 'updated_at': OLD}


@pytest.mark.parametrize('row', ['edge', 'endpoint'])
def test_an_unclassified_column_is_pinned_until_it_is_classified(row):
    from topos.permissions_v2.knowledge_projections import relationship_revision
    def with_column(value):
        edge, endpoint = dict(EDGE), dict(ENDPOINT)
        (edge if row == 'edge' else endpoint)['added_by_a_migration'] = value
        return relationship_revision(edge, endpoint, 'goal-revision')
    assert with_column('a') != with_column('b')
    # NULL is absent: a migration that adds a column stales nothing until a value appears in it.
    assert with_column(None) == relationship_revision(EDGE, ENDPOINT, 'goal-revision')


def test_the_goal_link_must_parse_as_the_release_parses_it():
    from topos.permissions_v2.knowledge_projections import relationship_revision
    with pytest.raises(PolicyError):
        relationship_revision({**EDGE, 'metadata_json': '{"source_object_id": "goal-1", "source_object_id": "x"}'},
                              ENDPOINT, 'goal-revision')


def test_the_goal_revision_is_part_of_the_relationship():
    from topos.permissions_v2.knowledge_projections import relationship_revision
    assert relationship_revision(EDGE, ENDPOINT, 'goal-a') != relationship_revision(EDGE, ENDPOINT, 'goal-b')
