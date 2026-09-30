"""Owner-local and read-only on the live node: ONE consistent copy of every store that
permission eligibility reads, for the WS1 grant census and its read-only readers.

Plan: audits/2026-09-14-permissions/latency-coverage-2026-09-28/PLAN_FORWARD_2026-09-28.md §4.2.

The stores, by role (paths relative to the directory that holds the canonical database; every
one is named by the code that reads it):

  canonical_db            database.db (+ its -wal)       evidence.py EvidenceResolver._read; also holds the
                                                          ingest-provenance ledger tables (ingest_provenance_*)
  node_config             permissions-v2/config.json     runtime.py load_runtime (binding, ledger path)
  policy_ledger           permissions-v2/ledger.db       ledger.py PolicyLedger._authority
  evidence_review_store   permissions-v2/evidence-reviews.db          runtime.py DEFAULT_EVIDENCE_REVIEW_STORE
  evidence_review_marker  permissions-v2/evidence-reviews.db.enrollment.json   evidence_review_runtime.py
  ingest_marker           permissions-v2/ingest-snapshots.enrollment.json      ingest_provenance.py _marker_read
  native_snapshot         permissions-v2/ingest-snapshots/<snapshot>.db|.json  ingest_provenance.py _snapshot
  grant_index             permissions-v2/message-search/grant-<h>.db   search_index.py index_path
  record_keys             permissions-v2/message-search/keys.db        opaque_ids.py RecordKeys (PRIVATE dir)
  canonical_floor         permissions-v2/canonical-floor.json          runtime.py canonical_floor (if present)

Never copied: the node signing key, protocol.lock, backups, logs.

Method. SQLite stores go through the online backup API in ONE step (a single read transaction,
so each is a consistent image of a running node) from a `mode=ro` connection; the copies are
then checkpointed and switched to journal_mode=DELETE so later read-only opens grow no
sidecars. Native snapshots are byte copies: eligibility re-hashes their exact bytes against the
enrollment's pinned SHA-256 and requires mode 0400, so they must stay byte-identical. Order:
review marker, review store, review marker again; ingest marker, ledger, canonical database,
ingest marker again; then index files, keys and snapshots. The set is then checked with the
engine's own digests: review store identity, generation and authority digest against its
marker; the ingest ledger through IngestProvenanceService._check_locked; every enrolled snapshot
through _snapshot; every index basis against the ledger authority, the clock and the review
digest. A disagreement retakes the whole copy, at most three times; after that the copy is void.

Keys go to a separate per-run private directory, never into the shared copy (plan §4.2).
Files are 0400 inside 0700 directories once placed. Prints one JSON object of roles, byte
counts, booleans and fixed codes; never a name, identifier, path of owner data, or content.

Run (zsh; every flag its own token):
  export TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/census_copy.py \\
      --dest-root <candidates>/census-copy --private-root <candidates>/census-private
  ... --size-only   lists and sizes the inputs against free disk and copies nothing
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import sqlite3
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_support as cs  # noqa: E402

GIB = 1 << 30
FLOOR_BYTES = int(1.5 * GIB)
MAX_RETAKES = 3
SCHEMA = "census-copy/v1"


def _stores(source: Path) -> dict:
    """Role -> list of absolute source paths, from the node config and the fixed layout."""
    durable = source / "permissions-v2"
    config = json.loads((durable / "config.json").read_text())
    canonical = Path(config["canonical_database_path"])
    if canonical != source / "database.db":
        raise cs.CensusRefused("canonical_path_unexpected")
    ledger = Path(config["ledger_path"])
    if ledger.parent != durable:
        raise cs.CensusRefused("ledger_path_unexpected")
    reviews = Path(config.get("evidence_review_store_path") or durable / "evidence-reviews.db")
    if reviews.parent != durable:
        raise cs.CensusRefused("review_store_path_unexpected")
    index_root = durable / "message-search"
    snapshots = durable / "ingest-snapshots"
    found = {
        "canonical_db": [canonical],
        "node_config": [durable / "config.json"],
        "policy_ledger": [ledger],
        "evidence_review_store": [reviews],
        "evidence_review_marker": [reviews.with_name(reviews.name + ".enrollment.json")],
        "ingest_marker": [durable / "ingest-snapshots.enrollment.json"],
        "canonical_floor": [durable / "canonical-floor.json"],
        "grant_index": sorted(index_root.glob("grant-*.db")) if index_root.is_dir() else [],
        "record_keys": [index_root / "keys.db"],
        "native_snapshot": sorted(p for p in snapshots.iterdir()
                                  if p.is_file() and not p.name.startswith(".")
                                  and not p.name.endswith(cs.SIDECARS)) if snapshots.is_dir() else [],
    }
    return {role: [p for p in paths if p.exists()] for role, paths in found.items()}, config


def _size(paths) -> int:
    total = 0
    for path in paths:
        total += path.stat().st_size
        if path.suffix == ".db":
            for suffix in ("-wal",):
                side = Path(str(path) + suffix)
                if side.exists():
                    total += side.stat().st_size
    return total


def _backup(source: Path, dest: Path) -> None:
    """One-step online backup from a read-only source; the copy is left in DELETE journal mode."""
    fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(fd)
    src = cs.ro(source)
    dst = sqlite3.connect(str(dest))
    try:
        src.backup(dst, pages=-1)
    finally:
        src.close()
    try:
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = dst.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise cs.CensusRefused("copy_journal_mode")
    finally:
        dst.close()
    for suffix in cs.SIDECARS:
        if Path(str(dest) + suffix).exists():
            raise cs.CensusRefused("copy_not_closed")


def _read_bytes(path: Path) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read()
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != after.st_size:
        raise cs.CensusRefused("source_changed_during_read")
    return data


def _copy_bytes(data: bytes, dest: Path, mode: int) -> None:
    cs.write_private(dest, data, mode=mode)


def _counts(path: Path) -> dict:
    conn = cs.ro(path, immutable=True)
    try:
        out = {}
        for table in ("conversation_messages", "ai_chat_messages", "signal_objects", "journal_entries", "activity_events"):
            try:
                out[table] = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            except sqlite3.Error:
                out[table] = None
        out["quick_check_ok"] = conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        out["user_version"] = conn.execute("PRAGMA user_version").fetchone()[0]
        return out
    finally:
        conn.close()


def consistency(copy_root: Path, live_canonical: str, keys_path: Path | None, now: int) -> dict:
    """Cross-store agreement on the copy, with the engine's own digests. Booleans and codes only."""
    from topos.permissions_v2.canonical import PolicyError, canonical_bytes
    from topos.permissions_v2.entity_boundary import EntityBoundary
    from topos.permissions_v2.evidence import EvidenceResolver, EvidenceReviewStore
    from topos.permissions_v2.evidence_review_runtime import ReviewEnrollment
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService, _read_json
    from topos.permissions_v2.protection_clock import clock_state, current_protection_revision
    from topos.permissions_v2.search_index import basis_of, index_path
    from topos.permissions_v2.search_contract import CAPABILITY_KNOWLEDGE_SEARCH, DIRECT_SEARCH_CAPABILITIES, SEARCH_CAPABILITIES

    copy_db = copy_root / "database.db"
    durable = copy_root / "permissions-v2"
    config = cs.load_config(copy_root)
    binding = cs.binding_from_config(config)
    result = {"clock": None, "review": {}, "ingest": {}, "ledger": {}, "index": {}, "snapshots": {}}
    with cs.copy_session(copy_db, live_canonical) as counters:
        resolver = EvidenceResolver(copy_db, binding=binding)
        revision = resolver._file_revision()
        conn = cs.ro(copy_db)
        try:
            conn.execute("BEGIN")
            clock_id, generation = clock_state(conn)
            result["clock"] = {"generation": generation}
            try:
                floor = current_protection_revision(conn, owner_id=binding.owner_id)
            except PolicyError as exc:
                floor, result["protection_code"] = None, exc.code
            # Review store and its external marker.
            rdb = cs.ro(durable / "evidence-reviews.db")
            rdb.row_factory = None  # the store's own connections stream plain tuples into its digest
            try:
                row = rdb.execute("SELECT binding_json,file_revision,clock_id,highest_generation,store_id FROM review_identity WHERE singleton=1").fetchone()
                binding_json = canonical_bytes(binding.model_dump()).decode("ascii")
                review_digest = EvidenceReviewStore._authority_digest(rdb)
                marker = ReviewEnrollment.parse((durable / "evidence-reviews.db.enrollment.json").read_bytes())
                result["review"] = {
                    "identity_ok": row is not None and tuple(row[:3]) == (binding_json, revision, clock_id),
                    "generation_not_ahead": row is not None and row[3] <= generation,
                    "generation_lag": (generation - row[3]) if row is not None else None,
                    "marker_active": marker.state == "active",
                    "marker_digest_ok": marker.authority_digest == review_digest,
                    "marker_store_ok": marker.store_id == (row[4] if row else None),
                    "marker_revision_ok": marker.canonical_file_revision == revision and marker.binding == binding,
                    "active_reviews": rdb.execute("SELECT count(*) FROM fact_reviews WHERE active=1").fetchone()[0],
                    "opt_outs": rdb.execute("SELECT count(*) FROM fact_opt_outs").fetchone()[0],
                }
            finally:
                rdb.close()
            # Ingest-provenance ledger (tables in the canonical copy) against its marker.
            ingest = {}
            try:
                service = IngestProvenanceService(canonical_database=copy_db, binding=binding,
                                                  snapshot_root=durable / "ingest-snapshots")
                ingest["generation"] = service._check_locked(conn)
                ingest["ok"] = True
                ingest["marker_generation"] = service._marker["generation"]
                snapshots_ok, snapshots_total = 0, 0
                for (raw,) in conn.execute("SELECT snapshot_json FROM ingest_provenance_enrollments WHERE state='active'"):
                    descriptor = _read_json(raw)
                    snapshots_total += 1
                    try:
                        if service._snapshot(descriptor["snapshot_id"], descriptor["reader_contract"])[0] == descriptor:
                            snapshots_ok += 1
                    except PolicyError as exc:
                        result["snapshots"].setdefault("codes", {}).setdefault(exc.code, 0)
                        result["snapshots"]["codes"][exc.code] += 1
                result["snapshots"].update({"active_enrollments": snapshots_total, "matching": snapshots_ok})
            except PolicyError as exc:
                ingest.update({"ok": False, "code": exc.code})
            result["ingest"] = ingest
            # Ledger: search grants, their authority, and each index basis.
            lconn = cs.ro(durable / "ledger.db")
            try:
                from topos.permissions_v2.ledger import PolicyLedger
                ledger = object.__new__(PolicyLedger)
                node = lconn.execute("SELECT protection_revision FROM p2a_node WHERE singleton=1").fetchone()
                result["ledger"]["protection_synced"] = node is not None and floor is not None and node[0] == floor
                search_grants = []
                inactive = 0
                for (grant_id,) in lconn.execute("SELECT grant_id FROM p2a_grants"):
                    try:
                        authority, policy = PolicyLedger._authority(ledger, lconn, grant_id, now)
                    except PolicyError:
                        inactive += 1
                        continue
                    if policy.versions.capability in SEARCH_CAPABILITIES:
                        search_grants.append((grant_id, authority, policy))
                result["ledger"].update({"grants_inactive_or_expired": inactive, "active_search_grants": len(search_grants),
                                         "active_knowledge_search_grants": sum(1 for _, _, p in search_grants
                                             if p.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH)})
                index_root = durable / "message-search"
                files = sorted(index_root.glob("grant-*.db")) if index_root.is_dir() else []
                result["index"] = {"files": len(files), "basis_ok": 0, "basis_mismatch": 0, "orphan": 0, "members": {}}
                boundary = EntityBoundary(conn)
                named = {index_path(index_root, grant_id).name: (grant_id, authority, policy)
                         for grant_id, authority, policy in search_grants}
                for path in files:
                    if path.name not in named:
                        result["index"]["orphan"] += 1
                        continue
                    grant_id, authority, policy = named[path.name]
                    iconn = cs.ro(path, immutable=True)
                    try:
                        meta = iconn.execute("SELECT basis_json,state,member_count FROM meta WHERE singleton=1").fetchone()
                        vectors = iconn.execute("SELECT count(DISTINCT opaque_id) FROM vectors").fetchone()[0]
                    finally:
                        iconn.close()
                    basis = json.loads(meta[0])
                    expected = basis_of(authority, clock=(clock_id, generation), boundary_revision=boundary.revision)
                    if policy.versions.capability in DIRECT_SEARCH_CAPABILITIES:
                        expected["message_review_revision"] = review_digest
                    if policy.versions.capability == CAPABILITY_KNOWLEDGE_SEARCH:
                        from topos.permissions_v2.automatic_message_review import MODEL_REVISION, rubric_revision
                        expected["automatic_rubric_revision"] = rubric_revision()
                        expected["automatic_model_revision"] = MODEL_REVISION
                    same = ({k: v for k, v in basis.items() if k != "protection_revision"}
                            == {k: v for k, v in expected.items() if k != "protection_revision"})
                    result["index"]["basis_ok" if same else "basis_mismatch"] += 1
                    result["index"]["members"][f"grant_{len(result['index']['members'])}"] = {
                        "state": meta[1], "member_count": meta[2], "with_vectors": vectors}
            finally:
                lconn.close()
        finally:
            conn.close()
        result["keys_present"] = keys_path is not None and keys_path.exists()
        result["session"] = {"aliased_revisions": counters.aliased_revisions,
                             "ingest_marker_publishes_held_in_memory": counters.ingest_marker_publishes_held_in_memory,
                             "lineage_key_completions_skipped": counters.lineage_key_completions_skipped}
    review = result["review"]
    result["consistent"] = bool(
        review.get("identity_ok") and review.get("generation_not_ahead") and review.get("marker_active")
        and review.get("marker_digest_ok") and review.get("marker_store_ok") and review.get("marker_revision_ok")
        and result["ingest"].get("ok")
        and result["snapshots"].get("matching") == result["snapshots"].get("active_enrollments")
        and result["index"].get("basis_mismatch", 0) == 0)
    return result


