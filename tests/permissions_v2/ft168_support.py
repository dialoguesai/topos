"""FT168: one invented node for the fact-ontology tests (`test_ft168_*.py`).

Everything here is made up: the owner, the employers, the projects, every sentence. A node is `direct_search_twins`'s
corpus with the texts, ages and labels a test names: owner-authored iMessages recovered from one native snapshot
enrollment, each with one current machine assessment (a fixed answer, so no model runs), under a p2c-v3 knowledge share
that signs selective consent for Work and career beside its ordinary message records.

The owner's switch is the route's own code: `seed_pack_registry` (what `GET /derivation/packs` runs) and
`set_pack_enabled` (what `PUT /derivation/packs/{pack_id}` runs), never a hand UPDATE.

`Running` starts the node's own background work on such a node: `Runtime._start_sweeper` (the daemon sweep thread
`p2c-index-sweep`, which calls `timed_sweep` and then the loop's `after_sweep`) and `Runtime.refresh_loop` (the settings
`RefreshSettings.from_env` reads, the `RefreshLoop` it builds, its thread `p2c-search-refresh`). The methods are the
runtime's, unbound, on a stand-in that carries only the attributes they read; the index, the ledger, the release and
the review store are the real ones. Only the timers are shorter. The catch-up is switched off, so no model is reachable.
"""
from __future__ import annotations

import dataclasses
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from tests.permissions_v2 import direct_search_twins as twins
from tests.permissions_v2 import message_search_corpus as mc
from tests.permissions_v2.message_search_harness import Node, owner
from tests.permissions_v2.test_owner_identity_binding import add_entity, do_attest
from topos.permissions_v2.automatic_message_review import parse_assessment, prepare, publish
from topos.permissions_v2.evidence import EvidenceReviewStore
from topos.permissions_v2.imessage_reconciliation import ATTRIBUTED_CONTRACT, parse_reconciliation_snapshot
from topos.permissions_v2.ingest_provenance import OWNER_ATTESTATION, IngestProvenanceService
from topos.permissions_v2.protection_clock import resync_identity_coverage
from topos.permissions_v2.reconciliation_provenance import publish_existing
from topos.permissions_v2.selective_facts import selection

WORK = "work.career"
FACTS_SWITCH = "TOPOS_PERMISSIONS_V2_SELECTIVE_FACTS_ENABLED"
RESTORE_SWITCH = "TOPOS_PERMISSIONS_V2_INDEX_RESTORE_ENABLED"
CATCHUP_SWITCH = "TOPOS_PERMISSIONS_V2_ASSESSMENT_CATCHUP_ENABLED"
MIN_INTERVAL = "TOPOS_PERMISSIONS_V2_INDEX_RESTORE_MIN_INTERVAL_SECONDS"
GRANT = "grant-search"
APPLE_EPOCH = 978_307_200
PLAIN = dict(domains=["work"], sensitivity="none", speech="original_message", protected_content="none")


@dataclass
class Message:
    """One of the owner's own messages: its words, how long before `now` it was sent, and what the machine
    assessment says of it (the fields of `PLAIN` a test overrides)."""
    text: str
    age: int = 300
    labels: dict = field(default_factory=dict)


def knowledge_policy(*, grant: str = GRANT, answers: str | None = None, kinds=("message", "fact"),
                     ontologies=(WORK,), excluded=(), max_permitted: int | None = None, max_k: int = 10,
                     max_age_seconds: int | None = None) -> dict:
    """A p2c-v3 share: records of the owner's work messages, and facts of the ontologies it names (None: no
    selective consent at all)."""
    raw = twins.knowledge_policy()
    raw["search"]["result_types"] = list(kinds)
    raw["search"]["max_k"] = max_k
    if ontologies is not None:
        raw["search"]["selective_facts"] = selection(list(ontologies), list(excluded))
    if answers is not None:
        raw["search"]["answers"] = answers
    if max_permitted is not None:
        raw["search"]["max_permitted_records"] = max_permitted
    if max_age_seconds is not None:
        raw["search"]["window"]["max_age_seconds"] = max_age_seconds
    if grant != GRANT:
        raw["policy_version_id"] = grant + "-version"
        raw["binding"].update(grant_id=grant, assignment_id=grant + "-assignment")
    return raw


