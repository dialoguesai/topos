"""Counts of a node home before and after an upgrade, compared (T9 B7; pass rule: A2A-6 amendment 8, item 7).

Two verbs. Both print counts and fixed words only: never a row, a name, an id, a path of the home, or a key.

  collect --source-root <a STOPPED COPY of a node home> --out FILE
      The source root is the folder that holds `database.db` and `permissions-v2/`.
      What an upgrade must keep, as counts, read from the copy and written to FILE (0600):
        - reviews and assessments by kind and state: the review store's rows by document type, active or
          superseded, and the owner's opt-outs; the projection review store when the node has one; the entailment
          verdict store; and in the canonical database the interest label assessments, the interest relabels and
          the owner's "not mine" decisions;
        - grants by state and capability profile, from the policy ledger;
        - per share: whether it has a search index, the index's revision and content digest (hashes of what it was
          built under and of what it holds, grant_census.py's own), its state and its member count. A share is
          keyed by a short fingerprint of its grant id, so the two files can be matched without either holding it;
        - Off-limits entries, how many carry the carry step's note, and by rebuild state;
        - the carry step (`carry-contact-excludes-to-off-limits`): its own dry-run counts; how many entries a run
          would add now (below); and its upgrade-ledger row when it has run;
        - the node id and the node key id as fingerprints (SHA-256 over a fixed prefix and the value, 16 hex
          characters). The key FILE is never opened: only whether it is there.
      It refuses a live home. It refuses `~/.topos` and anything under it (by path and by inode), any folder that
      holds a node's socket, any folder whose sharing lock (`permissions-v2/protocol.lock`) or graph-rebuild lock
      is held, and any database with a `-wal`, `-shm` or `-journal` file beside it, which a running node always
      has. It connects to nothing. Every database is opened `mode=ro&immutable=1`. The home's config names its
      stores by the absolute paths of the LIVE home; only the file names are used, and a file that resolves
      outside the copy is refused.

  diff BEFORE AFTER [--expect-off-limits-gain N]
      Exit 1, naming what moved, when: a review or assessment count is lower (active or in total, any kind, any
      store, or a store that was there is gone); a share (a grant that was active) that had an index has none;
      the node id or the node key id fingerprint changed; Off-limits has fewer entries; the canonical database
      passed SQLite's quick check before and does not after; or, with N given, Off-limits gained anything but N,
      or a second run of the carry step would still add an entry. Equal or higher anywhere else passes. A count
      that is lower somewhere this verb does not judge is listed as "moved", not failed.

When `collect` refuses, it prints one fixed word and exits 2:
  live_store_refused, node_socket_present, node_lock_held   the folder is the live home, or a node may be running
      in it. Never count a live home. A socket file that only came along in a copy is removed from a DUPLICATE of
      the copy, never from the copy.
  copy_not_closed   a database has a write-ahead log or journal beside it: the node was running, or was copied
      before SQLite folded its log in, and the newest rows are only in the log. An immutable read would miss them.
      Duplicate the copy, and in the duplicate run `PRAGMA wal_checkpoint(TRUNCATE); PRAGMA journal_mode=DELETE;`
      on each such database with the sqlite3 shell, then count the duplicate (the test pins this).
  source_escapes_root   a store in the copy is a link to somewhere else.
  scratch_environment_required   the two variables below are not set to scratch paths.

"Would add". The carry step's dry run reports how many explicit excludes the node holds, not how many entries a
run would create: an excluded contact that is already Off-limits (the owner's own entry) is merged, and two
excluded contacts with one linked entity share an entry. `would_add` walks the step's own explicit excludes
through the step's own naming (`contact_excludes._entry`, `_identity`) and the store's own lookups, read-only,
and counts the entries that do not exist yet. Before an upgrade it is the gain to expect; after it, it is 0 exactly
when a second run would add none. tests/permissions_v2/test_upgrade_census_diff.py pins it against the real step.

Run (zsh; every flag its own token). The two variables are required because engine code is imported:
  export TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/upgrade_census_diff.py \\
      collect --source-root <stopped copy> --out <scratch>/before.json
  ... diff <scratch>/before.json <scratch>/after.json --expect-off-limits-gain <would_add of before.json>
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_support as cs  # noqa: E402

SCHEMA = "upgrade-census/v1"
CARRY_STEP_ID = "carry-contact-excludes-to-off-limits"
DURABLE = "permissions-v2"
DEFAULT_CANONICAL = "database.db"
DEFAULT_LEDGER = "ledger.db"
DEFAULT_REVIEWS = "evidence-reviews.db"            # runtime.DEFAULT_EVIDENCE_REVIEW_STORE
ENTAILMENT_STORE = "entailment-verdicts.db"        # entailment_grounding.STORE_NAME
OTHER = "other"

# Closed vocabularies: a value read from a store is printed only when it is one of these, so no string a store
# holds can reach the output. The test pins each against the engine constant it mirrors.
REVIEW_KINDS = {
    "topos-owner-evidence-review/v1": "owner_fact_review",
    "topos-owner-message-review/v1": "owner_message_review",
    "topos-machine-message-review/v1": "machine_message_assessment",
    "topos-fact-projection-review/v1": "owner_projection_review",
}
CAPABILITY = re.compile(r"permissions-beta/p2[abc]-v[0-9]{1,2}\Z")
ANSWER_MODES = ("only", "with_sources", "records")
INDEX_STATES = ("ready", "over_cap")                # what search_index publishes in an index's meta row
REBUILD_STATES = ("pending", "running", "complete", "failed")
LEDGER_STATUSES = ("pending", "pending_consent", "running", "done", "failed")
VERSION = re.compile(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}\Z")
#: Assessment and owner-decision tables the canonical database holds (interest_review.TABLE, interest_relabel.TABLE,
#: ownership.DECISIONS). Absent on a node that never used the lane: counted as None, which is not 0.
CANONICAL_ASSESSMENTS = {
    "interest_label_assessments": "interest_label_assessments",
    "interest_relabels": "interest_relabels",
    "owner_not_mine_decisions": "share_ownership_decisions",
}
DRY_RUN_COUNTS = ("contacts", "no_stored_choice", "unreadable", "stored_without_row_choice", "explicit_excludes",
                  "explicit_includes", "hidden_names")
NAMING_BRANCHES = ("linked_entity", "name", "handle", "contact_id_only")


def fingerprint(kind: str, value) -> str | None:
    """16 hex characters of SHA-256 over a fixed prefix and the value. None for no value."""
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(f"upgrade-census/{kind}\0{value}".encode("utf-8")).hexdigest()[:16]


def _word(value, allowed) -> str:
    return value if isinstance(value, str) and value in allowed else OTHER


# --- refusing a live home -------------------------------------------------------------------------------------

def _lock_held(path: Path) -> bool:
    """Whether another process holds an exclusive flock on `path`. Takes a shared lock for an instant and drops it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return False
    except OSError:
        raise cs.CensusRefused("node_lock_unreadable") from None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def refuse_live_home(source_root: Path) -> Path:
    """The real path of a home that is safe to count, or a refusal. Reads directory entries and lock state only."""
    root = cs.refuse_live(Path(source_root).expanduser().absolute())
    if not root.is_dir():
        raise cs.CensusRefused("source_root_missing")
    root = cs.refuse_live(Path(os.path.realpath(root)))
    durable = root / DURABLE
    for folder in (root, durable):
        if not folder.is_dir():
            continue
        for entry in os.scandir(folder):
            mode = entry.stat(follow_symlinks=False).st_mode
            # The owner socket a running node serves (topos/uds.py). Nothing here ever connects to one: a socket
            # in the folder is refusal enough, and a copy made with `cp` holds none.
            if stat.S_ISSOCK(mode):
                raise cs.CensusRefused("node_socket_present")
            # A database a process has open in WAL mode, which is every database of a running node.
            if stat.S_ISREG(mode) and entry.name.endswith(cs.SIDECARS):
                raise cs.CensusRefused("copy_not_closed")
    # The sharing runtime holds protocol.lock for the life of its process (permissions_v2/runtime.py), and a graph
    # rebuild holds <database>.rebuild.lock (features/entities/rebuild_subprocess.py).
    for lock in (durable / "protocol.lock", *sorted(root.glob("*.rebuild.lock"))):
        if _lock_held(lock):
            raise cs.CensusRefused("node_lock_held")
    return root


