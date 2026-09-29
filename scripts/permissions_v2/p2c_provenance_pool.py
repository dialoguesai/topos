"""Count-only probe of the native iMessage provenance pool on a COPY of a node's stores (RD0/RD1).

The p2c-v3 message pool rests on the recovery lane's enrollments
(`reconciliation_provenance.py`). Each is immutable, so the pool only drains: a
linked message leaves the grant's rolling window once its own event time is older
than the window, and messages that arrive after the enrollment never gain proof.
Separately, one move of the ingest source clock stales an enrollment and every
message it proved goes dark at once. This probe answers, from a copy:

- whether each enrollment is still current (state, source generation against the
  store's generation, completed job) and which source-clock trigger form the store
  carries (v1: every write to the watched tables; v2: `UPDATE OF` the watched columns);
- how many linked messages each enrollment holds, by UTC event day, and when the
  pool leaves the window (day-by-day in-window counts until zero);
- how many owner-sent iMessage rows have no link, by UTC event day: what a later
  recovery could still prove, and whether its window would meet the 1,000-row cap;
- headroom against the Off-limits boundary's 100,000-row table caps, and (with
  `--boundary`) the protected vocabulary length against the 8,000-character cap.

Output is aggregates only: integers, UTC dates, lane names and booleans. No message
id, dataset id, enrollment id, content, name or term is printed. Every database is
opened `mode=ro&immutable=1`, so the copy gains no -wal/-shm sidecar and is never
written. Refuses the owner's live `~/.topos` tree.

Run from the engine root with a scratch app environment (the `--boundary` check
imports the engine):
    TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env \\
    .venv/bin/python3 scripts/permissions_v2/p2c_provenance_pool.py --copy <copy dir> --boundary
where <copy dir> has the node's layout: database.db, permissions-v2/...
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

SCHEMA = "p2c-provenance-pool/v1"
SOURCE_TABLES = ("engine_config", "user_ingestion_sources", "source_settings", "source_runtime_installs")
BOUNDARY_TABLES = ("entity_blackholes", "entities", "entity_merge_tombstones", "contacts", "contact_identifiers",
                   "entity_mentions")
MAX_ROWS = 100_000            # entity_boundary.MAX_ROWS
MAX_PROTECTED_CHARS = 8_000   # automatic_message_review.MAX_PROTECTED_CHARS
NATIVE_CAP = 1_000            # native_imessage_probe: sent-by-me rows per recovery window
RECONCILIATION_LANE = "imessage-existing-comparison/v2"


def _live_home() -> Path:
    return (Path.home() / ".topos").resolve()


def _refuse_live(path: Path) -> None:
    resolved = Path(path).resolve()
    live = _live_home()
    if resolved == live or live in resolved.parents:
        raise SystemExit("refused: the probe reads copies only, never the live ~/.topos tree")


def _refuse_alias(path: Path) -> None:
    """A path outside the live tree can still be the live file: a hard link passes any path check.

    Only stat is used, so nothing under the live tree is opened. A copy is a file of its own.
    """
    info = os.stat(path)
    if info.st_nlink != 1:
        raise SystemExit("refused: the probe reads single-link copies only")
    live = _live_home() / "database.db"
    try:
        if os.path.samefile(path, live):
            raise SystemExit("refused: that file is the live database")
    except FileNotFoundError:
        pass


def open_ro(path: Path) -> sqlite3.Connection:
    """Read-only and immutable: no journal, no sidecar, no write, whatever the file's mode."""
    _refuse_live(path)
    _refuse_alias(path)
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    conn.execute("PRAGMA query_only=ON")
    return conn


def _engine_import_allowed() -> bool:
    """The engine may be imported only with a scratch database path, never one under the live tree."""
    raw = os.environ.get("TOPOS_DATABASE_PATH")
    if not raw:
        return False
    _refuse_live(Path(raw))
    return True


