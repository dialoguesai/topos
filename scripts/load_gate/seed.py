"""A large, invented owner database for the load gate, built with the candidate's own schema.

The schema is never hand-written here. :func:`build` boots the candidate node once on
an empty scratch database (its start-up runs the migrations and every table manager),
installs a handful of the candidate's own built-in source definitions through its
``/source-install`` door, stops it, then bulk-inserts invented rows with plain SQLite.
Each insert keeps only the columns the candidate's table has and fills any other
NOT NULL column without a default by its declared type, so a release that adds a
column does not break the seed.

Everything is invented: people are syllable pairs, places and projects come from the
short lists below, and the same ``--seed`` gives the same rows. No row is read from
any real database. The result is cached per (candidate ``topos/`` tree, generator
version, scale, seed) and copied for each run, so repeats do not rebuild it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import time
from typing import Callable, Iterable, Iterator

GENERATOR_VERSION = 2
SCOPE = {"user_id": "loadgate-owner", "device_id": "*", "topos_id": "topos_loadgate",
         "dataset_id": "loadgate-owner:topos:topos_loadgate"}
#: Built-in definitions installed before seeding (attribute names in ``topos.sources.registry``).
SOURCES = {"IMESSAGE": "conversation_messages", "CHATGPT_FILE": "ai_chat_messages",
           "DEMO_JOURNAL_FILE": "journal_entries", "DEMO_CALENDAR_FILE": "calendar_events",
           "DEMO_BROWSER_FILE": "activity_events", "VOXTERM_TRANSCRIPTS": "transcript_segments",
           "DEMO_CONTACTS_FILE": "contacts"}
#: Rows per unit of ``--scale``. Scale 1 is about 0.4 M rows; the owner's node holds several times that.
COUNTS = {"contacts": 1_500, "conversation_messages": 200_000, "ai_chat_messages": 30_000,
          "journal_entries": 3_000, "calendar_events": 6_000, "transcript_segments": 20_000,
          "activity_events": 40_000, "user_goals": 6_000, "message_topics": 40_000, "entities": 4_000,
          "entity_edges": 20_000, "signal_objects": 15_000, "query_sessions": 15_000, "query_artifacts": 30_000}
SPAN_DAYS = 540

SYLLABLES = ["ka", "lo", "mir", "ten", "sa", "vor", "eli", "dun", "ro", "bex", "ta", "ni", "quil", "fen",
             "ar", "zo", "pel", "ush", "dra", "ki", "mo", "vel", "ith", "sor"]
TOPICS = ["garden plan", "bike repair", "quarterly budget", "bread recipe", "roof estimate", "conference talk",
          "moving boxes", "piano lesson", "tax forms", "hiking route", "server migration", "grant proposal",
          "kitchen paint", "soccer schedule", "book club pick", "camera lens", "lease renewal", "data pipeline",
          "design review", "onboarding doc", "travel itinerary", "dentist visit", "birthday dinner",
          "climbing gym", "podcast edit", "sprint retro", "invoice batch", "garage cleanup", "language course",
          "volunteer shift"]
VERBS = ["finish", "check", "move", "review", "plan", "fix", "send", "book", "draft", "split", "price", "start"]
WHEN = ["today", "tomorrow", "this weekend", "next week", "on Friday", "after lunch", "tonight", "before the trip"]
FEEL = ["tired but happy", "a bit stressed", "excited", "calm", "worried", "proud", "restless", "relieved"]
PLACES = ["the harbor market", "the north library", "the hill trail", "the corner cafe", "the old depot",
          "the river park", "the night studio", "the east gym"]


class Words:
    """Deterministic invented text."""

    def __init__(self, rng: random.Random):
        self.rng = rng

    def name(self) -> str:
        r = self.rng
        first = "".join(r.choice(SYLLABLES) for _ in range(2)).capitalize()
        last = "".join(r.choice(SYLLABLES) for _ in range(3)).capitalize()
        return f"{first} {last}"

    def sentence(self, first: str = "") -> str:
        r = self.rng
        topic = r.choice(TOPICS)
        shape = r.randrange(4)
        if shape == 0:
            return f"Hey {first}, can we {r.choice(VERBS)} the {topic} {r.choice(WHEN)}?"
        if shape == 1:
            return f"I am {r.choice(FEEL)} about the {topic}; meet at {r.choice(PLACES)} {r.choice(WHEN)}."
        if shape == 2:
            return f"Done with the {topic}. Next I need to {r.choice(VERBS)} the {r.choice(TOPICS)}."
        return f"Reminder: {r.choice(VERBS)} the {topic} with {first or 'the team'} {r.choice(WHEN)}."

    def paragraph(self, n: int = 3) -> str:
        return " ".join(self.sentence() for _ in range(n))


def _iso(t: datetime) -> str:
    return t.isoformat(timespec="seconds")


def _filler(declared: str):
    declared = (declared or "").upper()
    if "INT" in declared:
        return 0
    if any(k in declared for k in ("REAL", "FLOA", "DOUB")):
        return 0.0
    if "BLOB" in declared:
        return b""
    return ""


def insert(conn: sqlite3.Connection, table: str, rows: Iterable[dict], chunk: int = 5_000) -> int:
    """Insert ``rows`` into the candidate's ``table`` as it is; 0 when the table does not exist."""
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not info:
        return 0
    names = {c[1] for c in info}
    needed = {c[1]: _filler(c[2]) for c in info if c[3] and c[4] is None and not c[5]}
    total, batch, sql, keys = 0, [], None, None
    for row in rows:
        if keys is None:
            keys = [k for k in row if k in names]
            keys += [k for k in needed if k not in keys]
            sql = f"INSERT OR IGNORE INTO {table} ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})"
        batch.append(tuple(row.get(k, needed.get(k)) for k in keys))
        if len(batch) >= chunk:
            conn.executemany(sql, batch)
            total += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        total += len(batch)
    return total


