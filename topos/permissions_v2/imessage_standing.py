"""The owner's standing attestation for their own iMessages (owner decision 1, 1 Oct 2026).

Until now iMessage proof only grew when the owner sent the attestation sentence through the owner
socket, once to recover and again to refresh after syncs. Proof ages out of every grant window, so
without that command the pool ran dry. With this, the owner states once, at iMessage setup and through
a product surface (`put_source_settings`, field `proof_standing`), that the Messages accounts this Mac
sends from are theirs. From then on the node itself:

- enrolls every iMessage dataset that holds the owner's rows (the recovery, without its fact
  derivation, like the refresh), after checking the capture exactly first;
- refreshes every enrollment after each scheduled sync (`local_sync_schedule._settle_running`) and at
  least weekly, over the longest active grant window (`reconciliation_provenance.proof_coverage_seconds`):
  a dry run first, then the same capture for real. It never sets `accept_uncovered` or
  `accept_unproven`: a loss either would acknowledge refuses the run, and the owner is told.

**What the statement trusts.** A message is proven only as before: a sent-by-me row (`is_from_me` 1) of
the Messages database of the macOS user the node runs as, matching a canonical row of the same dataset
exactly (body, identity, sender, native time). The statement replaces the per-run attestation sentence
for rows whose Messages account the owner attested: every account identifier the row carries
(`message.account`, `message.account_guid`) must be one of those present on the owner's sent rows when
the owner made the statement and saw them listed. The record keeps only keyed digests of them.

**What it refuses.**
- A sent row whose account identifiers are not all attested, such as a second Apple ID signed in to
  Messages on the same Mac: the whole run refuses (`standing_account_unattested`) and writes nothing.
  The owner sees it, and can state again over the accounts listed then.
- A sent row with no account identifier: left out of the capture (`excluded_account_unknown`), never
  proven by this path. A Messages database with neither column cannot be attested at all
  (`standing_account_unavailable`).
- Any acknowledgement of a loss, any run whose dry run refuses, a record written for another owner,
  a statement other than `STANDING_STATEMENT`, and an attestation whose accounts changed between the
  owner's preview and their statement (`standing_accounts_changed`).
- Every owner operation except these four: the standing principal (`STANDING_CHANNEL`) passes the owner
  check only where the caller says so (`evidence._owner(standing=True)`: enroll, its install, publish,
  refresh). No request can carry it: it is set in this process, never resolved from a credential.

What it cannot detect: someone else using the owner's own account on this Mac sends as the owner; and
an account that is already signed in when the owner states is attested if the owner accepts the list
(the screen shows how many accounts and how many sent messages each, never the identifiers).

The record lives beside the ingest ledger, `permissions-v2/imessage-standing-attestation.json`, private
(0600, single link, no symlink), and is backed up with that directory. Disarming stops the automatic
runs; it revokes nothing already proven.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import time

from .canonical import PolicyError

STANDING_VERSION = "imessage-standing-attestation/v1"
STANDING_STATEMENT = ("I attest that the Messages accounts this screen lists are mine and that the messages they "
                      "send from this Mac are mine. Topos may prove them after each sync until I turn this off.")
#: The principal the node acts under for its automatic runs (evidence.STANDING_CHANNEL; a test pins the two).
#: Never resolved from a request.
STANDING_CHANNEL = "standing_attestation"
STANDING_CLIENT = "imessage_standing_attestation"
RECORD_NAME = "imessage-standing-attestation.json"
#: The native columns that name the Messages account a row was sent from.
ACCOUNT_COLUMNS = ("account", "account_guid")
#: At least this often, an armed node refreshes even when no scheduled sync imported anything.
CADENCE_SECONDS = 7 * 86400
#: After a run that could not start (busy, the ledger unreadable), the next try waits this long.
RETRY_SECONDS = 3600
_MAX_RECORD_BYTES = 64 * 1024
_MAX_SENT_ROWS = 50_000
_NATIVE_SECONDS = 10
_HEX64 = re.compile(r"[0-9a-f]{64}")
_STATES = ("previewed", "armed", "disarmed")
_log = logging.getLogger(__name__)


# -- the record ----------------------------------------------------------------------------------------------

def record_path(runtime) -> Path:
    return Path(runtime.protocol.canonical_database).parent / "permissions-v2" / RECORD_NAME


def _private_directory(directory: Path) -> None:
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise PolicyError("standing_record_invalid")


def read_record(path: Path):
    """The record, or None when there is none. Anything that is not exactly a private record refuses."""
    path = Path(path)
    _private_directory(path.parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077
            or info.st_size > _MAX_RECORD_BYTES):
        raise PolicyError("standing_record_invalid")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_RECORD_BYTES + 1)
    finally:
        os.close(fd)
    try:
        record = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise PolicyError("standing_record_invalid") from None
    if (type(record) is not dict or record.get("version") != STANDING_VERSION or record.get("state") not in _STATES
            or type(record.get("owner_id")) is not str or not record["owner_id"]
            or type(record.get("key")) is not str or not _HEX64.fullmatch(record["key"])
            or type(record.get("accounts")) is not list
            or any(type(item) is not str or not _HEX64.fullmatch(item) for item in record["accounts"])
            or record["accounts"] != sorted(set(record["accounts"]))
            or (record["state"] == "armed" and not record["accounts"])):
        raise PolicyError("standing_record_invalid")
    return record


def write_record(path: Path, record: dict) -> None:
    """Atomic, private and durable: a temporary file in the same directory, fsync, rename, fsync the directory."""
    path = Path(path)
    _private_directory(path.parent)
    temporary = path.with_name(path.name + "." + secrets.token_hex(8))
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary.exists():
            temporary.unlink()


def _digest(key: str, column: str, value: str) -> str:
    return hmac.new(bytes.fromhex(key), f"{column}\0{value}".encode("utf-8"), hashlib.sha256).hexdigest()


def _token(key: str, accounts) -> str:
    return hmac.new(bytes.fromhex(key), ("preview\0" + ",".join(accounts)).encode("ascii"), hashlib.sha256).hexdigest()


# -- the native accounts -------------------------------------------------------------------------------------

def _native_ns(unix_us: int) -> int:
    return (unix_us - 978307200 * 1_000_000) * 1000


def native_accounts(*, starts_us: int, ends_us: int, native_path=None) -> dict:
    """{ROWID: frozenset of (column, value)} for every sent-by-me row in [starts, ends). Read-only, bounded.

    A row's set holds only the account columns that carry a non-empty string; an empty set is a row whose
    account is unknown. Nothing here leaves the process: callers keep digests and counts only."""
    if type(starts_us) is not int or type(ends_us) is not int or starts_us >= ends_us:
        raise PolicyError("native_probe_window_invalid")
    path = Path(native_path) if native_path is not None else Path.home() / "Library" / "Messages" / "chat.db"
    db = None
    try:
        db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=1)
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("BEGIN")
        deadline = time.monotonic() + _NATIVE_SECONDS
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        schema = db.execute("SELECT type FROM sqlite_master WHERE name='message'").fetchone()
        if schema is None or schema[0] != "table" or any(row[6] != 0 for row in db.execute('PRAGMA table_xinfo("message")')):
            raise PolicyError("native_probe_schema_unsupported")
        columns = {row[1] for row in db.execute('PRAGMA table_info("message")')}
        if not {"ROWID", "date", "is_from_me"} <= columns:
            raise PolicyError("native_probe_schema_unsupported")
        present = [column for column in ACCOUNT_COLUMNS if column in columns]
        if not present:
            raise PolicyError("standing_account_unavailable")
        # The names come from the closed tuple above, never from the native schema.
        sql = ("SELECT ROWID," + ",".join('"' + column + '"' for column in present)
               + " FROM message WHERE is_from_me=1 AND date>=? AND date<? ORDER BY ROWID LIMIT ?")
        rows = db.execute(sql, (_native_ns(starts_us), _native_ns(ends_us), _MAX_SENT_ROWS + 1)).fetchall()
        if len(rows) > _MAX_SENT_ROWS:
            raise PolicyError("native_probe_message_limit")
        return {rowid: frozenset((column, value) for column, value in zip(present, values)
                                 if type(value) is str and value.strip() and len(value) <= 512)
                for rowid, *values in rows if type(rowid) is int}
    except PolicyError:
        raise
    except (sqlite3.Error, OSError, UnicodeError):
        raise PolicyError("native_probe_unavailable") from None
    finally:
        if db is not None:
            db.close()


def _accounts_view(key: str, rows: dict):
    """The digests of every identifier on these rows, and a count-only view of them for the owner's screen."""
    digests, groups, unknown = set(), Counter(), 0
    for identifiers in rows.values():
        if not identifiers:
            unknown += 1
            continue
        names = {column: _digest(key, column, value) for column, value in identifiers}
        digests.update(names.values())
        groups[names.get("account_guid") or names.get("account")] += 1
    view = {"accounts": len(groups), "sent_messages": sorted(groups.values(), reverse=True),
            "sent_without_account": unknown}
    return sorted(digests), view