def _utc(value) -> datetime | None:
    """A canonical event_at cell as an aware UTC datetime, or None when it is not one."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _stamp(value) -> datetime | None:
    """A node-written UTC write time: ISO with an offset, or SQLite's `datetime('now')` form without one."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace(" ", "T", 1)
    if not (text.endswith("Z") or "+" in text[10:] or "-" in text[10:]):
        text += "+00:00"
    return _utc(text)


def _day(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%d")


def _minute(epoch_seconds) -> str | None:
    if type(epoch_seconds) is not int:
        return None
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _table_exists(conn, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def trigger_forms(conn) -> dict:
    """Per watched table: how many source-clock triggers, and whether its UPDATE trigger is column-scoped (v2)."""
    rows = conn.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name GLOB 'ingest_provenance_*'").fetchall()
    forms = {}
    for table in SOURCE_TABLES:
        mine = [(name, sql or "") for name, sql in rows if name.startswith(f"ingest_provenance_{table}_")]
        update = [sql for name, sql in mine if name.endswith("_update")]
        forms[table] = {"triggers": len(mine),
                        "update_form": None if not update else ("update_of" if " UPDATE OF " in update[0] else "any_update")}
    return forms


def expected_schema_version(conn) -> int | None:
    """Which pinned store schema (source clock v1 or v2) the copy's objects match exactly, if the engine imports.

    None without a scratch TOPOS_DATABASE_PATH: importing the app must not resolve the live database.
    """
    if not _engine_import_allowed():
        return None
    try:
        from topos.permissions_v2.ingest_provenance import IngestProvenanceService
    except Exception:  # noqa: BLE001 -- the stdlib half of the probe still runs
        return None
    found = dict(conn.execute("SELECT name, sql FROM sqlite_master WHERE name GLOB 'ingest_provenance_*'").fetchall())
    for version in (2, 1):
        # _schema reads only the connection; it never touches the instance.
        if found == IngestProvenanceService._schema(None, conn, version):
            return version
    return 0


def read_marker(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    _refuse_live(path)
    _refuse_alias(path)
    data = json.loads(path.read_text())
    return {key: data.get(key) for key in ("state", "revision", "generation", "source_clock_version")}


def pool_window_seconds(ledger: Path | None) -> list[dict]:
    """The rolling window of each active search grant, from a ledger copy. Capability and seconds only."""
    if ledger is None or not ledger.exists():
        return []
    conn = open_ro(ledger)
    try:
        out = []
        rows = conn.execute("SELECT g.active, p.policy_json FROM p2a_grants g JOIN p2a_policies p ON p.version_id=g.version_id").fetchall()
        for active, policy_json in rows:
            try:
                policy = json.loads(policy_json)
            except (TypeError, ValueError):
                continue
            search = policy.get("search") if isinstance(policy, dict) else None
            window = search.get("window") if isinstance(search, dict) else None
            if not isinstance(window, dict):
                continue
            capability = (policy.get("versions") or {}).get("capability")
            out.append({"active": bool(active), "capability": capability,
                        "max_age_seconds": window.get("max_age_seconds")})
        return out
    finally:
        conn.close()


def drain(days: Counter, *, now: datetime, window: timedelta, horizon_days: int = 45) -> dict:
    """In-window counts at each UTC midnight from today until the pool is empty (or the horizon)."""
    moments = sorted((datetime.fromisoformat(day + "T00:00:00+00:00"), count) for day, count in days.items())

    def in_window(at: datetime) -> int:
        # A message is inside the rolling window while its event time >= at - window. Whole
        # UTC days are compared at their start, so a day counts until at - window passes it.
        return sum(count for day, count in moments if day + timedelta(days=1) > at - window)

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    series, current = [], in_window(now)
    for offset in range(horizon_days + 1):
        at = today + timedelta(days=offset)
        count = in_window(at)
        series.append({"at": _day(at), "in_window": count})
        if count == 0:
            break
    half = next((row["at"] for row in series if current and row["in_window"] <= current / 2), None)
    zero = next((row["at"] for row in series if row["in_window"] == 0), None)
    return {"in_window_now": current, "halved_by": half, "empty_by": zero, "series": series}


def probe(copy: Path, *, now: datetime | None = None, window_seconds: int = 30 * 86400, boundary: bool = False) -> dict:
    copy = Path(copy)
    _refuse_live(copy)
    now = now or datetime.now(timezone.utc)
    database = copy / "database.db"
    marker_path = copy / "permissions-v2" / "ingest-snapshots.enrollment.json"
    ledger_candidates = sorted((copy / "permissions-v2").glob("*ledger*.db")) if (copy / "permissions-v2").is_dir() else []
    window = timedelta(seconds=window_seconds)
    report: dict = {"schema": SCHEMA, "probed_at": now.strftime("%Y-%m-%dT%H:%MZ"), "window_seconds": window_seconds}
    conn = open_ro(database)
    try:
        report["copy"] = {"database_bytes": database.stat().st_size,
                          "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
                          "user_version": conn.execute("PRAGMA user_version").fetchone()[0]}
        installed = _table_exists(conn, "ingest_provenance_state")
        store: dict = {"installed": installed, "marker": read_marker(marker_path),
                       "trigger_forms": trigger_forms(conn) if installed else None,
                       "schema_matches_clock_version": expected_schema_version(conn) if installed else None}
        store["generation"] = conn.execute("SELECT generation FROM ingest_provenance_state WHERE singleton=1").fetchone()[0] if installed else None
        report["store"] = store
        report["grant_windows"] = [item for path in ledger_candidates for item in pool_window_seconds(path)]

        enrollments, linked_days_all, linked_ids = [], Counter(), set()
        if installed:
            rows = conn.execute("SELECT enrollment_id, snapshot_json, dataset_id, revision, state, source_generation, "
                                "authorized_at, channel FROM ingest_provenance_enrollments ORDER BY authorized_at, enrollment_id").fetchall()
            for ordinal, (enrollment_id, snapshot_json, dataset_id, revision, state, source_generation, authorized_at, channel) in enumerate(rows, 1):
                try:
                    lane = json.loads(snapshot_json).get("reader_contract")
                    snapshot_bytes = json.loads(snapshot_json).get("snapshot_bytes")
                except (TypeError, ValueError, AttributeError):
                    lane, snapshot_bytes = None, None
                jobs = Counter(status for (status,) in conn.execute(
                    "SELECT status FROM ingest_provenance_jobs WHERE enrollment_id=?", (enrollment_id,)))
                links = conn.execute(
                    "SELECT r.message_id, r.enrollment_revision, r.row_identity, m.event_at, m.dataset_id, m.source_id "
                    "FROM ingest_provenance_records r LEFT JOIN conversation_messages m ON m.message_id=r.message_id "
                    "WHERE r.enrollment_id=?", (enrollment_id,)).fetchall()
                days, classified, present, dataset_match, revision_match, undated = Counter(), 0, 0, 0, 0, 0
                for message_id, link_revision, row_identity, event_at, row_dataset, row_source in links:
                    linked_ids.add(message_id)
                    if link_revision != revision:
                        # A refresh retires a link it could not re-prove: kept at its old revision,
                        # where it proves nothing. It is counted, never in the pool.
                        continue
                    revision_match += 1
                    try:
                        identity = json.loads(row_identity)
                        if isinstance(identity, dict) and identity.get("classification") is not None:
                            classified += 1
                    except (TypeError, ValueError):
                        pass
                    if row_source is None and row_dataset is None and event_at is None:
                        continue
                    present += 1
                    if row_dataset == dataset_id and row_source == "imessage":
                        dataset_match += 1
                    moment = _utc(event_at)
                    if moment is None:
                        undated += 1
                        continue
                    days[_day(moment)] += 1
                linked_days_all.update(days)
                first, last = (min(days), max(days)) if days else (None, None)
                enrollments.append({
                    "n": ordinal, "lane": lane, "state": state, "revision": revision,
                    "source_generation": source_generation,
                    "stale": bool(installed and store["generation"] is not None and source_generation != store["generation"]),
                    "authorized_at": _minute(authorized_at), "channel": channel, "snapshot_bytes": snapshot_bytes,
                    "jobs": dict(jobs), "linked": len(links), "linked_retired": len(links) - revision_match,
                    "linked_row_present": present,
                    "linked_dataset_match": dataset_match, "linked_revision_match": revision_match,
                    "linked_classified": classified, "linked_undated": undated,
                    "event_day_first": first, "event_day_last": last,
                    # The recovery request's ends_at is not stored. It lies between the newest linked
                    # message and the enrollment time (a window may not end in the future).
                    "interval_end_between": [last, _minute(authorized_at)],
                    "linked_by_event_day": dict(sorted(days.items())),
                    "drain": drain(days, now=now, window=window),
                })
        report["enrollments"] = enrollments
        report["pool"] = drain(linked_days_all, now=now, window=window)
        report["indexes"] = index_members(copy / "permissions-v2" / "message-search", now=now, window=window)

        # Owner-sent iMessage rows with no link: what a later recovery could still prove.
        unlinked = Counter()
        if _table_exists(conn, "conversation_messages"):
            for message_id, event_at in conn.execute(
                    "SELECT message_id, event_at FROM conversation_messages WHERE source_id='imessage' AND is_from_self=1"):
                if message_id in linked_ids:
                    continue
                moment = _utc(event_at)
                if moment is not None and moment >= now - window - timedelta(days=1):
                    unlinked[_day(moment)] += 1
        last_linked = max(linked_days_all) if linked_days_all else None
        after = {day: n for day, n in unlinked.items() if last_linked is None or day > last_linked}
        in_window_unlinked = sum(n for day, n in unlinked.items()
                                 if datetime.fromisoformat(day + "T00:00:00+00:00") + timedelta(days=1) > now - window)
        per_day = in_window_unlinked / max(window.days, 1)
        report["unlinked_owner_sent"] = {
            "by_event_day": dict(sorted(unlinked.items())),
            "in_window_now": in_window_unlinked,
            "after_last_linked_day": sum(after.values()),
            "max_per_day": max(unlinked.values()) if unlinked else 0,
            "mean_per_day": round(per_day, 1),
            # A recovery refuses a window holding more than 1,000 native sent-by-me rows. Canonical
            # owner-sent rows are a lower bound on that native count: the native read counts every
            # sent-by-me form in the window before its form rules drop reactions and the like.
            "window_days_under_native_cap_at_mean": int(NATIVE_CAP / per_day) if per_day else None,
        }

        # What moves the ingest source clock, and when it last did (dates only).
        clock_tables = {}
        since = None
        if enrollments and installed:
            latest = conn.execute("SELECT max(authorized_at) FROM ingest_provenance_enrollments").fetchone()[0]
            since = datetime.fromtimestamp(latest, tz=timezone.utc) if type(latest) is int else None
        for table in SOURCE_TABLES:
            if not _table_exists(conn, table):
                clock_tables[table] = None
                continue
            entry = {"rows": conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]}
            columns = _columns(conn, table)
            if "updated_at" in columns:
                stamps = [stamp for (value,) in conn.execute(f'SELECT updated_at FROM "{table}"')
                          if (stamp := _stamp(value)) is not None]
                entry["updated_at_last_day"] = _day(max(stamps)) if stamps else None
                if since is not None:
                    entry["updated_after_latest_enrollment"] = sum(stamp > since for stamp in stamps)
            if table == "source_runtime_installs" and {"is_active", "status"} <= columns:
                entry["active"] = conn.execute('SELECT count(*) FROM source_runtime_installs WHERE is_active=1').fetchone()[0]
            clock_tables[table] = entry
        report["source_clock_tables"] = clock_tables

        protection = None
        if _table_exists(conn, "permissions_v2_protection_state"):
            row = conn.execute("SELECT generation, contract_version FROM permissions_v2_protection_state WHERE singleton=1").fetchone()
            protection = {"generation": row[0], "contract_version": row[1]} if row else None
        report["protection_clock"] = protection

        caps = {}
        for table in BOUNDARY_TABLES:
            if _table_exists(conn, table):
                count = conn.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
                caps[table] = {"rows": count, "headroom": MAX_ROWS - count}
            else:
                caps[table] = None
        blackholes = caps.get("entity_blackholes")
        caps["boundary_active"] = bool(blackholes and blackholes["rows"] > 0)
        caps["max_rows"] = MAX_ROWS
        if boundary:
            caps["boundary"] = boundary_measure(conn)
        report["caps"] = caps
    finally:
        conn.close()
    return report


def index_members(root: Path, *, now: datetime, window: timedelta) -> list[dict]:
    """Per published grant index: member count, vector coverage and the members' drain.

    Reads only `meta.state`, `meta.member_count`, `members.event_at_us` and the vector row
    count. The term bags and sealed fields are content derivatives and are never selected.
    Files are listed by ordinal, never by their grant-derived names.
    """
    if not root.is_dir():
        return []
    out = []
    for ordinal, path in enumerate(sorted(root.glob("grant-*.db")), 1):
        conn = open_ro(path)
        try:
            state, count = conn.execute("SELECT state, member_count FROM meta WHERE singleton=1").fetchone()
            days, undated = Counter(), 0
            for (event_us,) in conn.execute("SELECT event_at_us FROM members"):
                if type(event_us) is not int:
                    undated += 1
                    continue
                days[_day(datetime.fromtimestamp(event_us / 1_000_000, tz=timezone.utc))] += 1
            with_vectors = conn.execute("SELECT count(DISTINCT opaque_id) FROM vectors").fetchone()[0]
            out.append({"n": ordinal, "state": state, "member_count": count, "members_with_vectors": with_vectors,
                        "undated": undated, "members_by_event_day": dict(sorted(days.items())),
                        "drain": drain(days, now=now, window=window)})
        finally:
            conn.close()
    return out


def boundary_measure(conn) -> dict:
    """Instantiate the engine's own EntityBoundary on the read-only copy. Sizes only, never a term."""
    if not _engine_import_allowed():
        raise SystemExit("refused: export TOPOS_DATABASE_PATH=<scratch>/throwaway.db before the engine import")
    from topos.permissions_v2.canonical import PolicyError
    from topos.permissions_v2.entity_boundary import EntityBoundary
    conn.row_factory = None
    try:
        conn.execute("BEGIN")
        found = EntityBoundary(conn)
    except PolicyError as exc:
        return {"available": False, "code": exc.code}
    finally:
        if conn.in_transaction:
            conn.rollback()
    terms = sorted(found.terms | found.handles)
    size = sum(map(len, terms))
    return {"available": True, "active": found.active, "protected_ids": len(found.ids),
            "contacts": len(found.contacts), "terms": len(found.terms), "handles": len(found.handles),
            "mentions": len(getattr(found, "mentions", ())), "mentions_headroom": MAX_ROWS - len(getattr(found, "mentions", ())),
            "vocabulary_chars": size, "vocabulary_cap": MAX_PROTECTED_CHARS,
            "vocabulary_headroom": MAX_PROTECTED_CHARS - size}


def write_private(path: Path, text: str) -> None:
    """A 0600 report outside the live tree; never through a symlink, never over a non-regular file."""
    _refuse_live(path)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SystemExit("refused: the report path is not a regular file")
        data = memoryview(text.encode("utf-8"))
        while data:
            data = data[os.write(fd, data):]
    finally:
        os.close(fd)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--copy", type=Path, required=True, help="a copy directory with the node's layout")
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--now", help="ISO time to project from (default: now, UTC)")
    parser.add_argument("--boundary", action="store_true", help="also size the Off-limits closure (imports the engine)")
    parser.add_argument("--out", type=Path, help="write the report here (0600) as well as stdout")
    args = parser.parse_args()
    now = datetime.fromisoformat(args.now).astimezone(timezone.utc) if args.now else None
    report = probe(args.copy, now=now, window_seconds=args.window_days * 86400, boundary=args.boundary)
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.out:
        write_private(args.out, text + "\n")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
