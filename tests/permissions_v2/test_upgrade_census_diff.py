"""Counts of a node home before and after an upgrade, compared (scripts/permissions_v2/upgrade_census_diff.py; T9 B7).

protects: the 1.4.4 to 1.5.0 upgrade of a real node is judged from two stopped copies of its home (A2A-6 amendment
8, item 7): no review or assessment lost, every share that had an index still has one, the same node id and key,
Off-limits never smaller, and exactly the entries the carry step should add. The tool that judges it reads a copy
of a real person's home, so it must also print nothing but counts, never open a key, never follow the copy's
config back to the live home, never write into the copy, and refuse a home that is, or looks, live.

Every home here is built in the test's own temporary folder from invented rows: by hand for the systematic cases,
and once from stores the node's own code wrote (a signed grant, a machine review, a built index), with the real
carry step run between the two collections. Every id, person and handle is invented.
"""
from __future__ import annotations

import fcntl
import hashlib
import importlib
import json
import os
import runpy
import secrets
import shutil
import socket
import sqlite3
import sys
import typing
from pathlib import Path

import pytest

from tests.permissions_v2.test_grant_census import built
from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_search import node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.features.lifecycle import contact_excludes
from topos.features.lifecycle.blackhole import BlackholeStore
from topos.features.lifecycle.contact_excludes import NOTE, STEP_ID, carry_contact_excludes
from topos.permissions_v2 import (automatic_message_review, entailment_grounding, evidence, identity, interest_relabel,
                                  interest_review, knowledge_contract, ledger as ledger_module, message_evidence,
                                  ownership, runtime as runtime_module, search_index)
from topos.permissions_v2.search_index import index_path, root_for
from topos.storage.db.migrations import apply_all_migrations

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts" / "permissions_v2"


def _load(name, directory=SCRIPTS):
    """The scripts import each other by name from their own directory, as they do when run."""
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))
    return importlib.import_module(name)


ucd = _load("upgrade_census_diff")
cs = _load("census_support")
fixture = _load("build_upgrade_fixture", REPO / "scripts")

EXCLUDES = [choice for choice in fixture.CONTACT_CHOICES if choice.case == fixture.CARRIED]

# --- an invented home -------------------------------------------------------------------------------------------

NODE_ID, KEY_ID, OWNER_ID = "node-invented-quartz", "kid-invented-onyx", "owner-invented-heron"
OWNERS_OWN_ENTRY = "Halcyon Verity-Marsh"
#: (grant id, active, answer mode, index members or None for no index)
GRANTS = (("grant-invented-alpha", 1, "only", 12), ("grant-invented-bravo", 1, "only", 7),
          ("grant-invented-charlie", 0, None, None))
#: (review document version, active, how many)
REVIEWS = (("topos-owner-message-review/v1", 1, 2), ("topos-owner-message-review/v1", 0, 1),
           ("topos-machine-message-review/v1", 1, 5), ("topos-machine-message-review/v1", 0, 2),
           ("topos-owner-evidence-review/v1", 1, 1), ("topos-review-of-a-kind-not-known/v9", 1, 1))
OPT_OUTS, VERDICTS_STANDING, VERDICTS_REVOKED = 2, 3, 1
INTEREST_ASSESSMENTS, INTEREST_RELABELS, NOT_MINE = 4, 2, 3


def _identity():
    return {"environment_id": "permissions-beta-invented", "node_id": NODE_ID, "resource_id": "resource-invented-wren",
            "owner_id": OWNER_ID}


def _write_index(root: Path, grant_id: str, members: int, *, generation: int = 4) -> Path:
    """A search index file as the node publishes one, holding `members` invented members."""
    index_root = root / "permissions-v2" / "message-search"
    index_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = index_path(index_root, grant_id)
    path.unlink(missing_ok=True)                      # a rebuild publishes a new file
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    for sql in search_index._DDL:
        conn.execute(sql)
    basis = {"grant_id": grant_id, "clock_generation": generation, "format": search_index.FORMAT}
    conn.execute("INSERT INTO meta VALUES (1, ?, ?, 'ready', NULL, NULL, ?)",
                 (search_index.FORMAT, json.dumps(basis, sort_keys=True), members))
    for number in range(members):
        conn.execute("INSERT INTO members VALUES (?, ?, ?, ?, ?)",
                     (f"r.{grant_id}-member-{number}", 1_700_000_000_000_000 + number, 4, "{}", b"sealed"))
    conn.commit()
    conn.close()
    return path