def build(root: Path, messages: list[Message], *, now: int, policy=None, protected: bool = False,
          wal: bool = True, vectors: bool = False) -> Node:
    """A p2c-v3 node over `messages` (ids `imessage:1`..`imessage:N` in order). No index is built here.

    `vectors`: the index gets a second rank lane. No model is involved: every member a build makes (messages and
    facts alike) is embedded by the harness's own stand-in (`fake_embedder`, 32 hashed word counts), and so is every
    query. A build embeds at most 1,024 members (`SearchIndexService.EMBEDDINGS_PER_BUILD`).

    The served database is in WAL mode, as a node's is (`connection_tuning`): a writer commits while a build's
    read snapshot is open, which is what "a switch while a build is in flight" needs."""
    root.mkdir(parents=True, exist_ok=True)
    canonical, conn, snapshot_path = twins._canonical(root)
    if wal:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    def mutate(db):
        db.execute("UPDATE message SET is_from_me=1")
        for number, message in enumerate(messages, start=1):
            db.execute("UPDATE message SET text=?, date=? WHERE ROWID=?",
                       (message.text, (now - message.age - APPLE_EPOCH) * 1_000_000_000, number))
    data = twins.native_snapshot(count=len(messages), mutate=mutate)
    snapshot_path.write_bytes(data)
    snapshot_path.chmod(0o400)
    service = IngestProvenanceService(canonical_database=canonical, binding=twins.BINDING,
                                      snapshot_root=snapshot_path.parent)
    columns = [row[1] for row in conn.execute("PRAGMA table_info(conversation_messages)")]
    natives = parse_reconciliation_snapshot(data, now=datetime.fromtimestamp(now, tz=timezone.utc))
    for native in natives:
        row = {"message_id": native.message_id, "source_record_id": native.message_id, "source_id": "imessage",
               "dataset_id": twins.DATASET, "owner_user_id": None, "conversation_id": native.conversation_id,
               "content": native.content, "event_at": native.event_at, "is_from_self": 1, "sender_id": "self",
               "sender_type": "human", "actor_role": None,
               "metadata_json": json.dumps({"message_guid": native.message_guid, "chat_guid": native.chat_guid,
                                            "chat_identifier": native.chat_identifier, "associated_message_type": 0})}
        conn.execute("INSERT INTO conversation_messages VALUES(" + ",".join("?" for _ in columns) + ")",
                     [row.get(column) for column in columns])
    twins.apply_wiki_entities_v1_up(conn)
    add_entity(conn, mc.OWNER_ENTITY)
    if protected:
        twins.protect(conn)
    conn.execute("CREATE TABLE ai_chat_messages(message_id TEXT,content TEXT)")
    conn.commit()
    clock = conn.execute("SELECT clock_id,generation FROM permissions_v2_protection_state").fetchone()
    resync_identity_coverage(service.resolver.path, owner_id=mc.OWNER_ID, expected_clock_id=clock[0],
                             expected_generation=clock[1])
    do_attest(conn, mc.OWNER_ENTITY)
    conn.commit()
    with owner():
        described = service.describe_snapshot(conn, snapshot_id="canary", reader_contract=ATTRIBUTED_CONTRACT)
        enrollment = service.enroll(conn, snapshot_id="canary", dataset_id=twins.DATASET,
                                    snapshot_sha256=described["snapshot_sha256"], owner_attestation=OWNER_ATTESTATION,
                                    reader_contract=ATTRIBUTED_CONTRACT)
        publish_existing(service, conn, enrollment_id=enrollment["enrollment_id"])
    mc.apply_post_merge_indexes(conn)
    conn.commit()
    resolver = service.resolver
    with owner():
        reviews = EvidenceReviewStore(root / "permissions-v2" / "reviews.db", resolver=resolver)
        for native, message in zip(natives, messages):
            assess(resolver, reviews, native.message_id, **message.labels)
    saved, mc.NOW = mc.NOW, now
    try:
        node = Node(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path, units=natives),
                    root / "node", model="fake-model" if vectors else None, search_raw=policy or knowledge_policy(),
                    now=now)
    finally:
        mc.NOW = saved
    if vectors:
        from tests.permissions_v2.message_search_harness import fake_embedder
        node.index.passage_embedder = fake_embedder
    node.canonical = conn        # the fixture's own connection to the served database; closed by `close`
    return node


