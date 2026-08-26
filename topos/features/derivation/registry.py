"""Pack registry API (W2.2/F1.2) — which packs run on THIS node.

The pack_registry table (migration 65) is the runtime authority; the bundled
pack files are the catalog. Seeding follows the W5 wave plan, not the D2
end-state matrix: enabling everything at once would put 25 packs into ingest
before 22 of them have earned a junk gate. Wave A earned its gate 2026-08-26
(pipeline battery: junk 19/23 -> ~1/23 with A1-A4 active).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Dict

from .packs import Pack, load_packs

#: Packs seeded enabled by default. Wave A earned its gate 2026-08-26 (full-corpus,
#: junk ≈4-5%, zero critical misattribution); health.mental joined the same day
#: (Wave B first admit: 0/7 junk under the 0.2.4 felt-state contract).
WAVE_A = ("relationships.social", "work.career", "health.physical")
ENABLED_BY_DEFAULT = WAVE_A + ("health.mental",)


def seed_pack_registry(conn: sqlite3.Connection, pack_dir: Path) -> None:
    """Idempotent: INSERT OR IGNORE every bundled pack; Wave A enabled, rest off.
    Never flips an existing row — the owner's enable/disable choices are theirs."""
    packs = load_packs(pack_dir)
    for pid, pack in packs.items():
        conn.execute(
            """INSERT OR IGNORE INTO pack_registry
               (pack_id, version, enabled, disclosure_default)
               VALUES (?, ?, ?, ?)""",
            (pid, pack.version, 1 if pid in ENABLED_BY_DEFAULT else 0,
             getattr(pack, "disclosure_default", "owner_only") or "owner_only"),
        )
        # version follows the bundled catalog (upgrades are additive; the
        # ontology_version stamped on each fact preserves what actually wrote it)
        conn.execute(
            "UPDATE pack_registry SET version=?, updated_at=datetime('now')"
            " WHERE pack_id=? AND version<>?",
            (pack.version, pid, pack.version),
        )
    conn.commit()


def enabled_packs(conn: sqlite3.Connection, pack_dir: Path) -> Dict[str, Pack]:
    rows = conn.execute("SELECT pack_id FROM pack_registry WHERE enabled=1").fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return {}
    return load_packs(pack_dir, only=ids)


def mark_pack_run(conn: sqlite3.Connection, pack_id: str, version: str) -> None:
    conn.execute(
        "UPDATE pack_registry SET last_run_at=datetime('now'), last_run_version=?"
        " WHERE pack_id=?",
        (version, pack_id),
    )


def bundled_pack_dir() -> Path:
    """The packs that ship inside the engine wheel (MANIFEST.in includes them)."""
    return Path(__file__).resolve().parent / "bundled_packs"
