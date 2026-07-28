"""Attention triage M2 engine integration (PLAN_ATTENTION_TRIAGE.md).

Covers: migration, the daily triage pass end-to-end (verdicts + floors +
surface via reply-detection), the silence invariant on the attention_summary
payload (mini leak gate), the anabolic write-back objects, the formal
instruments, own-domain declared mappings (BT5), and the mapper hostname
fallback (BT6).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from topos.features.entities.declared_mappings import (
    extract_declared_entities,
    own_project_for_host,
)
from topos.features.triage import instruments
from topos.features.triage.daily import (
    ROUTINE_VERSION,
    TriageItem,
    _write_signal_objects,
    load_triage_delta,
    run_daily_triage,
)
from topos.ingestion.parsers.base import NormalizedRecord
from topos.canonicalization.mappers.browser_activity_mapper import (
    BrowserActivityCanonicalMapper,
)
from topos.storage.db.migrations import apply_all_migrations

DAY = "2026-07-20"


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "triage.db"))
    apply_all_migrations(c)
    # conversation_messages comes from legacy DDL outside the migration ledger;
    # create the minimal shape the triage loaders read.
    c.execute(
        """CREATE TABLE IF NOT EXISTS conversation_messages (
            message_id TEXT PRIMARY KEY, conversation_id TEXT, dataset_id TEXT,
            sender_type TEXT, sender_id TEXT, content TEXT, event_at TEXT,
            source_id TEXT, is_from_self INTEGER)"""
    )
    return c


def _timeline(c, event_at, record_id, source_id, table):
    c.execute(
        "INSERT OR IGNORE INTO timeline (event_at, record_id, source_id, canonical_table) "
        "VALUES (?,?,?,?)",
        (event_at, record_id, source_id, table),
    )


def _visit(c, event_at, rid, title, hostname):
    c.execute(
        "INSERT INTO activity_events (event_id, activity_type, url, title, occurred_at, "
        "source_id, hostname) VALUES (?,?,?,?,?,?,?)",
        (rid, "visit", f"https://{hostname}/x", title, event_at, "browser_visits", hostname),
    )
    _timeline(c, event_at, rid, "browser_visits", "activity_events")


def _journal(c, event_at, rid, content, category="Topos"):
    c.execute(
        "INSERT INTO journal_entries (entry_id, entry_at, category, content, source_id) "
        "VALUES (?,?,?,?,?)",
        (rid, event_at, category, content, "grow_journal"),
    )
    _timeline(c, event_at, rid, "grow_journal", "journal_entries")


def _message(c, event_at, rid, content, *, from_self, conv="c1"):
    c.execute(
        "INSERT INTO conversation_messages (message_id, conversation_id, dataset_id, "
        "sender_type, sender_id, content, event_at, source_id, is_from_self) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (rid, conv, "ds", "human", "s1", content, event_at, "imessage", int(from_self)),
    )
    _timeline(c, event_at, rid, "imessage", "conversation_messages")


def _entity(c, eid, name, mention_rid, event_at):
    c.execute(
        "INSERT OR IGNORE INTO entities (entity_id, entity_type, canonical_name, "
        "normalized_name) VALUES (?,?,?,?)",
        (eid, "project", name, name.lower()),
    )
    c.execute(
        "INSERT INTO entity_mentions (mention_id, entity_id, record_id, source_id, "
        "canonical_table, surface_text, confidence, event_at) VALUES (?,?,?,?,?,?,?,?)",
        (f"m-{mention_rid}-{eid}", eid, mention_rid, "x", "journal_entries", name, 1.0, event_at),
    )


def _seed_history(c):
    """Ten prior days of stable interests: topos journals + github visits."""
    for d in range(10, 0, -1):
        day = f"2026-07-{20 - d:02d}"
        _journal(c, f"{day}T12:00:00", f"j-{day}", "worked on topos retrieval", "Topos")
        _entity(c, "ent-topos", "topos", f"j-{day}", f"{day}T12:00:00")
        _visit(c, f"{day}T13:00:00", f"v-{day}", "topos repo", "github.com")
    c.execute(
        "INSERT INTO entity_edges (edge_id, src_entity_id, dst_entity_id, edge_type, weight) "
        "VALUES ('e1', 'ent-topos', 'ent-self', 'worked_on', 2.0)"
    )
    c.commit()


def test_migration_creates_verdict_table(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(triage_verdicts)")}
    assert {"day", "record_id", "verdict", "engagement_kind", "comp_novelty",
            "item_kl", "grounds_json", "routine_version"} <= cols
    assert conn.execute(
        "SELECT 1 FROM wiki_schema_migrations WHERE migration_id='attention_triage_v1'"
    ).fetchone()


def test_daily_triage_end_to_end(conn):
    _seed_history(conn)
    # target day: aligned visit, own-domain visit, misaligned visit,
    # unanswered aligned message (surface), deadline message (floor).
    _visit(conn, f"{DAY}T10:00:00", "v-aligned", "topos repo issues", "github.com")
    _visit(conn, f"{DAY}T10:05:00", "v-own", "links dashboard", "links.dialogues.ai")
    _visit(conn, f"{DAY}T10:10:00", "v-mis", "celebrity gossip weekly", "gossipsite.example")
    _message(conn, f"{DAY}T11:00:00", "msg-ask",
             "could you look at the topos retrieval doc?", from_self=False, conv="c-ask")
    _entity(conn, "ent-topos", "topos", "msg-ask", f"{DAY}T11:00:00")
    _message(conn, f"{DAY}T11:30:00", "msg-deadline",
             "reminder the application deadline is friday", from_self=False, conv="c-dl")
    conn.commit()

    summary = run_daily_triage(conn, DAY)
    verdicts = {r[0]: r[1] for r in conn.execute(
        "SELECT record_id, verdict FROM triage_verdicts WHERE day=?", (DAY,))}

    assert verdicts["v-aligned"] == "signal"
    assert verdicts["v-own"] == "signal"          # BT5: own domain never distraction
    assert verdicts["v-mis"] == "distraction"
    assert verdicts["msg-ask"] == "surface"       # aligned + unanswered = missed-but-matters
    assert verdicts["msg-deadline"] != "discard"  # floor: keyword + p2p
    assert summary["items"] == len(verdicts)
    row = conn.execute(
        "SELECT grounds_json, engagement_kind FROM triage_verdicts WHERE record_id='v-own'"
    ).fetchone()
    assert "own-project:Dialogues" in row[0]
    assert row[1] == "visited"

    # idempotent re-run
    run_daily_triage(conn, DAY)
    assert conn.execute(
        "SELECT COUNT(*) FROM triage_verdicts WHERE day=?", (DAY,)).fetchone()[0] == len(verdicts)


def test_anabolic_write_back_objects(conn):
    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v1", "topos repo", "github.com")
    conn.commit()
    run_daily_triage(conn, DAY)
    rows = {r[0]: json.loads(r[1]) for r in conn.execute(
        "SELECT object_type, payload_json FROM signal_objects "
        "WHERE signal_dimension='interests' AND extractor_version=?", (ROUTINE_VERSION,))}
    assert f"interest_profile" in {k for k in rows}
    profile = rows["interest_profile"]
    assert profile["top_vocab"] and profile["disclosure"] == "owner_only"
    assert "attention_summary" in rows


def test_silence_invariant_leak_gate(conn):
    """attention_summary must never reference a discarded item (A-SIL mini gate)."""
    items = [
        TriageItem(record_id="a", table="activity_events", source_id="s", event_at=f"{DAY}T01:00:00",
                   day=DAY, title="good thing", verdict="signal", aligned=True, grounds=["host:x"]),
        TriageItem(record_id="b", table="conversation_messages", source_id="s",
                   event_at=f"{DAY}T02:00:00", day=DAY,
                   title="SECRET-NOISE-XYZZY", verdict="discard"),
    ]
    _write_signal_objects(conn, DAY, items, [], 0.01, [], [], {"signal": 1, "discard": 1})
    payload = conn.execute(
        "SELECT payload_json FROM signal_objects WHERE object_type='attention_summary'"
    ).fetchone()[0]
    assert "SECRET-NOISE-XYZZY" not in payload
    assert "discard" not in json.loads(payload)  # no quadrant counts in digest substrate


def test_bayes_surprise_and_compression():
    prior = {"ent:topos": 10.0, "host:github.com": 8.0}
    kl_familiar, _ = instruments.bayes_surprise(prior, [["ent:topos"]])
    kl_novel, contrib = instruments.bayes_surprise(prior, [["ent:brand-new-thing"]])
    assert kl_novel > kl_familiar
    assert max(contrib, key=contrib.get) == "ent:brand-new-thing"

    corpus = ("worked on topos retrieval ranking " * 40).encode()
    familiar = instruments.comp_novelty("worked on topos retrieval ranking again today", corpus)
    novel = instruments.comp_novelty("volcanic basalt kayak migration in patagonia rivers", corpus)
    assert familiar is not None and novel is not None and novel > familiar


def test_own_domain_mapping_and_declared_entities():
    assert own_project_for_host("links.dialogues.ai") == "Dialogues"
    assert own_project_for_host("localhost:3000") == "Dialogues"
    assert own_project_for_host("pitchrotator.vercel.app") == "PitchRotator"
    assert own_project_for_host("gossipsite.example") is None

    rows = extract_declared_entities(
        {"source_id": "browser_visits", "event_id": "v1",
         "hostname": "links.dialogues.ai", "event_at": "2026-07-20T10:00:00"})
    assert rows and rows[0]["provider"] == "declared"
    assert any(r.get("surface_text") == "Dialogues" or "Dialogues" in str(r.values())
               for r in rows)
    assert extract_declared_entities(
        {"source_id": "browser_visits", "event_id": "v2",
         "hostname": "gossipsite.example", "event_at": "2026-07-20T10:00:00"}) == []


def test_mapper_hostname_fallback():
    mapper = BrowserActivityCanonicalMapper()
    normalized = NormalizedRecord(
        record_id="r1",
        payload={"url": "https://example.org/page", "title": "t",
                 "visited_at": "2026-07-20T10:00:00", "event_type": "visit"},
    )
    canonical = mapper.map(normalized)
    assert canonical.payload["hostname"] == "example.org"
    assert canonical.payload["metadata_json"]["hostname"] == "example.org"


def test_retrieval_surfaces_attention_objects(conn):
    from topos.query.retrieval import _load_attention_summary_items

    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v1", "topos repo", "github.com")
    conn.commit()
    run_daily_triage(conn, DAY)
    items = _load_attention_summary_items(conn)
    sources = {i["retrieval_source"] for i in items}
    assert {"attention_summary", "interest_profile"} <= sources
    digest = next(i for i in items if i["retrieval_source"] == "attention_summary")
    assert DAY in digest["summary_text"]
    assert ("surprise p" in digest["summary_text"]) or ("KL=" in digest["summary_text"])
    assert "discard" not in digest["summary_text"].lower()


def test_attention_dashboard_endpoint(conn, tmp_path, monkeypatch):
    """WS5.1: the /v1/signal/attention/dashboard data spine."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import topos.core.state as state

    _seed_history(conn)
    _visit(conn, f"{DAY}T10:00:00", "v1", "topos repo", "github.com")
    conn.commit()
    run_daily_triage(conn, DAY)

    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    api_conn = sqlite3.connect(db_path, check_same_thread=False)
    monkeypatch.setattr(state, "get_db_connection", lambda: api_conn)
    import topos.api.signal as signal_api
    monkeypatch.setattr(signal_api, "require_api_key", lambda: None, raising=False)
    app = FastAPI()
    app.include_router(signal_api.router, prefix="/v1")
    app.dependency_overrides = {}
    from topos.auth import require_api_key as real_key
    app.dependency_overrides[real_key] = lambda: None
    client = TestClient(app)

    r = client.get("/v1/signal/attention/dashboard?days=14")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["verdicts"] and d["summaries"]
    assert {"day", "verdict", "comp_novelty", "item_kl"} <= set(d["verdicts"][0])
    assert d["summaries"][0]["day"] == DAY

    r2 = client.get("/v1/signal/attention/dashboard?days=14&include_titles=false")
    assert all(v["record_id"] is None for v in r2.json()["verdicts"])