def make_home(root: Path, *, live: Path) -> Path:
    """One invented node home, closed, laid out as a real one. Its config names the stores at `live`, the place a
    copy's config still points to; nothing there may ever be opened."""
    durable = root / "permissions-v2"
    durable.mkdir(parents=True, mode=0o700)

    conn = sqlite3.connect(root / "database.db")
    fixture.seed_canonical_source(conn)               # three invented messages, as the upgrade fixture holds
    apply_all_migrations(conn)
    fixture.seed_contact_choices(conn)                # the older model's stored choices: eight explicit excludes
    conn.execute("CREATE TABLE IF NOT EXISTS engine_config (key TEXT PRIMARY KEY, value TEXT NOT NULL, "
                 "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")
    conn.execute("INSERT OR REPLACE INTO engine_config (key, value) VALUES ('engine.upgrade.baseline', '1.4.4')")
    interest_review.install(conn)
    interest_relabel.install(conn)
    ownership._install_decisions(conn)
    for number in range(INTEREST_ASSESSMENTS):
        conn.execute(f"INSERT INTO {interest_review.TABLE} VALUES (?, ?, ?, '{{}}', 1)",
                     (f"label-revision-{number}", OWNER_ID, f"cluster-invented-{number}"))
    for number in range(INTEREST_RELABELS):
        conn.execute(f"INSERT INTO {interest_relabel.TABLE} VALUES (?, ?, ?, '{{}}', 1)",
                     (f"base-revision-{number}", OWNER_ID, f"cluster-invented-{number}"))
    for number in range(NOT_MINE):
        conn.execute(f"INSERT INTO {ownership.DECISIONS} VALUES (?, ?, 'app', 'ai_chat_messages', NULL, ?, "
                     "'not_mine', 1)", (f"decision-invented-{number}", OWNER_ID, f"app-invented-{number}"))
    conn.execute("INSERT INTO entity_blackholes (blackhole_id, normalized_name, canonical_name, rebuild_state, note) "
                 "VALUES ('bh_owner_made', ?, ?, 'complete', 'mine')", (OWNERS_OWN_ENTRY.lower(), OWNERS_OWN_ENTRY))
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()

    config = {"version": "topos-policy-node-config/v1", "identity": _identity(), "cp_issuer_id": "cp-invented-finch",
              "frontend_client_id": "client-invented-lark",
              "trusted_cp_keys": {"cp-key-invented": secrets.token_hex(32)},
              "node_signing_kid": KEY_ID, "node_signing_key_path": str(live / "permissions-v2" / "node-signing.key"),
              "canonical_database_path": str(live / "database.db"),
              "ledger_path": str(live / "permissions-v2" / "ledger.db")}
    (durable / "config.json").write_text(json.dumps(config))
    os.chmod(durable / "config.json", 0o600)
    (durable / "node-signing.key").write_text(secrets.token_hex(32))
    os.chmod(durable / "node-signing.key", 0o600)
    (durable / "protocol.lock").write_text("")

    conn = sqlite3.connect(durable / "ledger.db")
    for sql in ledger_module._DDL:
        conn.execute(sql)
    conn.execute("INSERT INTO p2a_node VALUES (1, ?, 3, ?)",
                 (json.dumps(_identity()), hashlib.sha256(b"an invented protection revision").hexdigest()))
    for grant_id, active, answers, members in GRANTS:
        policy = {"versions": {"capability": "permissions-beta/p2c-v3"},
                  "search": {"answers": answers} if answers else {}, "policy_version_id": f"policy-{grant_id}"}
        conn.execute("INSERT INTO p2a_policies VALUES (?, ?, ?)",
                     (f"policy-{grant_id}", hashlib.sha256(grant_id.encode()).hexdigest(), json.dumps(policy)))
        conn.execute("INSERT INTO p2a_grants VALUES (?, ?, 1, 1, ?, ?)",
                     (grant_id, f"assignment-{grant_id}", f"policy-{grant_id}", active))
        if members is not None:
            _write_index(root, grant_id, members)
    conn.commit()
    conn.close()
    # The per-grant record keys. Never read by the census: there to prove it is not.
    sqlite3.connect(durable / "message-search" / "keys.db").close()

    conn = sqlite3.connect(durable / runtime_module.DEFAULT_EVIDENCE_REVIEW_STORE)
    conn.execute("CREATE TABLE fact_reviews(review_id TEXT PRIMARY KEY,fact_id TEXT NOT NULL,review_json TEXT NOT NULL,"
                 "active INTEGER NOT NULL CHECK(active IN (0,1)))")
    conn.execute(evidence._OPT_OUT_TABLE)
    number = 0
    for version, active, count in REVIEWS:
        for _ in range(count):
            number += 1
            conn.execute("INSERT INTO fact_reviews VALUES (?, ?, ?, ?)",
                         (f"review-invented-{number}", f"fact-invented-{number}",
                          json.dumps({"version": version, "review_id": f"review-invented-{number}", "owner_id": OWNER_ID}),
                          active))
    for number in range(OPT_OUTS):
        conn.execute("INSERT INTO fact_opt_outs VALUES (?, 1, NULL)", (f"fact-opted-out-{number}",))
    conn.commit()
    conn.close()

    conn = sqlite3.connect(durable / entailment_grounding.STORE_NAME)
    conn.execute(entailment_grounding._SCHEMA)
    for number in range(VERDICTS_STANDING + VERDICTS_REVOKED):
        conn.execute("INSERT INTO entailment_verdicts VALUES (?, 'claim', 'message', 'judge-invented', 'entailed', 1, ?)",
                     (hashlib.sha256(f"verdict-{number}".encode()).hexdigest(),
                      None if number < VERDICTS_STANDING else 2))
    conn.commit()
    conn.close()
    assert not [path for path in root.rglob("*") if path.name.endswith(cs.SIDECARS)]
    return root


#: Every invented value a home holds that must never be printed or written by either verb.
def _held_values() -> list:
    values = [NODE_ID, KEY_ID, OWNER_ID, OWNERS_OWN_ENTRY, OWNERS_OWN_ENTRY.lower(), "resource-invented-wren",
              "cp-invented-finch", "client-invented-lark", "cp-key-invented", "permissions-beta-invented",
              "review-invented-", "fact-invented-", "fact-opted-out-", "cluster-invented-", "decision-invented-",
              "app-invented-", "judge-invented", "-member-", "bh_owner_made", "topos-review-of-a-kind-not-known"]
    values += [grant_id for grant_id, _active, _answers, _members in GRANTS]
    for choice in fixture.CONTACT_CHOICES:
        values += [choice.contact_id, *choice.usernames, *(identifier for identifier, _kind in choice.handles)]
        values += [choice.display_name] if choice.display_name else []
        for entity_id, name, aliases, _mentions in choice.entities:
            values += [entity_id, name, *aliases]
    return values


def _holds_no_value(text: str) -> None:
    lowered = text.lower()
    for value in _held_values():
        assert value.lower() not in lowered, f"an invented value of the home is in the output ({len(value)} chars)"


@pytest.fixture(autouse=True)
def _scratch_environment(tmp_path, monkeypatch):
    """What the census requires before it imports engine code; and a stand-in for the live home, so no test names
    the real one."""
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(tmp_path / "scratch" / "throwaway.db"))
    monkeypatch.setenv("TOPOS_ENV_FILE", str(tmp_path / "scratch" / "topos.env"))
    monkeypatch.setattr(cs, "LIVE_HOME", tmp_path / "the-live-home")


@pytest.fixture
def home(tmp_path):
    return make_home(tmp_path / "copy-before", live=tmp_path / "where-it-was-copied-from")


def _collect(root: Path, out: Path, capsys) -> tuple:
    assert ucd.main(["collect", "--source-root", str(root), "--out", str(out)]) == 0
    printed = capsys.readouterr().out
    return json.loads(out.read_text()), printed


def _after(home: Path, tmp_path: Path, name: str = "copy-after") -> Path:
    return Path(shutil.copytree(home, tmp_path / name, symlinks=True))


def _sql(path: Path, *statements) -> None:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=DELETE")
    for statement in statements:
        conn.execute(statement) if isinstance(statement, str) else conn.execute(*statement)
    conn.commit()
    conn.close()


def _diff(before: Path, after: Path, capsys, *extra) -> tuple:
    code = ucd.main(["diff", str(before), str(after), *extra])
    return code, capsys.readouterr().out


# --- collect ---------------------------------------------------------------------------------------------------

