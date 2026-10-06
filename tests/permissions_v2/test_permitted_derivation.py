"""OD-46: typed items derived from the messages a p2c-v3 grant already permits.

Every case runs the lane against the p2c-v3 search harness, then asks the recipient door. The
extractor is injected, so no model runs: what is tested is which rows the lane reads, what it
stores, and what release does with it.
"""
import json
import sqlite3

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 -- fixture
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 -- fixture
from tests.permissions_v2.test_entity_boundary import protected_corpus  # noqa: F401 -- fixture
from tests.permissions_v2.test_evidence import corpus  # noqa: F401 -- fixture
from topos.permissions_v2 import permitted_derivation as pd
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.predicate_classes import CLASSES, EXCLUDED_FAMILIES, WIDENED, excluded_reason


class Spy:
    """An extractor that records every row it is shown and answers from a fixed list."""

    def __init__(self, *specs):
        self.specs, self.seen = specs, []

    def __call__(self, row, table):
        self.seen.append((table, row['message_id']))
        return list(self.specs)


def run_lane(node, extractor, **kwargs):
    with owner():
        return pd.PermittedDerivationPass(node.index, extractor=extractor, **kwargs).run(now=node.now[0])


def lane_rows(legacy):
    conn = legacy[1]
    facts = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='fact' AND valid_to IS NULL")]
    goals = [r[0] for r in conn.execute("SELECT goal_id FROM user_goals")] if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE name='user_goals'").fetchone() else []
    return facts, goals


def kinds(node, query='Synthetic message'):
    output, refused = node.search_request(query, k=10)
    assert refused is None
    return [r for r in output['records']]


PROJECT = pd.Spec('fact', 'work.project', 'Synthetic message', 0.7, {'kind': 'test'})


