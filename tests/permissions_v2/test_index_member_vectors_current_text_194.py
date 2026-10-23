"""Lane 194: a share index member's vector is the one built from the row's current text.

The embeddings job seals every chunk it stores with `content_hash` = sha256(embeddable_content(record)) of its parent.
A row rewritten after that (an edit, a re-capture, a re-import, a merged reply) keeps its old chunks until something
re-embeds it. Before this lane `SearchIndexService._members` loaded every stored chunk by (source_id, record_id, model),
so the rebuilt member ranked by words the row no longer holds: a recipient's query made only of pre-edit words still
found the edited line (DR-34 default-on review, 22 Oct 2026, residual (i); the same with DR-34 off). Measured on a copy
of a live store the same day: of 4,000 sampled AI-chat rows with a vector, 309 carried chunks for text the row no
longer holds (all re-ingested or rewritten after embedding); iMessage 0 of 3,998; journals 168 of 597.

Every line here is invented. The embedder is the harness's deterministic fake.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tests.ingestion.test_ai_chat_writer_class import captured_jobs  # noqa: F401
from tests.permissions_v2.test_fresh_node_message_tables import (  # noqa: F401
    EXPORT, LINE, NOW as AI_NOW, OWNER as AI_OWNER, _bound, _own_pass, _share_policy, ai_chat_only)
from tests.permissions_v2.test_nr188_narrow_member_drop import _ai, _path
from tests.permissions_v2.message_search_harness import fake_embedder
from topos.features.signal.embed_context import embeddable_content
from topos.permissions_v2 import search_index

MODEL = "fake-model"
OTHER = "Notes on the tide pools field trip for the volunteer group, invented for this test."
NEW = "Weekend grocery list with apples bread and oat milk."
PRE_EDIT_ONLY_QUERY = "sketching spring"   # words of LINE that NEW does not contain


def _hash(text: str) -> str:
    return hashlib.sha256(embeddable_content({"content": text}).encode("utf-8")).hexdigest()


def _copy(db, table, key, value, **changes):
    cursor = db.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,))
    row = dict(zip([column[0] for column in cursor.description], cursor.fetchone()))
    row.update(changes)
    db.execute(f"INSERT INTO {table}({','.join(row)}) VALUES ({','.join('?' * len(row))})", tuple(row.values()))


def _store_vector(db, record_id, text, *, chunk_index=0, content_hash="current", embedding_id=None):
    """A stored chunk as the embeddings job writes it; `content_hash` "current" seals it with `text`'s hash."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(signal_embeddings)")}
    row = {"embedding_id": embedding_id or f"emb-{record_id}-{chunk_index}", "record_id": record_id,
           "source_id": EXPORT, "model": MODEL, "dims": 32,
           "vector_blob": json.dumps(fake_embedder(text, MODEL)).encode(), "vector_format": "json",
           "chunk_index": chunk_index, "search_text": text,
           "content_hash": _hash(text) if content_hash == "current" else content_hash}
    row = {k: v for k, v in row.items() if k in cols}
    db.execute(f"INSERT INTO signal_embeddings({','.join(row)}) VALUES ({','.join('?' * len(row))})",
               tuple(row.values()))


@pytest.fixture
def share(ai_chat_only):
    """The fresh-node AI-chat share with a second chat line and a stored, hash-sealed vector for each line."""
    from tests.permissions_v2.message_search_harness import Node
    from tests.permissions_v2.test_ingest_provenance import owner as owner_as
    db = ai_chat_only.db
    _copy(db, "ai_chat_conversations", "conversation_id", "chatgpt:thread-file", conversation_id="chatgpt:thread-other",
          source_record_id="thread-other", title="an invented second chat", latest_title=None,
          created_at="2026-09-01T09:30:00Z", updated_at="2026-09-01T09:30:00Z")
    _copy(db, "ai_chat_messages", "message_id", "m-line", message_id="m-other", conversation_id="chatgpt:thread-other",
          source_record_id="m-other", event_at="2026-09-01T09:30:00Z", content=OTHER, sequence=0,
          metadata_json='{"thread_id": "thread-other", "original_source": "chatgpt"}')
    _store_vector(db, "m-line", LINE)
    _store_vector(db, "m-other", OTHER)
    db.commit()
    resolver, reviews = _bound(ai_chat_only)
    assert _own_pass(resolver, reviews)["assessed"] == 2
    node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), ai_chat_only.root / "share",
                model=MODEL, search_raw=_share_policy(), now=AI_NOW)
    with owner_as(actor=AI_OWNER):
        assert node.index.rebuild("grant-search", now=AI_NOW) == {"state": "ready", "member_count": 2}
    node.db = db
    node.store = ai_chat_only
    return node


def _rebuild(node, *, review=True):
    from tests.permissions_v2.test_ingest_provenance import owner as owner_as
    if review:
        _own_pass(node.corpus.resolver, node.corpus.reviews)
    with owner_as(actor=AI_OWNER):
        return node.index.rebuild("grant-search", now=AI_NOW)


