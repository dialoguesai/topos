"""N5 security review 3 (candidate 11 @ 8c641e5e): adversarial cases on an interest member (Lane C). Scratch only.

Changes to what an interest member depends on, landed between the checkpoint and the send: the label's assessment
turned `special`, the owner's opt-out of the cluster, the cluster relabelled. Each moves a token part (canonical or
review store), so the full check runs; the full check does not decide interest members on a request (deep=False),
so both send checks release what the checkpoint decided. The accepted WS0 exception as implemented reaches every
interest-member input, not relabels and visits alone. Plus the interest flag switched INSIDE the recheck.
"""
from __future__ import annotations

import os

import pytest

from tests.permissions_v2.interest_fixtures import cluster
from tests.permissions_v2.message_search_harness import owner
from tests.permissions_v2.test_interest_door import _assess, _db, browsing, canonical, flag  # noqa: F401
from tests.permissions_v2.test_message_search_batch import send_batch
from tests.permissions_v2.test_search_send_token import (after_batch_checkpoint, after_checkpoint, no_token, relayed,
                                                         verifications)
from tests.permissions_v2.test_search_send_token_interest import QUERY, build
from tests.permissions_v2.test_search_send_token_journal import copy_of, outputs
from tests.permissions_v2.test_zz_n5r3_journal import during_recheck
from topos.permissions_v2 import interest_family as fam
from topos.permissions_v2 import interest_index as ii


def subjects(browsing, tmp_path, monkeypatch):
    reference = copy_of(browsing, tmp_path / "reference")
    out = []
    for canonical_path, directory in ((browsing, tmp_path / "n5"), (reference, tmp_path / "reference")):
        monkeypatch.setenv(ii.FLAG, "true")
        node, state = build(canonical_path, directory, monkeypatch)
        assert state["member_count"] == 2
        out.append((node, canonical_path))
    return out


def reassessed_special(canonical_path, node):
    _assess(canonical_path, {"domains": ["hobbies"], "sensitivity": "special", "protected_content": "none"})


def cluster_opted_out(canonical_path, node):
    with owner():
        assert node.corpus.reviews.opt_out(fam.opt_out_key("tc_hobby"), now=node.now[0]) is not None


def relabelled(canonical_path, node):
    with _db(canonical_path) as conn:
        cluster(conn, "tc_hobby", "sourdough / bread")


def interest_flag_off(_canonical_path, _node):
    os.environ.pop(ii.FLAG, None)


CHANGES = {"reassessed_special": reassessed_special, "cluster_opted_out": cluster_opted_out, "relabelled": relabelled}
STATUS = {"reassessed_special": "ok", "cluster_opted_out": "error", "relabelled": "ok"}


async def pair(built, monkeypatch, *, change=None, during=None, door, tag):
    made = verifications(monkeypatch)
    frames = []
    for number, (node, canonical_path) in enumerate(built):
        if number:
            no_token(monkeypatch)
        monkeypatch.setenv(ii.FLAG, "true")
        if during is not None:
            during_recheck(node, monkeypatch, lambda c=canonical_path, n=node: during(c, n))
        action = (lambda c=canonical_path, n=node: change(c, n)) if change is not None else (lambda: None)
        if door == "single":
            after_checkpoint(node, monkeypatch, action)
            frames.append(await relayed(node, monkeypatch, f"{tag}-{door}"))
        else:
            after_batch_checkpoint(node, monkeypatch, action)
            frames.append(await send_batch(node, [dict(QUERY)], monkeypatch, batch_id=f"{tag}-{door}"))
    return frames[0], frames[1], made[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
@pytest.mark.parametrize("change", sorted(CHANGES))
async def test_an_interest_change_after_the_checkpoint_gives_the_full_send_checks_answer(browsing, tmp_path, monkeypatch,
                                                                                         change, door):
    built = subjects(browsing, tmp_path, monkeypatch)
    frame, full, verified = await pair(built, monkeypatch, change=CHANGES[change], door=door, tag=f"n5r3i-{change}")
    assert (verified.computed["send"], verified.reused["send"]) == (1, 0), "a token part moved: full check"
    assert frame["status"] == full["status"], (change, frame["status"], full["status"])
    assert outputs(frame) == outputs(full)
    # Recorded, not required. A reassessment or a relabel lives in the canonical database and is not read by the
    # send check's member loop (the interest currency check is deep-only): both release the interests as decided at
    # the checkpoint, and the next recheck withholds the changed member. The owner's opt-out is a review-store
    # write: it moves the review digest, so the basis's message_review_revision mismatches and BOTH refuse.
    assert frame["status"] == STATUS[change], (change, frame["status"])
    if frame["status"] == "ok":
        [records] = [[record["kind"] for record in output["records"]] for output in outputs(frame)]
        assert "interest" in records


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
async def test_the_interest_flag_switched_inside_the_recheck_is_never_trusted_by_the_send_check(browsing, tmp_path,
                                                                                                monkeypatch, door):
    built = subjects(browsing, tmp_path, monkeypatch)
    frame, full, verified = await pair(built, monkeypatch, during=interest_flag_off, door=door, tag="n5r3i-flag-recheck")
    assert (verified.computed["send"], verified.reused["send"]) == (1, 0)
    assert frame["status"] == full["status"] == "error", (frame["status"], full["status"])
