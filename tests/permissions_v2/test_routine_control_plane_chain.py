"""Joint-checkout routine chain: real CP signing, node enforcement and final forwarding.

Run with the control-plane checkout on PYTHONPATH. Without that checkout this
optional integration lane skips. Corpus, transport and LLM are invented; both
ledgers, policy compilation, protocol ACKs, retrieval and release proofs are real.
"""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

permissions = pytest.importorskip("control_plane.routine_query_permissions")

from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401, E402
from tests.permissions_v2.test_ingest_provenance import ingest_fixture  # noqa: F401, E402
from tests.permissions_v2.test_knowledge_search import node_for  # noqa: E402
from tests.permissions_v2.message_search_harness import owner  # noqa: E402
from topos.principal import reset_principal, set_principal  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize("excluded", [False, True])
async def test_saved_routine_through_real_node_and_verified_history(legacy, tmp_path, monkeypatch, excluded):
    from control_plane.config import settings
    from control_plane import relay_stamp, routines_engine_bridge, routines_executor
    from control_plane.permissions_v2 import journey_bridge
    from control_plane.permissions_v2.journey_contract import SourceOption
    from control_plane.permissions_v2.node_registry import REGISTRY_FILE, NodeRegistry
    from control_plane.permissions_v2.runtime import Coordinator, CoordinatorConfig
    from control_plane.routes import permissions_beta_source_read, signal_proxy, uma_policy_journey, uma
    from control_plane.routines_storage import InMemoryRoutinesStore
    from topos import relay_stamp as node_stamp

    node, _ = node_for(legacy, tmp_path, monkeypatch, labels={"domains": ["work", "health"]} if excluded else None)
    now = lambda: node.now[0]
    for field, value in dict(keycloak_frontend_client_id="client-2", permissions_beta_v2_enabled=True,
                            permissions_beta_v2_source_release_enabled=True, permissions_beta_v2_message_search_enabled=True,
                            permissions_beta_v2_message_search_client_ids=["client-2"]).items():
        monkeypatch.setattr(settings, field, value)
    clock = SimpleNamespace(time=now, monotonic=now)
    for module in (permissions, journey_bridge, permissions_beta_source_read, relay_stamp):
        monkeypatch.setattr(module, "time", clock)
    monkeypatch.setattr(relay_stamp, "_signing_key", lambda: node.cp_key)
    monkeypatch.setattr(node_stamp, "_load_public_key_bytes", lambda: node.cp_key.public_key().public_bytes_raw())
    monkeypatch.setattr(node_stamp, "_now", now)

    root = tmp_path / "cp"
    root.mkdir()
    binding = node.ledger.identity
    config = CoordinatorConfig.model_construct(deployment_mode="single-instance-beta", durable_directory=str(root),
        cp_private_key_path=str(root / "key"), cp_key_id="cp-key", cp_issuer_id="cp-issuer", environment_id=binding.environment_id)
    registry = NodeRegistry(root / REGISTRY_FILE, environment_id=binding.environment_id, cp_issuer_id="cp-issuer", now=now())
    coordinator = Coordinator(config=config, key=node.cp_key, registry=registry, frontend_client_id="client-2")
    coordinator.clock = now
    record = registry.register(node_id=binding.node_id, topos_id=binding.resource_id, owner_id=binding.owner_id,
        origin="script", store_path=registry.store_path_for(binding.node_id), key_id="node-key",
        public_key=node.node_key.public_key().public_bytes_raw(), now=now())
    runtime = coordinator.runtime(record)
    journey_bridge.install_coordinator(coordinator)
    sent = []

    async def relay(message, engine_key, **kwargs):
        assert engine_key == "routine-test-engine"
        sent.append(deepcopy(message))
        principal = node_stamp.verify_relay_stamp(message)
        assert principal is not None
        token = set_principal(principal)
        try:
            raw = message["payload"]["envelope"]
            if message["type"] == "permissions_v2_status":
                payload = {"ack": node.protocol.status(raw, now=now()).model_dump()}
            elif message["type"] == "permissions_v2_mutate":
                payload = {"ack": node.protocol.mutate(raw, now=now()).model_dump()}
            else:
                assert message["type"] == "permissions_v2_message_search"
                assert principal.cls == "third_party"
                # The production indexing worker runs after activation. Complete that
                # work synchronously here so timing cannot mask the disclosure checks.
                with owner():
                    node.index.rebuild(raw["grant_id"], now=now())
                proof, output = node.search.dispatch(envelope=raw, payload=message["payload"]["intent"], request_id=message["id"])
                payload = {"result": proof, "output": output}
            return {"id": message["id"], "type": message["type"], "status": "ok", "payload": payload}
        finally:
            reset_principal(token)

    state = SimpleNamespace(relay=SimpleNamespace(send_request=relay), revoked_keycloak_user_ids=set(), supabase=None,
        topos_database_profiles={binding.resource_id: {"resource_id": "engine-connector:routine-test-engine"}},
        allowed_keys={"routine-test-engine": {"user_id": binding.owner_id}}, send_request=AsyncMock())
    monkeypatch.setattr(signal_proxy, "_assert_signal_proxy_topos_access", AsyncMock())
    monkeypatch.setattr(signal_proxy, "_resolve_engine_key_for_topos", AsyncMock(return_value="routine-test-engine"))
    monkeypatch.setattr(uma, "get_uma_service_optional", lambda: SimpleNamespace())
    async def catalog(bridge):
        bridge.answers_available = False
        bridge.writer_disclosure_available = False
        return binding.resource_id, [SourceOption(source_id="imessage", label="iMessage", tables=["conversation_messages"])]
    monkeypatch.setattr(uma_policy_journey, "source_catalog", catalog)
    monkeypatch.setattr(uma_policy_journey, "client_ids", AsyncMock(return_value=["client-2"]))
    monkeypatch.setattr(routines_engine_bridge, "build_run_target_context", AsyncMock(return_value=SimpleNamespace(blocked_update=None)))
    monkeypatch.setattr(routines_executor, "_resolve_routine_pack", AsyncMock(return_value=None))
    writer = AsyncMock(return_value="Synthetic permitted work digest.")
    monkeypatch.setattr(routines_engine_bridge, "make_routines_synthesis_llm_call", lambda *a, **kw: writer)
    store = InMemoryRoutinesStore()
    try:
        routine = await store.create_routine(owner_user_id=binding.owner_id, payload={"name": "Work digest",
            "prompt": "Summarize Synthetic message work.", "trigger_type": "manual", "output_config": {"destination": "run_history"},
            "failure_policy": {"max_retries": 3, "retry_window_minutes": 1440, "on_missed_local_run": "run_when_node_reconnects"},
            "targets": [{"topos_id": binding.resource_id, "query_path": "grant", "access_mode": "raw_access",
                "grant_permissions": {"version": "uma-policy-draft/v1", "profile": "knowledge_search", "client_id": "client-2",
                    "source_ids": ["imessage"], "domains": ["work"], "excluded_domains": ["health"], "sensitivities": ["none"],
                    "lookback_days": 30, "ceiling": "raw", "result_types": ["message"], "questions_per_day": 10}}]})
        run = await store.create_run(owner_user_id=binding.owner_id, routine_id=routine["id"], trigger_source="manual")
        await permissions.execute_permissioned_routine(state, store, routine, run, item=None, run_id=run["id"], engine_id="", local=False)
        final = await store.get_run_by_id(run["id"])
        with runtime.store._read() as db:
            refusals = [dict(row) for row in db.execute("SELECT * FROM refusal_records")]
        assert final["status"] == "completed", (final, [m["type"] for m in sent], refusals)
        product = final["result"]["permission_products"][0]["product"]
        if excluded:
            assert product["records"] == []
            assert "Synthetic permitted work digest." not in final["result"]["summary"]
            writer.assert_not_called()
        else:
            assert product["records"][0]["content"] == "I am working on Synthetic message at work."
            assert "I am working on Synthetic message at work." in writer.call_args.args[0]
            assert final["result"]["summary"] == "Synthetic permitted work digest."
        assert {m["type"] for m in sent} == {"permissions_v2_status", "permissions_v2_mutate", "permissions_v2_message_search"}
        state.send_request.assert_not_called()
    finally:
        journey_bridge.clear_coordinator(coordinator)
        coordinator.close()