def assess(resolver, reviews, message_id: str, **labels) -> None:
    """One machine assessment of a message, with a fixed answer (no model). Call inside `owner()`."""
    identity = resolver._identity("conversation_messages", message_id, "imessage", twins.DATASET)
    prepared = prepare(resolver, reviews, identity)
    publish(resolver, reviews, prepared, parse_assessment({**PLAIN, **labels}, prepared["snapshot"].message), now=1)


def activate(node: Node, raw: dict) -> None:
    """A second share on the same node, valid at the node's own clock."""
    saved, mc.NOW = mc.NOW, node.now[0]
    try:
        node.activate(raw)
    finally:
        mc.NOW = saved


def policy_at(now: int, **kwargs) -> dict:
    """`knowledge_policy` with its validity read at `now` (the corpus module's clock is restored at once)."""
    saved, mc.NOW = mc.NOW, now
    try:
        return knowledge_policy(**kwargs)
    finally:
        mc.NOW = saved


class Reopened(Node):
    """A node opened again over a folder `build` made: the shares are already in the ledger on disk."""

    def activate(self, raw, *, generation=1):
        return None


def reopen(root: Path, *, now: int, policy: dict, release_class=None) -> Node:
    """The node of `build(root, ...)` as a new process would find it: every store read from disk, nothing
    activated, built or assessed. `release_class`: another `MessageSearchRelease` to answer with (the same class
    from another commit), over the very same protocol, stores, index and clock."""
    from tests.permissions_v2.message_search_harness import fake_embedder
    canonical = root / "canonical.db"
    service = IngestProvenanceService(canonical_database=canonical, binding=twins.BINDING,
                                      snapshot_root=root / "permissions-v2" / "ingest-snapshots")
    resolver = service.resolver
    with owner():
        reviews = EvidenceReviewStore(root / "permissions-v2" / "reviews.db", resolver=resolver)
    saved, mc.NOW = mc.NOW, now
    try:
        node = Reopened(SimpleNamespace(resolver=resolver, reviews=reviews, path=resolver.path), root / "node",
                        model=None, search_raw=policy, now=now)
    finally:
        mc.NOW = saved
    if release_class is not None:
        node.search = release_class(protocol=node.protocol, resolver=resolver, reviews=reviews, index=node.index,
                                    clock=lambda: node.now[0], embedder=fake_embedder)
    node.canonical = sqlite3.connect(canonical)
    return node


def module_at(commit: str, name: str):
    """`topos.permissions_v2.<name>` as `commit` has it, loaded beside the current one: same package, so its
    imports are this tree's. For a module the commits in between did not change the two are the same code. Skips
    the test when the checkout does not hold the commit (a shallow clone)."""
    import importlib.util
    import subprocess

    import pytest
    repo = Path(__file__).resolve().parents[2]
    found = subprocess.run(["git", "show", f"{commit}:topos/permissions_v2/{name}.py"], cwd=repo,
                           capture_output=True, text=True)
    if found.returncode != 0:
        pytest.skip(f"this checkout does not hold {commit}")
    import sys
    label = f"topos.permissions_v2._ft168_{name}_{commit}"
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(label, loader=None))
    module.__package__ = "topos.permissions_v2"
    sys.modules[label] = module          # a dataclass in it looks its own module up by name while it is defined
    exec(compile(found.stdout, f"<{name}.py at {commit}>", "exec"), module.__dict__)
    module.source_text = found.stdout
    return module


