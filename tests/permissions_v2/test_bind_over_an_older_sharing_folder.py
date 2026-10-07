"""A bind over an older identity's sharing folder is refused, not answered ``bound`` (review S4, finding M2).

The owner's review store lives in the sharing folder. It holds the owner's deselections, and it is enrolled for one
node identity: a store enrolled for another refuses every read. Until 1.5.0 a first bind on a node whose folder had
lost its config and kept its reviews answered ``bound`` under a new node id, then ``already_bound``, while the node
could build no index and serve nothing: the control plane registered it, the owner made shares, every recipient was
refused and every review screen failed. The store is never set aside to make such a bind work, because an empty
store would silently share again what the owner had taken out.

Now the bind is refused before anything is written, and a bound node whose review store refuses is no longer
vouched for by ``already_bound``. A healthy first bind and a re-bind of the same identity are as they were.

The nodes, keys and ids are test_self_bind's: in-process, fresh, invented.
"""
from __future__ import annotations

import json

import pytest

from tests.permissions_v2.test_self_bind import (  # noqa: F401 -- `node` is the fixture
    attest_owner, capabilities, node, restart, settled)
from topos.permissions_v2 import protection_doorbell, self_bind, switches
from topos.permissions_v2 import runtime as runtime_module

STORE = "evidence-reviews.db"
MARKER = STORE + ".enrollment.json"


def refused(message, cause):
    return {"id": message["id"], "type": "permissions_v2_bind", "status": "error", "code": 503,
            "error": "bind_failed", "cause": cause}


def unbound_and_idle() -> bool:
    switches.forget_bound()
    return not switches.is_bound() and runtime_module._runtime is None and not protection_doorbell.running()


async def a_folder_that_lost_its_config_and_kept_its_reviews(node):
    """The reviewer's case: a bound node whose review store works; then a restart, and the config is gone."""
    first, _ = await node.bind()
    attest_owner(node)
    assert runtime_module.get_runtime().evidence_reviews(require_existing=True) is not None
    settled(node)
    restart(node)
    (node.durable / "config.json").unlink()
    switches.forget_bound()
    assert (node.durable / STORE).is_file() and (node.durable / MARKER).is_file()
    assert json.loads((node.durable / MARKER).read_text())["binding"]["node_id"] == first.node_id
    return first


def enrolled_store(node) -> tuple:
    """Which store the folder holds and for whom: the enrollment's store id, binding and path. (The store's own
    bytes move whenever a runtime loads it, so they say nothing here.)"""
    marker = json.loads((node.durable / MARKER).read_text())
    assert (node.durable / STORE).is_file()
    return marker["store_id"], marker["binding"], marker["review_store_path"]


@pytest.mark.asyncio
async def test_a_first_bind_over_an_older_identitys_review_store_is_refused_and_writes_nothing(node):
    first = await a_folder_that_lost_its_config_and_kept_its_reviews(node)
    before = node.snapshot()

    # A FIRST bind: the control plane names no node id, so the node would make a new one.
    message = node.frame(node.bind_body(node_id=None, new_key_allowed=True))
    reply = await node.send(message)

    assert reply == refused(message, "review_enrollment_unavailable")
    # Nothing written: no backup, no key, no pending record, nothing set aside, the clock and the reviews as they were.
    assert node.snapshot() == before
    assert not (node.durable / "config.json").exists() and not (node.durable / "bind-pending.json").exists()
    assert unbound_and_idle()
    # Nothing for the control plane to register: no proof, and the heartbeat offers no key id.
    assert "payload" not in reply
    assert capabilities()["permissions_v2_node_key_id"] is None

    # The owner's next try is refused the same way: never ``already_bound`` for a node that cannot serve.
    again = node.frame(node.bind_body(node_id=None, new_key_allowed=True))
    assert await node.send(again) == refused(again, "review_enrollment_unavailable")
    assert node.snapshot() == before
    assert json.loads((node.durable / MARKER).read_text())["binding"]["node_id"] == first.node_id


@pytest.mark.asyncio
async def test_a_bind_that_names_another_node_id_than_the_review_stores_is_refused_too(node):
    await a_folder_that_lost_its_config_and_kept_its_reviews(node)
    before = node.snapshot()

    message = node.frame(node.bind_body(node_id="node_" + "7c" * 16, new_key_allowed=True))
    reply = await node.send(message)

    assert reply == refused(message, "review_enrollment_unavailable")
    assert node.snapshot() == before and unbound_and_idle()