def generate(conn: sqlite3.Connection, *, scale: float, seed: int, sources: dict[str, str]) -> dict[str, int]:
    """Fill every table in :data:`COUNTS`; returns rows written per table."""
    rng = random.Random(seed)
    words = Words(rng)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    n = {table: max(1, int(count * scale)) for table, count in COUNTS.items()}
    src = {table: source_id for source_id, table in sources.items()}
    dataset = SCOPE["dataset_id"]

    def when(days: int = SPAN_DAYS) -> datetime:
        return now - timedelta(seconds=rng.randrange(days * 86_400))

    people = [words.name() for _ in range(n["contacts"])]
    threads_n = max(2, n["contacts"] - 300)
    written: dict[str, int] = {}

    def contacts() -> Iterator[dict]:
        for i, name in enumerate(people):
            yield {"contact_id": f"lg-contact-{i}", "dataset_id": dataset, "source_id": src.get("contacts", "contacts"),
                   "display_name": name, "known_usernames_json": json.dumps([f"+1555{i:07d}"]),
                   "is_self": 1 if i == 0 else 0, "source_record_id": f"lg-c-{i}"}

    def threads() -> Iterator[dict]:
        for peer in range(1, threads_n):
            at = _iso(when())
            yield {"conversation_id": f"lg-conv-{peer}", "dataset_id": dataset,
                   "source_id": src.get("conversation_messages", "imessage"), "created_at": at, "updated_at": at}

    def ai_threads() -> Iterator[dict]:
        for conv in range((n["ai_chat_messages"] + 19) // 20):
            at = _iso(when())
            yield {"conversation_id": f"lg-ai-conv-{conv}", "owner_user_id": "owner",
                   "title": f"About the {rng.choice(TOPICS)}", "created_at": at, "updated_at": at,
                   "source_id": src.get("ai_chat_messages", "chatgpt_file_ingestion")}

    def messages() -> Iterator[dict]:
        for i in range(n["conversation_messages"]):
            peer = rng.randrange(1, threads_n)
            mine = rng.random() < 0.45
            text = words.sentence(people[peer].split()[0])
            yield {"message_id": f"lg-msg-{i}", "conversation_id": f"lg-conv-{peer}", "dataset_id": dataset,
                   "sender_type": "human", "sender_id": "self" if mine else f"+1555{peer:07d}",
                   "message_type": "message", "content": text, "event_at": _iso(when()),
                   "source_id": src.get("conversation_messages", "imessage"), "is_from_self": int(mine),
                   "metadata_json": "{}", "source_record_id": f"lg-m-{i}", "ingested_at": _iso(now),
                   "content_hash": hashlib.sha256(text.encode()).hexdigest(), "content_disclosure": text,
                   "content_disclosure_hash": hashlib.sha256(text.encode()).hexdigest()}

    def ai_messages() -> Iterator[dict]:
        for i in range(n["ai_chat_messages"]):
            conv, seq = divmod(i, 20)
            yield {"message_id": f"lg-ai-{i}", "conversation_id": f"lg-ai-conv-{conv}",
                   "sender_type": "user" if seq % 2 == 0 else "assistant", "event_at": _iso(when()),
                   "content": words.paragraph(2), "metadata_json": "{}", "sequence": seq,
                   "source_id": src.get("ai_chat_messages", "chatgpt_file_ingestion"), "source_record_id": f"lg-a-{i}",
                   "ingested_at": _iso(now)}

    def journal() -> Iterator[dict]:
        for i in range(n["journal_entries"]):
            yield {"entry_id": f"lg-journal-{i}", "entry_at": _iso(when()), "mood_tag": rng.choice(FEEL),
                   "category": rng.choice(["work", "home", "health", "friends"]), "content": words.paragraph(4),
                   "people": json.dumps([rng.choice(people)]), "place_name": rng.choice(PLACES),
                   "source_id": src.get("journal_entries", "demo_journal_file"), "source_record_id": f"lg-j-{i}",
                   "ingested_at": _iso(now)}

    def calendar() -> Iterator[dict]:
        for i in range(n["calendar_events"]):
            start = now + timedelta(days=60) - timedelta(seconds=rng.randrange((SPAN_DAYS + 60) * 86_400))
            topic = rng.choice(TOPICS)
            yield {"event_id": f"lg-cal-{i}", "title": f"{rng.choice(VERBS).capitalize()} the {topic}",
                   "starts_at": _iso(start), "ends_at": _iso(start + timedelta(minutes=rng.choice([30, 60, 90]))),
                   "source_id": src.get("calendar_events", "demo_calendar_file"), "source_record_id": f"lg-e-{i}",
                   "location": rng.choice(PLACES), "description": words.sentence(), "is_busy": 1, "status": "confirmed",
                   "ingested_at": _iso(now)}

    def transcripts() -> Iterator[dict]:
        for i in range(n["transcript_segments"]):
            tid, seg = divmod(i, 40)
            yield {"segment_id": f"lg-seg-{i}", "transcript_id": f"lg-tr-{tid}", "dataset_id": dataset,
                   "speaker_label": f"Speaker {seg % 2 + 1}", "content": words.sentence(), "start_sec": seg * 7.5,
                   "duration_sec": 7.0, "event_at": _iso(when()), "is_from_self": int(seg % 2 == 0),
                   "source_id": src.get("transcript_segments", "voxterm_transcripts"), "source_record_id": f"lg-t-{i}"}

    def activity() -> Iterator[dict]:
        for i in range(n["activity_events"]):
            topic = rng.choice(TOPICS)
            yield {"event_id": f"lg-act-{i}", "activity_type": "visit",
                   "url": f"https://example.test/{topic.replace(' ', '-')}/{i}", "title": f"Notes on the {topic}",
                   "occurred_at": _iso(when()), "hostname": "example.test", "content": words.sentence(),
                   "source_id": src.get("activity_events", "demo_browser_file"), "source_record_id": f"lg-v-{i}",
                   "ingested_at": _iso(now)}

    def goals() -> Iterator[dict]:
        for i in range(n["user_goals"]):
            text = f"{rng.choice(VERBS).capitalize()} the {rng.choice(TOPICS)} {rng.choice(WHEN)}"
            yield {"goal_id": f"lg-goal-{i}", "record_id": f"lg-msg-{rng.randrange(n['conversation_messages'])}",
                   "source_id": src.get("conversation_messages", "imessage"), "goal_text": text, "model": "seed",
                   "provider": "load-gate", "payload_json": json.dumps({"goal": text, "status": "active"}),
                   "created_at": _iso(when())}

    def topics() -> Iterator[dict]:
        for i in range(n["message_topics"]):
            topic = rng.choice(TOPICS)
            message = rng.randrange(n["conversation_messages"])
            yield {"topic_id": f"lg-topic-{i}", "record_id": f"lg-msg-{message}", "message_id": f"lg-msg-{message}",
                   "source_id": src.get("conversation_messages", "imessage"), "topic": topic, "model": "seed",
                   "provider": "load-gate", "payload_json": json.dumps({"topic": topic, "score": 0.7}),
                   "created_at": _iso(when())}

    def entities() -> Iterator[dict]:
        for i in range(n["entities"]):
            if i < len(TOPICS):
                kind, name = "topic", TOPICS[i]
            else:
                kind, name = "person", people[i % len(people)]
            yield {"entity_id": f"lg-ent-{i}", "entity_type": kind, "canonical_name": name,
                   "normalized_name": name.lower(), "aliases_json": "[]", "identifiers_json": "[]",
                   "is_self": 1 if i == len(TOPICS) else 0, "first_seen": _iso(when()), "last_seen": _iso(when(30)),
                   "mention_count": rng.randrange(1, 400), "metadata_json": "{}"}

    def edges() -> Iterator[dict]:
        for i in range(n["entity_edges"]):
            yield {"edge_id": f"lg-edge-{i}", "src_entity_id": f"lg-ent-{rng.randrange(n['entities'])}",
                   "dst_entity_id": f"lg-ent-{rng.randrange(n['entities'])}",
                   "edge_type": rng.choice(["communicates_with", "mentions", "works_on"]),
                   "weight": round(rng.random(), 3), "evidence_count": rng.randrange(1, 50),
                   "last_event_at": _iso(when(90)), "metadata_json": "{}"}

    def facts() -> Iterator[dict]:
        for i in range(n["signal_objects"]):
            topic = rng.choice(TOPICS)
            at = _iso(when())
            payload = {"subject_entity_id": "owner", "predicate": rng.choice(["interest.topic", "work.project"]),
                       "object_value": topic, "disclosure": "owner_only", "asserted_by": "owner",
                       "actor_role": "authored", "altitude": "stated", "quote": words.sentence()}
            yield {"object_id": f"lg-fact-{i}", "signal_dimension": "profile", "object_type": "fact",
                   "object_key": f"{payload['predicate']}:{topic}:{i}", "payload_json": json.dumps(payload),
                   "confidence": 0.8, "source_refs_json": json.dumps([{"table": "conversation_messages",
                                                                       "record_id": f"lg-msg-{i}"}]),
                   "valid_from": at, "created_at": at, "updated_at": at, "extractor_version": "load-gate-seed"}

    def sessions() -> Iterator[dict]:
        # Half are expired: the first query's purge has real work to do, as on a node that has been up a while.
        for i in range(n["query_sessions"]):
            at = when(60)
            ttl = at + timedelta(hours=24) if i % 2 else now + timedelta(hours=rng.randrange(1, 24))
            yield {"session_id": f"lg-session-{i}", "requester_id": "mcp", "intent_hash": f"{i:032x}",
                   "envelope_json": json.dumps({"scopes": ["messages:read"]}), "ttl_expires_at": _iso(ttl),
                   "created_at": _iso(at), "updated_at": _iso(at)}

    def artifacts() -> Iterator[dict]:
        for i in range(n["query_artifacts"]):
            yield {"artifact_id": f"lg-artifact-{i}", "session_id": f"lg-session-{i % n['query_sessions']}",
                   "cache_key": f"{i:032x}", "public_result_json": json.dumps({"items": [words.sentence()]}),
                   "retrieval_fingerprint": f"{i:040x}", "game_layer_strategy": "summary",
                   "created_at": _iso(when(60)), "duration_ms": rng.randrange(200, 20_000)}

    steps: list[tuple[str, Callable[[], Iterator[dict]]]] = [
        ("contacts", contacts), ("conversations", threads), ("conversation_messages", messages),
        ("ai_chat_conversations", ai_threads), ("ai_chat_messages", ai_messages),
        ("journal_entries", journal), ("calendar_events", calendar), ("transcript_segments", transcripts),
        ("activity_events", activity), ("user_goals", goals), ("message_topics", topics), ("entities", entities),
        ("entity_edges", edges), ("signal_objects", facts), ("query_sessions", sessions),
        ("query_artifacts", artifacts)]
    for table, rows in steps:
        with conn:
            if insert(conn, table, rows()):
                # Counted back, not as offered: INSERT OR IGNORE drops a row a constraint refuses.
                written[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            else:
                written[table] = 0
    return written


def source_definitions(node) -> dict[str, dict]:
    """The candidate's own built-in definitions for :data:`SOURCES`, read by its interpreter under the guards."""
    names = json.dumps(sorted(SOURCES))
    code = ("import json; from topos.sources import registry as r; "
            f"print(json.dumps({{n: getattr(r, n).to_dict() for n in {names} if hasattr(r, n)}}))")
    out = subprocess.run([str(node.node_python), "-c", code], env=node.env(), cwd=node.root,
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"reading source definitions failed: {out.stderr.strip()[-400:]}")
    return json.loads(out.stdout.strip().splitlines()[-1])


def ensure_canonical_tables(node) -> None:
    """The message tables a connector makes on its first sync, made by the candidate's own managers: an empty
    database has no ``conversation_messages`` or ``ai_chat_messages`` until something is ingested."""
    code = ("import sqlite3, sys; "
            "from topos.storage.canonical.conversations_tables import ensure_all_tables; "
            "from topos.storage.canonical.ai_chat.tables import CanonicalTablesManager; "
            "conn = sqlite3.connect(sys.argv[1]); ensure_all_tables(conn); CanonicalTablesManager(conn); "
            "conn.commit(); conn.close()")
    out = subprocess.run([str(node.node_python), "-c", code, str(node.db_path)], env=node.env(), cwd=node.root,
                         capture_output=True, text=True, timeout=300)
    if out.returncode != 0:
        raise RuntimeError(f"making the canonical message tables failed: {out.stderr.strip()[-400:]}")


def install_sources(node, definitions: dict[str, dict]) -> dict[str, str]:
    """Install each definition through the node's owner socket; returns {source_id: table}."""
    import httpx

    installed = {}
    transport = httpx.HTTPTransport(uds=str(node.socket_path))
    with httpx.Client(transport=transport, base_url="http://localhost", timeout=60) as client:
        for name, definition in definitions.items():
            r = client.post("/v1/source-install", json={"source_definition_json": definition, "scope": SCOPE})
            if r.status_code != 200:
                raise RuntimeError(f"installing {definition.get('source_id')} answered {r.status_code}: {r.text[:200]}")
            installed[str(definition["source_id"])] = SOURCES[name]
    return installed


def cache_key(tree_id: str, scale: float, seed: int) -> str:
    return hashlib.sha256(f"{tree_id}|{GENERATOR_VERSION}|{scale}|{seed}".encode()).hexdigest()[:16]


def build(*, node_factory, cache_dir: Path, tree_id: str, scale: float, seed: int, log=print) -> dict:
    """The cached seed database for this candidate, built first if needed. Returns its manifest."""
    key = cache_key(tree_id, scale, seed)
    db, manifest_path = cache_dir / f"seed-{key}.db", cache_dir / f"seed-{key}.json"
    if db.exists() and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["cached"] = True
        return manifest
    cache_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    node = node_factory("seed")
    # The owner socket refuses source installs from anything but the signed app (the dangerous-door split). This
    # boot is the only one that installs, on an empty scratch database, so the split is off for it alone; the
    # load runs keep the default.
    node.extra_env["TOPOS_OWNER_DOOR_SPLIT"] = "0"
    log(f"seed: booting the candidate once on an empty database ({node.db_path})")
    node.start()
    try:
        node.wait_serving()
        boot_s = node.wait_healthy()
        installed = install_sources(node, source_definitions(node))
    finally:
        code = node.stop()
    if node.journal_lines():
        raise RuntimeError(f"seed boot tripped the owner-database guard: {node.journal_path}")
    log(f"seed: schema boot {boot_s:.1f} s, node exit {code}, sources installed {sorted(installed)}")
    ensure_canonical_tables(node)
    if node.journal_lines():
        raise RuntimeError(f"making the message tables tripped the owner-database guard: {node.journal_path}")
    conn = sqlite3.connect(str(node.db_path))
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        written = generate(conn, scale=scale, seed=seed, sources=installed)
        conn.execute("ANALYZE")
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()
    shutil.copyfile(node.db_path, db)
    for suffix in ("", "-wal", "-shm"):  # the cache holds the copy; the build folder's 300 MB goes
        Path(str(node.db_path) + suffix).unlink(missing_ok=True)
    manifest = {"key": key, "path": str(db), "tree_id": tree_id, "generator_version": GENERATOR_VERSION,
                "scale": scale, "seed": seed, "user_version": user_version, "bytes": db.stat().st_size,
                "rows": written, "sources_installed": sorted(installed),
                "build_s": round(time.monotonic() - started, 1),
                "schema_boot_s": round(boot_s, 1)}
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    manifest["cached"] = False
    return manifest