def _inside(root: Path, path: Path) -> Path:
    """`path`, which must resolve inside the copy: a link out of it could lead to the live home."""
    real = Path(os.path.realpath(path))
    if real != root and root not in real.parents:
        raise cs.CensusRefused("source_escapes_root")
    cs.refuse_live(real)
    return path


def _open(root: Path, path: Path) -> sqlite3.Connection:
    """Read-only and immutable, with plain tuples. Refuses a database that is not closed (`copy_not_closed`)."""
    conn = cs.ro(_inside(root, path), immutable=True)
    conn.row_factory = None
    return conn


def _has(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None


def _count(conn: sqlite3.Connection, table: str, where: str = "", args=()) -> int | None:
    if not _has(conn, table):
        return None
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where}" if where else ""), args).fetchone()[0])


# --- what is counted ---------------------------------------------------------------------------------------------

def _stores(root: Path) -> dict:
    """Where each store is IN THE COPY. The config's own paths are the live home's: only their names are used."""
    durable = root / DURABLE
    config_path = durable / "config.json"
    config = None
    if config_path.is_file():
        try:
            config = json.loads(_inside(root, config_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise cs.CensusRefused("node_config_unreadable") from None
        if not isinstance(config, dict):
            raise cs.CensusRefused("node_config_unreadable")

    def named(key: str, default: str | None) -> str | None:
        value = (config or {}).get(key)
        return Path(value).name if isinstance(value, str) and value else default

    canonical = root / (named("canonical_database_path", DEFAULT_CANONICAL) or DEFAULT_CANONICAL)
    if not canonical.is_file():
        canonical = root / DEFAULT_CANONICAL
    if not canonical.is_file():
        raise cs.CensusRefused("canonical_database_missing")
    projection = named("projection_review_store_path", None)
    key_name = named("node_signing_key_path", None)
    return {
        "config": config,
        "canonical": canonical,
        "ledger": durable / (named("ledger_path", DEFAULT_LEDGER) or DEFAULT_LEDGER),
        "reviews": durable / (named("evidence_review_store_path", DEFAULT_REVIEWS) or DEFAULT_REVIEWS),
        "projection_reviews": durable / projection if projection else None,
        "entailment": durable / ENTAILMENT_STORE,
        "index_root": durable / "message-search",
        "key_file": durable / key_name if key_name else None,
    }


def _review_kind(raw) -> str:
    try:
        document = json.loads(raw)
    except (TypeError, ValueError):
        return "unreadable"
    return REVIEW_KINDS.get(document.get("version") if isinstance(document, dict) else None, OTHER)


def review_store(root: Path, path: Path | None) -> dict | None:
    """One review store: rows by document type and state, and the owner's opt-outs. None when there is no store."""
    if path is None or not path.exists():
        return None
    conn = _open(root, path)
    try:
        by_kind: dict = {}
        if _has(conn, "fact_reviews"):
            for raw, active in conn.execute("SELECT review_json, active FROM fact_reviews"):
                slot = by_kind.setdefault(_review_kind(raw), {"active": 0, "superseded": 0})
                slot["active" if active else "superseded"] += 1
        return {"by_kind": dict(sorted(by_kind.items())), "opt_outs": _count(conn, "fact_opt_outs")}
    finally:
        conn.close()


def entailment_store(root: Path, path: Path) -> dict | None:
    if not path.exists():
        return None
    conn = _open(root, path)
    try:
        if not _has(conn, "entailment_verdicts"):
            return {"standing": None, "revoked": None}
        return {"standing": _count(conn, "entailment_verdicts", "revoked_at IS NULL"),
                "revoked": _count(conn, "entailment_verdicts", "revoked_at IS NOT NULL")}
    finally:
        conn.close()


def _profile(policy_json) -> str:
    """A grant's capability profile from its policy, as fixed words: the capability and its answer mode, if any."""
    try:
        policy = json.loads(policy_json)
    except (TypeError, ValueError):
        return OTHER
    if not isinstance(policy, dict):
        return OTHER
    capability = (policy.get("versions") or {}).get("capability") if isinstance(policy.get("versions"), dict) else None
    if not isinstance(capability, str) or not CAPABILITY.match(capability):
        return OTHER
    answers = (policy.get("search") or {}).get("answers") if isinstance(policy.get("search"), dict) else None
    return capability if answers is None else f"{capability} answers={_word(answers, ANSWER_MODES)}"


def grants_and_shares(root: Path, ledger: Path, index_root: Path) -> tuple:
    """(grants by state and profile, shares keyed by fingerprint, index files no grant names)."""
    import grant_census as gc
    from topos.permissions_v2.search_index import index_path

    if not ledger.exists():
        return None, {}, None
    by_state: dict = {"active": {}, "inactive": {}}
    shares: dict = {}
    named: set = set()
    conn = _open(root, ledger)
    try:
        rows = conn.execute(
            "SELECT g.grant_id, g.active, p.policy_json FROM p2a_grants g "
            "LEFT JOIN p2a_policies p ON p.version_id = g.version_id ORDER BY g.grant_id").fetchall()
    finally:
        conn.close()
    for grant_id, active, policy_json in rows:
        state, profile = ("active" if active else "inactive"), _profile(policy_json)
        by_state[state][profile] = by_state[state].get(profile, 0) + 1
        path = index_path(index_root, str(grant_id))
        named.add(path.name)
        index = {"present": False, "revision": None, "content_digest": None, "state": None, "members": None}
        if path.exists():
            iconn = _open(root, path)
            try:
                meta = iconn.execute("SELECT basis_json, state, member_count FROM meta WHERE singleton=1").fetchone()
                if meta is not None:
                    index = {"present": True, "revision": gc.index_revision_of(json.loads(meta[0])),
                             "content_digest": gc.index_content_digest(iconn),
                             "state": _word(meta[1], INDEX_STATES), "members": int(meta[2])}
            finally:
                iconn.close()
        shares[fingerprint("share", str(grant_id))] = {"state": state, "profile": profile, "index": index}
    unnamed = (sum(1 for path in index_root.glob("grant-*.db") if path.name not in named)
               if index_root.is_dir() else 0)
    return by_state, dict(sorted(shares.items())), unnamed


def carry_step(conn: sqlite3.Connection) -> dict:
    """The carry step read-only: its dry run, what a run would add now, and its ledger row. Counts only."""
    from topos.features.lifecycle import contact_excludes
    from topos.features.lifecycle.blackhole import BlackholeStore, normalize_entity_name

    preview = contact_excludes.carry_contact_excludes(conn, dry_run=True)
    counts = preview.get("counts") or {}
    dry_run = {name: int(counts.get(name) or 0) for name in DRY_RUN_COUNTS}
    named_by = {branch: int((preview.get("named_by") or {}).get(branch) or 0) for branch in NAMING_BRANCHES}

    # What a run would add now. The step, for each explicit exclude: looks the entry up by the reference it would
    # write (an entity id, or a name); when that finds nothing, `BlackholeStore.blackhole_entity` resolves the
    # reference to an entity and looks up the name it would store. A new entry is one neither lookup finds, counted
    # once however many contacts lead to it.
    would_add = already = 0
    if _has(conn, "entity_blackholes") and dry_run["explicit_excludes"]:
        store = BlackholeStore(conn)
        ids = {str(row[0]) for row in conn.execute("SELECT entity_id FROM entity_blackholes WHERE entity_id != ''")}
        names = {str(row[0]) for row in conn.execute("SELECT normalized_name FROM entity_blackholes")}
        for contact in contact_excludes.explicit_choices(conn)["excludes"]:
            reference = str(contact_excludes._entry(contact_excludes._identity(conn, contact))["entity_ref"]).strip()
            if reference in ids or normalize_entity_name(reference) in names:
                already += 1
                continue
            entity_id, canonical_name, _aliases = store._resolve_entity(reference)
            stored_name = normalize_entity_name(canonical_name or reference)
            if stored_name in names or stored_name in ids:
                already += 1
                continue
            would_add += 1
            names.add(stored_name)
            if entity_id:
                ids.add(str(entity_id))
    elif dry_run["explicit_excludes"]:
        would_add = None        # no Off-limits table to compare with: not a number this census can give

    ledger = None
    if _has(conn, "derivation_ledger"):
        rows = conn.execute("SELECT version, status, started_at, finished_at, detail_json FROM derivation_ledger "
                            "WHERE step_id=?", (CARRY_STEP_ID,)).fetchall()
        if rows:
            version, status, started_at, finished_at, detail_json = rows[-1]
            try:
                detail = json.loads(detail_json or "{}")
            except ValueError:
                detail = {}
            detail = detail if isinstance(detail, dict) else {}

            def number(name):
                return int(detail[name]) if isinstance(detail.get(name), int) else None
            ledger = {"rows": len(rows), "status": _word(status, LEDGER_STATUSES),
                      "version": version if isinstance(version, str) and VERSION.match(version) else OTHER,
                      "started_and_finished": bool(started_at) and bool(finished_at),
                      "carried": number("carried"), "already_off_limits": number("already_off_limits"),
                      "rebuilds_failed": number("rebuilds_failed")}
    return {"dry_run": dry_run, "named_by": named_by, "would_add": would_add, "already_off_limits": already,
            "ledger": ledger}


def off_limits(conn: sqlite3.Connection) -> dict:
    from topos.features.lifecycle.contact_excludes import NOTE

    if not _has(conn, "entity_blackholes"):
        return {"entries": None, "with_carry_note": None, "by_rebuild_state": {}}
    by_state: dict = {}
    for state, count in conn.execute("SELECT rebuild_state, COUNT(*) FROM entity_blackholes GROUP BY rebuild_state"):
        word = _word(state, REBUILD_STATES)
        by_state[word] = by_state.get(word, 0) + int(count)
    return {"entries": _count(conn, "entity_blackholes"),
            "with_carry_note": _count(conn, "entity_blackholes", "note = ?", (NOTE,)),
            "by_rebuild_state": dict(sorted(by_state.items()))}


def collect(source_root: Path) -> dict:
    """Every count, from a stopped copy. Raises CensusRefused for a home that is, or looks, live."""
    import census_copy as cc

    cs.require_scratch_environment()
    root = refuse_live_home(source_root)
    stores = _stores(root)
    config = stores["config"]
    canonical_counts = cc._counts(_inside(root, stores["canonical"]))
    grants, shares, unnamed = grants_and_shares(root, stores["ledger"], stores["index_root"])
    conn = _open(root, stores["canonical"])
    try:
        baseline = None
        if _has(conn, "engine_config"):
            row = conn.execute("SELECT value FROM engine_config WHERE key='engine.upgrade.baseline'").fetchone()
            baseline = row[0] if row and isinstance(row[0], str) and VERSION.match(row[0]) else None
        assessments = {name: _count(conn, table) for name, table in CANONICAL_ASSESSMENTS.items()}
        limits = off_limits(conn)
        carry = carry_step(conn)
    finally:
        conn.close()
    identity = (config or {}).get("identity") if isinstance((config or {}).get("identity"), dict) else {}
    key_file = stores["key_file"]
    return {
        "schema": SCHEMA,
        "collected_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "canonical": {"upgrade_baseline": baseline, **canonical_counts},
        "reviews": {
            "evidence": review_store(root, stores["reviews"]),
            "projection": review_store(root, stores["projection_reviews"]),
            "entailment_verdicts": entailment_store(root, stores["entailment"]),
            "canonical": assessments,
        },
        "grants": grants,
        "shares": shares,
        "index_files_no_grant_names": unnamed,
        "off_limits": limits,
        "carry_step": carry,
        "node": {
            "bound": config is not None,
            "node_id": fingerprint("node-id", identity.get("node_id")),
            "key_id": fingerprint("key-id", (config or {}).get("node_signing_kid")),
            # Whether the key file is in the copy. It is never opened.
            "key_file_present": bool(key_file is not None and os.path.lexists(key_file)),
        },
    }


# --- comparing two collections -----------------------------------------------------------------------------------

def _review_counts(census: dict) -> dict:
    """Every review and assessment count as {label: number or None}; a store that is absent has no labels."""
    out: dict = {}
    reviews = census.get("reviews") or {}
    for store in ("evidence", "projection"):
        held = reviews.get(store)
        if held is None:
            continue
        out[f"{store} review store"] = 1
        for kind, states in (held.get("by_kind") or {}).items():
            out[f"{store} reviews: {kind}, active"] = states.get("active", 0)
            out[f"{store} reviews: {kind}, all rows"] = states.get("active", 0) + states.get("superseded", 0)
        out[f"{store} reviews: owner opt-outs"] = held.get("opt_outs")
    verdicts = reviews.get("entailment_verdicts")
    if verdicts is not None:
        out["entailment verdict store"] = 1
        standing, revoked = verdicts.get("standing"), verdicts.get("revoked")
        out["entailment verdicts, standing"] = standing
        out["entailment verdicts, all rows"] = None if standing is None else standing + (revoked or 0)
    for name, count in (reviews.get("canonical") or {}).items():
        out[name.replace("_", " ")] = count
    return out


def _moved(counts_before: dict, counts_after: dict) -> tuple:
    """(lines, lower): every label with both numbers, and the labels whose number fell or disappeared."""
    lines, lower = [], []
    for label in sorted(set(counts_before) | set(counts_after)):
        before, after = counts_before.get(label), counts_after.get(label)
        fell = before is not None and (after is None or after < before)
        lines.append((label, before, after, fell))
        if fell:
            lower.append(label)
    return lines, lower


def _show(value) -> str:
    return "none" if value is None else str(value)


def compare(before: dict, after: dict, *, expect_off_limits_gain: int | None = None) -> tuple:
    """(failures, lines). `failures` name what moved the wrong way; `lines` is the whole comparison, counts only."""
    for census in (before, after):
        if not isinstance(census, dict) or census.get("schema") != SCHEMA:
            raise cs.CensusRefused("not_an_upgrade_census")
    failures, lines = [], []

    def judge(section: str, label: str, was, now, failed: bool, note: str = "") -> None:
        verdict = "FAIL" if failed else ("same" if was == now else "moved")
        lines.append(f"  {verdict:5} {section}: {label}: {_show(was)} -> {_show(now)}" + (f"  ({note})" if note else ""))
        if failed:
            failures.append(f"{section}: {label}: {_show(was)} -> {_show(now)}" + (f" ({note})" if note else ""))

    # 1. Reviews and assessments: no count may fall, and no store that was there may be gone.
    rows, _lower = _moved(_review_counts(before), _review_counts(after))
    for label, was, now, fell in rows:
        judge("reviews and assessments", label, was, now, fell, "lower" if fell else "")

    # 2. Shares: one that had an index must still have one. Revision, digest and members are reported, not judged:
    # the carry step moves the protection clock, so every index is rebuilt under a new revision, and an index may
    # hold fewer members once more people are Off-limits.
    # A share is a grant that was active: the node itself removes the index of a grant that is no longer.
    shares_before, shares_after = before.get("shares") or {}, after.get("shares") or {}
    had = [key for key, share in shares_before.items()
           if share.get("state") == "active" and (share.get("index") or {}).get("present")]
    lost = [key for key in had if not ((shares_after.get(key) or {}).get("index") or {}).get("present")]
    judge("shares", "active with a search index", len(had), len(had) - len(lost), bool(lost),
          f"{len(lost)} that had an index {'has' if len(lost) == 1 else 'have'} none" if lost else "")
    kept = [key for key in had if key not in lost]
    rebuilt = sum(1 for key in kept
                  if shares_before[key]["index"]["revision"] != shares_after[key]["index"]["revision"])
    lines.append(f"  note  shares: {rebuilt} of {len(kept)} kept indexes are under a new revision")
    judge("shares", "index members, those shares",
          sum(shares_before[key]["index"]["members"] or 0 for key in had),
          sum((shares_after.get(key, {}).get("index") or {}).get("members") or 0 for key in had), False)
    judge("shares", "known to the ledger", len(shares_before), len(shares_after), False)
    for state in ("active", "inactive"):
        profiles = sorted(set((before.get("grants") or {}).get(state) or {}) | set((after.get("grants") or {}).get(state) or {}))
        for profile in profiles:
            judge("grants", f"{state}, {profile}", ((before.get("grants") or {}).get(state) or {}).get(profile, 0),
                  ((after.get("grants") or {}).get(state) or {}).get(profile, 0), False)

    # 3. The node is the same node: its id and its key id.
    for label, key in (("node id", "node_id"), ("node key id", "key_id")):
        was, now = (before.get("node") or {}).get(key), (after.get("node") or {}).get(key)
        failed = was != now
        lines.append(f"  {'FAIL' if failed else 'same':5} node: {label} fingerprint: "
                     f"{'none' if was is None else 'present'} -> "
                     f"{'none' if now is None else ('unchanged' if not failed else 'DIFFERENT')}")
        if failed:
            failures.append(f"node: {label} fingerprint changed")
    judge("node", "key file in place", int(bool((before.get("node") or {}).get("key_file_present"))),
          int(bool((after.get("node") or {}).get("key_file_present"))), False)

    # 4. Off-limits: never fewer; and with the expected gain given, exactly that many more.
    was, now = (before.get("off_limits") or {}).get("entries"), (after.get("off_limits") or {}).get("entries")
    fewer = was is not None and (now is None or now < was)
    judge("Off-limits", "entries", was, now, fewer, "fewer" if fewer else "")
    judge("Off-limits", "entries with the carry step's note", (before.get("off_limits") or {}).get("with_carry_note"),
          (after.get("off_limits") or {}).get("with_carry_note"), False)
    carry_before, carry_after = before.get("carry_step") or {}, after.get("carry_step") or {}
    judge("carry step", "explicit excludes (its own dry run)", (carry_before.get("dry_run") or {}).get("explicit_excludes"),
          (carry_after.get("dry_run") or {}).get("explicit_excludes"), False)
    if expect_off_limits_gain is not None:
        gain = None if was is None or now is None else now - was
        wrong = gain != expect_off_limits_gain
        lines.append(f"  {'FAIL' if wrong else 'ok':5} Off-limits: gained {_show(gain)}, expected {expect_off_limits_gain}")
        if wrong:
            failures.append(f"Off-limits: gained {_show(gain)}, expected {expect_off_limits_gain}")
        # A2A-6 amendment 8, item 7: "a second run of the step adds none". Read, not run: see `would_add`.
        again = carry_after.get("would_add")
        lines.append(f"  {'FAIL' if again != 0 else 'ok':5} carry step: a second run would add {_show(again)}, expected 0")
        if again != 0:
            failures.append(f"carry step: a second run would add {_show(again)}, expected 0")
        lines.append(f"  note  carry step: before, its dry run counted "
                     f"{_show((carry_before.get('dry_run') or {}).get('explicit_excludes'))} explicit excludes and a "
                     f"run would have added {_show(carry_before.get('would_add'))}")
    else:
        judge("carry step", "entries a run would add", carry_before.get("would_add"), carry_after.get("would_add"), False)
    ledger = carry_after.get("ledger")
    if ledger is not None:
        lines.append(f"  note  carry step: ledger row {ledger.get('status')} under {ledger.get('version')}: carried "
                     f"{_show(ledger.get('carried'))}, already Off-limits {_show(ledger.get('already_off_limits'))}, "
                     f"rebuilds failed {_show(ledger.get('rebuilds_failed'))}")

    # 5. The canonical database: it must not stop passing SQLite's quick check. Its row counts, schema number and
    # upgrade baseline are listed, never failed.
    canonical_before, canonical_after = before.get("canonical") or {}, after.get("canonical") or {}
    broke = canonical_before.get("quick_check_ok") is True and canonical_after.get("quick_check_ok") is not True
    judge("canonical database", "passes the quick check", canonical_before.get("quick_check_ok"),
          canonical_after.get("quick_check_ok"), broke, "it passed before" if broke else "")
    for name in sorted((set(canonical_before) | set(canonical_after)) - {"quick_check_ok"}):
        was, now = canonical_before.get(name), canonical_after.get(name)
        lower = isinstance(was, int) and isinstance(now, int) and now < was
        judge("canonical database", name.replace("_", " "), was, now, False, "lower, not judged here" if lower else "")
    return failures, lines


# --- command line ------------------------------------------------------------------------------------------------

def _summary(census: dict) -> dict:
    """What `collect` prints: totals only. The file holds the breakdown."""
    reviews = census.get("reviews") or {}
    kinds = [states for store in ("evidence", "projection") for states in ((reviews.get(store) or {}).get("by_kind") or {}).values()]
    shares = census.get("shares") or {}
    carry = census.get("carry_step") or {}
    return {
        "schema": census["schema"],
        "upgrade_baseline": (census.get("canonical") or {}).get("upgrade_baseline"),
        "node_bound": (census.get("node") or {}).get("bound"),
        "reviews_active": sum(states.get("active", 0) for states in kinds),
        "reviews_all_rows": sum(states.get("active", 0) + states.get("superseded", 0) for states in kinds),
        "owner_opt_outs": (reviews.get("evidence") or {}).get("opt_outs"),
        "shares_active": sum(1 for share in shares.values() if share.get("state") == "active"),
        "shares_active_with_an_index": sum(1 for share in shares.values() if share.get("state") == "active"
                                           and (share.get("index") or {}).get("present")),
        "off_limits_entries": (census.get("off_limits") or {}).get("entries"),
        "carry_step_explicit_excludes": (carry.get("dry_run") or {}).get("explicit_excludes"),
        "carry_step_would_add": carry.get("would_add"),
    }


def _read(path: Path) -> dict:
    try:
        return json.loads(cs.refuse_live(Path(path).expanduser().absolute()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise cs.CensusRefused("census_file_unreadable") from None


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    verbs = parser.add_subparsers(dest="verb", required=True)
    collecting = verbs.add_parser("collect", help="count a STOPPED COPY of a node home (never a live one)")
    collecting.add_argument("--source-root", type=Path, required=True)
    collecting.add_argument("--out", type=Path, required=True)
    comparing = verbs.add_parser("diff", help="compare two collections; exit 1 when a judged count moved the wrong way")
    comparing.add_argument("before", type=Path)
    comparing.add_argument("after", type=Path)
    comparing.add_argument("--expect-off-limits-gain", type=int, default=None, metavar="N")
    args = parser.parse_args(argv)
    if args.verb == "collect":
        out = cs.refuse_live(args.out.expanduser().absolute())
        root = Path(os.path.realpath(args.source_root.expanduser().absolute()))
        real_out = Path(os.path.realpath(out.parent)) / out.name
        if real_out == root or root in real_out.parents:
            raise cs.CensusRefused("out_inside_source_root")     # the copy is evidence: nothing is written into it
        if out.exists():
            raise cs.CensusRefused("out_exists")
        census = collect(args.source_root)
        out.parent.mkdir(parents=True, exist_ok=True)
        cs.write_private(out, (json.dumps(census, sort_keys=True, indent=1) + "\n").encode("utf-8"))
        print(json.dumps(_summary(census), sort_keys=True))
        return 0
    failures, lines = compare(_read(args.before), _read(args.after),
                              expect_off_limits_gain=args.expect_off_limits_gain)
    print("\n".join(lines))
    if failures:
        print(f"upgrade_census_failed: {len(failures)} moved the wrong way")
        for failure in failures:
            print(f"  {failure}")
        return 1
    print("upgrade_census_ok")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except cs.CensusRefused as exc:
        print(json.dumps({"refused": str(exc)}), file=sys.stderr)
        sys.exit(2)
