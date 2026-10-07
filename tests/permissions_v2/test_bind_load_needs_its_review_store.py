"""Step 16 of the bind: a load whose review store does not open is not a load (review S4 follow-up, question Q6).

The contract's step 16 loads the runtime and "enrols the review store"; a failure answers 503 ``bind_load_failed``
and the node is unbound again. The code only tried: a store that refused was logged and passed over, so the bind
answered ``bound`` for a node that could build no index.

It now fails the load. What happens to the store depends on whose it is, and that is decided by one thing only:

- **A store that was in the folder before the bind** is the owner's. It is never moved and never renamed, whatever
  the load found. (A store of another identity never gets this far: the bind is refused before anything is written,
  test_bind_over_an_older_sharing_folder.py.)
- **A store this bind's own load made** (the enrollment object of the runtime the bind loaded says it wrote the
  enrollment itself, by exclusive create, for this bind's identity) goes to ``stale/`` with the rest of what the
  failed bind left, so that it does not refuse the next first bind. It holds no deselection: it was made seconds
  ago for an identity that never came to exist.
- **A store that appeared from elsewhere while the bind ran** (a restore, another process) is neither. It is left
  exactly as it is and the bind fails. Its age, its size and its emptiness decide nothing: the stores planted here
  are as new and as empty as one a failed bind makes.

The nodes, keys and ids are test_self_bind's: in-process, fresh, invented.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from types import SimpleNamespace

import pytest

from tests.permissions_v2.test_bind_over_an_older_sharing_folder import (
    MARKER, STORE, a_folder_that_lost_its_config_and_kept_its_reviews, unbound_and_idle)
from tests.permissions_v2.test_self_bind import (  # noqa: F401 -- `node` is the fixture
    ENVIRONMENT, OWNER, TOPOS, node, restart, settled)
from topos.permissions_v2 import bind_protocol, self_bind
from topos.permissions_v2 import runtime as runtime_module
from topos.permissions_v2.canonical import PolicyError, canonical_bytes

ASIDE = "-reviews-of-a-failed-bind"
#: What every failed load has always left in the folder (contract A2A-1 §4.2 step 16, and the load's own files).
A_FAILED_LOAD_LEAVES = ["config.json.failed-<time>", "ledger.db", "node-signing.key", "protocol.lock"]


def load_failed(message):
    return {"id": message["id"], "type": "permissions_v2_bind", "status": "error", "code": 503,
            "error": "bind_load_failed"}


def names(folder) -> list:
    """What a folder holds, by name, with the time taken out of the one name that carries it."""
    if not folder.exists():
        return []
    out = []
    for path in sorted(folder.iterdir()):
        name = "config.json.failed-<time>" if path.name.startswith("config.json.failed-") else path.name
        out.append(name + ("/" if path.is_dir() else ""))
    return out


def review_files(folder) -> dict:
    """Every review store file directly in a folder: name -> (sha256, mode)."""
    if not folder.exists():
        return {}
    return {path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777)
            for path in sorted(folder.iterdir()) if path.name.startswith(STORE) and path.is_file()}


def set_aside(node) -> list:
    stale = node.durable / "stale"
    return sorted(path for path in stale.iterdir() if path.name.endswith(ASIDE)) if stale.exists() else []


def rows(path) -> list:
    """What a review store holds, read without changing it: every review and every deselection, and the store's
    own identity (whose it is, for which database and clock, its store id). The one value left out is the clock
    mark the store itself writes each time it opens."""
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        return [list(db.execute("SELECT binding_json, file_revision, clock_id, store_id FROM review_identity")),
                list(db.execute("SELECT * FROM fact_reviews ORDER BY 1")),
                list(db.execute("SELECT * FROM fact_opt_outs ORDER BY 1"))]


# --- 3. a store that was there before the bind ---------------------------------------------------------------------

def damage_the_stores_own_record(node):
    """The store's own identity record names another database: it refuses at its first read, before it writes."""
    with closing(sqlite3.connect(node.durable / STORE)) as db:
        db.execute("UPDATE review_identity SET file_revision=? WHERE singleton=1", ("ef" * 32,))
        db.commit()