# -- the owner's surface -------------------------------------------------------------------------------------

def _owner_principal(runtime):
    from topos.principal import OWNER_APP, current_principal
    principal = current_principal()
    owner_id = runtime.protocol.ledger.identity.owner_id
    if (principal is None or principal.cls != OWNER_APP or principal.channel not in {"uds", "cp_relay"}
            or (principal.acting_user and principal.acting_user != owner_id)
            or (principal.channel == "cp_relay" and principal.acting_user != owner_id)):
        raise PolicyError("owner_authority_required")
    return principal, owner_id


def _window(runtime, now: int):
    from .reconciliation_provenance import proof_coverage_seconds
    coverage = proof_coverage_seconds(runtime.protocol.ledger, now)
    return coverage, (now - coverage) * 1_000_000, now * 1_000_000


def status(runtime) -> dict:
    """Counts and states only, for the owner's settings screen. Never the digests, the key or a path."""
    record = read_record(record_path(runtime))
    if record is None or record["owner_id"] != runtime.protocol.ledger.identity.owner_id:
        return {"state": "off", "statement": STANDING_STATEMENT}
    return {"state": "armed" if record["state"] == "armed" else "off", "statement": STANDING_STATEMENT,
            "attested_at": record.get("attested_at"), "accounts": record.get("account_count", 0),
            "last_run": record.get("last_run")}


