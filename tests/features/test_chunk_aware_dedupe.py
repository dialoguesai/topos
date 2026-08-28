"""Deduping by content must not fold a document down to its first chunk.

`signal_embeddings.content_hash` is computed from the PARENT record, so every
chunk of one document carries the same hash. Nine live records already do — two
chunks each, two distinct `chunk_index` values, one hash between them.

The register called this "harmless at 9 rows today", and it was, right up until
something deduped on that column. Two things now do: the keyword contributor
(added to stop the budget buying the same document repeatedly) and the signal
service's read-time fold. Keyed on the hash alone, both return one chunk per
document and make every document's tail unsearchable — the cost rising exactly
when a long-document connector lands and chunking starts to matter.

The read-side key is therefore (content_hash, chunk_index). The write side is
left alone deliberately: making the hash per-chunk changes what the column means
for every existing row and needs a backfill, whereas the reads are where the
loss actually happens.
"""

from __future__ import annotations

import sqlite3

import pytest

from topos.features.signal.hybrid_search import fts_search


@pytest.fixture()
def conn(tmp_path):
    from topos.storage.db.migrations import apply_all_migrations

    c = sqlite3.connect(str(tmp_path / "chunks.db"))
    apply_all_migrations(c)
    yield c
    c.close()


def _embed(conn, eid, text, *, record_id, content_hash, chunk_index):
    conn.execute(
        "INSERT INTO signal_embeddings (embedding_id, record_id, source_id,"
        " signal_dimension, model, provider, dims, text_preview, provenance_json,"
        " vector_blob, content_hash, chunk_index, search_text)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (eid, record_id, "github_activity", "work", "m", "p", 2, text[:200],
         "{}", None, content_hash, chunk_index, text),
    )
    conn.commit()


def test_both_chunks_of_one_document_are_reachable(conn):
    """The failure: one hash, two chunks, and only the first ever returned."""
    _embed(conn, "e1", "kestrel migration notes part one", record_id="r1",
           content_hash="h-shared", chunk_index=0)
    _embed(conn, "e2", "kestrel migration notes part two", record_id="r1",
           content_hash="h-shared", chunk_index=1)

    ids = set(fts_search(conn, "kestrel migration", limit=60))

    assert ids == {"e1", "e2"}, f"a chunk was dropped by the content dedupe: {ids}"


def test_two_records_sharing_a_text_still_collapse(conn):
    """Control: the dedupe must still do its job across DIFFERENT records.

    2,634 of 9,429 live embeddings are redundant copies of another's text; if
    chunk-awareness turned the dedupe off entirely the budget goes back to
    buying the same document several times.
    """
    _embed(conn, "e3", "grow app", record_id="r2", content_hash="h-a", chunk_index=0)
    _embed(conn, "e4", "grow app", record_id="r3", content_hash="h-a", chunk_index=0)

    ids = fts_search(conn, "grow app", limit=60)

    assert len(ids) == 1, "identical documents should still collapse to one slot"


def test_rows_with_no_hash_fall_back_to_their_text(conn):
    _embed(conn, "e5", "unique alpha document", record_id="r4",
           content_hash="", chunk_index=0)
    _embed(conn, "e6", "unique beta document", record_id="r5",
           content_hash="", chunk_index=0)

    ids = set(fts_search(conn, "unique", limit=60))

    assert ids == {"e5", "e6"}


def test_the_service_fold_is_chunk_aware_too():
    """The other read-time dedupe, which had the same hazard."""
    import inspect

    from topos.features.signal import service

    src = inspect.getsource(service)
    assert "chunk_index" in src, "the service fold still keys on content_hash alone"
