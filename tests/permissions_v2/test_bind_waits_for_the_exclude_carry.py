"""Third fix round, review R2-M2, the bind's half: sharing cannot be turned on while the exclude carry is owed.

protects: until the upgrade step that carries the older per-person excludes into Off-limits has run, a person the
owner had excluded is not withheld. A new bind turns sharing on. It is now refused while the node holds
(`contact_excludes.hold`: the step is owed, not finished, and some exclude is not yet carried), before anything is
written, with the bind's own refusal for a node that could not serve: 503 `bind_failed` and the node's code as
`cause`. A node that is bound already answers the same way for as long as it holds, since it refuses every share
read meanwhile and must not be vouched for as serving. When the hold ends the same bind succeeds.
When the node holds is tests/topos/test_exclude_carry_runs_first_and_sharing_waits.py; this file is the wiring.
The nodes, keys and ids are test_self_bind's: in-process, fresh, invented.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_bind_over_an_older_sharing_folder import unbound_and_idle
from tests.permissions_v2.test_self_bind import node, restart, settled  # noqa: F401 -- `node` is the fixture
from topos.features.lifecycle import contact_excludes
from topos.permissions_v2 import runtime as runtime_module
from topos.permissions_v2.canonical import PolicyError

pytestmark = pytest.mark.public


def refused(message, cause):
    return {"id": message["id"], "type": "permissions_v2_bind", "status": "error", "code": 503, "error": "bind_failed",
            "cause": cause}


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", [contact_excludes.OWED, contact_excludes.FAILED])
async def test_a_new_bind_is_refused_while_the_node_holds_and_writes_nothing(node, monkeypatch, cause):
    """Rule: step 9a of the bind asks the hold. Remove it and this bind binds: sharing on, the excluded person not
    yet in Off-limits."""
    asked = []
    monkeypatch.setattr(contact_excludes, "hold", lambda database: asked.append(str(database)) or cause)
    before = node.snapshot()
    message = node.frame(node.bind_body(new_key_allowed=True))
    assert await node.send(message) == refused(message, cause)
    assert asked == [str(node.canonical)]                                # the database this node serves, and no other
    assert node.snapshot() == before and not node.durable.exists() and not node.backups.exists()
    assert unbound_and_idle()
    # the step has run: the same bind binds
    monkeypatch.setattr(contact_excludes, "hold", lambda database: None)
    proof, _ = await node.bind()
    assert proof.outcome == "bound"


@pytest.mark.asyncio
async def test_a_bound_node_that_holds_is_not_vouched_for_and_serves_no_share_read(node, monkeypatch):
    first, _ = await node.bind()
    settled(node)
    monkeypatch.setattr(contact_excludes, "hold", lambda database: contact_excludes.OWED)
    message = node.frame(node.bind_body(node_id=first.node_id, new_key_allowed=False))
    assert await node.send(message) == refused(message, contact_excludes.OWED)
    for read in (runtime_module.get_runtime().message_search, runtime_module.get_runtime().answers):
        with pytest.raises(PolicyError) as held:
            read()
        assert held.value.code == contact_excludes.OWED
    monkeypatch.setattr(contact_excludes, "hold", lambda database: None)
    again, _ = await node.bind(node_id=first.node_id, new_key_allowed=False)
    assert again.outcome == "already_bound"
    assert runtime_module.get_runtime().message_search() is not None
