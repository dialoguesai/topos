"""Pack registry API (W2.2/F1.2) — which packs run on THIS node.

The pack_registry table (migration 65) is the runtime authority; the bundled
pack files are the catalog. Seeding follows the W5 wave plan, not the D2
end-state matrix: enabling everything at once would put 25 packs into ingest
before 22 of them have earned a junk gate. Wave A earned its gate 2026-08-26
(pipeline battery: junk 19/23 -> ~1/23 with A1-A4 active).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict, Optional

from .packs import Pack, load_packs

#: Packs seeded enabled by default. Wave A earned its gate 2026-08-26 (full-corpus,
#: junk ≈4-5%, zero critical misattribution); health.mental joined the same day
#: (Wave B first admit: 0/7 junk under the 0.2.4 felt-state contract).
WAVE_A = ("relationships.social", "work.career", "health.physical")
ENABLED_BY_DEFAULT = WAVE_A + ("health.mental",)


def _add_column_if_missing(conn: sqlite3.Connection, table: str, column: str,
                           col_type: str) -> None:
    """Additive column, applied on every seed pass — deliberately NOT a registry
    migration.

    Bumping `user_version` past what the installed engine understands fences the node out
    of every write; that cost ~25 minutes of a dead node on 2026-08-25. An additive column
    on a table this code already owns needs none of that ceremony, and the same pattern is
    already in use for the messenger analytics tables.
    """
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")


def install_stamp_path(pack_file: Path, pack_id: str) -> Path:
    """The stamp beside an owner-added pack file: `.custom.name.added`, next to `custom.name.yaml`."""
    return Path(pack_file).with_name(f".{pack_id}.added")


def _install_stamp(pack: Pack) -> str:
    """The random value `custom_packs` writes beside a pack file each time the pack is added or removed.

    It is part of what the registry row records, so a pack that was removed and added again is not the pack the
    owner switched on even when the file is byte for byte the same and no seeding ran in between (node stopped,
    or no ingest batch and no Fact packs page view). Without it the row still said enabled with a matching
    digest, and the pack came back switched on. A file with no stamp (dropped in by hand) has the empty one."""
    try:
        path = install_stamp_path(Path(str(getattr(pack, "source_path", "") or "")), pack.pack)
        return path.read_text("utf-8")[:64] if path.is_file() else ""
    except (OSError, ValueError):
        return "unreadable"


def definition_sha(pack: Pack, *, stamped: bool = True) -> Optional[str]:
    """What a pack from outside the package declares, as one digest; None for a pack that ships with the engine.

    A bundled pack changes only with a release. A pack the owner added from a file (`custom_packs`) can change
    whenever the file does, and the registry row is the only record of what the owner said yes to. The digest is
    over the parsed definition, so a comment or a reordered key is not a change, and over the install stamp, so
    removing a pack and adding it again is one. `stamped=False` is the definition alone: what `packs add`
    compares to say a file is already added, unchanged."""
    if getattr(pack, "first_party", False):
        return None
    try:
        body = json.dumps(getattr(pack, "raw", None) or {}, sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        # A parsed file json cannot order or walk. `custom_packs` refuses such a file before it loads; this
        # keeps the seeding of every other pack alive if one arrives another way.
        body = repr(getattr(pack, "raw", None))
    if stamped:
        body += "\n" + _install_stamp(pack)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def runs_as_switched_on(conn: sqlite3.Connection, pack: Pack) -> bool:
    """Is this pack, as it loads now, the one the registry row was written for? Always true for a bundled pack.

    For a pack the owner added, false until `seed_pack_registry` has recorded this definition, and that same
    seeding switches a changed pack off. So between a file changing and the next seeding nothing reads with a
    definition the owner has not switched on: `enabled_packs` and a backfill batch both ask this first."""
    sha = definition_sha(pack)
    if sha is None:
        return True
    try:
        row = conn.execute("SELECT definition_sha FROM pack_registry WHERE pack_id=?", (pack.pack,)).fetchone()
    except sqlite3.OperationalError:        # a registry that was never seeded by this code has no such column
        return False
    return bool(row) and row[0] == sha


def seed_pack_registry(conn: sqlite3.Connection, pack_dir: Path) -> None:
    """Idempotent: INSERT OR IGNORE every bundled pack; Wave A enabled, rest off.
    Never flips an existing row — the owner's enable/disable choices are theirs.

    The one exception is a pack the owner added from a file (origin `third_party`). It is inserted off like
    every pack outside Wave A, and its row is switched off again when the file's definition or its install stamp
    is no longer the one the row records, or when the file is gone or no longer loads: the owner's yes was to a
    definition, not to an id, so adding a changed pack (or the same id again after removing it) starts off."""
    # `origin` answers "where did this pack come from" AFTER seeding. Nothing could
    # answer that before: the registry is the runtime authority, so a policy keyed on
    # first-party-ness (D9) had nothing to read once the YAML was out of scope — and the
    # compute-time half of that rule reads the registry, never the file on disk.
    _add_column_if_missing(conn, "pack_registry", "origin",
                           "TEXT NOT NULL DEFAULT 'unknown'")
    # Enable-time consent for OUTWARD packs (decision 5, 2026-09-05). `set_pack_enabled`
    # was a bare UPDATE, so the one lane that writes facts about non-owners could be
    # switched on with no more ceremony than any other. The row now records when the
    # owner said yes and what they were told; absent means never consented.
    _add_column_if_missing(conn, "pack_registry", "consented_at", "TEXT")
    _add_column_if_missing(conn, "pack_registry", "consent_note", "TEXT")
    # Record only known activation times. Existing enabled rows remain NULL: neither
    # created_at nor updated_at proves when the owner last turned a pack on.
    _add_column_if_missing(conn, "pack_registry", "active_since", "TEXT")
    # All enable paths (manual and accepted offers) share this transition rule.
    # Repeated enables and catalog/version refreshes do not restart the clock.
    conn.execute("""CREATE TRIGGER IF NOT EXISTS pack_registry_active_since
        AFTER UPDATE OF enabled ON pack_registry
        WHEN OLD.enabled IS NOT NEW.enabled
        BEGIN
          UPDATE pack_registry
          SET active_since=CASE WHEN NEW.enabled=1 THEN datetime('now') ELSE NULL END
          WHERE pack_id=NEW.pack_id;
        END""")
    _add_column_if_missing(conn, "pack_registry", "definition_sha", "TEXT")
    packs = load_packs(pack_dir)
    for pid, pack in packs.items():
        origin = "first_party" if getattr(pack, "first_party", False) else "third_party"
        sha = definition_sha(pack)
        conn.execute(
            """INSERT OR IGNORE INTO pack_registry
               (pack_id, version, enabled, disclosure_default, origin, active_since, definition_sha)
               VALUES (?, ?, ?, ?, ?, CASE WHEN ?=1 THEN datetime('now') ELSE NULL END, ?)""",
            (pid, pack.version, 1 if pid in ENABLED_BY_DEFAULT else 0,
             getattr(pack, "disclosure_default", "owner_only") or "owner_only", origin,
             1 if pid in ENABLED_BY_DEFAULT else 0, sha),
        )
        if sha is not None:
            conn.execute(
                "UPDATE pack_registry SET enabled=0, definition_sha=?, updated_at=datetime('now')"
                " WHERE pack_id=? AND COALESCE(definition_sha,'')<>?", (sha, pid, sha))
        # Origin follows the file, not the row: a pack that moves between the shipped
        # directory and anywhere else has genuinely changed provenance, and a stale
        # 'first_party' is the one value here that must never persist by inertia.
        conn.execute(
            "UPDATE pack_registry SET origin=?, updated_at=datetime('now')"
            " WHERE pack_id=? AND origin<>?", (origin, pid, origin))
        # version follows the bundled catalog (upgrades are additive; the
        # ontology_version stamped on each fact preserves what actually wrote it)
        conn.execute(
            "UPDATE pack_registry SET version=?, updated_at=datetime('now')"
            " WHERE pack_id=? AND version<>?",
            (pack.version, pid, pack.version),
        )
    # An owner-added pack whose file is gone or no longer loads. Only rows this seeding could have loaded:
    # `pack_dir` decides which third-party packs are in reach, and a caller seeding another directory must not
    # switch off what it never looked for.
    from .packs import _is_bundled_catalog

    if _is_bundled_catalog(pack_dir):
        present = sorted(pid for pid, pack in packs.items() if not getattr(pack, "first_party", False))
        conn.execute(
            "UPDATE pack_registry SET enabled=0, updated_at=datetime('now')"
            " WHERE origin='third_party' AND enabled=1"
            + (f" AND pack_id NOT IN ({','.join('?' for _ in present)})" if present else ""), present)
    conn.commit()


def enabled_packs(conn: sqlite3.Connection, pack_dir: Path) -> Dict[str, Pack]:
    rows = conn.execute("SELECT pack_id FROM pack_registry WHERE enabled=1").fetchall()
    ids = [r[0] for r in rows]
    if not ids:
        return {}
    return {pid: pack for pid, pack in load_packs(pack_dir, only=ids).items()
            if runs_as_switched_on(conn, pack)}


def mark_pack_run(conn: sqlite3.Connection, pack_id: str, version: str) -> None:
    conn.execute(
        "UPDATE pack_registry SET last_run_at=datetime('now'), last_run_version=?"
        " WHERE pack_id=?",
        (version, pack_id),
    )


def bundled_pack_dir() -> Path:
    """The packs that ship inside the engine wheel (MANIFEST.in includes them)."""
    return Path(__file__).resolve().parent / "bundled_packs"
