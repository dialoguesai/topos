"""CI guard: physical ANN table names stay behind the VectorIndex seam (PLAN M3).

``signal_embeddings_vec`` / ``vec0`` / ``sqlite_vec`` are storage-adapter
concerns. Callers outside the allow-list below must go through the
``VectorIndex`` protocol (``delete_embeddings`` / ``delete_by_record`` /
``search_similar``) so a future backend swap stays optional and cheap —
without paying for it today by shipping FAISS.
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOPOS_PKG = REPO_ROOT / "topos"

# Modules that legitimately know about the physical ANN table / extension.
_ALLOWED_PREFIXES = (
    "storage/adapters/",
    "storage/db/migrations/",
    "storage/db/storage_breakdown.py",
    "storage/db/connection_tuning.py",
    "upgrades/",
    "core/state.py",
    # Comment-only / docs references that explain *why* they do not touch vec0.
    "features/signal/tool_index.py",
    "engine/backends/huggingface.py",
    "features/signal/vector_settings.py",
)

_PATTERNS = ("signal_embeddings_vec", "vec0", "sqlite_vec")


def _rel(path: Path) -> str:
    return path.relative_to(TOPOS_PKG).as_posix()


def _is_allowed(rel: str) -> bool:
    return any(rel.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def test_physical_ann_names_stay_behind_the_vector_index_seam() -> None:
    offenders: list[str] = []
    for path in TOPOS_PKG.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        rel = _rel(path)
        if _is_allowed(rel):
            continue
        text = path.read_text(encoding="utf-8")
        hits = [p for p in _PATTERNS if p in text]
        if hits:
            offenders.append(f"{rel}: {', '.join(hits)}")
    assert not offenders, (
        "Physical ANN references outside the VectorIndex seam:\n  "
        + "\n  ".join(offenders)
        + "\nUse VectorIndex.delete_embeddings / delete_by_record / search_similar "
        "instead (PLAN_GRAPH_QUERY_AND_LATENT_EDGES §5)."
    )


def test_guard_fails_on_a_planted_violation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The allow-list is load-bearing: a planted violation must be reported."""
    planted = TOPOS_PKG / "features" / "entities" / "_seam_guard_probe.py"
    planted.write_text(
        '"""probe — deleted by test teardown"""\nX = "signal_embeddings_vec"\n',
        encoding="utf-8",
    )
    try:
        with pytest.raises(AssertionError, match="signal_embeddings_vec"):
            test_physical_ann_names_stay_behind_the_vector_index_seam()
    finally:
        planted.unlink(missing_ok=True)


def test_sqlite_vector_index_delete_embeddings_removes_ann_rows(tmp_path: Path) -> None:
    import sqlite3

    from topos.storage.adapters.sqlite.stores import SQLiteVectorIndex
    from topos.storage.adapters.sqlite.vector_search import _sqlite_vec_ready
    from topos.storage.db.migrations import apply_all_migrations

    db = tmp_path / "seam.db"
    conn = sqlite3.connect(db)
    apply_all_migrations(conn)
    if not _sqlite_vec_ready(conn):
        pytest.skip("sqlite-vec unavailable in this environment")

    index = SQLiteVectorIndex(conn)
    embedding_id = index.upsert(
        {
            "embedding_id": "emb_seam_1",
            "record_id": "rec_seam_1",
            "source_id": "test",
            "signal_dimension": "relationships",
            "model": "all-MiniLM-L6-v2",
            "dims": 3,
            "text_preview": "hello",
            "chunk_index": 0,
        },
        vector=[1.0, 0.0, 0.0],
    )
    assert index.delete_embeddings([embedding_id]) == 1