def take(source: Path, dest: Path, keys_dir: Path | None, stores: dict, *, keys: bool = True) -> dict:
    """One attempt. Returns the per-file manifest and the marker agreement flags."""
    durable = dest / "permissions-v2"
    cs.private_dir(dest)
    cs.private_dir(durable)
    cs.private_dir(durable / "ingest-snapshots")
    cs.private_dir(durable / "message-search")
    files, flags, timings = [], {}, {}

    def place(role, src, rel, how, data=None, mode=0o600):
        target = dest / rel if role != "record_keys" else keys_dir / "keys.db"
        started = time.monotonic()
        if how == "backup":
            _backup(src, target)
        else:
            _copy_bytes(data if data is not None else _read_bytes(src), target, mode)
        timings[role] = round(timings.get(role, 0) + time.monotonic() - started, 2)
        files.append({"role": role, "rel": str(target.relative_to(dest)) if role != "record_keys" else "<private>/keys.db",
                      "path": target, "bytes": target.stat().st_size})

    review_marker_1 = _read_bytes(stores["evidence_review_marker"][0])
    place("evidence_review_store", stores["evidence_review_store"][0], "permissions-v2/evidence-reviews.db", "backup")
    review_marker_2 = _read_bytes(stores["evidence_review_marker"][0])
    flags["review_marker_stable"] = review_marker_1 == review_marker_2
    place("evidence_review_marker", None, "permissions-v2/evidence-reviews.db.enrollment.json", "bytes", review_marker_2)
    ingest_marker_1 = _read_bytes(stores["ingest_marker"][0]) if stores["ingest_marker"] else None
    place("policy_ledger", stores["policy_ledger"][0], "permissions-v2/ledger.db", "backup")
    place("canonical_db", stores["canonical_db"][0], "database.db", "backup")
    copied_at = int(time.time())
    ingest_marker_2 = _read_bytes(stores["ingest_marker"][0]) if stores["ingest_marker"] else None
    flags["ingest_marker_stable"] = ingest_marker_1 == ingest_marker_2
    if ingest_marker_2 is not None:
        place("ingest_marker", None, "permissions-v2/ingest-snapshots.enrollment.json", "bytes", ingest_marker_2)
    for path in stores["grant_index"]:
        place("grant_index", path, f"permissions-v2/message-search/{path.name}", "backup")
    for path in (stores["record_keys"] if keys else ()):   # a keyless copy (OD-20 daily) never takes the grant key
        place("record_keys", path, None, "backup")
    for path in stores["native_snapshot"]:
        if any(Path(str(path) + suffix).exists() for suffix in cs.SIDECARS):
            raise cs.CensusRefused("snapshot_not_closed")
        mode = stat.S_IMODE(path.stat().st_mode)
        place("native_snapshot", path, f"permissions-v2/ingest-snapshots/{path.name}", "bytes", mode=mode)
    place("node_config", stores["node_config"][0], "permissions-v2/config.json", "bytes")
    for path in stores["canonical_floor"]:
        place("canonical_floor", path, "permissions-v2/canonical-floor.json", "bytes")
    return {"files": files, "flags": flags, "copied_at": copied_at, "timings_s": timings}