@pytest.mark.asyncio
@pytest.mark.parametrize("damage, cause", [
    ("the enrollment is gone", "review_enrollment_unavailable"),
    ("the enrollment is not an enrollment", "review_enrollment_unavailable"),
    ("the enrollment is a link", "review_enrollment_unavailable"),
    ("the enrollment is wider than private", "review_enrollment_unavailable"),
    ("the enrollment records a change that never finished", "review_enrollment_unavailable"),
    ("the store is gone", "review_database_binding"),
])
async def test_a_review_store_no_identity_can_use_refuses_the_bind_even_under_its_own_node_id(node, damage, cause):
    """The same identity, so only the store's own state is in question: a bind does not go ahead over a store that
    would refuse the node it makes."""
    first = await a_folder_that_lost_its_config_and_kept_its_reviews(node)
    marker, store = node.durable / MARKER, node.durable / STORE
    if damage == "the enrollment is gone":
        marker.unlink()
    elif damage == "the enrollment is not an enrollment":
        marker.write_text("{")
    elif damage == "the enrollment is a link":
        kept = node.root / "kept-enrollment.json"
        marker.rename(kept)
        marker.symlink_to(kept)
    elif damage == "the enrollment is wider than private":
        marker.chmod(0o644)
    elif damage == "the enrollment records a change that never finished":
        from topos.permissions_v2.canonical import canonical_bytes
        marker.write_bytes(canonical_bytes({**json.loads(marker.read_text()), "state": "pending"}))
    else:
        store.unlink()
    before = node.snapshot()

    message = node.frame(node.bind_body(node_id=first.node_id, new_key_allowed=True))
    reply = await node.send(message)

    assert reply == refused(message, cause)
    assert node.snapshot() == before and unbound_and_idle()


@pytest.mark.asyncio
async def test_a_rebind_of_the_same_identity_keeps_its_review_store(node):
    """Unchanged: the control plane's confirmed re-bind names the node id its registry holds, the store is that
    identity's, and the node binds with a new key over the owner's own reviews."""
    first = await a_folder_that_lost_its_config_and_kept_its_reviews(node)
    kept = enrolled_store(node)

    again, _ = await node.bind(node_id=first.node_id, new_key_allowed=True)

    assert again.outcome == "bound" and again.node_id == first.node_id and again.kid != first.kid
    assert runtime_module.get_runtime().evidence_reviews(require_existing=True) is not None
    assert self_bind._serving_refusal() is None
    assert enrolled_store(node) == kept                      # the same store, never a new or an emptied one
    assert not [path for path in node.durable.rglob("evidence-reviews*") if "stale" in path.parts]
    settled(node)
    named, _ = await node.bind(node_id=first.node_id, new_key_allowed=False)
    assert (named.outcome, named.kid) == ("already_bound", again.kid)


@pytest.mark.asyncio
async def test_a_healthy_first_bind_and_its_second_are_unchanged(node):
    assert not node.durable.exists()

    first, _ = await node.bind()

    assert first.outcome == "bound"
    assert runtime_module.get_runtime().evidence_reviews(require_existing=True) is not None
    settled(node)
    again, _ = await node.bind()
    assert (again.outcome, again.node_id, again.kid) == ("already_bound", first.node_id, first.kid)


@pytest.mark.asyncio
@pytest.mark.parametrize("loaded", [False, True], ids=["after_a_restart", "while_loaded"])
async def test_a_bound_node_whose_review_store_refuses_is_not_vouched_for(node, loaded):
    """The second half of the finding: ``already_bound`` told the control plane the node serves while its review
    store refused. It answers ``bind_failed`` with the store's own code, and writes nothing."""
    await node.bind()
    settled(node)
    restart(node)
    if loaded:
        runtime_module.get_runtime()
    (node.durable / MARKER).unlink()             # the store is there and nothing says whose it is
    before = node.snapshot()

    message = node.frame()
    reply = await node.send(message)

    assert reply == refused(message, "review_enrollment_unavailable")
    assert node.snapshot() == before