def preview(runtime, *, now=None, native_path=None) -> dict:
    """What the owner would attest: the accounts their sent rows carry over the coverage window, as counts.

    Writes the preview into the record (with the record's key, made now if there is none) and returns a
    token the statement must carry back, so the owner attests exactly the accounts they saw."""
    _principal, owner_id = _owner_principal(runtime)
    now = int(time.time()) if now is None else now
    path = record_path(runtime)
    record = read_record(path)
    if record is None or record["owner_id"] != owner_id:
        record = {"version": STANDING_VERSION, "state": "previewed", "owner_id": owner_id,
                  "key": secrets.token_hex(32), "accounts": []}
    _coverage, starts_us, ends_us = _window(runtime, now)
    digests, view = _accounts_view(record["key"], native_accounts(starts_us=starts_us, ends_us=ends_us,
                                                                   native_path=native_path))
    if not digests:
        raise PolicyError("standing_account_unavailable")
    record = {**record, "preview": {"accounts": digests, "at": now, "view": view}}
    write_record(path, record)
    return {**view, "accounts_token": _token(record["key"], digests), "statement": STANDING_STATEMENT,
            "state": "armed" if record["state"] == "armed" else "off",
            "matches_attested": record["state"] == "armed" and digests == record["accounts"]}


def arm(runtime, *, statement, accounts_token, now=None, native_path=None) -> dict:
    """The owner's statement over the accounts of their last preview. Refuses if they changed since."""
    principal, owner_id = _owner_principal(runtime)
    if statement != STANDING_STATEMENT:
        raise PolicyError("standing_statement_required")
    now = int(time.time()) if now is None else now
    path = record_path(runtime)
    record = read_record(path)
    previewed = (record or {}).get("preview")
    if record is None or record["owner_id"] != owner_id or type(previewed) is not dict:
        raise PolicyError("standing_preview_required")
    if type(accounts_token) is not str or not hmac.compare_digest(accounts_token, _token(record["key"], previewed["accounts"])):
        raise PolicyError("standing_accounts_changed")
    _coverage, starts_us, ends_us = _window(runtime, now)
    digests, view = _accounts_view(record["key"], native_accounts(starts_us=starts_us, ends_us=ends_us,
                                                                   native_path=native_path))
    if digests != previewed["accounts"]:
        raise PolicyError("standing_accounts_changed")
    record = {key: value for key, value in record.items() if key != "preview"}
    record.update(state="armed", accounts=digests, account_count=view["accounts"], attested_at=now,
                  channel=principal.channel, statement_sha256=hashlib.sha256(statement.encode("utf-8")).hexdigest(),
                  last_run=None)
    write_record(path, record)
    return status(runtime)


def disarm(runtime) -> dict:
    """Stops the automatic runs. Revokes nothing already proven."""
    _principal, owner_id = _owner_principal(runtime)
    path = record_path(runtime)
    record = read_record(path)
    if record is not None and record["owner_id"] == owner_id and record["state"] == "armed":
        write_record(path, {**record, "state": "disarmed"})
    return status(runtime)