def make_copy(source: Path, dest_root: Path, private_root: Path | None, *, run_id: str | None = None, keys: bool = True,
              stores: dict | None = None, sleep=time.sleep) -> dict:
    """One consistent copy (at most MAX_RETAKES retakes) and its manifest. Returns the public report (roles, bytes,
    booleans); `void: True` when no attempt agreed. With keys=False no private directory is made and no key is read."""
    if stores is None:
        stores, _config = _stores(source)
    if not stores["evidence_review_store"] or not stores["evidence_review_marker"]:
        raise cs.CensusRefused("review_store_missing")
    dest_root = cs.refuse_live(Path(dest_root).expanduser().absolute())
    cs.private_dir(dest_root)
    run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    dest = dest_root / run_id
    if dest.exists():
        raise cs.CensusRefused("run_exists")
    keys_dir = None
    if keys:
        if private_root is None:
            raise cs.CensusRefused("private_root_required")
        private_root = cs.refuse_live(Path(private_root).expanduser().absolute())
        cs.private_dir(private_root)
        keys_dir = cs.private_dir(private_root / (run_id + "-" + secrets.token_hex(8)))
    attempts = []
    for attempt in range(1 + MAX_RETAKES):
        started = time.monotonic()
        taken = take(source, dest, keys_dir, stores, keys=keys)
        checked = consistency(dest, str(stores["canonical_db"][0]), keys_dir / "keys.db" if keys_dir else None,
                              taken["copied_at"])
        consistent = checked["consistent"] and all(taken["flags"].values())
        attempts.append({"attempt": attempt + 1, "consistent": consistent, "flags": taken["flags"],
                         "seconds": round(time.monotonic() - started, 1)})
        if consistent:
            break
        shutil.rmtree(dest)
        for leftover in (keys_dir.iterdir() if keys_dir else ()):
            cs.shred(leftover)
        sleep(20)
    else:
        return {"run_id": run_id, "attempts": attempts, "void": True}
    canonical_counts = _counts(dest / "database.db")
    manifest_files = []
    for item in taken["files"]:
        path = item["path"]
        manifest_files.append({"role": item["role"], "rel": item["rel"], "bytes": item["bytes"],
                               "sha256": cs.sha256_file(path)})
        if item["role"] != "native_snapshot":
            os.chmod(path, 0o400)
    manifest = {"schema": SCHEMA, "run_id": run_id, "copied_at": taken["copied_at"],
                "copied_at_utc": datetime.fromtimestamp(taken["copied_at"], timezone.utc).isoformat(),
                "live_canonical_path": str(stores["canonical_db"][0]),
                "method": "sqlite_online_backup_one_step+delete_journal; byte_copy_for_snapshots_markers_config",
                "files": manifest_files, "keys_dir": str(keys_dir) if keys_dir else None, "consistency": checked,
                "attempts": attempts, "canonical_counts": canonical_counts, "timings_s": taken["timings_s"]}
    cs.write_private(dest / "census-copy-manifest.json", json.dumps(manifest, sort_keys=True, indent=1).encode(), mode=0o400)
    os.chmod(dest, 0o700)
    return {"schema": SCHEMA, "run_id": run_id, "path": str(dest), "copied_at_utc": manifest["copied_at_utc"],
            "attempts": attempts, "files": [{"role": f["role"], "bytes": f["bytes"]} for f in manifest_files],
            "copy_bytes": sum(f["bytes"] for f in manifest_files), "free_bytes_after": shutil.disk_usage(dest).free,
            "consistency": checked, "canonical_counts": canonical_counts, "timings_s": taken["timings_s"]}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-root", type=Path, default=cs.LIVE_HOME)
    parser.add_argument("--dest-root", type=Path)
    parser.add_argument("--private-root", type=Path)
    parser.add_argument("--run-id")
    parser.add_argument("--size-only", action="store_true")
    parser.add_argument("--no-keys", action="store_true", help="never take the grant key (the OD-20 daily run)")
    parser.add_argument("--floor-bytes", type=int, default=FLOOR_BYTES)
    args = parser.parse_args(argv)
    cs.require_scratch_environment()
    source = args.source_root.expanduser().resolve()
    stores, _config = _stores(source)
    sizes = {role: {"files": len(paths), "bytes": _size(paths)} for role, paths in stores.items()}
    total = sum(item["bytes"] for item in sizes.values())
    probe = args.dest_root if args.dest_root is not None else source
    free = shutil.disk_usage(probe if probe.exists() else probe.parent).free
    report = {"schema": SCHEMA, "inputs": sizes, "input_bytes": total, "free_bytes_before": free,
              "free_bytes_after_estimate": free - total, "floor_bytes": args.floor_bytes,
              "fits": free - total >= args.floor_bytes}
    if args.size_only:
        print(json.dumps(report, sort_keys=True))
        return 0
    if not report["fits"]:
        report["refused"] = "disk_floor"
        print(json.dumps(report, sort_keys=True))
        return 2
    if not stores["evidence_review_store"] or not stores["evidence_review_marker"]:
        raise cs.CensusRefused("review_store_missing")
    made = make_copy(source, args.dest_root, args.private_root, run_id=args.run_id, keys=not args.no_keys, stores=stores)
    if made.get("void"):
        report.update(made)
        print(json.dumps(report, sort_keys=True))
        return 3
    print(json.dumps(made, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except cs.CensusRefused as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        sys.exit(2)
