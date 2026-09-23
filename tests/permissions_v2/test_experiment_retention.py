"""Opt-in body retention for synthetic bridge runs; fake transports and tmp dirs only.

A wrong prose-arm verdict is only diagnosable from the exact request and raw
response, so a synthetic run may keep them in a private directory. These tests
pin the boundary: unflagged runs, symlinks and loose permissions are refused,
files are private, and neither a bridge result nor its cache ever holds a body.
"""
import json
import os
import stat

import pytest

from tests.permissions_v2.test_evidence import corpus
from tests.permissions_v2.test_fact_bridge import MESSAGE, Fake, capsule, judgment, record_output
from tests.permissions_v2.test_fact_policy import timed, policy, AS_OF
from tests.permissions_v2.test_projection_reviews import service as projection_service
from topos.permissions_v2.canonical import PolicyError
from topos.permissions_v2.contract import Binding
from topos.permissions_v2.experiments.evaluators import ModelRequest
from topos.permissions_v2.experiments.fact_bridge import FactShadowBridge
from topos.permissions_v2.experiments.retention import SyntheticBodyRetention


def retaining(corpus, service, *, retention, transport=None):
    raw = policy(corpus)
    return FactShadowBridge(capsule(raw), projections=service, binding=Binding.parse(raw["binding"]), clock=lambda: AS_OF,
                            transport=transport, retention=retention)


def request(stage="evidence_use"):
    return ModelRequest(arm="semantic_v1", processor="owner-engine-local", model_id="synthetic-local-model", model_revision="7" * 64,
        prompt_revision="8" * 64, stage=stage, system="Synthetic approved policy", candidate_data="Synthetic Fabrikam note",
        max_output_tokens=64, sampling="temperature_zero", reasoning_effort=None)


@pytest.mark.parametrize("flag", [False, None, 1, "true"])
def test_a_run_not_flagged_synthetic_is_refused_before_anything_is_created(tmp_path, flag):
    target = tmp_path / "bodies"
    with pytest.raises(PolicyError, match="retention_requires_synthetic_run"):
        SyntheticBodyRetention(target, synthetic_run=flag)
    assert not target.exists()


@pytest.mark.parametrize("case", ["symlinked_directory", "symlinked_parent", "relative", "dotdot", "loose_mode", "a_file"])
def test_symlinks_relative_paths_and_non_private_directories_are_refused(tmp_path, case, monkeypatch):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    real.chmod(0o700)
    if case == "symlinked_directory":
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
        target = tmp_path / "link"
    elif case == "symlinked_parent":
        (tmp_path / "parent-link").symlink_to(real, target_is_directory=True)
        target = tmp_path / "parent-link" / "bodies"
    elif case == "relative":
        monkeypatch.chdir(tmp_path)
        target = "bodies"
    elif case == "dotdot":
        target = str(real) + "/../real"
    elif case == "loose_mode":
        target = tmp_path / "shared"
        target.mkdir()
        target.chmod(0o755)
    else:
        target = tmp_path / "plain-file"
        target.write_text("synthetic")
    with pytest.raises(PolicyError, match="retention_directory_unsafe"):
        SyntheticBodyRetention(target, synthetic_run=True)
    assert list(real.iterdir()) == []
    if case == "loose_mode":
        # Refused, never silently tightened on the caller's behalf.
        assert stat.S_IMODE(target.stat().st_mode) == 0o755


def test_created_directory_is_0700_and_every_file_is_0600(tmp_path):
    # A hostile umask strips the owner's own write and search bits from both the
    # directory and the file; neither may be left narrower or wider than private.
    old = os.umask(0o077 | 0o300)
    try:
        with SyntheticBodyRetention(tmp_path / "bodies", synthetic_run=True) as retention:
            name = retention.keep(stage="evidence_use", request=request(), response_body='{"verdict":"deny"}',
                                  reason_code="semantic_deny")
    finally:
        os.umask(old)
    directory = tmp_path / "bodies"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    record = json.loads((directory / name).read_text())
    assert record["request"]["candidate_data"] == "Synthetic Fabrikam note"
    assert record["response_body"] == '{"verdict":"deny"}' and record["reason_code"] == "semantic_deny"
    # The file name is the only thing a report may carry, and it carries no content.
    assert "Fabrikam" not in name and "deny" not in name
    with SyntheticBodyRetention(directory, synthetic_run=True) as retention:
        with pytest.raises(PolicyError, match="retention_stage"):
            retention.keep(stage="../escape", request=request(), response_body=None, reason_code="model_error")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["bodies"] and [path.name for path in directory.iterdir()] == [name]


def test_a_directory_swapped_for_a_symlink_after_construction_cannot_redirect_writes(tmp_path):
    kept, elsewhere = tmp_path / "bodies", tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    with SyntheticBodyRetention(kept, synthetic_run=True) as retention:
        kept.rename(tmp_path / "moved")
        kept.symlink_to(elsewhere, target_is_directory=True)
        name = retention.keep(stage="output_release", request=request("output_release"), response_body=None,
                              reason_code="model_timeout")
    assert list(elsewhere.iterdir()) == []
    assert json.loads((tmp_path / "moved" / name).read_text())["response_body"] is None


@pytest.mark.asyncio
async def test_bridge_retains_each_exchange_privately_and_results_carry_no_body(timed, projection_service, tmp_path):
    record_output(timed, projection_service)
    model = Fake()
    with SyntheticBodyRetention(tmp_path / "bodies", synthetic_run=True) as retention:
        shadow = retaining(timed, projection_service, retention=retention, transport=model)
        result = await shadow.run(timed[2], request_as_of=AS_OF, arm="semantic_v1")
        files = list(retention.files)
    assert result.verdict == "permit" and len(files) == 2 == len(model.calls)
    records = [json.loads((tmp_path / "bodies" / name).read_text()) for name in files]
    assert [row["stage"] for row in records] == ["evidence_use", "output_release"]
    assert [row["request"] for row in records] == [call.model_dump() for call in model.calls]
    assert all(json.loads(row["response_body"]) == judgment() for row in records)
    assert [row["reason_code"] for row in records] == [stage.reason_code for stage in result.stages]
    dumped = result.model_dump_json()
    assert MESSAGE not in dumped and "APPROVED_POLICY_JSON" not in dumped and "history books" not in dumped
    assert all(MESSAGE.encode() not in body and b"APPROVED_POLICY_JSON" not in body for body in shadow.cache._entries.values())


@pytest.mark.asyncio
async def test_a_retention_failure_stops_the_run_instead_of_reading_as_a_model_error(timed, projection_service, tmp_path):
    record_output(timed, projection_service)
    retention = SyntheticBodyRetention(tmp_path / "bodies", synthetic_run=True)
    retention.close()
    shadow = retaining(timed, projection_service, retention=retention, transport=Fake())
    with pytest.raises(PolicyError, match="retention_closed"):
        await shadow.run(timed[2], request_as_of=AS_OF, arm="semantic_v1")


def test_the_bridge_accepts_only_a_retention_object(timed, projection_service):
    with pytest.raises(PolicyError, match="retention_invalid"):
        retaining(timed, projection_service, retention=str(timed[0].path))