def name_another_store_in_the_enrollment(node):
    """The enrollment names a store id that is not the store's: found only after the store has opened."""
    marker = node.durable / MARKER
    enrollment = json.loads(marker.read_text())
    assert enrollment["store_id"] != "ab" * 32
    marker.write_bytes(canonical_bytes({**enrollment, "store_id": "ab" * 32}))


@pytest.mark.asyncio
async def test_a_bind_over_its_own_store_that_does_not_open_fails_the_load_and_leaves_the_store_byte_for_byte(node):
    """The enrollment reads as healthy, so the check before the bind passes; only opening the store shows it."""
    first = await a_folder_that_lost_its_config_and_kept_its_reviews(node)
    damage_the_stores_own_record(node)
    kept = review_files(node.durable)
    assert set(kept) == {STORE, MARKER}

    message = node.frame(node.bind_body(node_id=first.node_id, new_key_allowed=True))
    reply = await node.send(message)

    assert reply == load_failed(message)
    assert unbound_and_idle()
    assert "config.json" not in names(node.durable) and "config.json.failed-<time>" in names(node.durable)
    # The owner's store: in its place, byte for byte, with no file beside it that was not there, and nothing moved.
    assert review_files(node.durable) == kept
    assert set_aside(node) == []

    # A second attempt is the same answer, and the store is still exactly the owner's.
    restart(node)
    again = node.frame(node.bind_body(node_id=first.node_id, new_key_allowed=True))
    assert await node.send(again) == load_failed(again)
    assert review_files(node.durable) == kept and set_aside(node) == [] and unbound_and_idle()


@pytest.mark.asyncio
async def test_a_bind_over_its_own_store_whose_enrollment_names_another_store_fails_the_load_and_moves_nothing(node):
    """Here the store itself opens, as it does at every load of every node, and writes the clock mark it writes at
    every open; the mismatch is found after. So this store's bytes do move, by its own open and by nothing of the
    bind's: the enrollment is byte for byte the same, every review, every deselection and the store's identity are
    the same, and the store is where it was."""
    first = await a_folder_that_lost_its_config_and_kept_its_reviews(node)
    name_another_store_in_the_enrollment(node)
    enrollment = review_files(node.durable)[MARKER]
    content = rows(node.durable / STORE)

    for attempt in range(2):
        message = node.frame(node.bind_body(node_id=first.node_id, new_key_allowed=True))
        assert await node.send(message) == load_failed(message)
        assert unbound_and_idle()
        assert review_files(node.durable)[MARKER] == enrollment
        assert rows(node.durable / STORE) == content
        assert set(review_files(node.durable)) == {STORE, MARKER} and set_aside(node) == []
        restart(node)


# --- 1. a store that appears while the bind runs is not this bind's --------------------------------------------------

def plant(node, binding) -> dict:
    """What another process, or a restore, could leave: a review store as new and as empty as one a bind's own load
    makes when it fails, with an enrollment that never finished. Returns the files as they were left."""
    store, marker = node.durable / STORE, node.durable / MARKER
    for path, body in ((store, b""), (marker, canonical_bytes({
            "version": "topos-owner-evidence-enrollment/v2", "state": "pending", "binding": binding,
            "canonical_file_revision": "cd" * 32, "review_store_path": str(store), "store_id": None,
            "authority_digest": None, "revision": 1}))):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
    return review_files(node.durable)


def another_identity() -> dict:
    return {"environment_id": ENVIRONMENT, "node_id": "node_" + "3c" * 16, "resource_id": TOPOS, "owner_id": OWNER}


@pytest.mark.asyncio
async def test_a_store_that_appears_between_the_check_and_the_commit_is_left_alone_and_the_bind_fails(node, monkeypatch):
    planted = {}
    real = self_bind._install_clock

    def install_then_something_else_writes_a_store(served, owner_id):
        real(served, owner_id)
        planted.update(plant(node, another_identity()))
    monkeypatch.setattr(self_bind, "_install_clock", install_then_something_else_writes_a_store)

    message = node.frame()
    reply = await node.send(message)

    assert planted and reply == {"id": message["id"], "type": "permissions_v2_bind", "status": "error", "code": 503,
                                 "error": "bind_failed", "cause": "review_enrollment_unavailable"}
    assert unbound_and_idle()
    # Stopped before the commit: no config of any name. And the store that appeared is exactly as it was left.
    assert not [name for name in names(node.durable) if name.startswith("config.json")]
    assert review_files(node.durable) == planted and set_aside(node) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("whose", ["another identity's", "named for this very bind"])
