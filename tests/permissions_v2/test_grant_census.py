"""WS1 grant census (scripts/permissions_v2/grant_census.py, census_copy.py, census_support.py).

The census must reproduce the node's own index build member for member, class every withheld
row with a known code, keep special and protected content out of probes, and never write the
stores it reads. All on synthetic fixtures: nothing here reads the owner's node.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path

import pytest

from tests.permissions_v2.test_ingest_provenance import ingest_fixture, owner  # noqa: F401 (fixture)
from tests.permissions_v2.test_knowledge_search import knowledge_policy, node_for
from tests.permissions_v2.test_reconciliation_provenance import legacy  # noqa: F401 (fixture)
from topos.permissions_v2 import evidence, ingest_provenance
from topos.permissions_v2.search_index import root_for

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "permissions_v2"
CONTENT = "I am working on Synthetic message at work."


def _load(name):
    """The census scripts import each other by name from their own directory, as they do when run."""
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return importlib.import_module(name)


gc = _load("grant_census")
cc = _load("census_copy")
cs = _load("census_support")


def census_of(node, *, canonical=None, live=None, durable=None):
    """The census over the node's own stores, or over a copy of them at `canonical` / `durable`."""
    resolver = node.index.resolver
    durable = Path(durable) if durable else root_for(resolver.path).parent
    return gc.run(canonical=Path(canonical or resolver.path), reviews=durable / Path(node.index.reviews.path).name,
                  ledger=node.ledger.path, index_root=durable / "message-search",
                  keys=durable / "message-search" / "keys.db", binding=resolver.binding, live_canonical=live,
                  now=node.now[0])


def built(node):
    with owner():
        assert node.index.rebuild("grant-search", now=node.now[0])["state"] == "ready"


def row_template(conn):
    columns = [r[1] for r in conn.execute("PRAGMA table_info(conversation_messages)")]
    row = dict(zip(columns, conn.execute("SELECT * FROM conversation_messages").fetchone()))
    return columns, row


def insert(conn, columns, row, **changes):
    values = {**row, **changes}
    conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                 [values.get(c) for c in columns])
    conn.commit()


