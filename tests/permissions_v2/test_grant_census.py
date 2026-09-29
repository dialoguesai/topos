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
    assert agg["gate"] == {"census_equals_live_count": True, "census_equals_live_set": True, "unknown_reasons": 0}
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


def test_the_live_store_is_refused_as_input_or_output(tmp_path, monkeypatch):
    with pytest.raises(cs.CensusRefused):
        cs.refuse_live(Path.home() / ".topos" / "database.db")
    with pytest.raises(cs.CensusRefused):
        cs.refuse_live(Path.home() / ".topos" / "permissions-v2" / "anything")
    assert cs.refuse_live(tmp_path / "x") == tmp_path / "x"
    monkeypatch.setenv("TOPOS_DATABASE_PATH", str(Path.home() / ".topos" / "database.db"))
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
        "index_revision", "live_index_members", "index_state", "copied_at"}
    assert "grant-search" not in json.dumps(printed)


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