async def test_a_store_that_appears_inside_the_load_is_left_alone_and_the_bind_fails(node, monkeypatch, whose):
    """After the bind's last look and before its load enrolls anything. The second case is the one that only the
    load's own knowledge settles: the store names this bind's identity, node id included, it is new, it is empty,
    it was not there a moment ago, and it is still not this bind's, because this bind's load did not create it."""
    planted = {}
    real = self_bind._commit_config

    def commit_then_something_else_writes_a_store(durable, served, bind, node_id, kid):
        target = real(durable, served, bind, node_id, kid)
        mine = {"environment_id": bind.environment_id, "node_id": node_id, "resource_id": bind.resource_id,
                "owner_id": bind.owner_id}
        planted.update(plant(node, mine if whose == "named for this very bind" else another_identity()))
        return target
    monkeypatch.setattr(self_bind, "_commit_config", commit_then_something_else_writes_a_store)

    message = node.frame()
    reply = await node.send(message)

    assert planted and reply == load_failed(message)
    assert unbound_and_idle()
    assert review_files(node.durable) == planted and set_aside(node) == []


@pytest.mark.asyncio
async def test_a_store_put_in_the_place_of_the_one_this_bind_made_is_left_alone(node, monkeypatch):
    """This bind's load did make a store, and it failed. Before the bind clears up, something else puts another
    identity's store where this bind's was. What is on disk is no longer what this bind made: it is not moved."""
    break_the_enrollment(monkeypatch)
    planted = {}
    real = self_bind._set_failed

    def fail_then_something_else_replaces_the_store(durable, target):
        real(durable, target)
        for name in (STORE, MARKER):
            (durable / name).unlink()
        planted.update(plant(node, another_identity()))
    monkeypatch.setattr(self_bind, "_set_failed", fail_then_something_else_replaces_the_store)

    message = node.frame()
    assert await node.send(message) == load_failed(message)

    assert planted and review_files(node.durable) == planted and set_aside(node) == []


# --- 2. a store this bind's own load made goes with the failed bind ---------------------------------------------------

def break_the_enrollment(monkeypatch, *, times=1):
    """The store's enrollment writes its intent, makes the store, then records it as active. Fail that last step:
    what a full disk or a crash leaves."""
    from topos.permissions_v2.evidence_review_runtime import ReviewEnrollmentRuntime
    real = ReviewEnrollmentRuntime._replace_marker
    state = {"left": times}

    def replace(self, body):
        if state["left"] > 0:
            state["left"] -= 1
            raise PolicyError("review_enrollment_unavailable")
        return real(self, body)
    monkeypatch.setattr(ReviewEnrollmentRuntime, "_replace_marker", replace)
    return state


def healthy(node, proof) -> bool:
    assert proof.outcome == "bound"
    assert runtime_module.get_runtime().evidence_reviews(require_existing=True) is not None
    assert self_bind._serving_refusal() is None
    enrollment = json.loads((node.durable / MARKER).read_text())
    return enrollment["state"] == "active" and enrollment["binding"]["node_id"] == proof.node_id


@pytest.mark.asyncio
async def test_a_failed_first_bind_takes_the_store_it_began_with_it_and_the_next_first_bind_succeeds(node, monkeypatch):
    break_the_enrollment(monkeypatch)
    before, reviews_before = names(node.durable), review_files(node.durable)
    assert before == [] and reviews_before == {}                 # a fresh node: no sharing folder at all

    failed = node.frame()
    assert await node.send(failed) == load_failed(failed)
    restart(node)

    # The count, before and after. No review store before, none in place after. The folder gained what every
    # failed load has always left, and one more entry: stale/.
    after = names(node.durable)
    assert review_files(node.durable) == reviews_before == {}
    assert after == A_FAILED_LOAD_LEAVES + ["stale/"]
    assert len(after) - len(before) == len(A_FAILED_LOAD_LEAVES) + 1
    # stale/ holds one thing: the store this bind began, as it left it. Its enrollment never became active.
    [aside] = list((node.durable / "stale").iterdir())
    assert aside.name.endswith(ASIDE) and aside.stat().st_mode & 0o777 == 0o700
    assert sorted(path.name for path in aside.iterdir()) == [STORE, MARKER]
    assert json.loads((aside / MARKER).read_text())["state"] == "pending"
    assert len(list(node.backups.iterdir())) == 1                # the bind's one backup, as before

    # The owner turns on sharing again: a first bind on a healthy node, and nothing is in its way.
    proof, _ = await node.bind()
    assert healthy(node, proof)
    settled(node)