def iso(epoch):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def files_digest(root: Path) -> dict:
    out = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        out[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


# --- equivalence with the node's own build -----------------------------------------------------

def test_census_members_are_the_index_members_byte_for_byte(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    census = census_of(node)
    comparison = gc.compare_index(census)
    assert comparison["sets_equal"] and comparison["census_members"] == comparison["live_members"] == 1
    (member,) = census.members.values()
    assert member.family == "message" and member.reason == "permitted"
    assert member.wire == hashlib.sha256(CONTENT.encode("utf-8")).hexdigest() == member.raw_hashes[0]
    agg = gc.aggregate(census, run_at="t")
    assert agg["gate"] == {"census_equals_live_count": True, "census_equals_live_set": True, "unknown_reasons": 0,
                           "node_source_drift": None, "void_reasons": []}
    assert agg["U"] == 1 and agg["U_by_class"] == {"member": 1}
    (row,) = [r for r in agg["funnel"] if r["source_id"] == "imessage"]
    assert (row["display_name"], row["canonical_group_id"]) == ("iMessage", "conversations") and row["p_impl"] == 1


@pytest.mark.parametrize("labels,code,klass", [
    ({"domains": ["work", "health"]}, "special_sensitivity", "policy"),
    ({"sensitivity": "special"}, "special_sensitivity", "policy"),
    ({"protected_content": "unknown"}, "protected_content_unknown_model", "engineering"),
    ({"speech": "third_party_quote"}, "not_original_message", "policy"),
])
def test_withheld_labels_are_classed_and_the_index_agrees(legacy, tmp_path, monkeypatch, labels, code, klass):
    node, _ = node_for(legacy, tmp_path, monkeypatch, labels=labels)
    built(node)
    census = census_of(node)
    assert gc.compare_index(census)["sets_equal"] and not census.members
    (outcome,) = census.outcomes
    assert (outcome.reason, gc.reason_class(outcome.reason)) == (code, klass)
    agg = gc.aggregate(census, run_at="t")
    assert agg["gate"]["unknown_reasons"] == 0
    body = gc.private(census, run_at=100)
    assert body["probes"] == []                      # nothing withheld ever becomes query text
    assert {"sha256": hashlib.sha256(CONTENT.encode()).hexdigest(), "class": gc.public_code(code)} in body["forbidden"]


def test_every_row_is_examined_and_policy_vetoes_separate_real_losses(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    conn = legacy[1]
    columns, row = row_template(conn)
    now = node.now[0]
    base = {"conversation_id": "other-conversation", "owner_user_id": None}
    insert(conn, columns, row, message_id="imessage:2", content="Someone else wrote this reply.", is_from_self=0,
           sender_id="contact-9", event_at=iso(now - 3600), **base)
    insert(conn, columns, row, message_id="imessage:3", content="I sent this without a provenance link.", is_from_self=1,
           event_at=iso(now - 7200), **base)
    window = knowledge_policy()["search"]["window"]["max_age_seconds"]
    insert(conn, columns, row, message_id="imessage:4", content="An old message from long ago.", is_from_self=1,
           event_at=iso(now - window - 2 * 86400), **base)
    insert(conn, columns, row, message_id="other:5", source_id="other-source", content="From a source nobody selected.",
           is_from_self=1, event_at=iso(now - 600), **base)
    census = census_of(node)
    reasons = {o.record_id: (o.reason, o.veto) for o in census.outcomes}
    assert reasons["imessage:2"] == ("provenance_unlinked", "not_owner_authored")
    assert reasons["imessage:3"] == ("provenance_unlinked", None)
    assert reasons["other:5"][1] == "source_unselected"
    assert "imessage:4" not in reasons and [r[2] for r in census.other_rows] == ["old"]
    agg = gc.aggregate(census, run_at="t")
    assert agg["U_by_class"] == {"member": 1, "engineering_masked_by_policy": 2, "engineering_loss": 1}
    assert agg["gate"]["unknown_reasons"] == 0
    assert gc.compare_index(census)["sets_equal"]


# --- the copy: alias, no writes, restoration ---------------------------------------------------

def _copy_layout(tmp_path, node):
    """The node's stores at another path, as census_copy places them: backups, then byte copies."""
    source_db = Path(node.index.resolver.path)
    durable = source_db.parent / "permissions-v2"
    target = tmp_path / "copy"
    shutil.copytree(durable, target / "permissions-v2")
    for path in (target / "permissions-v2").rglob("*.db"):
        if path.parent.name != "ingest-snapshots":
            path.unlink()
            cc._backup(durable / path.relative_to(target / "permissions-v2"), path)
    cc._backup(source_db, target / source_db.name)
    for directory in (target, target / "permissions-v2", target / "permissions-v2" / "ingest-snapshots",
                      target / "permissions-v2" / "message-search"):
        os.chmod(directory, 0o700)
    return target


def test_a_copy_at_another_path_reads_as_the_live_node_only_through_the_alias(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path / "node-home", monkeypatch)
    built(node)
    live = census_of(node)
    copy = _copy_layout(tmp_path, node)
    canonical = copy / Path(node.index.resolver.path).name
    before = files_digest(copy)
    original = evidence.EvidenceResolver._file_revision, ingest_provenance.IngestProvenanceService._publish_marker
    aliased = census_of(node, canonical=canonical, live=str(node.index.resolver.path), durable=copy / "permissions-v2")
    assert set(aliased.members) == set(live.members) and len(aliased.members) == 1
    assert aliased.counters["aliased_revisions"] > 0
    unaliased = census_of(node, canonical=canonical, live=None, durable=copy / "permissions-v2")
    assert not unaliased.members                      # the path is part of every pinned revision
    assert files_digest(copy) == before               # nothing the census read was written
    assert (evidence.EvidenceResolver._file_revision, ingest_provenance.IngestProvenanceService._publish_marker) == original


def test_backup_folds_the_wal_into_a_closed_copy(tmp_path):
    source = tmp_path / "wal.db"
    writer = sqlite3.connect(source)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("CREATE TABLE t(v)")
    writer.executemany("INSERT INTO t VALUES(?)", [(n,) for n in range(500)])
    writer.commit()
    assert Path(str(source) + "-wal").stat().st_size > 0   # the rows live in the WAL, not the main file
    target = tmp_path / "copy.db"
    cc._backup(source, target)
    writer.close()
    assert not any(Path(str(target) + s).exists() for s in cs.SIDECARS)
    conn = cs.ro(target, immutable=True)
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 500
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    assert oct(target.stat().st_mode & 0o777) == "0o600"


def test_the_census_takes_the_owners_topos_directory_as_the_live_home():
    assert cs.LIVE_HOME == Path.home() / ".topos"


def test_the_live_store_is_refused_as_input_or_output(tmp_path, monkeypatch):
    # Built from cs.LIVE_HOME (pinned to the owner's directory by the test above), so the paths are only ever
    # refused, never opened; tests/test_owner_database_hermeticity.py reads a spelled-out home path as a reach.
    with pytest.raises(cs.CensusRefused):
        cs.refuse_live(cs.LIVE_HOME / "database.db")
    with pytest.raises(cs.CensusRefused):
        cs.refuse_live(cs.LIVE_HOME / "permissions-v2" / "anything")
    assert cs.refuse_live(tmp_path / "x") == tmp_path / "x"
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(cs.LIVE_HOME / "database.db"))
    with pytest.raises(cs.CensusRefused):
        cs.require_scratch_environment()


# --- the private oracle --------------------------------------------------------------------------

def test_private_file_keys_members_by_wire_hash_and_expires_within_seven_days(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    census = census_of(node)
    body = gc.private(census, run_at=1000)
    assert body["delete_after"] - body["run_at"] <= 7 * 86400
    (member,) = body["members"]
    assert member["sha256_wire"] == hashlib.sha256(CONTENT.encode()).hexdigest()
    assert member["opaque_id"].startswith("r.") and member["stage_reached"] in ("indexed", "vector")
    probes = [p for p in body["probes"] if p["kind"] == "idf"]
    assert probes and all(p["target_opaque_id"] == member["opaque_id"] and p["expect"] == "hit" for p in probes)
    assert body["projection_version"] == gc.PROJECTION_VERSION and member["family"] == "message"
    target = tmp_path / "private"
    cs.private_dir(target)
    cs.write_private(target / "if1-private-1000.json", json.dumps(body).encode())
    assert oct((target / "if1-private-1000.json").stat().st_mode & 0o777) == "0o600"
    assert gc.purge(target, now=1000 + 7 * 86400) == {"private_files": 1}
    assert not any(target.iterdir())


def test_each_probe_says_whether_its_target_has_a_vector(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    census = census_of(node)
    (opaque,) = census.members
    for vectored in (True, False):               # both ways, so a constant flag cannot pass
        census.index["members"][opaque]["vector"] = vectored
        idf = [p for p in gc.private(census, run_at=1)["probes"] if p["kind"] == "idf"]
        assert idf and all(p["target_vectored"] is vectored for p in idf)
        assert gc.mark_vectors(idf, census) == {"idf": {("vectored" if vectored else "unvectored"): len(idf)}}
    others = [{"kind": "paraphrase", "target_opaque_id": "r.not-in-the-index"}, {"kind": "negative"}]
    assert gc.mark_vectors(others, census) == {"negative": {"no_target": 1}, "paraphrase": {"unvectored": 1}}
    assert [p["target_vectored"] for p in others] == [False, None]
    census.index["state"] = "missing"            # an unreadable index says unknown, never "no vector"
    assert gc.mark_vectors(others, census) == {"negative": {"no_target": 1}, "paraphrase": {"unknown": 1}}
    assert others[0]["target_vectored"] is None


def _node_package(tmp_path, *, edit=None, drop=None):
    """A stand-in install: the checkout's own mirrored modules, one function edited or one module left out."""
    root = tmp_path / "installed" / "topos"
    (root / "permissions_v2").mkdir(parents=True)
    checkout = Path(gc.__file__).resolve().parents[2] / "topos" / "permissions_v2"
    for module in sorted({name.split(".", 1)[0] for name in gc.PINNED} - {drop}):
        text = (checkout / f"{module}.py").read_text()
        if edit and module == edit[0]:
            assert text.count(edit[1]) == 1
            text = text.replace(edit[1], edit[1] + "\n        # a moved line")
        (root / "permissions_v2" / f"{module}.py").write_text(text)
    return root


def test_the_installed_nodes_source_is_read_as_text_and_compared_with_the_pins(tmp_path):
    same = _node_package(tmp_path / "a")
    assert gc.node_source_check(same) == {"checked": True, "drift": []}     # parsed text hashes as inspect does
    moved = _node_package(tmp_path / "b", edit=("search_index", "    def _members(self, conn, key, grant_id, members, model):"))
    assert gc.node_source_check(moved) == {"checked": True, "drift": ["search_index.SearchIndexService._members"]}
    missing = _node_package(tmp_path / "c", drop="release")
    assert gc.node_source_check(missing)["drift"] == ["release.source_message_decision"]
    assert gc.node_source_check(None) == gc.node_source_check(tmp_path / "nowhere") == {"checked": False, "drift": None}


def test_node_drift_voids_the_census_only_where_the_build_disagrees(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    census = census_of(node)
    drifted, clean = {"checked": True, "drift": ["search_index.SearchIndexService._members"]}, {"checked": True, "drift": []}
    assert gc.aggregate(census, run_at="t", node_source=drifted)["gate"]["void_reasons"] == []   # the sets agree
    census.index["members"]["r.aged"] = {"event_us": census.lower_us - 1, "vector": False, "fields": None}
    assert gc.aggregate(census, run_at="t", node_source=drifted)["gate"]["void_reasons"] == []   # aging explains it
    census.index["members"]["r.unexplained"] = {"event_us": census.upper_us - 1, "vector": False, "fields": None}
    gate = gc.aggregate(census, run_at="t", node_source=drifted)["gate"]
    assert gate["void_reasons"] == ["node_source_drift_with_unexplained_members"] and gate["node_source_drift"] == 1
    assert gc.aggregate(census, run_at="t", node_source=clean)["gate"]["void_reasons"] == []    # IF-1's own rule judges it
    del census.index["members"]["r.unexplained"], census.index["members"]["r.aged"]
    census.members["r.census-only"] = next(iter(census.members.values()))
    assert gc.aggregate(census, run_at="t", node_source=drifted)["gate"]["void_reasons"] == [
        "node_source_drift_with_unexplained_members"]


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)   # the message states the goal outright
def test_a_lane_goal_whose_lineage_went_stale_leaves_every_goal_column(legacy, tmp_path, monkeypatch):
    from tests.permissions_v2 import test_permitted_derivation as lane
    from topos.permissions_v2 import permitted_derivation as pd
    lane.goal_store(legacy)
    node, _ = lane.node_for(legacy, tmp_path, monkeypatch, labels={"domains": ["work", "plans"]})
    node.rebuild()
    assert lane.run_lane(node, lane.Spy(pd.Spec("goal", "goal", "finish the compiler at work by Friday")))["goal:written"] == 1
    levers = lambda goals: {k: v for k, v in goals.items() if k.startswith("levers:")}
    fresh = census_of(node).rd11["goals"]
    assert fresh["od46_lane"] == 1 and fresh["levers:none"] == 1        # released as it stands
    conn = legacy[1]
    (goal_id, payload_json), = conn.execute("SELECT goal_id, payload_json FROM user_goals").fetchall()
    payload = json.loads(payload_json)
    payload["lineage"]["message_revision"] = "0" * 64                    # the message it names has since changed
    conn.execute("UPDATE user_goals SET payload_json=? WHERE goal_id=?", (json.dumps(payload), goal_id))
    conn.commit()
    node.rebuild()
    stale = census_of(node).rd11["goals"]
    assert stale["od46_lane"] == 1 and stale["only_fails_lineage"] == 1
    assert levers(stale) and not any(levers(stale).values())            # no lever brings a stale lane goal back


def test_a_classed_pack_fact_is_judged_on_its_scalar_field_not_the_raw_value(legacy, tmp_path, monkeypatch):
    from tests.permissions_v2 import test_permitted_derivation as lane
    from topos.permissions_v2.predicate_classes import CLASSES, scalar
    key = CLASSES["work.project"].key
    assert key is not None                                               # a keyed class: the value lives in value_struct
    node, _ = lane.node_for(legacy, tmp_path, monkeypatch)
    node.rebuild()
    lane.run_lane(node, lane.Spy(lane.PROJECT))
    conn = legacy[1]
    (object_id, payload_json), = conn.execute(
        "SELECT object_id, payload_json FROM signal_objects WHERE object_type='fact'").fetchall()
    payload = json.loads(payload_json)
    released = scalar("work.project", payload)
    payload["value_struct"] = {**(payload.get("value_struct") or {}), key: released}
    payload["object_value"] = json.dumps({"kind": "test"})                # the raw field is the pack's JSON, not a label
    assert scalar("work.project", payload) == released
    conn.execute("UPDATE signal_objects SET payload_json=? WHERE object_id=?", (json.dumps(payload), object_id))
    conn.commit()
    node.rebuild()
    facts = census_of(node).rd11["facts"]
    assert facts["funnel_grounded"] == 1 and facts["levers:none"] == 1 and facts["widened_levers:none"] == 1


def test_only_the_widened_predicates_with_a_census_case_have_an_entailment_template():
    """A template for a widened predicate makes widened_levers under the entailment and owner_confirms_all
    columns live. work.project has one, with its census case in test_permitted_derivation
    (test_the_census_counts_a_widened_fact_under_owner_confirm_once_it_has_a_template). commit.made has none:
    the not-yet-started guard would refuse every commitment. Add a census case before adding any other."""
    from topos.permissions_v2 import entailment_grounding as eg
    from topos.permissions_v2.predicate_classes import WIDENED
    templated = {predicate for predicate in WIDENED if eg.fact_claim(predicate, "a label") is not None}
    assert templated == {"work.project"}


@pytest.mark.parametrize("words", [0, 2, 3, 7, 8, 20])
def test_the_run_extraction_is_the_vendored_entries_rule(words):
    import census_shingles
    scheme = census_shingles.Scheme(3, 8, "hmac-sha256", bytes(range(32)))
    text = " ".join(f"Word{i}," for i in range(words))
    assert [scheme.hash(run) for run in gc._shingle_runs(scheme, text)] == scheme.entries(text)


def test_convergent_phrasing_needs_member_sources_and_no_message_with_the_words(legacy, tmp_path, monkeypatch):
    import census_shingles
    from tests.permissions_v2.test_permitted_derivation import goal_store
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    conn = legacy[1]
    (member_id,) = conn.execute("SELECT message_id FROM conversation_messages").fetchone()
    # A withheld message OUTSIDE the member's conversation (an undated AI-chat row): a row added to the member's own
    # conversation would move its review context and rightly cost it membership.
    conn.execute("CREATE TABLE IF NOT EXISTS ai_chat_messages(message_id TEXT, content TEXT)")
    conn.execute("INSERT INTO ai_chat_messages(message_id, content) VALUES(?, ?)",
                 ("withheld-1", "please ship the release notes to everyone on the platform team today thanks"))
    conn.commit()
    goal_store(legacy)
    goals = {"A": (member_id, "complete the compiler work before the weekend arrives soon"),
             "B": ("not-a-member", "prepare the quarterly budget slides for the leadership offsite meeting"),
             "C": (member_id, "ship the release notes to everyone on the platform team today")}
    for goal_id, (record_id, text) in goals.items():
        conn.execute("INSERT INTO user_goals(goal_id,record_id,source_id,goal_text) VALUES(?,?,?,?)",
                     (goal_id, record_id, "imessage", text))
    conn.commit()
    census = census_of(node)
    assert {o.record_id for o in census.members.values() if o.family == "message"} == {member_id}
    key = bytes(range(32))
    block = gc.private(census, run_at=1, shingle_key=key)["shingles"]
    scheme = census_shingles.Scheme(3, 8, "hmac-sha256", key)
    hashes = {g: {scheme.hash(run) for run in gc._shingle_runs(scheme, text)} for g, (_rid, text) in goals.items()}
    eligible = set(block["convergent_eligible"])
    assert hashes["A"] and hashes["A"] <= eligible                     # member-sourced, words in no message
    assert not hashes["B"] & eligible                                  # derived from a message the grant withholds
    assert not hashes["C"] & eligible                                  # its words are in a (withheld) message
    assert all(set(block["classes"][h]) == {"typed_withheld_goal"} for h in eligible)
    assert hashes["A"] <= set(block["hashes"]) and hashes["B"] <= set(block["hashes"])   # nothing leaves the scan


def test_shingles_are_the_harness_scheme_with_the_pinned_vectors():
    """census_shingles.py is WS8's reference (boundary battery fe8e5cdc) vendored verbatim; these are its vectors."""
    import census_shingles as sh
    words = "zorbel quiffle plonk vesk trillow snib wopple klemt"
    assert sh.Scheme(3, 8, "hmac-sha256", bytes(range(32))).hash(words) == \
        "56dd0169c6941938b1d2935b829c93af11417e577d9f1370294d25beff9113d8"
    assert sh.Scheme(3, 8, "sha256", None).hash(words) == "a4c2fd19863e679c6406150fdb9e9e6ef259d47c6fff5ef1a56ca69bcea8a82f"
    assert sh.normalize("Hello,  WORLD_x!") == gc.normalize("Hello,  WORLD_x!") == "hello world x"
    key = bytes(range(32))
    block = sh.build([("one two three four five six seven eight nine", "a"), ("short text here", "b"), ("ok thanks", "c"),
                      ("shared words appear in a member too", "d")],
                     ["members say shared words appear in a member too"], key=key)
    assert block["scheme"] == "canary-v1/words:3-8/hmac-sha256" and block["key_hex"] == key.hex()
    assert block["counts"] == {"items": 4, "items_whole": 2, "items_skipped": 1, "ambiguous_dropped": 1, "hashes": 3}


def test_every_reason_code_has_a_class():
    assert not (gc.ENGINEERING & gc.POLICY)
    assert gc.reason_class("entity_protected") == "policy" and gc.public_code("entity_protected") == "protected"
    assert gc.reason_class("something_new") == "unknown"


def test_mirrored_engine_source_is_the_source_the_census_was_read_against():
    """A change to any mirrored engine function stops the census until this file and grant_census.py are re-read."""
    assert gc.mirrored_sources() == gc.PINNED
    from topos.permissions_v2.search_index import SearchIndexService
    assert gc.EMBED_CAP == SearchIndexService.EMBEDDINGS_PER_BUILD and gc.KNOWLEDGE_MAX_CHARS == 8000


def node_with_window(legacy, tmp_path, monkeypatch, *, age_days, window_days, labels=None, sources=None,
                     permit_only=False):
    """node_for with the grant's rolling window and the run instant chosen: one reviewed, provenanced row."""
    from types import SimpleNamespace
    from tests.permissions_v2 import message_search_corpus as mc
    from tests.permissions_v2.message_search_harness import Node
    from tests.permissions_v2.test_automatic_message_review import answer, setup
    from topos.permissions_v2.automatic_message_review import publish
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    resolver, reviews, _identity, prepared = setup(legacy)
    classification = answer(prepared)
    if labels:
        classification = classification.model_copy(update=labels)
    with owner():
        publish(resolver, reviews, prepared, classification, now=1)
    stamp = legacy[1].execute("SELECT event_at FROM conversation_messages").fetchone()[0]
    now = canonical_utc_microseconds(stamp) // 1_000_000 + int(age_days * 86400)
    monkeypatch.setattr(mc, "NOW", now)
    raw = knowledge_policy()
    raw["search"]["window"]["max_age_seconds"] = int(window_days * 86400)
    if permit_only:  # the live grant's shape: one permit rule, no deny rules
        raw["rules"] = [rule for rule in raw["rules"] if rule["effect"] == "permit"]
    if sources is not None:
        raw["source_universe"]["source_ids"] = list(sources)
        for rule in raw["rules"]:
            rule["evidence_use"]["sources"] = {"kind": "only", "values": list(sources)}
    return Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), tmp_path / "node",
                model=None, search_raw=raw, now=now)


@pytest.mark.parametrize("labels,probed", [(None, True), ({"sensitivity": "special"}, False)])
def test_a_row_just_past_the_window_edge_is_forbidden_and_probed_only_when_ordinary(legacy, tmp_path, monkeypatch,
                                                                                   labels, probed):
    node = node_with_window(legacy, tmp_path, monkeypatch, age_days=1.5, window_days=1, labels=labels)
    built(node)                                       # the node's own build keeps nothing past the edge
    census = census_of(node)
    (outcome,) = census.outcomes
    assert outcome.band == "edge_outside" and not census.members and gc.compare_index(census)["sets_equal"]
    body = gc.private(census, run_at=1)
    negatives = [p for p in body["probes"] if p["kind"] == "negative"]
    content = hashlib.sha256(CONTENT.encode()).hexdigest()
    if probed:
        assert outcome.permitted and {"sha256": content, "class": "time_edge_outside"} in body["forbidden"]
        assert [p["target_sha256"] for p in negatives] == [content] and negatives[0]["expect"] == "miss"
        assert body["time_edge"] == [{"sha256": content, "side": "outside"}] and body["time_tolerance"] == []
    else:
        assert not outcome.permitted and outcome.reason == "special_sensitivity" and negatives == []
        assert {"sha256": content, "class": "special_sensitivity"} in body["forbidden"]


def test_an_assessable_row_without_a_current_review_is_unassessed_not_hidden(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    with sqlite3.connect(node.index.reviews.path) as db:
        db.execute("UPDATE fact_reviews SET active=0")
    census = census_of(node)
    (outcome,) = census.outcomes
    assert (outcome.reason, outcome.veto, gc.reason_class(outcome.reason)) == ("unassessed", None, "engineering")
    assert gc.aggregate(census, run_at="t")["U_by_class"] == {"engineering_loss": 1}


@pytest.mark.parametrize("gate,code", [(None, "unassessed"), ("text", "message_classification_too_large"),
                                       ("context", "message_context_too_large")])
def test_unassessed_means_a_pass_would_assess_it_and_the_reviewers_own_pass_agrees(legacy, tmp_path, monkeypatch,
                                                                                    gate, code):
    """A row the reviewer's prepare() refuses is named by that gate: the worker files it as withheld on every
    pass, so calling it `unassessed` would send the owner to wait for a pass that never assesses it."""
    import asyncio
    from tests.permissions_v2.test_automatic_message_review import answer
    from topos.permissions_v2 import automatic_message_review as amr
    from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
    from topos.permissions_v2.fact_eligibility import canonical_utc_microseconds
    from topos.permissions_v2.message_review_contract import AutomaticReviewRequest
    node, identity = node_for(legacy, tmp_path, monkeypatch)
    conn = legacy[1]
    columns, row = row_template(conn)
    stamp = canonical_utc_microseconds(row["event_at"]) // 1_000_000
    if gate == "text":
        monkeypatch.setattr(amr, "MAX_TEXT_CHARS", len(CONTENT) - 1)
    if gate == "context":  # one reply in the same conversation longer than the classifier's whole context budget
        insert(conn, columns, row, message_id="imessage:reply", is_from_self=0, event_at=iso(stamp + 30),
               content="word " * (amr.MAX_CONTEXT_CHARS // 5 + 1))
    with sqlite3.connect(node.index.reviews.path) as db:
        db.execute("UPDATE fact_reviews SET active=0")
    census = census_of(node)
    (outcome,) = [o for o in census.outcomes if o.record_id == row["message_id"]]
    assert (outcome.reason, outcome.veto, gc.reason_class(outcome.reason)) == (code, None, "engineering")

    async def classify(prepared):
        return answer(prepared)
    worker = AutomaticReviewWorker(node.index.resolver, node.index.reviews, classifier=classify)
    with owner():
        asyncio.run(worker._process(AutomaticReviewRequest(after=stamp - 86400, before=stamp + 86400), refresh=False))
        status = worker.status()
        with node.index.reviews._db() as db:
            assessed = node.index.reviews._current_in(db, amr.machine_key(identity)) is not None
    assert assessed == (code == "unassessed") and status.assessed == int(code == "unassessed")
    assert status.withheld == {"unassessed": 0, "message_classification_too_large": 1,
                               "message_context_too_large": 2}[code]  # the context case: its unprovenanced reply too


def test_the_family_table_walks_only_the_message_tables_and_declares_journal_and_interest():
    """IF-5: every evidence table the census knows is one declared family. Only families the engine can qualify are
    walked, with the census's own time rule; the rest are counted until the engine's registry lands."""
    walked = [f for f in gc.FAMILIES if f.walked]
    assert gc.LEAF_TABLES == ("conversation_messages", "ai_chat_messages") == tuple(f.table for f in walked)
    assert {f.time_semantics for f in walked} == {"canonical_utc"}
    declared = {f.family: f for f in gc.FAMILIES if not f.walked}
    assert set(declared) == {"journal_entry", "interest"}
    assert declared["journal_entry"].time_semantics == "stated_day_v1" and declared["journal_entry"].content_column
    assert declared["interest"].table == "activity_events" and declared["interest"].content_column is None
    assert {"journal_owner_unproven", "interest_source_unproven"} <= gc.UNPROVEN
    assert all(gc.reason_class(code) != "unknown" for code in (          # IF-5 §6: every new code is classed
        "journal_owner_unproven", "journal_time_unknown", "journal_copy_alias", "journal_citation_needs_record_option",
        "interest_below_threshold", "interest_label_withheld", "interest_source_unproven"))


def test_the_exposure_card_splits_in_window_rows_into_provable_assessed_and_members(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    conn = legacy[1]
    columns, row = row_template(conn)
    insert(conn, columns, row, message_id="imessage:3", content="I sent this without a provenance link.", is_from_self=1,
           event_at=iso(node.now[0] - 7200), conversation_id="other-conversation", owner_user_id=None)
    card = gc.aggregate(census_of(node), run_at="t")["exposure"]["message"]
    assert card == {"walked": True, "in_window": 2, "provable": 1, "assessed": 1, "members": 1}
    with sqlite3.connect(node.index.reviews.path) as db:     # the proven row loses its review
        db.execute("UPDATE fact_reviews SET active=0")
    card = gc.aggregate(census_of(node), run_at="t")["exposure"]["message"]
    assert card == {"walked": True, "in_window": 2, "provable": 1, "assessed": 0, "members": 0}
    conn.execute("INSERT INTO ai_chat_messages VALUES('chat:copy', ?)", (CONTENT,))   # now withheld as a copy, before
    conn.commit()                                                                     # any review: still not assessed
    census = census_of(node)
    assert {o.record_id: o.reason for o in census.outcomes}["imessage:1"] == "independent_copy_lineage"
    assert gc.aggregate(census, run_at="t")["exposure"]["message"] == card


def test_a_declared_family_is_counted_and_its_text_is_withheld_until_the_engine_walks_it(legacy, tmp_path, monkeypatch):
    """No journal entry can be a member yet, so every journal text is forbidden text (with its shingles) and never
    a probe; activity rows are counted in their window but their titles are not text the census releases or forbids."""
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    conn, now = legacy[1], node.now[0]
    from datetime import datetime, timezone
    conn.execute("CREATE TABLE journal_entries(entry_id TEXT PRIMARY KEY, entry_at TEXT, content TEXT, source_id TEXT NOT NULL, "
                 "writer_class TEXT, ingested_at TEXT)")
    conn.execute("CREATE TABLE activity_events(event_id TEXT PRIMARY KEY, occurred_at TEXT, title TEXT, url TEXT, "
                 "source_id TEXT NOT NULL)")
    journal = "Today I wrote a synthetic journal entry about the compiler at work."
    recent = datetime.fromtimestamp(now - 3 * 86400, timezone.utc).strftime("%Y-%m-%dT08:00:00")   # naive: a stated day
    conn.executemany("INSERT INTO journal_entries VALUES(?,?,?,?,?,?)", [
        ("j1", recent, journal, "grow_journal", "owner_app", "2026-09-01 10:00:00"),       # the door's first stamp
        ("j2", recent, "", "grow_journal", None, "2026-09-02 10:00:00"),                   # unstamped after it
        ("j3", "sometime", "Another synthetic line.", "grow_journal", None, "2026-08-01 10:00:00")])  # pre-stamp, no time
    conn.executemany("INSERT INTO activity_events VALUES(?,?,?,?,?)",
                     [("a1", iso(now - 3600), "A synthetic page title", "https://example.invalid/a", "browser_visits"),
                      ("a2", iso(now - 400 * 86400), "An old synthetic page", "https://example.invalid/b", "browser_visits")])
    conn.commit()
    census = census_of(node)
    assert {o.table for o in census.outcomes} == {"conversation_messages"}          # declared families are not walked
    agg = gc.aggregate(census, run_at="t")
    rows = {r["family"]: r for r in agg["funnel"] if not r["walked"]}
    journal_row = rows["journal_entry"]
    assert {k: journal_row[k] for k in ("rows", "U", "time_unknown", "provable", "time_rule", "writer_unstamped",
                                        "receipt_missing", "install_bound")} == \
        {"rows": 3, "U": 2, "time_unknown": 1, "provable": 0, "time_rule": "stated_day_v1", "writer_unstamped": 1,
         "receipt_missing": None, "install_bound": False}      # no install binds the source: nothing is attestable
    assert (rows["interest"]["rows"], rows["interest"]["U"], rows["interest"]["provable"]) == (2, 1, None)
    assert agg["exposure"]["journal_entry"] == {"walked": False, "in_window": 2, "provable": 0, "assessed": None,
                                                "members": 0}
    assert agg["exposure"]["interest"]["in_window"] == 1 and agg["exposure"]["message"]["members"] == 1
    body = gc.private(census, run_at=1)
    forbidden = {f["sha256"]: f["class"] for f in body["forbidden"]}
    assert forbidden[hashlib.sha256(journal.encode()).hexdigest()] == "not_walked_journal_entry"
    assert "not_walked_journal_entry" in {c for classes in body["shingles"]["classes"].values() for c in classes}
    assert not any(hashlib.sha256(t.encode()).hexdigest() in forbidden for t in ("A synthetic page title", "An old synthetic page"))
    assert all(p.get("target_sha256") != hashlib.sha256(journal.encode()).hexdigest() for p in body["probes"])


def test_a_short_typed_phrase_inside_a_journal_entry_is_never_convergent_phrasing(legacy, tmp_path, monkeypatch):
    """A 3-7 word typed phrase is shingled whole, a journal entry in 8-word runs, so their hashes never meet: only
    the word scan of convergent rule (b) sees that the phrase is journal wording, which no recipient may echo."""
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    conn = legacy[1]
    conn.execute("CREATE TABLE journal_entries(entry_id TEXT PRIMARY KEY, entry_at TEXT, content TEXT, source_id TEXT NOT NULL)")
    conn.execute("INSERT INTO journal_entries VALUES('j1','2026-09-29T08:00:00',?,'grow_journal')",
                 ("Today I wrote a synthetic journal entry about the compiler at work.",))
    conn.commit()
    census = census_of(node)
    census.typed_withheld += [("goal", "journal entry about the compiler", True),        # inside the journal entry
                              ("goal", "a short phrase from nowhere", True)]             # control: nowhere else
    assert len(gc.private(census, run_at=1)["shingles"]["convergent_eligible"]) == 1


def test_a_withheld_typed_item_is_keyed_by_what_it_cites_never_guessed(tmp_path):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE conversation_messages(message_id TEXT, source_id TEXT, content TEXT)")
        db.execute("CREATE TABLE journal_entries(entry_id TEXT, source_id TEXT, content TEXT)")
        db.execute("CREATE TABLE signal_objects(object_id TEXT, source_refs_json TEXT)")
        db.execute("CREATE TABLE user_goals(goal_id TEXT, record_id TEXT)")
        db.execute("INSERT INTO conversation_messages VALUES('m1','imessage','x')")
        db.execute("INSERT INTO journal_entries VALUES('j1','grow_journal','y')")
        db.executemany("INSERT INTO signal_objects VALUES(?,?)", [
            ("both", json.dumps([{"record_id": "m1"}, {"table": "journal_entries", "record_id": "j1"}])),
            ("named", json.dumps([{"table": "journal_entries", "record_id": "j9", "source_id": "grow_data_file"}])),
            ("nowhere", json.dumps([{"record_id": "gone"}]))])
        db.execute("INSERT INTO user_goals VALUES('g1','j1')")
        index = gc.evidence_records(db)
        assert gc.typed_evidence(db, "signal_objects", "both", index) == (("conversation_messages", "imessage"),
                                                                          ("journal_entries", "grow_journal"))
        assert gc.typed_evidence(db, "signal_objects", "named", index) == (("journal_entries", "grow_data_file"),)
        assert gc.typed_evidence(db, "signal_objects", "nowhere", index) == (("unresolved", None),)
        assert gc.typed_evidence(db, "user_goals", "g1", index) == (("journal_entries", "grow_journal"),)


def test_an_item_grounded_in_two_families_is_counted_under_each_and_flagged():
    rows = [{"family": "goal", "table": "conversation_messages", "candidates": 1, "p_impl": 0, "multi_evidence": 1},
            {"family": "goal", "table": "journal_entries", "candidates": 1, "p_impl": 0, "multi_evidence": 1},
            {"family": "message", "table": "conversation_messages", "U": 5}]
    assert gc.typed_by_evidence(rows) == {"goal": {"conversation_messages": {"candidates": 1, "members": 0, "multi_evidence": 1},
                                                   "journal_entries": {"candidates": 1, "members": 0, "multi_evidence": 1}}}


def test_the_copy_report_counts_the_family_tables(tmp_path):
    path = tmp_path / "db.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE journal_entries(entry_id TEXT)")
        db.executemany("INSERT INTO journal_entries VALUES(?)", [("a",), ("b",)])
    counts = cc._counts(path)
    assert counts["journal_entries"] == 2 and counts["activity_events"] is None and counts["conversation_messages"] is None


@pytest.mark.parametrize("labels,probed", [(None, True), ({"domains": ["work", "health"]}, False)])
def test_an_unselected_source_is_probed_only_when_its_labels_are_ordinary(legacy, tmp_path, monkeypatch, labels, probed):
    node = node_with_window(legacy, tmp_path, monkeypatch, age_days=0.001, window_days=30, labels=labels,
                            sources=["signal"])
    built(node)
    census = census_of(node)
    (outcome,) = census.outcomes
    assert outcome.reason == "source_unselected" and outcome.veto == "source_unselected" and not census.members
    strata = [r for r in gc.aggregate(census, run_at="t")["strata"] if r["source_id"] == "imessage"]
    assert strata and all("unselect" in r["reason_code"] and r["reason_class"] == "policy" for r in strata)
    negatives = [p for p in gc.private(census, run_at=1)["probes"] if p["kind"] == "negative"]
    assert (len(negatives) == 1) is probed


@pytest.mark.parametrize("legacy", ["goal"], indirect=True)
def test_typed_members_match_the_build_and_stay_out_of_the_message_count(legacy, tmp_path, monkeypatch):
    from tests.permissions_v2.test_knowledge_search import add_goal_graph
    add_goal_graph(legacy)
    node, _ = node_for(legacy, tmp_path, monkeypatch, labels={"domains": ["work", "plans"]})
    built(node)
    census = census_of(node)
    assert gc.compare_index(census)["sets_equal"]
    families = sorted(o.family for o in census.members.values())
    assert families == ["goal", "message", "relationship"]
    agg = gc.aggregate(census, run_at="t")
    assert agg["U_by_class"] == {"member": 1} and agg["families"]["goal"] == agg["families"]["relationship"] == 1
    (goal,) = [o for o in census.members.values() if o.family == "goal"]
    assert goal.wire == hashlib.sha256("finish the compiler at work by Friday".encode()).hexdigest()
    assert goal.raw_hashes == [hashlib.sha256("My goal is to finish the compiler at work by Friday.".encode()).hexdigest()]
    # WS2: typed rows are keyed by the evidence they are grounded in, not by their store table.
    typed_rows = {(r["family"], r["table"], r["source_id"]) for r in agg["funnel"] if r["family"] in gc.TYPED}
    assert typed_rows == {("goal", "conversation_messages", "imessage"), ("relationship", "conversation_messages", "imessage")}
    assert agg["typed_by_evidence"] == {family: {"conversation_messages": {"candidates": 1, "members": 1, "multi_evidence": 0}}
                                        for family in ("goal", "relationship")}


def test_the_tolerance_band_excuses_time_only_and_the_private_file_carries_the_harness_shingles(legacy, tmp_path,
                                                                                                monkeypatch):
    node = node_with_window(legacy, tmp_path, monkeypatch, age_days=1 + 600 / 86400, window_days=1)
    built(node)
    census = census_of(node)
    (outcome,) = census.outcomes
    content = hashlib.sha256(CONTENT.encode()).hexdigest()
    body = gc.private(census, run_at=1, shingle_key=bytes(range(32)))
    # Ten minutes past the edge, inside the 3600 s tolerance: its time is ambiguous, so it is neither forbidden nor edge.
    assert outcome.permitted and body["time_tolerance"] == [content]
    assert all(entry["sha256"] != content for entry in body["forbidden"]) and body["time_edge"] == []
    block = body["shingles"]
    assert block["scheme"] == "canary-v1/words:3-8/hmac-sha256" and len(block["key_hex"]) == 64
    assert all(len(h) == 64 for h in block["hashes"]) and set(block["classes"]) == set(block["hashes"])


def test_a_member_near_the_edge_is_the_inside_edge_and_its_stage_is_in_the_scorer_vocabulary(legacy, tmp_path,
                                                                                             monkeypatch):
    node = node_with_window(legacy, tmp_path, monkeypatch, age_days=26, window_days=30)
    built(node)
    census = census_of(node)
    body = gc.private(census, run_at=1)
    (member,) = body["members"]
    assert body["time_edge"] == [{"sha256": member["sha256_wire"], "side": "inside"}]
    assert {m["stage_reached"] for m in body["members"]} <= {"p_impl", "eligible", "indexed", "vector"}


class _Answer:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class _StubModel:
    """The pinned local transport's shape: verify(), then client.post(); answers from a fixed list."""
    def __init__(self, queries, base_url="http://127.0.0.1:11434"):
        from topos.permissions_v2.shadow_labeler_local import MODEL
        self.base_url, self.queries, self.verified, self.sent = base_url, list(queries), False, []
        outer = self

        class Client:
            async def post(self, url, *, timeout, json):
                outer.sent.append(json["messages"][1]["content"])
                return _Answer({"model": MODEL, "done": True,
                                "message": {"content": __import__("json").dumps({"query": outer.queries.pop(0)})}})
        self.client = Client()

    async def verify(self):
        self.verified = True


def test_paraphrase_probes_come_only_from_the_local_model_and_avoid_the_members_unique_words(legacy, tmp_path,
                                                                                            monkeypatch):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    census = census_of(node)
    model = _StubModel(["someone describing an ongoing job task"])
    probes, counts = gc.paraphrase_probes(census, transport=model)
    assert model.verified and model.sent == [CONTENT] and counts == {"kept": 1}
    assert probes[0]["kind"] == "paraphrase" and probes[0]["expect"] == "hit"
    assert probes[0]["target_opaque_id"] in census.members
    reused = _StubModel(["the synthetic message at work"])     # "synthetic" is unique to the member
    assert gc.paraphrase_probes(census, transport=reused) == ([], {"reuses_unique_token": 1})
    with pytest.raises(cs.CensusRefused):
        gc.paraphrase_probes(census, transport=_StubModel(["x"], base_url="https://models.example.com"))


def test_the_revision_only_mode_reads_a_copy_and_agrees_with_the_full_census(legacy, tmp_path, monkeypatch, capsys):
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    built(node)
    census = census_of(node)
    aggregate = gc.aggregate(census, run_at="t")
    index_root = root_for(node.index.resolver.path)
    work = tmp_path / "work"
    work.mkdir()
    before = files_digest(index_root)
    chosen = gc.live_index_revision(index_root=index_root, ledger=node.ledger.path, now=node.now[0], work_parent=work)
    assert chosen["index_revision"] == aggregate["index_revision"] is not None
    assert chosen["index_content_digest"] == aggregate["index_content_digest"] is not None
    # The run record's derivation, spelled out (IF-2): sha256 over the basis JSON with sorted keys, first 16 hex.
    from topos.permissions_v2.search_index import index_path
    with sqlite3.connect(index_path(index_root, "grant-search")) as raw:
        basis_json = raw.execute("SELECT basis_json FROM meta").fetchone()[0]
    assert chosen["index_revision"] == hashlib.sha256(
        json.dumps(json.loads(basis_json), sort_keys=True).encode()).hexdigest()[:16]
    assert chosen["live_index_members"] == aggregate["live_index_members"] == 1 and chosen["index_state"] == "ready"
    assert not any(work.iterdir()) and files_digest(index_root) == before     # the copies are gone; the source untouched
    named = gc.live_index_revision(index_root=index_root, grant_id="grant-search", work_parent=work)
    assert named["index_revision"] == aggregate["index_revision"]
    assert gc.live_index_revision(index_root=index_root, grant_id="no-such-grant", work_parent=work)["index_state"] == "missing"
    grant_file = tmp_path / "grant-id"
    grant_file.write_text("grant-search\n")
    os.chmod(grant_file, 0o600)
    assert gc.main(["--index-revision", "--source-root", str(index_root.parent.parent), "--grant-id-file",
                    str(grant_file)]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["index_revision"] == aggregate["index_revision"] and set(printed) == {
        "index_revision", "index_content_digest", "live_index_members", "index_state", "copied_at"}
    assert printed["index_content_digest"] == aggregate["index_content_digest"]
    assert "grant-search" not in json.dumps(printed)


def test_the_content_digest_moves_with_members_and_vectors_but_not_with_a_reseal(tmp_path):
    def index(path, *, members, vectors, model="m", dims=4):
        conn = sqlite3.connect(path)
        conn.executescript("CREATE TABLE meta (singleton INTEGER, model TEXT, dims INTEGER);"
                           "CREATE TABLE members (opaque_id TEXT, event_at_us INTEGER, doc_len INTEGER, terms_json TEXT,"
                           " sealed BLOB); CREATE TABLE vectors (opaque_id TEXT, chunk_index INTEGER, vector BLOB);")
        conn.execute("INSERT INTO meta VALUES (1, ?, ?)", (model, dims))
        conn.executemany("INSERT INTO members VALUES (?, ?, ?, ?, ?)", members)
        conn.executemany("INSERT INTO vectors VALUES (?, ?, ?)", vectors)
        conn.commit()
        return gc.index_content_digest(conn)
    one = ("r.a", 10, 3, '{"alpha": 1}', b"seal-1")
    two = ("r.b", 20, 2, '{"beta": 2}', b"seal-2")
    base = dict(members=[one, two], vectors=[("r.a", 0, b"v")])
    digest = index(tmp_path / "base.db", **base)
    assert index(tmp_path / "again.db", **base) == digest                                     # stable
    assert index(tmp_path / "reseal.db", members=[one[:4] + (b"new",), two], vectors=base["vectors"]) == digest
    for name, changed in {"swap": dict(base, members=[one, ("r.c",) + two[1:]]),
                          "vector": dict(base, vectors=base["vectors"] + [("r.b", 0, b"v")]),
                          "terms": dict(base, members=[one, two[:3] + ('{"beta": 3}', two[4])]),
                          "time": dict(base, members=[one, (two[0], 21) + two[2:]]),
                          "model": dict(base, model="m2")}.items():
        assert index(tmp_path / f"{name}.db", **changed) != digest, name


def widened_census(node, widen):
    resolver = node.index.resolver
    durable = root_for(resolver.path).parent
    return gc.run(canonical=Path(resolver.path), reviews=durable / Path(node.index.reviews.path).name,
                  ledger=node.ledger.path, index_root=durable / "message-search", keys=None, binding=resolver.binding,
                  live_canonical=None, now=node.now[0], widen=widen)


def test_a_widened_window_is_the_census_the_node_builds_under_that_grant(legacy, tmp_path, monkeypatch):
    node = node_with_window(legacy, tmp_path, monkeypatch, age_days=40, window_days=90)     # a real 90-day grant
    built(node)
    real = census_of(node)
    assert gc.compare_index(real)["sets_equal"] and len(real.members) == 1                # the node's own build agrees
    same = widened_census(node, {"max_age_seconds": 90 * 86400})
    assert [(o.reason, o.band) for o in same.outcomes] == [(o.reason, o.band) for o in real.outcomes]
    assert len(same.members) == 1 and same.what_if["widened"]["max_age_seconds"] == 90 * 86400
    narrow = widened_census(node, {"max_age_seconds": 30 * 86400})                         # the 40-day row falls out
    agg = gc.aggregate(narrow, run_at="t")
    assert not narrow.members and agg["U"] == 0 and agg["window"]["max_age_seconds"] == 30 * 86400
    assert agg["gate"]["not_applicable"] and agg["what_if"]["label_dependent"] is False


def test_an_added_source_is_evaluated_as_if_the_grant_selected_it(legacy, tmp_path, monkeypatch):
    from topos.permissions_v2.canonical import PolicyError
    node = node_with_window(legacy, tmp_path, monkeypatch, age_days=0.001, window_days=30, permit_only=True,
                            sources=["signal"])
    built(node)
    real = census_of(node)
    assert [o.reason for o in real.outcomes] == ["source_unselected"] and not real.members
    added = widened_census(node, {"add_sources": ["imessage"]})
    (member,) = added.members.values()
    assert member.reason == "permitted" and added.what_if["widened"]["add_sources"] == ["imessage"]
    policy = gc.widen_policy(real.policy, add_sources=["imessage"])
    assert "imessage" in policy.source_universe.source_ids and all(
        "imessage" in rule.evidence_use.sources.values for rule in policy.rules if rule.effect == "permit")
    with pytest.raises(PolicyError):                    # the engine's own validator judges every synthetic grant
        gc.widen_policy(real.policy, add_tables=["not_a_table"])


def golden(domains, sensitivities, result_types=("message", "fact")):
    """A WS9-shaped golden draft: one permit rule over `domains` and `sensitivities` (predicate as compile_policy writes it)."""
    raw = knowledge_policy()
    raw["rules"] = [rule for rule in raw["rules"] if rule["effect"] == "permit"]
    predicate = {"kind": "all_of", "terms": [
        {"kind": "atom", "attribute": "domain", "operator": "intersects", "values": list(domains)},
        {"kind": "atom", "attribute": "sensitivity", "operator": "intersects", "values": list(sensitivities)}]}
    raw["rules"][0]["evidence_use"]["predicate"] = predicate
    raw["rules"][0]["release"]["predicate"] = predicate
    raw["search"]["result_types"] = list(result_types)
    return raw


def what_if_census(node, gold, labels=None):
    resolver = node.index.resolver
    durable = root_for(resolver.path).parent
    return gc.run(canonical=Path(resolver.path), reviews=durable / Path(node.index.reviews.path).name,
                  ledger=node.ledger.path, index_root=durable / "message-search", keys=None, binding=resolver.binding,
                  live_canonical=None, now=node.now[0], what_if=gold, labels=labels)


@pytest.mark.parametrize("row_domains,gold_domains,member,reason", [
    (["work"], ["work"], True, "permitted"),
    (["work", "plans"], ["work"], False, "category_excluded"),     # every label must be granted, not any
    (["work"], ["relationships"], False, "category_excluded"),
])
def test_a_what_if_policy_tallies_membership_with_the_nodes_own_decision(legacy, tmp_path, monkeypatch, row_domains,
                                                                        gold_domains, member, reason):
    node = node_with_window(legacy, tmp_path, monkeypatch, age_days=0.001, window_days=30, permit_only=True,
                            labels={"domains": row_domains})
    census = what_if_census(node, golden(gold_domains, ["none", "personal"]))
    (outcome,) = census.outcomes
    assert (bool(census.members), outcome.reason) == (member, reason)
    agg = gc.aggregate(census, run_at="t")
    assert agg["what_if"]["label_dependent"] is True and agg["index_state"] == "not_applicable"
    assert agg["gate"]["unknown_reasons"] == 0 and "census_equals_live_count" not in agg["gate"]
    assert agg["what_if"]["base_policy_hash"] != agg["policy_hash"] == agg["what_if"]["policy_hash"]
    assert agg["what_if"]["label_sources"] == {"review_store": 1}


def test_a_frozen_label_replaces_only_the_models_answer(legacy, tmp_path, monkeypatch):
    node = node_with_window(legacy, tmp_path, monkeypatch, age_days=0.001, window_days=30, permit_only=True)
    content = hashlib.sha256(CONTENT.encode()).hexdigest()
    labels = {"schema": "ws1-frozen-labels/v1", "rubric_revision": "r-test", "labels": {content: {
        "domains": ["relationships"], "sensitivity": "personal", "speech": "original_message", "protected_content": "none"}}}
    census = what_if_census(node, golden(["relationships"], ["none", "personal"]), labels=labels)
    (outcome,) = census.outcomes
    assert outcome.label_source == "frozen" and outcome.reason == "permitted" and len(census.members) == 1
    assert gc.aggregate(census, run_at="t")["what_if"]["labels"] == "frozen:r-test"
    # The floors still apply to a frozen label: a special one is refused whatever the policy says.
    labels["labels"][content]["sensitivity"] = "special"
    census = what_if_census(node, golden(["relationships"], ["none", "personal"]), labels=labels)
    assert census.outcomes[0].reason == "special_sensitivity" and not census.members


# --- OD-39: the owner's AI-chat capture ---------------------------------------------------------

def _capture_rows(node, conn):
    """Four in-window rows of the extension's source in the owner's conversation, plus one reply."""
    from topos.storage.canonical.ai_chat import CanonicalTablesManager
    from topos.storage.db.migrations.actor_role_v1 import apply_actor_role_v1_up
    # The legacy fixture carries a two-column stand-in; the capture rule reads the real chat schema.
    assert conn.execute("SELECT COUNT(*) FROM ai_chat_messages").fetchone()[0] == 0
    conn.execute("DROP TABLE ai_chat_messages")
    CanonicalTablesManager(conn)
    apply_actor_role_v1_up(conn)
    owner_id, source, when = node.index.resolver.binding.owner_id, "chatgpt_ui_conversation", iso(node.now[0] - 3600)
    conn.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, "
                 "updated_at) VALUES ('capture-1', ?, NULL, ?, ?, ?)", (owner_id, source, when, when))
    rows = [("cap-old", "user", None, None, "I am drafting a synthetic plan for work."),
            ("cap-stamped", "user", "owner_app", "chatgpt-shadow-extension", "I am drafting a second synthetic plan."),
            ("cap-grantee", "user", "cp_relay", None, "A grantee wrote this synthetic line."),
            ("cap-reply", "assistant", None, None, "Here is a synthetic reply.")]
    conn.executemany("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, "
                     "source_id, writer_class, writer_app_id) VALUES (?, 'capture-1', ?, ?, ?, ?, ?, ?)",
                     [(m, role, when, text, source, writer, app) for m, role, writer, app, text in rows])
    conn.commit()


def test_capture_prompts_are_split_by_writer_and_the_attestation_what_if_moves_only_the_pre_stamp_one(
        legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path / "node-home", monkeypatch)
    built(node)
    _capture_rows(node, legacy[1])
    copy = _copy_layout(tmp_path, node)
    canonical, durable = copy / Path(node.index.resolver.path).name, copy / "permissions-v2"
    before = files_digest(copy)

    census = census_of(node, canonical=canonical, live=str(node.index.resolver.path), durable=durable)
    # This fixture's grant selects iMessage only, so every chat row also carries that veto; the first check is
    # what the capture rule decides.
    reasons = {o.record_id: o.reason for o in census.outcomes if o.table == "ai_chat_messages"}
    assert reasons["cap-old"] == "ai_chat_capture_unattested"
    assert gc.reason_class("ai_chat_capture_unattested") == "engineering"
    assert reasons["cap-grantee"] == "ai_chat_capture_writer_refused"
    assert gc.reason_class("ai_chat_capture_writer_refused") == "policy"
    assert reasons["cap-reply"] == "provenance_unlinked"
    # The stamped capture already has its proof: it fails later, where every other message would.
    assert reasons["cap-stamped"] not in {"ai_chat_capture_unattested", "provenance_unlinked"}

    tally = {}
    with gc.assume_capture_attestation(node.index.resolver.binding.owner_id, tally):
        assumed = census_of(node, canonical=canonical, live=str(node.index.resolver.path), durable=durable)
    moved = {o.record_id: o.reason for o in assumed.outcomes if o.table == "ai_chat_messages"}
    assert tally == {"chatgpt_ui_conversation": 1}
    assert moved["cap-old"] == reasons["cap-stamped"]              # now exactly where a stamped prompt stands
    assert {k: moved[k] for k in ("cap-grantee", "cap-reply", "cap-stamped")} == {
        k: reasons[k] for k in ("cap-grantee", "cap-reply", "cap-stamped")}
    from topos.permissions_v2 import ai_chat_capture
    assert ai_chat_capture.attested_revisions.__name__ == "attested_revisions"   # restored
    assert files_digest(copy) == before                                         # nothing written


def test_a_prompt_between_long_replies_is_unassessed_and_the_reviewers_own_pass_assesses_it(
        legacy, tmp_path, monkeypatch):
    """OD-54: the census and the reviewer agree on a capture prompt whose two nearest replies exceed the cap.

    The reviewer's context is the owner's own turns, so the pass assesses every stamped prompt and the census's
    `unassessed` (a pass would assess the row) is true of it. Before OD-54 the pass withheld three of the five
    prompts as message_context_too_large while the census called them `unassessed`.
    """
    import asyncio

    from tests.permissions_v2 import test_automatic_message_review as chat
    from topos.permissions_v2 import automatic_message_review as amr
    from topos.permissions_v2.automatic_review_worker import AutomaticReviewWorker
    from topos.permissions_v2.message_review_contract import AutomaticReviewRequest
    monkeypatch.delenv("TOPOS_OWNER_CAPTURE_APP_IDS", raising=False)
    node, _ = node_for(legacy, tmp_path, monkeypatch)
    resolver, reviews, start = node.index.resolver, node.index.reviews, node.now[0] - 3600
    chat.capture_conversation(legacy[1], resolver.binding.owner_id, chat.TURNS, start=start)
    prompts = {m for m, sender, _ in chat.TURNS if sender != "assistant"}

    def chat_reasons():
        return {o.record_id: o.reason for o in census_of(node).outcomes if o.table == "ai_chat_messages"}

    before = chat_reasons()
    assert {m: before[m] for m in prompts} == dict.fromkeys(prompts, "unassessed")

    async def classify(prepared):
        return chat.answer(prepared)
    worker = AutomaticReviewWorker(resolver, reviews, classifier=classify)
    with owner():
        asyncio.run(worker._process(AutomaticReviewRequest(after=start - 60, before=start + 3600), refresh=False))
        status = worker.status()
        with reviews._db() as db:
            assessed = {m for m, _, _ in chat.TURNS if reviews._current_in(
                db, amr.machine_key(resolver._identity("ai_chat_messages", m, chat.CAPTURE_SOURCE))) is not None}
    # Exactly the five prompts; the replies (no owner provenance) are withheld. So is the fixture's iMessage row,
    # which the chat schema's migrations moved past its enrollment (provenance_link_invalid): not under test here.
    assert assessed == prompts and status.assessed == len(prompts)
    after = chat_reasons()
    # The census reads each new review as current (the same context revision as the reviewer's): no prompt is
    # unassessed or stale any more; the fixture grant selects iMessage only, so each now stops at its source.
    assert {m: after[m] for m in prompts} == dict.fromkeys(prompts, "source_unselected")
    assert {m: after[m] for m in after.keys() - prompts} == {m: before[m] for m in before.keys() - prompts}


def test_the_capture_delta_reports_only_what_moved():
    base = {"U": 5, "U_by_class": {"engineering_loss": 3, "member": 2}, "census_members": 2,
            "families": {"message": 2, "fact": 0}, "typed_candidates": {"fact:x": 1},
            "withheld_in_window": [{"source_id": "s", "reason_code": "ai_chat_capture_unattested", "policy_veto": "none",
                                    "count": 3}]}
    after = {**base, "U_by_class": {"engineering_loss": 1, "engineering_masked_by_policy": 2, "member": 2},
             "withheld_in_window": [{"source_id": "s", "reason_code": "unassessed", "policy_veto": "none", "count": 3}]}
    delta = gc.capture_delta(base, after)
    assert delta["U_by_class"] == {"engineering_loss": {"before": 3, "after": 1},
                                   "engineering_masked_by_policy": {"before": 0, "after": 2}}
    assert delta["withheld_in_window"] == {"s|ai_chat_capture_unattested|none": {"before": 3, "after": 0},
                                           "s|unassessed|none": {"before": 0, "after": 3}}
    assert delta["families"] == {} and delta["typed_candidates"] == {}


def test_the_rd5_posture_lever_lifts_only_the_scoped_install_refusal(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path / "node-home", monkeypatch)
    built(node)
    conn = legacy[1]
    _capture_rows(node, conn)
    binding = node.index.resolver.binding
    # Any configuration _source_posture cannot resolve refuses every row of the source before provenance is asked.
    # (On the node it is a runtime install scoped to one dataset; the ingest-provenance store pins that table, so
    # this fixture uses a malformed per-dataset override, the same refusal.)
    columns = {r[1] for r in conn.execute("PRAGMA table_info(user_ingestion_sources)")}
    if not columns:
        conn.execute("CREATE TABLE user_ingestion_sources (dataset_id TEXT, source_id TEXT, posture TEXT)")
    conn.execute("INSERT INTO user_ingestion_sources (dataset_id, source_id, posture) VALUES "
                 "(' padded ', 'chatgpt_ui_conversation', 'mixed'), (' padded ', 'other_ai_source', 'mixed')")
    # A prompt of an AI-chat source nobody attached as a capture: the lever must leave its refusal alone.
    when = iso(node.now[0] - 3600)
    conn.execute("INSERT INTO ai_chat_conversations (conversation_id, owner_user_id, title, source_id, created_at, "
                 "updated_at) VALUES ('other-1', ?, NULL, 'other_ai_source', ?, ?)", (binding.owner_id, when, when))
    conn.execute("INSERT INTO ai_chat_messages (message_id, conversation_id, sender_type, event_at, content, source_id) "
                 "VALUES ('other-prompt', 'other-1', 'user', ?, 'A synthetic prompt elsewhere.', 'other_ai_source')", (when,))
    conn.commit()
    copy = _copy_layout(tmp_path, node)
    canonical, durable = copy / Path(node.index.resolver.path).name, copy / "permissions-v2"

    def reasons():
        census = census_of(node, canonical=canonical, live=str(node.index.resolver.path), durable=durable)
        return {o.record_id: o.reason for o in census.outcomes if o.table == "ai_chat_messages"}

    blocked = reasons()
    assert set(blocked.values()) == {"source_posture_unknown"}
    with gc.assume_capture_attestation(binding.owner_id, {}), gc.assume_capture_posture(binding.owner_id):
        lifted = reasons()
    assert lifted["cap-old"] == lifted["cap-stamped"] not in {"source_posture_unknown", "ai_chat_capture_unattested"}
    assert lifted["cap-grantee"] == "ai_chat_capture_writer_refused"
    assert lifted["other-prompt"] == "source_posture_unknown"
    with gc.assume_capture_posture(binding.owner_id):
        assert reasons()["cap-old"] == "ai_chat_capture_unattested"   # the lever alone proves nothing
    from topos.permissions_v2 import evidence, message_evidence
    assert evidence._source_posture is message_evidence._source_posture                  # restored
    assert reasons() == blocked


def _scoped_installs(conn, owner_id, resource_id, *datasets):
    """The extension source's installs as a current node records them: the last one active, each scoped to a dataset."""
    conn.execute("CREATE TABLE IF NOT EXISTS source_runtime_installs (install_id TEXT PRIMARY KEY, scope_key TEXT, "
                 "source_id TEXT, version_id TEXT, status TEXT, is_active INTEGER, source_definition_json TEXT)")
    for number, dataset in enumerate(datasets):
        last = number == len(datasets) - 1
        conn.execute("INSERT INTO source_runtime_installs VALUES (?, ?, 'chatgpt_ui_conversation', 'v1', ?, ?, ?)",
                     (f"install-{number}", json.dumps({"user_id": owner_id, "topos_id": resource_id,
                                                       "device_id": "*", "dataset_id": dataset}),
                      "active" if last else "rolled_back", 1 if last else 0,
                      json.dumps({"source_id": "chatgpt_ui_conversation"})))
    conn.commit()


@pytest.mark.parametrize("history", ["one_dataset", "two_datasets"])
def test_rd5_binds_a_prompt_only_through_a_dataset_the_node_recorded(history, legacy, tmp_path, monkeypatch):
    from topos.permissions_v2.evidence import EvidenceResolver
    node, _ = node_for(legacy, tmp_path / "node-home", monkeypatch)
    built(node)
    conn, binding = legacy[1], node.index.resolver.binding
    _capture_rows(node, conn)
    dataset = f"{binding.owner_id}:topos:default"
    _scoped_installs(conn, binding.owner_id, binding.resource_id,
                     *([dataset] if history == "one_dataset" else [f"{binding.owner_id}:old", dataset]))
    # This fixture enrolled before any install existed, and the ingest store pins the install table (a reinstall on a
    # node moves its source generation the same way), so every native-origin check now refuses. No AI-chat row here
    # has a native link: keep their answer what it was before the install, False; iMessage rows are not under test.
    native = EvidenceResolver._validate_native_origin
    monkeypatch.setattr(EvidenceResolver, "_validate_native_origin", lambda self, conn, identity, row: (
        False if identity.table == "ai_chat_messages" else native(self, conn, identity, row)))
    # The doors recorded where the two stamped rows came in; the pre-stamp prompt and the reply have no record.
    conn.execute("UPDATE ai_chat_messages SET writer_dataset_id=? WHERE writer_class IS NOT NULL", (dataset,))
    conn.commit()
    copy = _copy_layout(tmp_path, node)
    canonical, durable = copy / Path(node.index.resolver.path).name, copy / "permissions-v2"
    before = files_digest(copy)

    def reasons():
        census = census_of(node, canonical=canonical, live=str(node.index.resolver.path), durable=durable)
        return {o.record_id: o.reason for o in census.outcomes if o.table == "ai_chat_messages"}

    built_rule = reasons()
    assert built_rule["cap-old"] == built_rule["cap-reply"] == "source_posture_unknown"
    assert built_rule["cap-stamped"] != "source_posture_unknown"
    assert built_rule["cap-grantee"] == "ai_chat_capture_writer_refused"   # its dataset is known; its writer is not the owner
    with gc.assume_capture_attestation(binding.owner_id, {}):
        attested = reasons()
    with gc.assume_capture_attestation(binding.owner_id, {}), gc.assume_capture_posture(binding.owner_id):
        upper = reasons()
    assert upper["cap-old"] == built_rule["cap-stamped"]
    prompts = ("cap-old", "cap-stamped", "cap-grantee")
    if history == "one_dataset":
        assert {k: attested[k] for k in prompts} == {k: upper[k] for k in prompts}   # RD5 reaches its bound
    else:
        assert attested["cap-old"] == "source_posture_unknown"              # two datasets: nothing to certify
        assert {k: attested[k] for k in prompts[1:]} == {k: upper[k] for k in prompts[1:]}
    # The bound lifts the pre-stamp reply too; nothing records its dataset (no receipt ever covers a reply).
    assert (attested["cap-reply"], upper["cap-reply"]) == ("source_posture_unknown", "provenance_unlinked")
    from topos.permissions_v2 import ai_chat_capture
    assert ai_chat_capture.attested_datasets.__name__ == "attested_datasets"  # restored
    assert reasons() == built_rule
    assert files_digest(copy) == before


def test_the_attestation_what_if_widens_only_the_assumed_owners_lookup(legacy, tmp_path, monkeypatch):
    node, _ = node_for(legacy, tmp_path / "node-home", monkeypatch)
    _capture_rows(node, legacy[1])
    from topos.permissions_v2 import ai_chat_capture
    owner_id, tally = node.index.resolver.binding.owner_id, {}
    ask = dict(source_id="chatgpt_ui_conversation", message_id="cap-old", conversation_id="capture-1")
    with gc.assume_capture_attestation(owner_id, tally):
        assert ai_chat_capture.attested_revisions(legacy[1], owner_id="someone-else", **ask) == frozenset()
        assert tally == {}
        (revision,) = ai_chat_capture.attested_revisions(legacy[1], owner_id=owner_id, **ask)
        assert tally == {"chatgpt_ui_conversation": 1}
        # No install record on this fixture: the assumed receipt certifies no dataset, and only at the row's revision.
        assert ai_chat_capture.attested_datasets(legacy[1], owner_id=owner_id, content_revision=revision, **ask) == {None}
        assert ai_chat_capture.attested_datasets(legacy[1], owner_id=owner_id, content_revision="0" * 64, **ask) == frozenset()
        assert ai_chat_capture.attested_datasets(legacy[1], owner_id="someone-else", content_revision=revision,
                                                 **ask) == frozenset()
        # A source nobody captures with is never assumed attested, even when the posture lookup asks about it.
        assert ai_chat_capture.attested_datasets(legacy[1], owner_id=owner_id, content_revision=revision,
                                                 **{**ask, "source_id": "chatgpt_file_ingestion"}) == frozenset()
        assert tally == {"chatgpt_ui_conversation": 1}
    assert ai_chat_capture.attested_revisions(legacy[1], owner_id=owner_id, **ask) == frozenset()