def test_a_fact_derived_from_a_permitted_message_is_stored_with_lineage_and_releases(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    spy = Spy(PROJECT)
    counts = run_lane(node, spy)
    assert counts['permitted_messages'] == 1 and counts['fact:written'] == 1
    assert spy.seen == [('conversation_messages', identity.record_id)]
    (fact,), _ = lane_rows(legacy)
    assert fact['subject_entity_id'] == 'owner-entity'          # the attested self, OD-29
    assert fact['asserted_by'] == 'owner' and fact['disclosure'] == 'owner_only'
    lineage = fact['lineage']
    assert lineage['lane'] == pd.LANE and lineage['message'] == identity.model_dump()
    content = legacy[1].execute('SELECT content FROM conversation_messages').fetchone()[0]
    assert lineage['message_revision'] == pd.message_revision(identity, content)
    records = kinds(node)
    facts = [r for r in records if r['kind'] == 'fact']
    assert [r['content'] for r in facts] == ['Owner works on the project Synthetic message.']
    assert facts[0]['citations'][0]['content'] == content


def test_a_rerun_is_idempotent(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(PROJECT))
    counts = run_lane(node, Spy(PROJECT))
    assert counts.get('fact:unchanged') == 1 and 'fact:written' not in counts
    assert len(lane_rows(legacy)[0]) == 1


def _graph_marks(monkeypatch):
    """Each arming of the node's debounced graph rebuild, recorded instead of armed."""
    from topos.features.entities import graph_refresh
    armed = []
    monkeypatch.setattr(graph_refresh, 'schedule_graph_refresh', lambda: armed.append(1))
    return armed


def test_a_write_marks_the_graph_dirty_and_arms_its_rebuild_and_a_rerun_does_neither(legacy, tmp_path, monkeypatch):
    """The graph derives the lane's facts and goals only when it is rebuilt, and only a mark rebuilds it: a write marks
    it dirty in its own transaction (a restart before the debounce still rebuilds) and arms the debounce."""
    from topos.storage.db.migrations.pipeline_jobs_v1 import apply_pipeline_jobs_v1_up
    apply_pipeline_jobs_v1_up(legacy[1])
    legacy[1].commit()
    armed = _graph_marks(monkeypatch)
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    generation = lambda: tuple(legacy[1].execute(
        "SELECT dirty_generation, materialized_generation FROM graph_materialization_state").fetchone())
    counts = run_lane(node, Spy(PROJECT))
    assert counts['fact:written'] == 1 and counts['graph:marked_dirty'] == 1 and armed == [1]
    assert generation() == (1, 0)
    counts = run_lane(node, Spy(PROJECT))
    assert counts.get('fact:unchanged') == 1 and not any(k.startswith('graph:') for k in counts)
    assert armed == [1] and generation() == (1, 0)


def test_without_a_graph_state_row_a_write_still_arms_the_rebuild(legacy, tmp_path, monkeypatch):
    legacy[1].execute("DROP TABLE IF EXISTS graph_materialization_state")
    legacy[1].commit()
    armed = _graph_marks(monkeypatch)
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    counts = run_lane(node, Spy(PROJECT))
    assert counts['fact:written'] == 1 and counts['graph:dirty_not_recorded'] == 1 and armed == [1]


@pytest.mark.parametrize('labels', [{'domains': ['work', 'health']}, {'sensitivity': 'special'},
                                    {'protected_content': 'unknown'}, {'speech': 'third_party_quote'},
                                    {'domains': ['finance']}])
def test_the_lane_never_reads_a_message_the_grant_does_not_permit(legacy, tmp_path, monkeypatch, labels):
    node, _ = node_for(legacy, tmp_path, monkeypatch, labels=labels)
    node.rebuild()
    spy = Spy(PROJECT)
    counts = run_lane(node, spy)
    assert spy.seen == [] and counts['permitted_messages'] == 0
    assert lane_rows(legacy)[0] == []


def test_the_lanes_permitted_set_is_the_index_builds_member_set(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    with owner():
        state = node.index.rebuild('grant-search', now=node.now[0])
        pass_ = pd.PermittedDerivationPass(node.index, extractor=Spy())
        selected = pass_._selected(node.now[0])
    assert state['member_count'] == len(selected) == 1
    assert [ident for ident, _row in selected.values()] == [identity]


def test_an_opted_out_message_is_never_a_source(legacy, tmp_path, monkeypatch):
    from topos.permissions_v2.message_evidence import message_key
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    with owner():
        node.corpus.reviews.opt_out(message_key(identity), now=node.now[0])
    spy = Spy(PROJECT)
    run_lane(node, spy)
    assert spy.seen == []


@pytest.mark.parametrize('predicate', ['health.medication', 'mind.self_reported_state', 'rel.relationship',
                                       'practices', 'training_for', 'values.declaration', 'trait.bfi2_domain',
                                       'invented_predicate'])
def test_special_third_party_inferred_and_unclassed_predicates_are_never_stored(legacy, tmp_path, monkeypatch,
                                                                                predicate):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    counts = run_lane(node, Spy(pd.Spec('fact', predicate, 'Synthetic message')))
    assert lane_rows(legacy)[0] == []
    assert any(key.startswith('refused:predicate_') for key in counts)


@pytest.mark.parametrize('value', ['Synthetic message, and more', 'x' * 81, '', 'two\nlines'])
def test_a_value_that_is_not_one_atomic_label_is_never_stored(legacy, tmp_path, monkeypatch, value):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(pd.Spec('fact', 'work.project', value)))
    assert lane_rows(legacy)[0] == []


def test_an_off_limits_name_in_a_derived_value_is_refused_by_the_boundarys_own_match(protected_corpus):
    from topos.permissions_v2.entity_boundary import EntityBoundary
    with sqlite3.connect(protected_corpus[0].path) as conn:
        boundary = EntityBoundary(conn)
        assert boundary.active
        for value in ('Mara Example', 'Mara\u200b Example', 'Ｍａｒａ Ｅｘａｍｐｌｅ'):
            assert pd.refusal(pd.Spec('fact', 'work.project', value), boundary) in ('entity_protected', 'value_not_atomic')
        assert pd.refusal(pd.Spec('fact', 'work.project', 'Mara Example'), boundary) == 'entity_protected'
        assert pd.refusal(pd.Spec('goal', 'goal', 'ship the plan with Mara Example'), boundary) == 'entity_protected'
        assert pd.refusal(PROJECT, boundary) is None


@pytest.mark.parametrize('legacy', ['unattested'], indirect=True)
def test_an_unattested_owner_gets_nothing(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    counts = run_lane(node, Spy(PROJECT))
    assert counts.get('refused:owner_subject_unattested') == 1
    assert lane_rows(legacy)[0] == []


def edit_message(legacy, content):
    conn = legacy[1]
    conn.execute('UPDATE conversation_messages SET content=?', (content,))
    conn.commit()


def test_a_fact_never_releases_after_its_cited_message_changed(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(PROJECT))
    (fact,), _ = lane_rows(legacy)
    content = legacy[1].execute('SELECT content FROM conversation_messages').fetchone()[0]
    # Release's own check, directly: the stored lineage against a changed message.
    class Q:  # the shape check_lineage reads
        class snapshot:
            class message:
                pass
    Q.snapshot.message.identity = identity
    from topos.permissions_v2.evidence import _key
    pd.check_lineage(fact, [(Q, {_key(identity): {'content': content}})])
    with pytest.raises(PolicyError, match='lineage_revision_stale'):
        pd.check_lineage(fact, [(Q, {_key(identity): {'content': content + ' Also something else.'}})])


def test_a_fact_whose_reference_was_moved_to_another_message_never_releases(legacy, tmp_path, monkeypatch):
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(PROJECT))
    conn = legacy[1]
    (object_id, payload_json), = conn.execute(
        "SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact'").fetchall()
    payload = json.loads(payload_json)
    payload['lineage']['message'] = {**payload['lineage']['message'], 'record_id': 'imessage:other'}
    conn.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?', (json.dumps(payload), object_id))
    conn.commit()
    node.rebuild()
    assert all(r['kind'] != 'fact' for r in kinds(node))


def test_a_lineage_this_code_cannot_read_withholds(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(PROJECT))
    conn = legacy[1]
    (object_id, payload_json), = conn.execute(
        "SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact'").fetchall()
    payload = json.loads(payload_json)
    payload['lineage']['lane'] = 'od46-permitted-message/v0'
    conn.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?', (json.dumps(payload), object_id))
    conn.commit()
    node.rebuild()
    assert all(r['kind'] != 'fact' for r in kinds(node))


def test_a_lane_fact_is_withheld_when_its_message_leaves_the_grant(legacy, tmp_path, monkeypatch):
    from topos.permissions_v2.message_evidence import message_key
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(PROJECT))
    assert any(r['kind'] == 'fact' for r in kinds(node))
    with owner():
        node.corpus.reviews.opt_out(message_key(identity), now=node.now[0])
    node.rebuild()
    output, refused = node.search_request('Synthetic message', k=10)
    assert refused is not None or all(r['kind'] != 'fact' for r in output['records'])