def close(node: Node) -> None:
    try:
        node.canonical.close()
    except Exception:  # noqa: BLE001 -- a fixture's own handle
        pass


# --- facts ---------------------------------------------------------------------------------------------------

def state_fact(node: Node, message_id, predicate: str, value: str, *, disclosure: str = "scoped",
               asserted_by: str = "owner", subject: str = "self") -> str:
    """A fact as the fact store writes it (`fact_store_v1`), citing one of the owner's messages, or several (a list)."""
    from topos.features.facts.store import FactStore
    conn = node.canonical
    cited = [message_id] if isinstance(message_id, str) else list(message_id)
    before = {row[0] for row in conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'")}
    FactStore(conn).assert_fact(subject_entity_id=subject, predicate=predicate, object_value=value, confidence=1,
                                source_refs=[{"table": "conversation_messages", "record_id": record_id}
                                             for record_id in cited],
                                disclosure=disclosure, asserted_by=asserted_by)
    conn.commit()
    [new] = {row[0] for row in conn.execute("SELECT object_id FROM signal_objects WHERE object_type='fact'")} - before
    return new


def pack_fact(node: Node, message_id: str, *, ontology_id: str, version: str, predicate: str, value: str,
              altitude: str = "stated", disclosure: str = "scoped", extra: dict | None = None) -> str:
    """A stored fact with a derivation pack's provenance (the columns and payload keys `ontology_of` reads)."""
    from topos.storage.db.migrations.derivation_provenance_v1 import apply_derivation_provenance_v1_up
    conn = node.canonical
    apply_derivation_provenance_v1_up(conn)
    fact_id = state_fact(node, message_id, "works_on", f"placeholder {value}", disclosure=disclosure)
    payload = json.loads(conn.execute("SELECT payload_json FROM signal_objects WHERE object_id=?",
                                      (fact_id,)).fetchone()[0])
    payload.update(predicate=predicate, object_value=value, pack=ontology_id, altitude=altitude,
                   extractor={"pack_version": version}, **(extra or {}))
    conn.execute("UPDATE signal_objects SET ontology_id=?,ontology_version=?,altitude=?,payload_json=? "
                 "WHERE object_id=?", (ontology_id, version, altitude, json.dumps(payload), fact_id))
    conn.commit()
    return fact_id


# --- the owner's switch: the route's own code ------------------------------------------------------------------

def seed_registry(node: Node) -> None:
    """What the owner's first `GET /derivation/packs` does: the registry's table and the bundled packs' rows
    (Work and career on by default)."""
    from topos.features.derivation.registry import bundled_pack_dir, seed_pack_registry
    from topos.storage.db.migrations.pack_registry_v1 import apply_pack_registry_v1_up
    apply_pack_registry_v1_up(node.canonical)
    seed_pack_registry(node.canonical, bundled_pack_dir())


def switch(node: Node, ontology_id: str, on: bool, *, consent: bool = False) -> bool:
    """What `PUT /derivation/packs/{pack_id}` runs, on a connection of its own (any thread). True when a registry
    row was written."""
    from topos.features.derivation.surfaces import set_pack_enabled
    conn = sqlite3.connect(str(node.corpus.resolver.path), timeout=30)
    try:
        return set_pack_enabled(conn, ontology_id, on, consent=consent)
    finally:
        conn.close()


def switched_on(node: Node) -> set:
    """The registry as the release reads it."""
    from topos.permissions_v2.selective_facts import enabled_ontologies
    conn = sqlite3.connect(str(node.corpus.resolver.path), timeout=30)
    try:
        return set(enabled_ontologies(conn))
    finally:
        conn.close()


def sync(node: Node) -> bool:
    """The protection sync a recipient's next request (or the loop's restore) makes after a protection move."""
    from topos.permissions_v2.refresh_loop import protection_sync
    return protection_sync(node.protocol)()


def owner_count(node: Node, raw: dict | None = None) -> dict:
    """The owner's count and fact preview for one share (`permissions_v2_share_counts`), computed afresh."""
    from topos.permissions_v2 import share_counts
    from topos.permissions_v2.registry import parse_policy
    share_counts.forget()
    with owner():
        return share_counts.count(node.corpus.resolver, node.corpus.reviews, parse_policy(raw or node.search_raw),
                                  now=node.now[0])


# --- asking --------------------------------------------------------------------------------------------------

def ask(node: Node, query: str, *, grant: str = GRANT, k: int = 10, request_id: str | None = None):
    """One recipient search: (fact records, other records, refusal code or None)."""
    output, refused = node.search_request(query, k=k, grant_id=grant, request_id=request_id)
    if refused is not None:
        return [], [], refused
    facts = [record for record in output["records"] if record["kind"] == "fact"]
    return facts, [record for record in output["records"] if record["kind"] != "fact"], None


def receipts(node: Node) -> list[dict]:
    with node.ledger._transaction() as db:
        return [json.loads(row["receipt_json"]) for row in
                db.execute("SELECT receipt_json FROM p2a_system_actions ORDER BY recorded_at, action_id")]


def answer_receipts(node: Node) -> list[dict]:
    """The counts-only receipt of every written answer, oldest first (reason, items used and cited, sentences)."""
    with sqlite3.connect(node.ledger.path) as conn:
        rows = conn.execute("SELECT receipt_json FROM p2a_receipts ORDER BY rowid").fetchall()
    found = [json.loads(row[0]) for row in rows]
    return [receipt for receipt in found if receipt.get("version") == "topos-local-receipt/answer-v1"]


def restores(node: Node) -> list[dict]:
    return [receipt for receipt in receipts(node) if receipt["action"] == "search_index_restore"]


def record_refusal_reasons(node: Node) -> list[tuple]:
    """The search door answers every refusal with one word (`permission_denied`). This keeps the reason each one
    had inside the door, as (seconds on the monotonic clock, code or exception class, thread), for a test to read.
    It changes nothing the door does: the exception is raised on."""
    reasons: list[tuple] = []
    decide = node.search._decide

    def recording(*args, **kwargs):
        try:
            return decide(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 -- recorded and raised on, unchanged
            reasons.append((time.monotonic(), getattr(exc, "code", None) or type(exc).__name__,
                            threading.current_thread().name))
            raise
    node.search._decide = recording
    return reasons


class WallClock:
    """`Node.now` for a node whose loops run on the machine's clock: `node.now[0]` is the time now."""

    def __getitem__(self, _index) -> int:
        return int(time.time())


class Asker:
    """A recipient who keeps asking while the test goes on, on a thread of its own. Every answer is recorded as
    (seconds since the start, facts, other records, refusal): the share must never refuse."""

    def __init__(self, node: Node, query: str, *, grant: str = GRANT, every: float = 0.05):
        self.node, self.query, self.grant, self.every = node, query, grant, every
        self.seen: list[tuple] = []
        self.started = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="ft168-asker", daemon=True)

    def _run(self):
        number = 0
        while not self._stop.is_set():
            number += 1
            try:
                facts, others, refused = ask(self.node, self.query, grant=self.grant,
                                             request_id=f"ft168-asker-{id(self)}-{number}")
                self.seen.append((time.monotonic() - self.started, len(facts), len(others), refused))
            except Exception as exc:  # noqa: BLE001 -- recorded, and failed by the test that reads it
                self.seen.append((time.monotonic() - self.started, 0, 0, "raised:" + type(exc).__name__))
            self._stop.wait(self.every)

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_exc):
        self._stop.set()
        self._thread.join(30)

    def refusals(self) -> list[tuple]:
        return [(round(at, 2), refused) for at, _facts, _others, refused in self.seen if refused is not None]