def test_collect_counts_a_stopped_copy_and_prints_totals_only(home, tmp_path, capsys):
    census, printed = _collect(home, tmp_path / "before.json", capsys)
    assert census["schema"] == "upgrade-census/v1"
    assert census["canonical"]["upgrade_baseline"] == "1.4.4" and census["canonical"]["quick_check_ok"] is True
    assert census["reviews"]["evidence"] == {
        "by_kind": {"machine_message_assessment": {"active": 5, "superseded": 2}, "other": {"active": 1, "superseded": 0},
                    "owner_fact_review": {"active": 1, "superseded": 0},
                    "owner_message_review": {"active": 2, "superseded": 1}},
        "opt_outs": OPT_OUTS}
    assert census["reviews"]["projection"] is None
    assert census["reviews"]["entailment_verdicts"] == {"standing": VERDICTS_STANDING, "revoked": VERDICTS_REVOKED}
    assert census["reviews"]["canonical"] == {"interest_label_assessments": INTEREST_ASSESSMENTS,
                                              "interest_relabels": INTEREST_RELABELS,
                                              "owner_not_mine_decisions": NOT_MINE}
    assert census["grants"] == {"active": {"permissions-beta/p2c-v3 answers=only": 2},
                                "inactive": {"permissions-beta/p2c-v3": 1}}
    shares = census["shares"]
    assert len(shares) == 3 and all(len(key) == 16 for key in shares)
    with_index = sorted((share["index"]["members"], share["index"]["state"]) for share in shares.values()
                        if share["index"]["present"])
    assert with_index == [(7, "ready"), (12, "ready")]
    assert all(len(share["index"]["revision"]) == 16 and len(share["index"]["content_digest"]) == 16
               for share in shares.values() if share["index"]["present"])
    assert census["index_files_no_grant_names"] == 0
    assert census["off_limits"] == {"entries": 1, "with_carry_note": 0, "carried_waiting": 0,
                                    "by_rebuild_state": {"complete": 1}}
    carry = census["carry_step"]
    assert carry["dry_run"]["explicit_excludes"] == len(EXCLUDES) == 8 and carry["dry_run"]["contacts"] == 15
    assert carry["named_by"] == {"linked_entity": 2, "name": 3, "handle": 1, "contact_id_only": 2}
    assert carry["would_add"] == 8 and carry["already_off_limits"] == 0 and carry["ledger"] is None
    assert census["node"] == {"bound": True, "node_id": ucd.fingerprint("node-id", NODE_ID),
                              "key_id": ucd.fingerprint("key-id", KEY_ID), "key_file_present": True}
    assert json.loads(printed) == {
        "schema": "upgrade-census/v1", "upgrade_baseline": "1.4.4", "node_bound": True, "reviews_active": 9,
        "reviews_all_rows": 12, "owner_opt_outs": 2, "shares_active": 2, "shares_active_with_an_index": 2,
        "off_limits_entries": 1, "carry_step_explicit_excludes": 8, "carry_step_would_add": 8}
    assert (tmp_path / "before.json").stat().st_mode & 0o777 == 0o600


