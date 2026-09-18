"""What a search leaves behind, and what it reads.

- Query text is never logged or persisted in plaintext on the node (design review
  condition 5): after searches with sentinel queries, no private store, no index,
  no key file, no ledger row and no captured log line contains a sentinel.
- Opaque ids: the channel-11 fix. Ids carry no ordinal, differ across grants for
  the same record, and are stable within a grant across rebuilds.
- The zero-permitted-set test (design §12a): with P empty the answer is an empty
  list and no canonical content table is read at all.
- The closed lane allowlist: search modules import no owner lane or cache.
- No shared state: the process-wide query-embedding cache is never touched.
"""
from __future__ import annotations

import ast
import json
import logging
import sqlite3
from pathlib import Path

import pytest

from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, embed_corpus, pin_record_key
from topos.permissions_v2 import search_lanes
from topos.permissions_v2.opaque_ids import opaque_record_id

SENTINEL = "qqsentinel7731"


def build(tmp_path, counts=None, **kwargs):
    corpus = mc.build(tmp_path / "corpus", seed=17, counts=counts or {name: 1 for name in mc.KINDS} | {"clean_positive_C": 5})
    embed_corpus(corpus)
    node = Node(corpus, tmp_path, **kwargs)
    node.rebuild()
    return node


def test_query_text_is_never_persisted_or_logged(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    node = build(tmp_path)
    queries = [f"{SENTINEL} roadmap", f"deploy {SENTINEL}", SENTINEL * 3, f"{SENTINEL}ÄÖÜ"]
    for query in queries:
        output, refused = node.search_request(query, k=5)
        assert refused is None
    node.search_request(SENTINEL, k=25, window={"after": 0, "before": 1})  # a refused one too
    needles = [SENTINEL.encode(), SENTINEL.upper().encode(), "qqsentinel".encode("utf-16-le")]
    files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert files
    for path in files:
        data = path.read_bytes()
        for needle in needles:
            assert needle not in data, path
    for record in caplog.records:
        assert SENTINEL not in record.getMessage()


def test_ids_are_keyed_hashes_without_order_and_differ_across_grants(tmp_path):
    key_a, key_b = bytes(range(32)), bytes(range(1, 33))
    ids = [opaque_record_id(key_a, grant_id="g", table="conversation_messages", source_id="imessage",
                            dataset_id="d", record_id=f"imessage:{n}") for n in range(2_000)]
    assert len(set(ids)) == len(ids)
    # No monotone relation to the ROWID: about half of consecutive pairs go each way.
    ups = sum(1 for left, right in zip(ids, ids[1:]) if right > left)
    assert 900 < ups < 1_100
    other = opaque_record_id(key_b, grant_id="g", table="conversation_messages", source_id="imessage",
                             dataset_id="d", record_id="imessage:0")
    assert other != ids[0]
    assert opaque_record_id(key_a, grant_id="g2", table="conversation_messages", source_id="imessage",
                            dataset_id="d", record_id="imessage:0") != ids[0]


def test_ids_are_stable_within_a_grant_across_rebuilds(tmp_path):
    node = build(tmp_path)
    first = node.search_request("roadmap deploy", k=25)[0]
    node.rebuild()
    second = node.search_request("roadmap deploy", k=25)[0]
    assert first == second and first["records"]
    raw = json.dumps(first)
    for unit in node.corpus.units:
        assert unit.message_id not in raw


def test_two_search_grants_see_unlinkable_ids(tmp_path):
    node = build(tmp_path)
    other = mc.search_policy(grant="grant-other", actor="actor-9", client="client-9")
    node.activate(other)
    node.rebuild()
    mine = {r["content"]: r["record_id"] for r in node.search_request("roadmap deploy", k=25)[0]["records"]}
    theirs = {r["content"]: r["record_id"]
              for r in node.search_request("roadmap deploy", k=25, grant_id="grant-other", actor="actor-9",
                                           client="client-9")[0]["records"]}
    shared = set(mine) & set(theirs)
    assert shared and all(mine[content] != theirs[content] for content in shared)


def test_zero_permitted_set_answers_empty_and_reads_no_content(tmp_path, monkeypatch):
    node = build(tmp_path, counts={"clean_positive_G": 3, "adjacent_negative": 3, "unreviewed": 3})
    reads = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        target = str(args[0] if args else kwargs.get("database"))
        if "canonical.db" in target:
            def authorizer(action, table, *_):
                if action == sqlite3.SQLITE_READ:
                    reads.append(table)
                return sqlite3.SQLITE_OK
            conn.set_authorizer(authorizer)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    output, refused = node.search_request("roadmap oncologist", k=25)
    assert refused is None and output["records"] == []
    assert not {"conversation_messages", "ai_chat_messages", "signal_embeddings", "signal_objects"} & set(reads)


def test_discovery_reads_no_embedding_table_and_only_member_rows_by_key(tmp_path, monkeypatch):
    """Before the release re-check, canonical data is touched only by point lookups of R(g)'s own rows."""
    node = build(tmp_path)
    stage = ["discovery"]
    statements = []
    node.search.observe = lambda name, _elapsed: stage.__setitem__(0, "recheck" if name == "rank" else stage[0])
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        if "canonical.db" in str(args[0] if args else kwargs.get("database")):
            conn.set_trace_callback(lambda sql: statements.append(sql) if stage[0] == "discovery" else None)
        return conn
    monkeypatch.setattr(sqlite3, "connect", connect)
    output, refused = node.search_request("roadmap deploy", k=5)
    assert refused is None and output["records"]
    content = [sql for sql in statements if "sqlite_master" not in sql and any(table in sql for table in
               ("conversation_messages", "ai_chat_messages", "signal_objects", "signal_embeddings"))]
    assert content and not any("signal_embeddings" in sql or "ai_chat_messages" in sql for sql in content)
    for sql in content:
        assert ("WHERE message_id=" in sql and "AND source_id=" in sql) or "WHERE object_id=" in sql, sql


FORBIDDEN_IMPORTS = ("topos.query", "topos.features.signal.service", "topos.features.signal.hybrid_search",
                     "topos.features.signal.query_embed_cache", "topos.storage.adapters", "topos.storage.adapters.sqlite.vector_search")


@pytest.mark.parametrize("module", ["search_lanes", "search_index", "search_release", "search_transport"])
def test_search_modules_import_no_owner_lane_or_cache(module):
    source = Path(search_lanes.__file__).with_name(module + ".py").read_text()
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            prefix = "topos.permissions_v2." if node.level == 1 else ""
            names.add(prefix + (node.module or ""))
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    for name in names:
        assert not any(name == bad or name.startswith(bad + ".") for bad in FORBIDDEN_IMPORTS), name


def test_the_shared_query_embedding_cache_is_never_used(tmp_path, monkeypatch):
    from topos.features.signal import query_embed_cache
    touched = []
    monkeypatch.setattr(query_embed_cache, "get_cached_query_embedding", lambda *a, **k: touched.append(a))
    monkeypatch.setattr(query_embed_cache, "set_cached_query_embedding", lambda *a, **k: touched.append(a))
    node = build(tmp_path)
    assert node.search_request("roadmap", k=5)[1] is None
    assert touched == []


def test_rerank_hook_sees_only_releasable_members(tmp_path):
    node = build(tmp_path)
    grant = node.search_raw["binding"]["grant_id"]
    with node.ledger._transaction() as db:
        authority, _ = node.ledger._authority(db, grant, mc.NOW)
    loaded = node.index.load(grant, authority)
    seen = []

    def rerank(query, order):
        seen.extend(order)
        return list(reversed(order))
    members = {member.opaque_id for member in loaded.members}
    lower, upper = (mc.NOW - mc.WINDOW_SECONDS) * 1_000_000, mc.NOW * 1_000_000
    order = search_lanes.rank(loaded, "roadmap deploy", None, limit=50, lower_us=lower, upper_us=upper, rerank=rerank)
    assert seen and set(seen) <= members and order == list(reversed(seen))[:50]