# -- the automatic runs --------------------------------------------------------------------------------------

@contextmanager
def standing_principal(owner_id: str):
    """The node acting on the owner's standing statement. Set only here; no request resolves to it."""
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal
    token = set_principal(Principal(cls=OWNER_APP, channel=STANDING_CHANNEL, client_id=STANDING_CLIENT,
                                    acting_user=owner_id))
    try:
        yield
    finally:
        reset_principal(token)


def owner_datasets(conn, owner_id: str) -> list:
    """Every iMessage dataset that holds a sent-by-me row of this owner (or of no recorded owner)."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversation_messages'").fetchone() is None:
        return []
    return [row[0] for row in conn.execute(
        "SELECT DISTINCT dataset_id FROM conversation_messages WHERE source_id='imessage' AND is_from_self=1 "
        "AND (owner_user_id IS NULL OR owner_user_id=?) AND dataset_id IS NOT NULL ORDER BY dataset_id", (owner_id,))]


def _iso(unix_us: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(microseconds=unix_us)).isoformat(timespec="microseconds")


def _capture(service, db, *, dataset_id, owner_id, starts_us, ends_us, skip):
    from datetime import datetime, timezone
    from .native_imessage_probe import capture_matching_snapshot
    db.execute("PRAGMA query_only=ON")
    db.execute("BEGIN")
    try:
        return capture_matching_snapshot(db, snapshot_root=service.root, dataset_id=dataset_id, owner_id=owner_id,
                                         starts_at=_iso(starts_us), ends_at=_iso(ends_us),
                                         now=datetime.now(timezone.utc), skip=skip)
    finally:
        db.rollback()
        db.execute("PRAGMA query_only=OFF")


def _checked_exactly(service, db, snapshot_id, dataset_id, owner_id):
    """The enrollment's dry run: every captured row compares exactly against its canonical row, on one read."""
    from datetime import datetime, timezone
    from .imessage_reconciliation import FORMS_CONTRACT, compare_existing_message, parse_reconciliation_snapshot
    from .reconciliation_provenance import _canonical_row
    description, data = service._snapshot(snapshot_id, FORMS_CONTRACT)
    records = parse_reconciliation_snapshot(data, now=datetime.now(timezone.utc), reader_contract=FORMS_CONTRACT)
    db.execute("BEGIN")
    try:
        for record in records:
            compare_existing_message(_canonical_row(db, record.message_id), record, dataset_id=dataset_id,
                                     owner_id=owner_id)
    finally:
        db.rollback()
    return description, len(records)