@pytest.mark.parametrize('legacy', ['goal'], indirect=True)
def test_a_goal_from_a_permitted_message_is_stored_with_lineage_and_releases(legacy, tmp_path, monkeypatch):
    goal_store(legacy)
    node, identity = node_for(legacy, tmp_path, monkeypatch, labels={'domains': ['work', 'plans']})
    node.rebuild()
    counts = run_lane(node, Spy(pd.Spec('goal', 'goal', 'finish the compiler at work by Friday')))
    assert counts['goal:written'] == 1
    _facts, goals = lane_rows(legacy)
    assert len(goals) == 1
    records = kinds(node, 'compiler Friday')
    assert [r['content'] for r in records if r['kind'] == 'goal'] == ['finish the compiler at work by Friday']


def goal_store(legacy):
    conn = sqlite3.connect(legacy[0].resolver.path)
    from topos.storage.db.migrations.entity_edges_validity_v1 import apply_entity_edges_validity_v1_up
    apply_entity_edges_validity_v1_up(conn)
    conn.execute('CREATE TABLE IF NOT EXISTS user_goals(goal_id TEXT PRIMARY KEY,record_id TEXT,source_id TEXT,'
                 'goal_text TEXT,model TEXT,provider TEXT,payload_json TEXT)')
    conn.commit()
    conn.close()