def test_neither_verb_prints_or_writes_a_name_or_an_id(home, tmp_path, capsys):
    after = _after(home, tmp_path)
    conn = sqlite3.connect(after / "database.db")
    carry_contact_excludes(conn)                                # entries named after people, in the copy
    conn.close()
    for grant_id, active, _answers, members in GRANTS:
        if members is not None:
            _write_index(after, grant_id, members - 1, generation=5)       # the purge the step ran, then a rebuild
    _census, printed_before = _collect(home, tmp_path / "before.json", capsys)
    _census, printed_after = _collect(after, tmp_path / "after.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys, "--expect-off-limits-gain", "8")
    assert code == 0, compared
    for text in (printed_before, printed_after, compared, (tmp_path / "before.json").read_text(),
                 (tmp_path / "after.json").read_text()):
        _holds_no_value(text)
    # The check itself bites: the values are in the stores the census read.
    stored = sqlite3.connect(after / "database.db").execute("SELECT canonical_name FROM entity_blackholes").fetchall()
    with pytest.raises(AssertionError):
        _holds_no_value(json.dumps(stored))


class _Watch:
    """Every path Python opened and every database SQLite was asked for, while armed."""
    seen: list = []
    armed = False

    @classmethod
    def hook(cls, event, args):
        if cls.armed and event in ("open", "sqlite3.connect") and args:
            cls.seen.append(os.fsdecode(args[0]) if isinstance(args[0], (str, bytes, os.PathLike)) else repr(args[0]))


sys.addaudithook(_Watch.hook)


def test_collect_opens_no_key_follows_no_path_out_of_the_copy_and_writes_nothing_into_it(home, tmp_path, capsys):
    live = tmp_path / "where-it-was-copied-from"
    # The live home the copy's config still names, with stores of its own. Counting them would be the bug.
    decoy = make_home(live, live=live)
    _sql(decoy / "permissions-v2" / runtime_module.DEFAULT_EVIDENCE_REVIEW_STORE, "DELETE FROM fact_reviews")

    def state(root):
        return {str(path.relative_to(root)): (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
                for path in sorted(root.rglob("*")) if path.is_file()}
    before = state(home)
    _Watch.seen, _Watch.armed = [], True
    try:
        census, _printed = _collect(home, tmp_path / "before.json", capsys)
    finally:
        _Watch.armed = False
    assert census["reviews"]["evidence"]["by_kind"]["machine_message_assessment"]["active"] == 5      # the copy's
    assert state(home) == before                                           # no byte and no file added or changed
    touched = "\n".join(_Watch.seen)
    assert str(live) not in touched                                        # the config's own paths: never followed
    assert "node-signing.key" not in touched and "keys.db" not in touched
    databases = [entry for entry in _Watch.seen if entry.startswith("file:")]
    assert databases and all("mode=ro" in entry and "immutable=1" in entry for entry in databases)
    assert not [entry for entry in _Watch.seen if entry.endswith(".db") and str(home) in entry]     # none opened plain


def test_a_node_that_never_shared_is_counted_without_a_sharing_folder(tmp_path, capsys):
    root = tmp_path / "copy-unbound"
    root.mkdir()
    conn = sqlite3.connect(root / "database.db")
    apply_all_migrations(conn)
    fixture.seed_contact_choices(conn)
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    census, _printed = _collect(root, tmp_path / "unbound.json", capsys)
    assert census["node"] == {"bound": False, "node_id": None, "key_id": None, "key_file_present": False}
    assert census["grants"] is None and census["shares"] == {} and census["reviews"]["evidence"] is None
    assert census["carry_step"]["would_add"] == 8 and census["off_limits"]["entries"] == 0
    code, compared = _diff(tmp_path / "unbound.json", tmp_path / "unbound.json", capsys)
    assert code == 0 and compared.rstrip().endswith("upgrade_census_ok")


# --- diff: a clean pair passes ------------------------------------------------------------------------------------

def test_a_clean_pair_passes_and_so_does_more_of_anything(home, tmp_path, capsys):
    _collect(home, tmp_path / "before.json", capsys)
    after = _after(home, tmp_path)
    _collect(after, tmp_path / "same.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "same.json", capsys)
    assert code == 0 and compared.rstrip().endswith("upgrade_census_ok") and "FAIL" not in compared
    reviews = after / "permissions-v2" / runtime_module.DEFAULT_EVIDENCE_REVIEW_STORE
    _sql(reviews,
         # A re-assessment: the old row is superseded and a new one is active. Active stays, rows grow.
         "UPDATE fact_reviews SET active=0 WHERE review_id='review-invented-4'",
         ("INSERT INTO fact_reviews VALUES ('review-invented-new', 'fact-invented-4', ?, 1)",
          (json.dumps({"version": "topos-machine-message-review/v1"}),)),
         "INSERT INTO fact_opt_outs VALUES ('fact-opted-out-new', 2, NULL)")
    _sql(after / "database.db",
         "INSERT INTO entity_blackholes (blackhole_id, normalized_name, canonical_name, note) "
         "VALUES ('bh_another', 'orsolya penhallow', 'Orsolya Penhallow', 'mine too')")
    _write_index(after, GRANTS[2][0], 3)                                    # an index for a share that had none
    _collect(after, tmp_path / "more.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "more.json", capsys)
    assert code == 0, compared
    assert "moved reviews and assessments: evidence reviews: machine_message_assessment, all rows: 7 -> 8" in compared
    assert "same  reviews and assessments: evidence reviews: machine_message_assessment, active: 5 -> 5" in compared
    assert "moved Off-limits: entries: 1 -> 2" in compared


# --- diff: each thing an upgrade must not do is caught and named -------------------------------------------------

def _reviews(root):
    return root / "permissions-v2" / runtime_module.DEFAULT_EVIDENCE_REVIEW_STORE


LOSSES = {
    "a review row is gone": (
        lambda root: _sql(_reviews(root), "DELETE FROM fact_reviews WHERE review_id='review-invented-1'"),
        ["reviews and assessments: evidence reviews: owner_message_review, active: 2 -> 1 (lower)",
         "reviews and assessments: evidence reviews: owner_message_review, all rows: 3 -> 2 (lower)"]),
    "an assessment stopped being current with nothing in its place": (
        lambda root: _sql(_reviews(root), "UPDATE fact_reviews SET active=0 WHERE review_id='review-invented-4'"),
        ["reviews and assessments: evidence reviews: machine_message_assessment, active: 5 -> 4 (lower)"]),
    "a superseded assessment row is gone": (
        lambda root: _sql(_reviews(root), "DELETE FROM fact_reviews WHERE review_id='review-invented-9'"),
        ["reviews and assessments: evidence reviews: machine_message_assessment, all rows: 7 -> 6 (lower)"]),
    "a review of a kind this tool does not know is gone": (
        lambda root: _sql(_reviews(root), "DELETE FROM fact_reviews WHERE review_id='review-invented-12'"),
        ["reviews and assessments: evidence reviews: other, active: 1 -> none (lower)",
         "reviews and assessments: evidence reviews: other, all rows: 1 -> none (lower)"]),
    "an opt-out is gone": (
        lambda root: _sql(_reviews(root), "DELETE FROM fact_opt_outs WHERE fact_id='fact-opted-out-0'"),
        ["reviews and assessments: evidence reviews: owner opt-outs: 2 -> 1 (lower)"]),
    "the review store is gone": (
        lambda root: _reviews(root).unlink(),
        ["reviews and assessments: evidence review store: 1 -> none (lower)"]),
    "an entailment verdict is gone": (
        lambda root: _sql(root / "permissions-v2" / entailment_grounding.STORE_NAME,
                          "DELETE FROM entailment_verdicts WHERE revoked_at IS NULL AND rowid = "
                          "(SELECT MIN(rowid) FROM entailment_verdicts WHERE revoked_at IS NULL)"),
        ["reviews and assessments: entailment verdicts, all rows: 4 -> 3 (lower)",
         "reviews and assessments: entailment verdicts, standing: 3 -> 2 (lower)"]),
    "an interest label assessment is gone": (
        lambda root: _sql(root / "database.db",
                          f"DELETE FROM {interest_review.TABLE} WHERE label_revision='label-revision-0'"),
        ["reviews and assessments: interest label assessments: 4 -> 3 (lower)"]),
    "the owner's not-mine decisions are gone": (
        lambda root: _sql(root / "database.db", f"DROP TABLE {ownership.DECISIONS}"),
        ["reviews and assessments: owner not mine decisions: 3 -> none (lower)"]),
    "a share that had an index has none": (
        lambda root: index_path(root / "permissions-v2" / "message-search", GRANTS[0][0]).unlink(),
        ["shares: active with a search index: 2 -> 1 (1 that had an index has none)"]),
    "every index is gone": (
        lambda root: shutil.rmtree(root / "permissions-v2" / "message-search"),
        ["shares: active with a search index: 2 -> 0 (2 that had an index have none)"]),
    "the node id changed": (
        lambda root: _config(root, lambda config: config["identity"].update(node_id="node-invented-other")),
        ["node: node id fingerprint changed"]),
    "the node key id changed": (
        lambda root: _config(root, lambda config: config.update(node_signing_kid="kid-invented-other")),
        ["node: node key id fingerprint changed"]),
    "the node is no longer bound": (
        lambda root: (root / "permissions-v2" / "config.json").unlink(),
        ["node: node id fingerprint changed", "node: node key id fingerprint changed"]),
    "Off-limits has fewer entries": (
        lambda root: _sql(root / "database.db", "DELETE FROM entity_blackholes"),
        ["Off-limits: entries: 1 -> 0 (fewer)"]),
}


def _config(root, change):
    path = root / "permissions-v2" / "config.json"
    config = json.loads(path.read_text())
    change(config)
    path.write_text(json.dumps(config))


@pytest.mark.parametrize("loss", sorted(LOSSES))
def test_each_loss_fails_and_is_named(home, tmp_path, capsys, loss):
    damage, named = LOSSES[loss]
    _collect(home, tmp_path / "before.json", capsys)
    after = _after(home, tmp_path)
    damage(after)
    _collect(after, tmp_path / "after.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys)
    assert code == 1, compared
    assert "upgrade_census_failed:" in compared and "upgrade_census_ok" not in compared
    failures = [line.strip() for line in compared.split("upgrade_census_failed:")[1].splitlines()[1:]]
    if loss == "the review store is gone":
        # Everything the store held went with it, and each count is named.
        assert set(named) < set(failures) and all(line.startswith("reviews and assessments: evidence") for line in failures)
    else:
        assert failures == named
    _holds_no_value(compared)
    # And the same pair the other way round is not a loss: more of everything passes.
    code, _compared = _diff(tmp_path / "after.json", tmp_path / "before.json", capsys)
    assert code == (1 if "node" in loss else 0)


def test_an_index_that_went_with_its_inactive_grant_is_not_a_lost_share(home, tmp_path, capsys):
    """The node itself removes the index of a grant that is no longer active."""
    _write_index(home, GRANTS[2][0], 3)
    _collect(home, tmp_path / "before.json", capsys)
    after = _after(home, tmp_path)
    index_path(after / "permissions-v2" / "message-search", GRANTS[2][0]).unlink()
    _collect(after, tmp_path / "after.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys)
    assert code == 0 and "active with a search index: 2 -> 2" in compared


def test_a_database_that_stops_passing_its_quick_check_fails(home, tmp_path, capsys):
    census, _printed = _collect(home, tmp_path / "before.json", capsys)
    broken = json.loads(json.dumps(census))
    broken["canonical"]["quick_check_ok"] = False
    failures, _lines = ucd.compare(census, broken)
    assert failures == ["canonical database: passes the quick check: True -> False (it passed before)"]
    assert ucd.compare(broken, broken)[0] == []                 # one that never passed is not this upgrade's doing


def test_a_lower_count_this_verb_does_not_judge_is_listed_and_passes(home, tmp_path, capsys):
    _collect(home, tmp_path / "before.json", capsys)
    after = _after(home, tmp_path)
    for grant_id, _active, _answers, members in GRANTS[:2]:
        _write_index(after, grant_id, members - 3, generation=5)            # rebuilt, with fewer members
    _sql(after / "permissions-v2" / "ledger.db", f"UPDATE p2a_grants SET active=0 WHERE grant_id='{GRANTS[1][0]}'")
    _sql(after / "database.db", "DELETE FROM conversation_messages WHERE message_id='upgrade-fixture-m1'")
    _collect(after, tmp_path / "after.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys)
    assert code == 0, compared
    assert "note  shares: 2 of 2 kept indexes are under a new revision" in compared
    assert "moved shares: index members, those shares: 19 -> 13" in compared
    assert "moved grants: active, permissions-beta/p2c-v3 answers=only: 2 -> 1" in compared
    assert "moved grants: inactive, permissions-beta/p2c-v3 answers=only: 0 -> 1" in compared
    assert "moved canonical database: conversation messages: 3 -> 2  (lower, not judged here)" in compared


# --- the carry step between the two copies ------------------------------------------------------------------------

def _rebuild_indexes(root, drop=1):
    for grant_id, active, _answers, members in GRANTS:
        if active and members is not None:
            _write_index(root, grant_id, members - drop, generation=5)


def test_the_carry_step_between_two_copies_is_judged_on_the_gain_it_should_make(home, tmp_path, capsys):
    before, _printed = _collect(home, tmp_path / "before.json", capsys)
    after = _after(home, tmp_path)
    conn = sqlite3.connect(after / "database.db")
    report = carry_contact_excludes(conn)                       # what the 1.5.0 upgrade runs, on the copy
    conn.close()
    assert report["carried"] == before["carry_step"]["would_add"] == 8
    # The step's own hook purged every search index. Until the node has rebuilt them the upgrade is not done, and
    # a copy taken that early says so.
    _collect(after, tmp_path / "too-early.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "too-early.json", capsys, "--expect-off-limits-gain", "8")
    assert code == 1 and "shares: active with a search index: 2 -> 0 (2 that had an index have none)" in compared
    _rebuild_indexes(after)
    census, _printed = _collect(after, tmp_path / "after.json", capsys)
    # Carried and waiting: the eight are marked and stay `pending` (no clean-up is run by the step); the owner's
    # own entry keeps the state it had.
    assert census["off_limits"] == {"entries": 9, "with_carry_note": 8, "carried_waiting": 8,
                                    "by_rebuild_state": {"complete": 1, "pending": 8}}
    # A second run would add none: the step remembers each of the eight contacts and skips it.
    assert census["carry_step"]["would_add"] == 0 and census["carry_step"]["carried_before"] == 8
    assert census["carry_step"]["already_off_limits"] == 0 and census["carry_step"]["own_card_skipped"] == 0
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys, "--expect-off-limits-gain", "8")
    assert code == 0, compared
    assert "ok    Off-limits: gained 8, expected 8" in compared
    assert "ok    carry step: a second run would add 0, expected 0" in compared
    assert "ok    Off-limits: 8 more carried and waiting, expected 8" in compared
    # Any other expected gain fails, either way.
    for wrong in ("7", "9", "0"):
        code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys, "--expect-off-limits-gain", wrong)
        assert code == 1 and f"FAIL  Off-limits: gained 8, expected {wrong}" in compared
    # Without the flag the gain is not judged: more Off-limits entries only protect more.
    assert _diff(tmp_path / "before.json", tmp_path / "after.json", capsys)[0] == 0


def test_a_step_that_carried_only_some_fails_even_when_the_gain_was_expected(home, tmp_path, capsys, monkeypatch):
    _collect(home, tmp_path / "before.json", capsys)
    after = _after(home, tmp_path)
    real = contact_excludes.explicit_choices
    with monkeypatch.context() as patched:
        patched.setattr(contact_excludes, "explicit_choices",
                        lambda conn: {**real(conn), "excludes": real(conn)["excludes"][::2]})
        conn = sqlite3.connect(after / "database.db")
        assert carry_contact_excludes(conn)["carried"] == 4
        conn.close()
    _rebuild_indexes(after)
    census, _printed = _collect(after, tmp_path / "after.json", capsys)
    assert census["carry_step"]["would_add"] == 4               # read with the step as it really is
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys, "--expect-off-limits-gain", "4")
    assert code == 1
    assert "ok    Off-limits: gained 4, expected 4" in compared
    assert "FAIL  carry step: a second run would add 4, expected 0" in compared


@pytest.mark.parametrize("overlap", ["none", "the owner already made one of them Off-limits",
                                     "the owner already made a linked entity Off-limits",
                                     "two excluded contacts are one linked person"])
def test_what_a_run_would_add_is_what_the_real_step_adds(home, tmp_path, capsys, overlap):
    """The dry run counts explicit excludes; the list gains fewer when an entry is already there or is shared.
    `would_add` is the census's own reading of that, and this is where it is held to the step itself."""
    named = next(choice for choice in EXCLUDES if choice.named_by == "name")
    linked = next(choice for choice in EXCLUDES if choice.named_by == "linked_entity")
    excludes = len(EXCLUDES)
    if overlap == "the owner already made one of them Off-limits":
        conn = sqlite3.connect(home / "database.db")
        BlackholeStore(conn).blackhole_entity(entity_ref=named.display_name, note="mine")
        conn.close()
    elif overlap == "the owner already made a linked entity Off-limits":
        conn = sqlite3.connect(home / "database.db")
        BlackholeStore(conn).blackhole_entity(entity_ref=linked.entities[0][0], note="mine")
        conn.close()
    elif overlap == "two excluded contacts are one linked person":
        conn = sqlite3.connect(home / "database.db")
        conn.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, sharing_policy_json) "
                     "VALUES ('upgrade-fixture-contact-same-person', 'default', 'upgrade_fixture_contacts', ?)",
                     (fixture._EXCLUDE,))
        conn.execute("INSERT INTO entities (entity_id, entity_type, canonical_name, normalized_name, aliases_json, "
                     "is_self, contact_id, mention_count) VALUES ('upgrade-fixture-entity-same-person', 'person', "
                     "?, ?, '[]', 0, 'upgrade-fixture-contact-same-person', 1)",
                     (linked.entities[0][1], linked.entities[0][1].lower()))
        conn.commit()
        conn.close()
        excludes += 1
    _sql(home / "database.db")                                              # closed, no sidecar
    before, _printed = _collect(home, tmp_path / "before.json", capsys)
    assert before["carry_step"]["dry_run"]["explicit_excludes"] == excludes
    after = _after(home, tmp_path)
    conn = sqlite3.connect(after / "database.db")
    entries = conn.execute("SELECT COUNT(*) FROM entity_blackholes").fetchone()[0]
    report = carry_contact_excludes(conn)
    gained = conn.execute("SELECT COUNT(*) FROM entity_blackholes").fetchone()[0] - entries
    conn.close()
    assert before["carry_step"]["would_add"] == report["carried"] == gained
    assert before["carry_step"]["already_off_limits"] == report["already_off_limits"] == excludes - gained
    assert gained == (excludes if overlap == "none" else 7 if "owner" in overlap else 8)
    _rebuild_indexes(after)
    census, _printed = _collect(after, tmp_path / "after.json", capsys)
    assert census["carry_step"]["would_add"] == 0
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys,
                           "--expect-off-limits-gain", str(before["carry_step"]["would_add"]))
    assert code == 0, compared
    if overlap != "none":
        # Judged against the dry run's count, as amendment 8 item 7 is worded, the same sound upgrade fails.
        code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys,
                               "--expect-off-limits-gain", str(excludes))
        assert code == 1 and f"FAIL  Off-limits: gained {gained}, expected {excludes}" in compared


def test_the_upgrade_ledger_row_of_the_step_is_read_as_counts(home, tmp_path, capsys):
    _sql(home / "database.db",
         ("INSERT INTO derivation_ledger (version, step_id, status, started_at, finished_at, detail_json) "
          "VALUES ('1.5.0', ?, 'done', datetime('now'), datetime('now'), ?)",
          (STEP_ID, json.dumps({"carried": 8, "already_off_limits": 0, "added_to_existing": 0, "carried_before": 0,
                                "own_card_skipped": 1, "failed": 0, "waiting": 8, "boundary": "built",
                                "named_by": {"name": 8}, "ran_under": "1.5.0"}))))
    census, _printed = _collect(home, tmp_path / "after.json", capsys)
    assert census["carry_step"]["ledger"] == {"rows": 1, "status": "done", "version": "1.5.0",
                                              "started_and_finished": True, "carried": 8, "already_off_limits": 0,
                                              "added_to_existing": 0, "carried_before": 0, "own_card_skipped": 1,
                                              "failed": 0, "waiting": 8, "boundary": "built"}
    _code, compared = _diff(tmp_path / "after.json", tmp_path / "after.json", capsys)
    assert ("note  carry step: ledger row done under 1.5.0: carried 8, already Off-limits 0, failed 0, waiting 8, "
            "boundary built") in compared
    # A refusal's code in the row is a word a store holds: it is shown as "other", never as itself.
    _sql(home / "database.db", ("UPDATE derivation_ledger SET detail_json=? WHERE step_id=?",
                                (json.dumps({"boundary": "entity_protection_lineage_unavailable", "failed": 0}), STEP_ID)))
    census, _printed = _collect(home, tmp_path / "refused.json", capsys)
    assert census["carry_step"]["ledger"]["boundary"] == "other" and census["carry_step"]["ledger"]["carried"] is None


def test_would_add_leaves_out_what_the_step_leaves_out(home, tmp_path, capsys):
    """The owner's own card is never carried, and a contact the step dealt with is never carried again. Counted
    as the step counts them, or the gain to expect is one too many on a home whose owner had hidden his own name
    (the older app stored an exclude with it), and an entry he removed after the upgrade reads as one a second run
    would put back."""
    conn = sqlite3.connect(home / "database.db")
    conn.execute("INSERT INTO contacts (contact_id, dataset_id, source_id, display_name, is_self, sharing_policy_json) "
                 "VALUES ('upgrade-fixture-contact-own-card', 'default', 'upgrade_fixture_contacts', 'Mine', 1, ?)",
                 (json.dumps({"name_visibility": "hidden", "row_visibility": "exclude_from_grants"}),))
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    before, _printed = _collect(home, tmp_path / "before.json", capsys)
    assert before["carry_step"]["dry_run"]["explicit_excludes"] == 9
    assert (before["carry_step"]["would_add"], before["carry_step"]["own_card_skipped"]) == (8, 1)
    after = _after(home, tmp_path)
    conn = sqlite3.connect(after / "database.db")
    report = carry_contact_excludes(conn)                       # the real step agrees
    assert (report["carried"], report["own_card_skipped"]) == (8, 1)
    removed = next(e["blackhole_id"] for e in BlackholeStore(conn).list() if e["carried_waiting"])
    BlackholeStore(conn).unblackhole_entity(entity_ref=removed)             # the owner removes one afterwards
    assert carry_contact_excludes(conn)["carried"] == 0                    # and a second run does not put it back
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    census, _printed = _collect(after, tmp_path / "after.json", capsys)
    assert (census["carry_step"]["would_add"], census["carry_step"]["carried_before"]) == (0, 8)
    assert census["off_limits"]["carried_waiting"] == 7


def test_an_entry_the_step_made_without_its_mark_fails_the_comparison(home, tmp_path, capsys):
    """Eight more entries, each with the step's note, and a second run would add none: every older check passes.
    But one of them is not carried and waiting, so every reader that serves the owner himself sees it."""
    _collect(home, tmp_path / "before.json", capsys)
    after = _after(home, tmp_path)
    conn = sqlite3.connect(after / "database.db")
    carry_contact_excludes(conn)
    one = conn.execute("SELECT blackhole_id FROM entity_blackholes WHERE carried_waiting_json IS NOT NULL").fetchone()[0]
    conn.execute("UPDATE entity_blackholes SET carried_waiting_json=NULL WHERE blackhole_id=?", (one,))
    conn.commit()
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    _rebuild_indexes(after)
    _collect(after, tmp_path / "after.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys, "--expect-off-limits-gain", "8")
    assert code == 1 and "ok    Off-limits: gained 8, expected 8" in compared
    assert "FAIL  Off-limits: 7 more carried and waiting, expected 8" in compared


# --- stores the node's own code wrote, and the real step under the protection clock -------------------------------

def _copy_of_a_node(node, tmp_path, name):
    """A stopped copy, in a home's layout, of a node the harness built: the canonical database, the review store,
    the ledger and the indexes are the files the node's own code wrote. Its config names the node's own paths."""
    resolver = node.index.resolver
    durable = root_for(resolver.path).parent
    root = tmp_path / name
    (root / "permissions-v2" / "message-search").mkdir(parents=True)
    shutil.copy2(resolver.path, root / "database.db")
    shutil.copy2(node.index.reviews.path, root / "permissions-v2" / runtime_module.DEFAULT_EVIDENCE_REVIEW_STORE)
    shutil.copy2(node.ledger.path, root / "permissions-v2" / "ledger.db")
    for path in (durable / "message-search").glob("grant-*.db"):
        shutil.copy2(path, root / "permissions-v2" / "message-search" / path.name)
    config = {"version": "topos-policy-node-config/v1", "identity": resolver.binding.model_dump(),
              "node_signing_kid": "node-key", "node_signing_key_path": str(durable / "node-signing.key"),
              "canonical_database_path": str(resolver.path), "ledger_path": str(node.ledger.path)}
    (root / "permissions-v2" / "config.json").write_text(json.dumps(config))
    return root


def test_stores_the_node_wrote_are_counted_and_the_real_step_passes_once_the_index_is_back(legacy, tmp_path,  # noqa: F811
                                                                                           monkeypatch, capsys):
    node, _identity_of_the_message = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    conn = legacy[1]
    fixture.seed_contact_choices(conn)                  # the older model's stored choices, on a node bound for sharing
    before = _copy_of_a_node(node, tmp_path, "copy-before")
    census, _printed = _collect(before, tmp_path / "before.json", capsys)
    assert census["reviews"]["evidence"]["by_kind"] == {"machine_message_assessment": {"active": 1, "superseded": 0}}
    assert census["grants"]["active"] == {"permissions-beta/p2a-v2": 1, "permissions-beta/p2c-v3": 1}
    with_index = [share for share in census["shares"].values() if share["index"]["present"]]
    assert [(share["profile"], share["index"]["state"], share["index"]["members"]) for share in with_index] == [
        ("permissions-beta/p2c-v3", "ready", 1)]
    assert census["node"]["bound"] and census["node"]["node_id"] == ucd.fingerprint("node-id", "node-1")
    assert census["carry_step"]["would_add"] == 8 and census["off_limits"]["entries"] == 0

    # The step itself, on the node's database, where the protection clock's triggers watch the Off-limits table and
    # the step's hook drops every search index.
    report = carry_contact_excludes(conn)
    assert (report["carried"], report["failed"], report["boundary"]) == (8, 0, "built")
    too_early = _copy_of_a_node(node, tmp_path, "copy-too-early")
    _collect(too_early, tmp_path / "too-early.json", capsys)
    code, compared = _diff(tmp_path / "before.json", tmp_path / "too-early.json", capsys, "--expect-off-limits-gain", "8")
    assert code == 1 and "shares: active with a search index: 1 -> 0 (1 that had an index has none)" in compared

    # What the node does next on its own: its ledger takes up the protection revision the new entries made (until
    # then a rebuild answers "stale"), and the index is rebuilt under it.
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "stale"
    node.epoch()
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "ready"
    after = _copy_of_a_node(node, tmp_path, "copy-after")
    census, _printed = _collect(after, tmp_path / "after.json", capsys)
    assert census["off_limits"]["entries"] == census["off_limits"]["with_carry_note"] == 8
    code, compared = _diff(tmp_path / "before.json", tmp_path / "after.json", capsys, "--expect-off-limits-gain", "8")
    assert code == 0, compared
    assert "note  shares: 1 of 1 kept indexes are under a new revision" in compared      # the clock moved: a rebuild
    assert "same  reviews and assessments: evidence reviews: machine_message_assessment, active: 1 -> 1" in compared
    _holds_no_value(compared)


# --- a live home is refused -----------------------------------------------------------------------------------

def _refused(*argv) -> str:
    with pytest.raises(cs.CensusRefused) as refusal:
        ucd.main(list(argv))
    return str(refusal.value)


def test_the_live_home_is_refused_by_path_through_a_link_and_as_the_place_to_write(home, tmp_path):
    live = tmp_path / "the-live-home"
    make_home(live, live=live)
    assert _refused("collect", "--source-root", str(live), "--out", str(tmp_path / "out.json")) == "live_store_refused"
    assert _refused("collect", "--source-root", str(live / "permissions-v2"), "--out",
                    str(tmp_path / "out.json")) == "live_store_refused"
    (tmp_path / "a-link-to-it").symlink_to(live)
    assert _refused("collect", "--source-root", str(tmp_path / "a-link-to-it"), "--out",
                    str(tmp_path / "out.json")) == "live_store_refused"
    assert _refused("collect", "--source-root", str(home), "--out", str(live / "out.json")) == "live_store_refused"
    assert _refused("diff", str(live / "a.json"), str(tmp_path / "b.json")) == "live_store_refused"
    assert not (tmp_path / "out.json").exists() and not (live / "out.json").exists()


def test_a_store_that_links_out_of_the_copy_is_refused(home, tmp_path):
    elsewhere = make_home(tmp_path / "elsewhere", live=tmp_path / "elsewhere")
    ledger = home / "permissions-v2" / "ledger.db"
    ledger.unlink()
    ledger.symlink_to(elsewhere / "permissions-v2" / "ledger.db")
    assert _refused("collect", "--source-root", str(home), "--out", str(tmp_path / "out.json")) == "source_escapes_root"


def test_a_folder_with_a_node_socket_is_refused_without_connecting_to_it(home, tmp_path, monkeypatch):
    monkeypatch.chdir(home)                             # a socket path is short: bind by a relative name
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind("engine.sock")
        listener.listen(1)
        listener.settimeout(0.2)
        assert _refused("collect", "--source-root", str(home), "--out", str(tmp_path / "out.json")) == "node_socket_present"
        with pytest.raises(socket.timeout):
            listener.accept()                           # nobody connected
    finally:
        listener.close()


def test_a_folder_whose_sharing_lock_is_held_is_refused(home, tmp_path):
    held = open(home / "permissions-v2" / "protocol.lock", "a+")
    try:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)      # as the sharing runtime holds it
        assert _refused("collect", "--source-root", str(home), "--out", str(tmp_path / "out.json")) == "node_lock_held"
    finally:
        held.close()
    assert ucd.main(["collect", "--source-root", str(home), "--out", str(tmp_path / "out.json")]) == 0   # released
    # And the probe left the lock free for a node to take.
    again = open(home / "permissions-v2" / "protocol.lock", "a+")
    fcntl.flock(again.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    again.close()


def test_a_graph_rebuild_lock_that_is_held_is_refused(home, tmp_path):
    held = open(home / "database.db.rebuild.lock", "a+")
    try:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert _refused("collect", "--source-root", str(home), "--out", str(tmp_path / "out.json")) == "node_lock_held"
    finally:
        held.close()


@pytest.mark.parametrize("sidecar", ["database.db-wal", "database.db-shm", "permissions-v2/ledger.db-journal",
                                     "permissions-v2/evidence-reviews.db-wal", "permissions-v2/message-search/INDEX-wal"])
def test_a_database_that_is_not_closed_is_refused(home, tmp_path, sidecar):
    name = sidecar.replace("INDEX", index_path(Path("x"), GRANTS[0][0]).name)
    (home / name).write_bytes(b"")
    assert _refused("collect", "--source-root", str(home), "--out", str(tmp_path / "out.json")) == "copy_not_closed"
    assert not (tmp_path / "out.json").exists()


def test_a_copy_taken_with_rows_still_in_the_log_is_refused_and_a_checkpointed_duplicate_counts_them(home, tmp_path,
                                                                                                     capsys):
    """An immutable read ignores the write-ahead log, so it would count the node as it was some time before the
    copy. The census refuses instead; the way through is a checkpoint, in a duplicate, never in the copy."""
    writer = sqlite3.connect(home / "database.db")
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("INSERT INTO entity_blackholes (blackhole_id, normalized_name, canonical_name, note) "
                   "VALUES ('bh_only_in_the_log', 'orsolya penhallow', 'Orsolya Penhallow', 'mine too')")
    writer.commit()
    copy = Path(shutil.copytree(home, tmp_path / "copy-with-a-log"))      # taken while the database is open
    writer.close()
    assert (copy / "database.db-wal").stat().st_size > 0
    held = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in copy.iterdir() if path.is_file()}
    assert _refused("collect", "--source-root", str(copy), "--out", str(tmp_path / "out.json")) == "copy_not_closed"
    # What an immutable read of that copy would have said: one entry, not two.
    stale = sqlite3.connect((copy / "database.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    assert stale.execute("SELECT COUNT(*) FROM entity_blackholes").fetchone()[0] == 1
    stale.close()
    duplicate = Path(shutil.copytree(copy, tmp_path / "duplicate"))
    conn = sqlite3.connect(duplicate / "database.db")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    conn.close()
    census, _printed = _collect(duplicate, tmp_path / "out.json", capsys)
    assert census["off_limits"]["entries"] == 2
    assert held == {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in copy.iterdir()
                    if path.is_file()}                                     # the copy itself was never written


def test_other_refusals_are_fixed_codes(home, tmp_path, monkeypatch, capsys):
    out = tmp_path / "out.json"
    assert _refused("collect", "--source-root", str(tmp_path / "no-such-folder"), "--out", str(out)) == "source_root_missing"
    assert _refused("collect", "--source-root", str(home), "--out", str(home / "inside.json")) == "out_inside_source_root"
    (tmp_path / "empty").mkdir()
    assert _refused("collect", "--source-root", str(tmp_path / "empty"), "--out", str(out)) == "canonical_database_missing"
    out.write_text("{}")
    assert _refused("collect", "--source-root", str(home), "--out", str(out)) == "out_exists"
    assert out.read_text() == "{}"
    assert _refused("diff", str(out), str(out)) == "not_an_upgrade_census"
    assert _refused("diff", str(tmp_path / "missing.json"), str(out)) == "census_file_unreadable"
    out.unlink()
    monkeypatch.delenv("TOPOS_DATABASE_PATH")
    assert _refused("collect", "--source-root", str(home), "--out", str(out)) == "scratch_environment_required"
    assert not out.exists()


def test_run_as_a_script_a_refusal_is_one_fixed_word_and_exit_two(home, tmp_path, monkeypatch, capsys):
    (home / "database.db-wal").write_bytes(b"")
    monkeypatch.setattr(sys, "argv", ["upgrade_census_diff.py", "collect", "--source-root", str(home), "--out",
                                      str(tmp_path / "out.json")])
    with pytest.raises(SystemExit) as stopped:
        runpy.run_path(str(SCRIPTS / "upgrade_census_diff.py"), run_name="__main__")
    captured = capsys.readouterr()
    assert stopped.value.code == 2 and json.loads(captured.err) == {"refused": "copy_not_closed"}
    assert captured.out == "" and str(home) not in captured.err


# --- the fixed words are the engine's --------------------------------------------------------------------------

def test_every_fixed_word_mirrors_the_engine_constant_it_stands_for():
    assert set(ucd.REVIEW_KINDS) == {
        typing.get_args(evidence.OwnerEvidenceReview.model_fields["version"].annotation)[0],
        message_evidence.MESSAGE_REVIEW, automatic_message_review.VERSION,
        typing.get_args(importlib.import_module("topos.permissions_v2.fact_projection")
                        .FactProjectionReview.model_fields["version"].annotation)[0]}
    assert all(ucd.CAPABILITY.match(capability) for capability in identity.SUBJECT_CONTRACT_BY_CAPABILITY)
    assert not ucd.CAPABILITY.match("permissions-beta/p2c-v3 and a name")
    assert ucd.ANSWER_MODES == typing.get_args(knowledge_contract.AnswerMode)
    assert ucd.REBUILD_STATES == importlib.import_module("topos.features.lifecycle.blackhole").REBUILD_STATES
    assert ucd.DEFAULT_REVIEWS == runtime_module.DEFAULT_EVIDENCE_REVIEW_STORE
    assert ucd.ENTAILMENT_STORE == entailment_grounding.STORE_NAME
    assert set(ucd.CANONICAL_ASSESSMENTS.values()) == {interest_review.TABLE, interest_relabel.TABLE, ownership.DECISIONS}
    assert ucd.CARRY_STEP_ID == STEP_ID and ucd.NAMING_BRANCHES == fixture.NAMING_BRANCHES
    conn = sqlite3.connect(":memory:")
    apply_all_migrations(conn)
    fixture.seed_contact_choices(conn)
    assert tuple(carry_contact_excludes(conn, dry_run=True)["counts"]) == ucd.DRY_RUN_COUNTS
    assert index_path(Path("root"), "grant-invented-alpha").name.startswith("grant-")
    assert NOTE and ucd.fingerprint("node-id", NODE_ID) != ucd.fingerprint("key-id", NODE_ID)
    assert ucd.fingerprint("node-id", "") is None and NODE_ID not in ucd.fingerprint("node-id", NODE_ID)