def _maintain_dataset(service, db, *, dataset_id, owner_id, coverage, starts_us, ends_us, accounts):
    """Enroll or refresh one dataset. Counts only; raises a PolicyError to refuse this dataset."""
    from .imessage_reconciliation import FORMS_CONTRACT, RECONCILIATION_CONTRACTS
    from .ingest_provenance import OWNER_ATTESTATION, IngestProvenanceService, _read_json
    from .reconciliation_provenance import publish_existing, refresh_existing
    try:
        IngestProvenanceService._source_enabled(db, dataset_id)
    except PolicyError as exc:
        if exc.code == "ingest_source_disabled":
            return {"skipped": "source_disabled"}  # the owner switched this dataset off
        raise
    ledger = db.execute("SELECT 1 FROM sqlite_master WHERE name='ingest_provenance_enrollments'").fetchone() is not None
    found = []
    if ledger:
        found = db.execute("SELECT enrollment_id,snapshot_json,state,source_generation FROM ingest_provenance_enrollments "
                           "WHERE dataset_id=?", (dataset_id,)).fetchall()
    if found and _read_json(found[0][1]).get("reader_contract") not in RECONCILIATION_CONTRACTS:
        return {"skipped": "another_lane"}
    if found and found[0][2] != "active":
        return {"skipped": "revoked"}  # the owner revoked it; only the owner enrolls it again
    enrollment_id = found[0][0] if found else None
    if enrollment_id is not None and db.execute("SELECT 1 FROM ingest_provenance_jobs WHERE enrollment_id=?",
                                                (enrollment_id,)).fetchone() is None:
        # An earlier automatic enrollment whose publication failed: publish its own capture now.
        return {"published": publish_existing(service, db, enrollment_id=enrollment_id)["reconciled"]}

    def skip(message_id):
        # A row another enrollment proves stays its; a row whose account is unknown is never proven here.
        link = db.execute("SELECT enrollment_id FROM ingest_provenance_records WHERE message_id=?",
                          (message_id,)).fetchone() if ledger else None
        if link is not None and link[0] != enrollment_id:
            return "row_owned_elsewhere"
        try:
            rowid = int(message_id.split(":", 1)[1])
        except (IndexError, ValueError):
            return "account_unknown"
        return None if accounts.get(rowid) else "account_unknown"
    try:
        snapshot_id, measured = _capture(service, db, dataset_id=dataset_id, owner_id=owner_id, starts_us=starts_us,
                                         ends_us=ends_us, skip=skip)
    except PolicyError as exc:
        if exc.code == "reconciliation_empty":
            # Nothing of the owner's in the window matches exactly (or every row's account is unknown): there is
            # nothing to enroll or re-prove. Not a refusal: existing links age out of the window as they would.
            return {"skipped": "nothing_to_prove"}
        raise
    created = {"snapshot_id": snapshot_id, "reader_contract": FORMS_CONTRACT}
    try:
        if enrollment_id is None:
            description, checked = _checked_exactly(service, db, snapshot_id, dataset_id, owner_id)
            enrollment = service.enroll(db, snapshot_id=snapshot_id, dataset_id=dataset_id,
                                        snapshot_sha256=description["snapshot_sha256"],
                                        owner_attestation=OWNER_ATTESTATION, reader_contract=FORMS_CONTRACT)
            created = None  # the enrollment names the capture now
            result = publish_existing(service, db, enrollment_id=enrollment["enrollment_id"])
            return {"enrolled": 1, "checked": checked, "linked_new": result["reconciled"], "counts": measured["counts"]}
        description, _ = service._snapshot(snapshot_id, FORMS_CONTRACT)
        arguments = dict(dataset_id=dataset_id, snapshot_id=snapshot_id, snapshot_sha256=description["snapshot_sha256"],
                         owner_attestation=OWNER_ATTESTATION, window_start_us=starts_us, window_end_us=ends_us,
                         coverage_seconds=coverage)
        preview = refresh_existing(service, db, dry_run=True, **arguments)
        # Every refresh moves the protection clock, so one that would change nothing is not made: only
        # re-proven links (with the ceilings they carry, and links already retired), on an enrollment that
        # is current and read by the current reader.
        changes = {key for key, value in preview.items()
                   if value and key not in ("dry_run", "reproven", "ceiling_carried", "still_retired")}
        generation = db.execute("SELECT generation FROM ingest_provenance_state WHERE singleton=1").fetchone()[0]
        current = found[0][3] == generation and _read_json(found[0][1]).get("reader_contract") == FORMS_CONTRACT
        if not changes and current:
            return {"unchanged": 1, "dry_run": preview, "counts": measured["counts"]}
        applied = refresh_existing(service, db, **arguments)
        created = None
        return {"dry_run": preview, "refresh": applied, "counts": measured["counts"]}
    finally:
        if created is not None:
            _discard(service, db, created)


def _discard(service, db, created):
    """A capture this run made and no enrollment names is deleted. Before the first enrollment there is no
    ledger to name it (`discard_capture` then deletes nothing), so it is deleted directly; it holds message text."""
    from .ingest_provenance import _lane
    from .reconciliation_provenance import discard_capture
    try:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='ingest_provenance_enrollments'").fetchone() is None:
            (service.root / (created["snapshot_id"] + _lane(created["reader_contract"]).suffix)).unlink()
            return True
    except Exception:  # noqa: BLE001 -- best effort, as discard_capture
        return False
    return discard_capture(service, db, created)


def _record_run(path, record, run):
    try:
        write_record(path, {**record, "last_run": run})
    except Exception as exc:  # noqa: BLE001 -- the run stands; class name only
        _log.warning("standing attestation run not recorded (%s)", type(exc).__name__)