@pytest.mark.parametrize('goal', ['what should I do next at work?', 'short', 'finish\nthe compiler', 'one'])
def test_a_goal_that_is_a_question_fragment_or_multiline_is_never_stored(legacy, tmp_path, monkeypatch, goal):
    goal_store(legacy)
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    counts = run_lane(node, Spy(pd.Spec('goal', 'goal', goal)))
    assert counts.get('refused:goal_shape') == 1 and 'goal:written' not in counts
    assert lane_rows(legacy)[1] == []


def test_a_goal_without_a_goal_store_is_refused_not_raised(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    counts = run_lane(node, Spy(pd.Spec('goal', 'goal', 'finish the compiler at work by Friday'), PROJECT))
    assert counts.get('refused:goal_store_missing') == 1 and counts.get('fact:written') == 1


def test_the_lane_writes_nothing_when_the_boundary_cannot_be_built_at_write_time(legacy, tmp_path, monkeypatch):
    from topos.permissions_v2 import entity_boundary
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    lane = pd.PermittedDerivationPass(node.index, extractor=Spy(PROJECT))
    selected = lane._selected

    class Unavailable:
        def __init__(self, conn):
            raise PolicyError('entity_protection_lineage_unavailable')

    def select_then_break(now):
        out = selected(now)
        monkeypatch.setattr(entity_boundary, 'EntityBoundary', Unavailable)
        return out
    lane._selected = select_then_break
    with owner():
        counts = lane.run(now=node.now[0])
    assert counts.get('refused:entity_boundary_unavailable') == 1
    assert lane_rows(legacy)[0] == []


def test_a_row_that_is_not_owner_authored_never_reaches_the_extractor(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    spy = Spy(PROJECT)
    lane = pd.PermittedDerivationPass(node.index, extractor=spy)
    selected = lane._selected

    def as_someone_else(now):
        # Qualification already requires owner authorship; this is the lane's own second check.
        return {key: (ident, {**row, 'is_from_self': 0, 'sender_id': 'someone-else'})
                for key, (ident, row) in selected(now).items()}
    lane._selected = as_someone_else
    with owner():
        counts = lane.run(now=node.now[0])
    assert spy.seen == [] and counts.get('refused:not_owner_authored') == 1


def test_a_message_older_than_the_grants_window_is_never_a_source(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    spy = Spy(PROJECT)
    with owner():
        counts = pd.PermittedDerivationPass(node.index, extractor=spy).run(now=node.now[0] + 400 * 86400)
    assert spy.seen == [] and counts['permitted_messages'] == 0


# --- the class table ---------------------------------------------------------------------------

def test_no_classed_predicate_is_a_special_category():
    for predicate, klass in CLASSES.items():
        assert klass.sensitivity in ('none', 'personal'), predicate
        assert 'health' not in klass.domains, predicate
        assert excluded_reason(predicate) is None, predicate


def test_every_widened_predicate_is_a_stated_pack_predicate_with_its_key():
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    predicates = {name: pred for pack in load_packs(bundled_pack_dir(), trusted=True).values()
                  for name, pred in pack.predicates.items()}
    for name, klass in WIDENED.items():
        assert name in predicates, name
        assert predicates[name].altitude == 'stated', name
        schema = getattr(predicates[name], 'value_schema', None)
        assert klass.key in (schema or {}), name
        assert klass.first_person and klass.forms


def test_every_pack_predicate_the_lane_may_meet_is_classed_or_excluded_on_purpose():
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    for pack in load_packs(bundled_pack_dir(), trusted=True).values():
        for name, pred in pack.predicates.items():
            if name in CLASSES:
                continue
            # Unclassed means never stored by the lane; these must never be classed by accident.
            if name.split('.')[0] + '.' in EXCLUDED_FAMILIES:
                assert excluded_reason(name) is not None


def test_a_message_that_changes_while_the_model_runs_is_not_written(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()

    def extractor(row, table):
        edit_message(legacy, row['content'] + ' Edited later.')
        return [PROJECT]
    counts = run_lane(node, extractor)
    assert counts.get('refused:message_changed') == 1
    assert lane_rows(legacy)[0] == []


def test_only_the_owner_may_run_the_lane(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    spy = Spy(PROJECT)
    with pytest.raises(PolicyError, match='owner_authority_required'):
        pd.PermittedDerivationPass(node.index, extractor=spy).run(now=node.now[0])
    assert spy.seen == [] and lane_rows(legacy)[0] == []


def test_a_pack_fact_releases_only_through_its_classes_scalar_field(legacy, tmp_path, monkeypatch):
    """A structured pack value (the pack writer's shape) is released as its key field, never its JSON."""
    conn = legacy[1]
    payload = {'subject_entity_id': 'owner-entity', 'predicate': 'work.project',
               'object_value': json.dumps({'collaborators': [], 'project': 'Synthetic message', 'status': 'active'}),
               'value_struct': {'project': 'Synthetic message', 'status': 'active', 'collaborators': []},
               'disclosure': 'owner_only', 'asserted_by': 'owner'}
    conn.execute("INSERT INTO signal_objects(object_id,signal_dimension,object_type,object_key,payload_json,confidence,"
                 "source_refs_json,valid_from,created_at,updated_at) VALUES('pack-fact','profile','fact','k',?,0.7,?,"
                 "'2026-01-01','2026-01-01','2026-01-01')",
                 (json.dumps(payload), json.dumps([{'table': 'conversation_messages', 'record_id': 'imessage:1'}])))
    conn.commit()
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    facts = [r['content'] for r in kinds(node) if r['kind'] == 'fact']
    assert facts == ['Owner works on the project Synthetic message.']


def test_the_census_classes_every_code_the_lane_adds():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'permissions_v2'))
    import grant_census as gc
    assert gc.reason_class('lineage_revision_stale') == 'engineering'


def test_the_census_mirrors_a_lane_fact_and_its_lineage_gate(legacy, tmp_path, monkeypatch):
    from tests.permissions_v2.test_grant_census import census_of
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(PROJECT))
    node.rebuild()
    census = census_of(node)
    facts = census.rd11['facts']
    assert facts['od46_lane'] == 1 and facts['widened_predicate'] == 1
    assert facts['funnel_grounded'] == 1 and facts['levers:none'] == 1 and facts['widened_levers:none'] == 1
    assert sum(1 for o in census.members.values() if o.family == 'fact') == 1


def test_the_census_withholds_a_lane_fact_whose_lineage_went_stale(legacy, tmp_path, monkeypatch):
    from tests.permissions_v2.test_grant_census import census_of
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(PROJECT))
    conn = legacy[1]
    (object_id, payload_json), = conn.execute(
        "SELECT object_id,payload_json FROM signal_objects WHERE object_type='fact'").fetchall()
    payload = json.loads(payload_json)
    payload['lineage']['message_revision'] = '0' * 64
    conn.execute('UPDATE signal_objects SET payload_json=? WHERE object_id=?', (json.dumps(payload), object_id))
    conn.commit()
    node.rebuild()
    census = census_of(node)
    facts = census.rd11['facts']
    assert facts['alone_lineage'] == 0 and facts['only_fails_lineage'] == 1
    assert facts['levers:provenance+entailment+attestation'] == 0
    typed = [o for o in census.typed if o.family == 'fact']
    assert [o.reason for o in typed] == ['lineage_revision_stale']


# --- the owner-socket route (OD-46 live plan) ----------------------------------------------------

ROUTE = "/v1/sharing/message-search/permitted-derivation"


@pytest.fixture
def lane_route(legacy, tmp_path, monkeypatch):
    """The route against the harness node: the runtime is the node's, the pass runs at the harness clock,
    and the model extractor is a spy (the route itself never reaches a model here)."""
    from types import SimpleNamespace
    from fastapi import FastAPI
    from topos.api.permissions_search_maintenance import router
    from topos.permissions_v2 import runtime
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    binding = node.index.resolver.binding
    fake = SimpleNamespace(protocol=SimpleNamespace(ledger=SimpleNamespace(identity=binding)),
                           message_search_index=lambda: node.index)
    monkeypatch.setattr(runtime, "get_runtime", lambda: fake)
    monkeypatch.setattr(pd.time, "time", lambda: node.now[0])
    spy = Spy(PROJECT)

    class Counted:
        counts = {"pack_calls": 0}
    seen = {}

    def node_extractor(conn, *, packs, goals):
        seen.update(packs=packs, goals=goals)
        return spy, Counted
    monkeypatch.setattr(pd, "node_extractor", node_extractor)
    app = FastAPI()
    app.include_router(router)
    return app, node, binding.model_dump(), spy, seen


def test_the_route_is_off_unless_its_flag_is_on(lane_route, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, _node, binding, spy, _seen = lane_route
    monkeypatch.delenv(pd.FLAG, raising=False)
    with TestClient(UDSChannelApp(app)) as client:
        assert client.post(ROUTE, json={"binding": binding, "operation": "run"}).status_code == 404
    assert spy.seen == []


def test_the_owner_runs_the_lane_and_gets_counts_only(lane_route, legacy, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, _node, binding, spy, seen = lane_route
    monkeypatch.setenv(pd.FLAG, "true")
    with TestClient(UDSChannelApp(app)) as client:
        response = client.post(ROUTE, json={"binding": binding, "operation": "run"})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["counts"]["fact:written"] == 1 and body["packs"] == sorted(pd.DEFAULT_PACKS)
    assert seen == {"packs": pd.DEFAULT_PACKS, "goals": True}
    assert "Synthetic message" not in response.text           # counts and codes, never a value
    assert len(lane_rows(legacy)[0]) == 1


@pytest.mark.parametrize("body", [
    {"operation": "run"},
    {"operation": "list"},
    {"operation": "run", "packs": ["relationships.social"]},
    {"operation": "run", "packs": ["health.mental"]},
    {"operation": "run", "packs": ["work.career", "work.career"]},
    {"operation": "run", "packs": "work.career"},
    {"operation": "run", "goals": "yes"},
    {"operation": "run", "budget": 0},
    {"operation": "run", "budget": pd.DEFAULT_BUDGET + 1},
    {"operation": "run", "budget": True},
    {"operation": "run", "extra": 1},
])
def test_a_malformed_or_widened_request_is_refused_before_anything_runs(lane_route, monkeypatch, body):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, _node, binding, spy, _seen = lane_route
    monkeypatch.setenv(pd.FLAG, "true")
    payload = {**body, "binding": binding} if body != {"operation": "run"} else {"operation": "run"}
    with TestClient(UDSChannelApp(app)) as client:
        assert client.post(ROUTE, json=payload).status_code == 400
    assert spy.seen == []


def test_nobody_but_the_owner_can_run_the_lane(lane_route, legacy, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.auth import resolve_request_principal
    from topos.principal import OWNER_APP, THIRD_PARTY, Principal
    app, _node, binding, spy, _seen = lane_route
    monkeypatch.setenv(pd.FLAG, "true")
    for principal in (Principal(THIRD_PARTY, "cp_relay", acting_user="owner-1"),
                      Principal(OWNER_APP, "local_http", acting_user="owner-1"),
                      Principal(OWNER_APP, "uds", acting_user="someone-else")):
        app.dependency_overrides[resolve_request_principal] = lambda principal=principal: principal
        with TestClient(app) as client:
            assert client.post(ROUTE, json={"binding": binding, "operation": "run"}).status_code == 403
    assert spy.seen == [] and lane_rows(legacy)[0] == []


def test_a_foreign_binding_is_refused(lane_route, legacy, monkeypatch):
    from fastapi.testclient import TestClient
    from topos.uds import UDSChannelApp
    app, _node, binding, spy, _seen = lane_route
    monkeypatch.setenv(pd.FLAG, "true")
    other = {**binding, "owner_id": "owner-2"}
    with TestClient(UDSChannelApp(app)) as client:
        assert client.post(ROUTE, json={"binding": other, "operation": "run"}).status_code in (400, 403)
    assert spy.seen == []


def test_the_node_extractor_refuses_a_pack_it_cannot_store_and_a_missing_model(monkeypatch):
    from topos.features.facts import llm_extract
    with pytest.raises(PolicyError, match="permitted_derivation_pack_unsupported"):
        pd.node_extractor(None, packs=("relationships.social",))
    monkeypatch.setattr(llm_extract, "_resolved_extraction_model", lambda settings, conn=None: "")
    with pytest.raises(PolicyError, match="permitted_derivation_model_unavailable"):
        pd.node_extractor(None, packs=pd.DEFAULT_PACKS)


def test_the_allowed_packs_are_exactly_those_whose_output_the_lane_can_store():
    from topos.features.derivation.packs import load_packs
    from topos.features.derivation.registry import bundled_pack_dir
    packs = load_packs(bundled_pack_dir(), trusted=True)
    storable = {pid for pid, pack in packs.items()
                if set(pack.predicates) & (set(CLASSES) | set(pd.GOAL_PACK_KEYS))}
    assert set(pd.ALLOWED_PACKS) == storable
    assert set(pd.DEFAULT_PACKS) <= set(pd.ALLOWED_PACKS) and 'aspirations.goals' not in pd.DEFAULT_PACKS


def test_od38_grounds_a_work_project_claim_and_never_a_commitment():
    from topos.permissions_v2 import entailment_grounding as eg
    claim = eg.fact_claim('work.project', 'Atlas')
    assert claim is not None and claim.relation == 'work.project'
    assert eg.RELATION_CUES['work.project'] == eg.RELATION_CUES['works_on']
    assert eg.fact_claim('commit.made', 'send the report') is None


@pytest.mark.parametrize('message,code', [
    ('I am working on Atlas.', None),
    ('I am working on Atlas at work.', None),
    ('Maybe I am working on Atlas.', 'entailment_hedged'),
    ('She is working on Atlas.', 'entailment_not_first_person'),
    ('I am not working on Atlas.', 'entailment_negated'),
])
def test_od38_guards_judge_a_work_project_claim_like_works_on(message, code):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts' / 'permissions_v2'))
    import entailment_eval as ev
    from topos.permissions_v2 import entailment_grounding as eg
    got = eg.guard_failure(eg.fact_claim('work.project', 'Atlas'), message, author_is_owner=True,
                           subject_attested=True, boundary=ev.TermBoundary([]))
    assert got == code


def test_the_census_counts_a_widened_fact_under_owner_confirm_once_it_has_a_template(legacy, tmp_path, monkeypatch):
    """WS1's guard (test_grant_census): a template for a widened predicate makes widened_levers under the
    entailment and owner_confirms_all columns live. This is that census case. 'Synthetic' is not the whole
    message's value, so fullmatch fails; every OD-38 guard passes, so only the owner-confirm ceiling counts it."""
    from tests.permissions_v2.test_grant_census import census_of
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    run_lane(node, Spy(pd.Spec('fact', 'work.project', 'Synthetic')))
    node.rebuild()
    facts = census_of(node).rd11['facts']
    assert facts['od46_lane'] == 1 and facts['widened_predicate'] == 1
    assert facts['funnel_grounded'] == 0 and facts['widened_levers:none'] == 0
    assert facts['widened_levers:owner_confirms_all'] == 1 and facts['levers:owner_confirms_all'] == 1
