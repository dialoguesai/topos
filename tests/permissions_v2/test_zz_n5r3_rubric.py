"""N5 security review 3: the one input of the full check that is neither in the token nor read unconditionally.

`_current` (every pass, knowledge grants) computes the basis's `automatic_rubric_revision` through
`shadow_labeler_local.rubric()`, which reads `shadow_rubric.pinned.md` from the installed package on every call and
refuses (RubricMismatch) when it is missing or not the reviewed bytes. The token holds no part for it. Here the file
becomes unreadable between the checkpoint and the send: the pre-N5 send check refuses, N5 skips and releases.
The file is code, not owner data (its only possible runtime change is to become unreadable), so this is recorded as
a note, not a hole; the assertion is written in the direction of the stated property so that it fails visibly.
"""
from __future__ import annotations

import pytest

from tests.permissions_v2.test_journal_family import node  # noqa: F401
from tests.permissions_v2.test_zz_n5r3_journal import pair, subjects
from topos.permissions_v2 import shadow_labeler_local

MISSING = {}


def rubric_unreadable(_canonical):
    MISSING["monkeypatch"].setattr(shadow_labeler_local, "RUBRIC_PATH", MISSING["path"])


@pytest.mark.asyncio
@pytest.mark.parametrize("door", ["single", "batch"])
async def test_the_pinned_rubric_unreadable_after_the_checkpoint(node, tmp_path, monkeypatch, door):
    MISSING.update(monkeypatch=monkeypatch, path=tmp_path / "missing-rubric.md")
    built = subjects(node, tmp_path, monkeypatch)
    frame, full, verified = await pair(built, monkeypatch, change=rubric_unreadable, door=door, tag="n5r3-rubric")
    assert frame["status"] == full["status"], ("N5", frame["status"], "pre-N5", full["status"],
                                               (verified.computed["send"], verified.reused["send"]))