def _vectors_of(node, record_id) -> list:
    """The vectors the share index file holds for one record, read through its sealed members."""
    opened = node.index._open(_path(node))
    by_record = {_record_of(node, opaque, sealed): opaque for opaque, sealed in opened["sealed"]}
    conn = sqlite3.connect(_path(node).as_uri() + "?mode=ro", uri=True)
    try:
        return [search_index._unf32(blob) for (blob,) in conn.execute(
            "SELECT vector FROM vectors WHERE opaque_id=? ORDER BY chunk_index", (by_record[record_id],))]
    finally:
        conn.close()


def _record_of(node, opaque, sealed):
    from tests.permissions_v2.test_nr188_narrow_member_drop import _grant
    key = node.index.keys.get(_grant(node), create=False)
    return search_index.unseal(key, opaque, sealed)["record_id"]


def _close(a, b) -> bool:
    return len(a) == len(b) and all(abs(x - y) < 1e-5 for x, y in zip(a, b))


def _served(node, query) -> list:
    output, refused = node.search_request(query, k=10)
    assert refused is None, refused
    return [record["content"] for record in output["records"]]


# --- the fix ------------------------------------------------------------------------------------------------------

def test_an_unedited_line_keeps_its_stored_vector(share):
    assert _close(_vectors_of(share, "m-line")[0], fake_embedder(LINE, MODEL))
    assert _close(_vectors_of(share, "m-other")[0], fake_embedder(OTHER, MODEL))


def test_an_edited_line_is_rebuilt_without_its_pre_edit_vector(share):
    _ai(share, "UPDATE ai_chat_messages SET content=? WHERE message_id='m-line'", (NEW,))
    assert _rebuild(share)["member_count"] == 2               # fresh review of the new text, then rebuild
    old, new = fake_embedder(LINE, MODEL), fake_embedder(NEW, MODEL)
    assert not any(_close(vector, old) for vector in _vectors_of(share, "m-line"))
    # With a passage embedder the node embeds the current text; without one the member is lexical-only.
    vectors = _vectors_of(share, "m-line")
    if share.index.passage_embedder is not None:
        assert vectors and all(_close(vector, new) for vector in vectors)
    else:
        assert vectors == []


def test_a_query_of_pre_edit_words_no_longer_finds_the_edited_line(share):
    assert not set(search_index.tokenize(PRE_EDIT_ONLY_QUERY)) & set(search_index.tokenize(NEW))
    _ai(share, "UPDATE ai_chat_messages SET content=? WHERE message_id='m-line'", (NEW,))
    assert _rebuild(share)["member_count"] == 2
    assert NEW not in _served(share, PRE_EDIT_ONLY_QUERY)


def test_a_re_embedded_line_uses_its_new_vector_and_drops_a_left_over_old_chunk(share):
    """A shorter new text re-embedded as one chunk leaves the old text's chunk 1 behind (the store upserts by
    chunk index): the member takes only the chunks sealed with the current text."""
    _store_vector(share.db, "m-line", LINE + " (second half)", chunk_index=1, content_hash=_hash(LINE),
                  embedding_id="emb-old-chunk-1")
    share.db.execute("UPDATE signal_embeddings SET vector_blob=?, content_hash=? WHERE record_id='m-line' "
                     "AND chunk_index=0", (json.dumps(fake_embedder(NEW, MODEL)).encode(), _hash(NEW)))
    share.db.commit()
    _ai(share, "UPDATE ai_chat_messages SET content=? WHERE message_id='m-line'", (NEW,))
    assert _rebuild(share)["member_count"] == 2
    vectors = _vectors_of(share, "m-line")
    assert len(vectors) == 1 and _close(vectors[0], fake_embedder(NEW, MODEL))


@pytest.mark.parametrize("stored_hash", [None, "", "0" * 64], ids=["no_hash", "empty_hash", "other_text"])
def test_a_vector_not_sealed_with_the_current_text_is_never_taken(share, stored_hash):
    share.db.execute("UPDATE signal_embeddings SET content_hash=? WHERE record_id='m-other'", (stored_hash,))
    share.db.commit()
    assert _rebuild(share)["member_count"] == 2
    # The stored chunk is not taken. A node with a passage embedder embeds the current text instead (the same
    # vector here, as the fake embedder is deterministic); one without leaves the member lexical-only.
    vectors = _vectors_of(share, "m-other")
    if share.index.passage_embedder is not None:
        assert vectors and all(_close(vector, fake_embedder(OTHER, MODEL)) for vector in vectors)
    else:
        assert vectors == []


def test_the_hash_is_the_embeddings_jobs_hash():
    from topos.enrichment.jobs.canonical.embeddings_job import _content_hash
    row = {"content": LINE, "message_id": "m", "source_id": EXPORT}
    assert search_index._embedded_content_hash(row) == _content_hash(embeddable_content(row))
    assert search_index._embedded_content_hash({"content": ""}) is None