class Running:
    """The node's own sweep and refresh loop on a fixture node (module docstring). `sweeps`, `observations`,
    `passes` and `builds` count what they did."""

    def __init__(self, node: Node, monkeypatch, *, sweep_every: float = 0.25, debounce: float = 1.0,
                 tick: float = 0.2, min_interval: float | None = 0.0):
        """`min_interval` None keeps the fewest seconds a node allows between two restore passes: the switch's
        floor of 60 s (its default is 300 s), which no environment value can go below."""
        from topos.permissions_v2.runtime import Runtime
        monkeypatch.setenv(RESTORE_SWITCH, "true")
        monkeypatch.setenv(CATCHUP_SWITCH, "false")          # no assessment pass, so no model call
        monkeypatch.setenv(MIN_INTERVAL, "0")                # read as the floor, 60 s
        self.node = node
        self.sweeps = self.observations = self.passes = 0
        self.builds: list[tuple] = []                        # (grant, state, members, seconds)
        self._count(node.index)
        self.runtime = SimpleNamespace(_refresh=None, _sweeper=None, _sweeper_stop=threading.Event(),
                                       _message_search_index=node.index, _index_rebuilds=None,
                                       protocol=node.protocol, message_search_index=lambda: node.index,
                                       automatic_message_reviews=None)
        self.loop = Runtime.refresh_loop(self.runtime)       # from_env settings, the RefreshLoop, its thread
        assert self.loop is not None and self.loop.settings.restore and self.loop.settings.facts
        assert not self.loop.settings.catchup
        self.defaults = self.loop.settings
        assert self.defaults.min_interval == 60.0
        self.loop.settings = dataclasses.replace(
            self.loop.settings, debounce=debounce, tick=tick,
            **({} if min_interval is None else {"min_interval": min_interval}))
        step, observe = self.loop.step, self.loop.observe

        def counted_step():
            self.passes += 1
            return step()

        def counted_observe(service):
            self.observations += 1
            return observe(service)
        self.loop.step, self.loop.observe = counted_step, counted_observe
        self.loop._wake.set()                                # leave the first 5 s wait: the shorter tick from here
        Runtime._start_sweeper(self.runtime, interval=sweep_every)

    def _count(self, index):
        sweep, rebuild = index.sweep, index.rebuild

        def counted_sweep(*args, **kwargs):
            self.sweeps += 1
            return sweep(*args, **kwargs)

        def counted_rebuild(grant_id, *args, **kwargs):
            started = time.monotonic()
            result = rebuild(grant_id, *args, **kwargs)
            self.builds.append((grant_id, result["state"], result["member_count"], time.monotonic() - started))
            return result
        index.sweep, index.rebuild = counted_sweep, counted_rebuild

    def counts(self) -> dict:
        return dict(sweeps=self.sweeps, observations=self.observations, passes=self.passes, builds=len(self.builds))

    def wait(self, predicate, seconds: float = 30.0) -> float | None:
        """Seconds until `predicate()` holds, or None after `seconds`."""
        started = time.monotonic()
        while time.monotonic() - started < seconds:
            if predicate():
                return time.monotonic() - started
            time.sleep(0.02)
        return time.monotonic() - started if predicate() else None

    def quiet(self) -> bool:
        with self.loop._lock:
            return not self.loop._pending

    def stop(self):
        self.runtime._sweeper_stop.set()
        self.loop.close()
        for thread in (self.runtime._sweeper, self.loop._thread):
            if thread is not None:
                thread.join(30)


def load() -> str:
    """`sysctl -n vm.loadavg`, for the line a timing is printed on."""
    import subprocess
    try:
        return subprocess.run(["sysctl", "-n", "vm.loadavg"], capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:  # noqa: BLE001
        return "unknown"