@pytest.mark.asyncio
async def test_a_store_the_bind_finished_enrolling_goes_too_when_its_load_fails_afterwards(node, monkeypatch):
    """The store is whole and active, and still this bind's own: the load made it, and the bind did not happen."""
    real = runtime_module.get_runtime
    with monkeypatch.context() as during:
        def a_load_that_reports_another_identity():
            runtime = real()
            other = {**runtime.protocol.ledger.identity.model_dump(), "node_id": "node_" + "0f" * 16}
            return SimpleNamespace(config_path=runtime.config_path, protocol=SimpleNamespace(
                ledger=SimpleNamespace(identity=SimpleNamespace(model_dump=lambda: other))))
        during.setattr(runtime_module, "get_runtime", a_load_that_reports_another_identity)
        failed = node.frame()
        assert await node.send(failed) == load_failed(failed)
    restart(node)

    assert review_files(node.durable) == {}
    [aside] = set_aside(node)
    assert json.loads((aside / MARKER).read_text())["state"] == "active"
    proof, _ = await node.bind()
    assert healthy(node, proof)
    settled(node)


@pytest.mark.asyncio
async def test_a_failed_bind_under_a_node_id_the_control_plane_named_leaves_the_next_one_free_too(node, monkeypatch):
    """A node that lost its whole sharing folder binds again under the registry's node id. Its failed load must
    not leave a half-made store that refuses the same bind when it is sent again."""
    break_the_enrollment(monkeypatch)
    named = bind_protocol.mint_node_id()

    failed = node.frame(node.bind_body(node_id=named, new_key_allowed=True))
    assert await node.send(failed) == load_failed(failed)
    restart(node)
    assert review_files(node.durable) == {} and len(set_aside(node)) == 1

    proof, _ = await node.bind(node_id=named, new_key_allowed=True)
    assert proof.node_id == named and healthy(node, proof)
    settled(node)


# --- 4. stale/ : bounded for these, and never read back ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_failed_binds_in_a_row_keep_three_of_their_stores_and_no_more(node, monkeypatch):
    break_the_enrollment(monkeypatch, times=6)
    for attempt in range(6):
        failed = node.frame()
        assert await node.send(failed) == load_failed(failed)
        restart(node)
        assert review_files(node.durable) == {}
        assert len(set_aside(node)) == min(attempt + 1, self_bind.FAILED_BIND_REVIEWS_KEPT)

    assert self_bind.FAILED_BIND_REVIEWS_KEPT == 3 and len(set_aside(node)) == 3
    others = [path.name for path in (node.durable / "stale").iterdir() if not path.name.endswith(ASIDE)]
    print("\nstale/ after six failed binds: 3 of their review stores;",
          sum(name.endswith("-previous-ledger") for name in others), "previous ledgers;",
          sum(not name.endswith("-previous-ledger") for name in others), "other entries (keys);",
          sum(".failed-" in path.name for path in node.durable.iterdir()), "failed configs;",
          len(list(node.backups.iterdir())), "backups")
    # And the seventh, with nothing broken any more, binds.
    proof, _ = await node.bind()
    assert healthy(node, proof)
    settled(node)


@pytest.mark.asyncio
async def test_nothing_set_aside_is_ever_read_back_as_a_store(node, monkeypatch):
    break_the_enrollment(monkeypatch)
    failed = node.frame()
    assert await node.send(failed) == load_failed(failed)
    restart(node)
    [aside] = set_aside(node)
    kept = review_files(aside)
    # Nobody can open them now. Anything that tried to read one back would fail, or would not find a store.
    for path in aside.iterdir():
        path.chmod(0)

    proof, _ = await node.bind()
    assert healthy(node, proof)
    settled(node)

    # The node's store is a new one, in its own place; what was set aside is where it was put, as it was put.
    assert json.loads((node.durable / MARKER).read_text())["store_id"] is not None
    for path in aside.iterdir():
        path.chmod(0o600)
    assert review_files(aside) == kept and set_aside(node) == [aside]