def maintain(runtime, *, reason, now=None, native_path=None) -> dict:
    """One automatic pass over every iMessage dataset of the owner. Counts only; never raises.

    Runs only under an armed record of this node's owner, one at a time with the owner's own recovery
    and refresh (their lock), and records its outcome in the record for the owner's screen."""
    from topos.api.permissions_native_probe import _RECOVERY_LOCK, _resync_search
    try:
        path = record_path(runtime)
        record = read_record(path)
        owner_id = runtime.protocol.ledger.identity.owner_id
    except Exception as exc:  # noqa: BLE001 -- nothing to run against
        return {"ran": False, "reason": getattr(exc, "code", type(exc).__name__)}
    if record is None or record["state"] != "armed":
        return {"ran": False, "reason": "not_armed"}
    now = int(time.time()) if now is None else now
    run = {"at": now, "reason": reason}
    if record["owner_id"] != owner_id:
        run.update(outcome="refused", refusal="standing_owner_changed")
        _record_run(path, record, run)
        return {"ran": False, **run}
    if not _RECOVERY_LOCK.acquire(blocking=False):
        return {"ran": False, "reason": "busy"}
    db = None
    try:
        coverage, starts_us, ends_us = _window(runtime, now)
        accounts = native_accounts(starts_us=starts_us, ends_us=ends_us, native_path=native_path)
        attested = set(record["accounts"])
        foreign = sum(1 for identifiers in accounts.values()
                      if identifiers and not all(_digest(record["key"], *item) in attested for item in identifiers))
        if foreign:
            run.update(outcome="refused", refusal="standing_account_unattested", unattested_rows=foreign)
            return {"ran": True, **run}
        run["rows_without_account"] = sum(1 for identifiers in accounts.values() if not identifiers)
        service = runtime.ingestion()
        db = runtime.ingestion_connection()
        datasets, committed = {}, False
        with standing_principal(owner_id):
            for dataset_id in owner_datasets(db, owner_id):
                try:
                    datasets[dataset_id] = _maintain_dataset(
                        service, db, dataset_id=dataset_id, owner_id=owner_id, coverage=coverage,
                        starts_us=starts_us, ends_us=ends_us, accounts=accounts)
                    committed = committed or any(key in datasets[dataset_id] for key in ("enrolled", "refresh", "published"))
                except PolicyError as exc:
                    datasets[dataset_id] = {"refused": exc.code}
                except Exception as exc:  # noqa: BLE001 -- one dataset never stops the others; class name only
                    datasets[dataset_id] = {"refused": "standing_run_failed", "error": type(exc).__name__}
        run.update(outcome="refused" if any("refused" in item for item in datasets.values()) else "ok",
                   coverage_days=coverage // 86400, datasets=datasets)
        if committed:
            run["search"] = _resync_search(runtime)
        return {"ran": True, **run}
    except PolicyError as exc:
        run.update(outcome="refused", refusal=exc.code)
        return {"ran": True, **run}
    except Exception as exc:  # noqa: BLE001 -- never raises into the scheduler; class name only
        run.update(outcome="refused", refusal="standing_run_failed", error=type(exc).__name__)
        return {"ran": True, **run}
    finally:
        if db is not None:
            db.close()
        _RECOVERY_LOCK.release()
        if "outcome" in run:
            _record_run(path, record, run)


def due(record, now: int) -> bool:
    """Whether an armed record wants a run without a sync: never run since the statement, a week since the
    last, or an hour since a run that could not read what it needed."""
    if record is None or record.get("state") != "armed":
        return False
    last = record.get("last_run")
    if type(last) is not dict or type(last.get("at")) is not int:
        return True
    if type(record.get("attested_at")) is int and last["at"] < record["attested_at"]:
        return True
    if last.get("refusal") in ("reconciliation_coverage_unavailable", "native_probe_unavailable", "standing_run_failed"):
        return now - last["at"] >= RETRY_SECONDS
    return now - last["at"] >= CADENCE_SECONDS


def _runtime():
    from .runtime import get_runtime
    runtime = get_runtime()
    runtime.ingestion()  # the ingest snapshots must be configured, or nothing here can run
    return runtime


def after_scheduled_sync(dataset_id: str, outcome: str) -> dict:
    """The scheduler's hook (`local_sync_schedule._settle_running`): a run after every settled iMessage sync
    that imported rows. Never raises."""
    if outcome != "imported":
        return {"ran": False, "reason": "nothing_imported"}
    try:
        runtime = _runtime()
    except Exception as exc:  # noqa: BLE001 -- permissions v2 off or not configured: nothing to do
        return {"ran": False, "reason": getattr(exc, "code", type(exc).__name__)}
    return maintain(runtime, reason="scheduled_sync")


def run_if_due(now=None) -> dict:
    """The scheduler's tick: a run when the record is due (see `due`). Never raises."""
    now = int(time.time()) if now is None else now
    try:
        runtime = _runtime()
        record = read_record(record_path(runtime))
    except Exception as exc:  # noqa: BLE001
        return {"ran": False, "reason": getattr(exc, "code", type(exc).__name__)}
    if not due(record, now):
        return {"ran": False, "reason": "not_due"}
    return maintain(runtime, reason="due", now=now)
