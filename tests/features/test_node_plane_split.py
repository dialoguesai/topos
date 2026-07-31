"""Node plane-split invariants — SYS-node I1 and I2 in topos-ops-wiki.

The intent (docs/ml-manager-v2/DECOUPLING_AND_REMOTE_ENGINE.md) is that the data
plane and the engine plane can run on two separate machines: a cheap always-on box
holds the data, a GPU box does the processing.

Two properties make that possible, both true of the *contract* today, and both easy
to lose by accident in a single commit. These tests are the ratchet.

  I1  the engine plane does not reach into the node database
  I2  the task contract round-trips JSON

I1 has known exceptions, recorded below. The test fails on *new* ones, not on the
existing ones — a ratchet, not a wish.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from topos.engine.tasks import (
    ExecutionSpec,
    ModelRequest,
    ProcessingResult,
    ProcessingTask,
    Provenance,
    RequestedBy,
    TaskOptions,
)

ENGINE_DIR = pathlib.Path(__file__).resolve().parents[2] / "topos" / "engine"

# Modules under topos/engine/ that reach for storage or sqlite today.
# Each entry is a deliberate, reasoned exception — not an oversight. Adding to this
# set is a decision about the plane split (see D-001 in topos-research-wiki), so it
# should happen in a PR that says so, not incidentally.
KNOWN_DATA_PLANE_REACHES = {
    # Blackhole enforcement opens the node DB per call, on purpose: a cached
    # snapshot would leave a window where a just-protected entity is still
    # processed under the old rules. This is SYS-cognitive-firewall I1 (leak rate
    # zero) outranking SYS-node I1 (engine opens no DB) — the correct precedence,
    # and the reason the split needs blackhole policy to travel *in the task*.
    "engine.py",
    # Capability probing only: builds an in-memory adapter bundle to answer
    # "does the vector/graph tier work here?". No node data is read.
    "registration.py",
}

DATA_PLANE_MARKERS = ("sqlite3", "topos.storage", "..storage", ".storage")


def _module_reaches_data_plane(path: pathlib.Path) -> bool:
    """True when the module imports sqlite3 or the storage layer, at any nesting."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if any(alias.name.startswith(m.lstrip(".")) for m in DATA_PLANE_MARKERS):
                    return True
        elif isinstance(node, ast.ImportFrom):
            # module is None for `from . import x`; level encodes the dots.
            mod = "." * node.level + (node.module or "")
            if any(m in mod for m in DATA_PLANE_MARKERS):
                return True
    return False


@pytest.mark.check("C-eng-engine-opens-no-database")
def test_engine_plane_does_not_reach_the_data_plane() -> None:
    """SYS-node I1 (ratchet).

    A remote engine cannot open the node's database — it will be on a different
    machine. Every module here that does so is a thing that must be solved before
    the planes can split, so the set of them must not grow silently.
    """
    offenders = {
        p.name
        for p in sorted(ENGINE_DIR.glob("*.py"))
        if _module_reaches_data_plane(p)
    }
    new = offenders - KNOWN_DATA_PLANE_REACHES
    assert not new, (
        f"New engine module(s) reaching into the data plane: {sorted(new)}.\n"
        "This forecloses the node plane split (SYS-node I1). Either pass the data "
        "in the ProcessingTask, or add an entry to KNOWN_DATA_PLANE_REACHES with "
        "the reason and update D-001 in topos-research-wiki."
    )

    # The ratchet tightens: if an exception is fixed, remove it from the set.
    stale = KNOWN_DATA_PLANE_REACHES - offenders
    assert not stale, (
        f"KNOWN_DATA_PLANE_REACHES lists module(s) that no longer reach the data "
        f"plane: {sorted(stale)}. Remove them so the ratchet keeps holding."
    )


@pytest.mark.check("C-eng-task-contract-round-trips")
def test_processing_task_round_trips_json() -> None:
    """SYS-node I2.

    A single non-serialisable field on the task contract ends remote execution.
    This exercises every nested model, not just the top level.
    """
    task = ProcessingTask(
        id="task-1",
        type="enrichment",
        subtype="emotion_classification",
        source_id="src-1",
        record_ids=["r1", "r2"],
        input={"text": "hello", "nested": {"n": 1, "list": [1, 2, 3]}},
        model_request=ModelRequest(provider="ollama", model="llama3.2"),
        execution=ExecutionSpec(mode="sync", priority=10, batch_key="b1"),
        options=TaskOptions(store_result=True, apply_fisher_filter=True),
        requested_by=RequestedBy(user_id="u1", origin="write_event"),
    )

    restored = ProcessingTask.model_validate_json(task.model_dump_json())
    assert restored == task, "ProcessingTask did not survive a JSON round trip"

    # mode="json" is what a transport layer would actually send.
    assert ProcessingTask.model_validate(task.model_dump(mode="json")) == task


@pytest.mark.check("C-eng-task-contract-round-trips")
def test_processing_result_round_trips_json() -> None:
    """SYS-node I2, the return leg."""
    result = ProcessingResult(
        task_id="task-1",
        status="completed",
        output={"label": "joy", "score": 0.91, "spans": [[0, 5]]},
        provenance=Provenance(source_id="src-1", record_ids=["r1"]),
    )

    restored = ProcessingResult.model_validate_json(result.model_dump_json())
    assert restored == result, "ProcessingResult did not survive a JSON round trip"
    assert ProcessingResult.model_validate(result.model_dump(mode="json")) == result
