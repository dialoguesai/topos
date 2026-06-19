"""Gap: relationship edges use person_id — PRD_04"""
import pytest
from topos.enrichment.job_writer import write_signal_records
from topos.storage.adapters.factory import AdapterFactory
from topos.storage.person.identity_resolver import IdentityResolver
from remediation_helpers import sqlite_conn
pytestmark = pytest.mark.gap

def test_entities_graph_uses_resolved_nodes() -> None:
    conn = sqlite_conn()
    bundle = AdapterFactory.create("local_database", conn=conn)
    resolver = IdentityResolver(conn, owner_user_id="owner")
    person_id = resolver.resolve("imessage_handle", "alice").person_id
    write_signal_records("entities", [{"message_id":"m1","record_id":"m1","entity_text":"Alice","source_id":"imessage","person_id":person_id}], adapters=bundle, conn=conn, provenance={"job_id":"entities"})
    page = bundle.graph.list_graph(limit_nodes=10, limit_edges=10)
    assert page.get("nodes")
