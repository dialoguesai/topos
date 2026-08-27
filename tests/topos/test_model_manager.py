"""What the model manager may delete, and what it must never delete.

The floor is the owner's number; eviction is what the node does to honour it.
The tests that matter here are the refusals — a manager that frees space by
removing the model the node answers with has traded a visible problem (a full
disk) for an invisible one (a 404 on the next question).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from topos.engine import model_manager as mm

GB = 1024**3


class FakeAdapter:
    """Ollama, reduced to the three calls the manager makes."""

    def __init__(self, models, *, undeletable=()):
        self._models = list(models)
        self.deleted = []
        self._undeletable = set(undeletable)

    def list_models_detailed(self):
        return list(self._models)

    def delete_model(self, tag):
        if tag in self._undeletable:
            raise RuntimeError(f"{tag} is busy")
        self.deleted.append(tag)
        self._models = [m for m in self._models if m["name"] != tag]


def _model(name, size, modified_at):
    return {"name": name, "size": size, "modified_at": modified_at}


FLEET = [
    _model("llama3.2:latest", 2 * GB, "2026-08-01T00:00:00Z"),
    _model("qwen3:8b", 5 * GB, "2026-06-01T00:00:00Z"),
    _model("mistral:7b", 4 * GB, "2026-07-01T00:00:00Z"),
]


@pytest.fixture
def no_protection():
    """Nothing bound, so ordering and arithmetic are what is under test."""
    with patch.object(mm, "protected_tags", return_value=set()):
        yield


def test_a_bare_name_and_its_latest_tag_are_one_model():
    """Protecting one spelling while the tag list carries the other is how a
    bound model gets deleted."""
    assert mm.normalize_tag("llama3.2") == "llama3.2:latest"
    assert mm.normalize_tag("llama3.2:latest") == "llama3.2:latest"
    assert mm.normalize_tag("qwen3:8b") == "qwen3:8b"
    assert mm.normalize_tag("llama3.2@sha256:abc") == "llama3.2:latest"
    assert mm.normalize_tag("  ") == ""


def test_a_bound_model_is_never_a_candidate():
    adapter = FakeAdapter(FLEET)
    with patch.object(mm, "protected_tags", return_value={"qwen3:8b"}):
        tags = [c.tag for c in mm.eviction_candidates(adapter=adapter)]

    assert "qwen3:8b" not in tags, (
        "the pack's model was removed; the node would 404 on its next answer"
    )
    assert set(tags) == {"llama3.2:latest", "mistral:7b"}


def test_the_model_being_made_room_for_is_never_a_candidate(no_protection):
    adapter = FakeAdapter(FLEET)
    tags = [c.tag for c in mm.eviction_candidates(adapter=adapter, keep=["mistral:7b"])]
    assert "mistral:7b" not in tags


def test_candidates_are_least_recently_written_first(no_protection):
    adapter = FakeAdapter(FLEET)
    tags = [c.tag for c in mm.eviction_candidates(adapter=adapter)]
    assert tags == ["qwen3:8b", "mistral:7b", "llama3.2:latest"]


def test_a_volume_already_above_the_floor_deletes_nothing(no_protection):
    adapter = FakeAdapter(FLEET)
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", return_value=40 * GB
    ):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert adapter.deleted == []
    assert result.satisfied and result.reason == "already_above_floor"


def test_eviction_stops_as_soon_as_the_floor_is_cleared(no_protection):
    """Freeing more than the shortfall costs the owner downloads they did not need."""
    adapter = FakeAdapter(FLEET)
    free = [6 * GB]  # need 2 GB + a 10 GB floor = 12 GB

    def _probe(_path=None):
        return free[0]

    def _delete(tag):
        FakeAdapter.delete_model(adapter, tag)
        free[0] += next(m["size"] for m in FLEET if m["name"] == tag)

    adapter.delete_model = _delete
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", _probe
    ):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert adapter.deleted == ["qwen3:8b", "mistral:7b"], adapter.deleted
    assert "llama3.2:latest" not in adapter.deleted, "kept deleting past the target"
    assert result.satisfied


def test_shared_blobs_mean_the_volume_is_the_authority_not_the_arithmetic(no_protection):
    """Ollama shares layers between tags: a 5 GB tag can free almost nothing.

    Subtracting the reported sizes would call the job done while the volume is
    still under the floor, which is the failure this whole module exists to
    prevent.
    """
    adapter = FakeAdapter(FLEET)
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", return_value=1 * GB
    ):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert not result.satisfied
    assert result.shortfall_bytes == 11 * GB
    assert result.reason == "still_short"


def test_the_floor_spends_no_backup_it_was_never_handed(
    no_protection, tmp_path, monkeypatch, node_backups_withheld
):
    """SYS-node I1: on a machine that is not this node's, rule 0 does not apply.

    Same directory, same volume, same breached floor as
    `test_a_superseded_backup_is_spent_before_a_model` — the one difference is
    that nobody handed the backups over, which is the state a remote engine box
    is permanently in. The ladder there belongs to somebody else, so a model is
    what gives way instead.
    """
    directory = tmp_path / "backups"
    monkeypatch.setenv("TOPOS_BACKUP_DIR", str(directory))
    made = _condemned_backups(directory)
    adapter = FakeAdapter(FLEET)

    with patch("topos.engine.disk_space.on_same_volume", return_value=True), patch.object(
        mm, "min_free_bytes", return_value=10 * GB
    ), patch.object(mm, "free_bytes", return_value=5 * GB):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert all(path.is_file() for path in made), "not ours to spend"
    assert result.removed_backups == ()
    assert adapter.deleted, "a model is what gives way when there is no ladder to spend"


def test_a_remote_ollama_is_never_ours_to_prune(no_protection):
    adapter = FakeAdapter(FLEET)
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", return_value=0
    ):
        result = mm.reclaim_for(2 * GB, adapter=adapter, base_url="http://gpu-box:11434")

    assert adapter.deleted == []
    assert result.reason == "remote_ollama"


def test_an_unreadable_volume_deletes_nothing(no_protection):
    """The same rule the space check follows: not knowing is not a finding, and
    a deletion made on a guess cannot be undone without a download."""
    adapter = FakeAdapter(FLEET)
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", return_value=None
    ):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert adapter.deleted == []
    assert result.reason == "volume_unreadable"


def test_nothing_evictable_is_reported_rather_than_forcing_a_deletion():
    """Every model bound: the floor loses to the node staying able to answer."""
    adapter = FakeAdapter(FLEET)
    with patch.object(
        mm, "protected_tags", return_value={m["name"] for m in FLEET}
    ), patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", return_value=1 * GB
    ):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert adapter.deleted == []
    assert result.reason == "nothing_evictable"
    assert not result.satisfied


def test_a_tag_that_will_not_delete_moves_on_to_the_next(no_protection):
    adapter = FakeAdapter(FLEET, undeletable={"qwen3:8b"})
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", return_value=1 * GB
    ):
        mm.reclaim_for(2 * GB, adapter=adapter)

    assert "mistral:7b" in adapter.deleted
    assert "qwen3:8b" not in adapter.deleted


def test_an_unreachable_ollama_lists_nothing_rather_than_raising():
    class Dead:
        def list_models_detailed(self):
            raise OSError("connection refused")

    assert mm.installed_models(Dead()) == []


def test_a_zero_target_just_climbs_back_to_the_floor(no_protection):
    """The sweep case: nothing is being downloaded, the volume is simply low."""
    adapter = FakeAdapter(FLEET)
    free = [8 * GB]

    def _probe(_path=None):
        return free[0]

    def _delete(tag):
        FakeAdapter.delete_model(adapter, tag)
        free[0] += next(m["size"] for m in FLEET if m["name"] == tag)

    adapter.delete_model = _delete
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", _probe
    ):
        result = mm.reclaim_for(0, adapter=adapter)

    assert adapter.deleted == ["qwen3:8b"], "one 5 GB model clears a 2 GB shortfall"
    assert result.satisfied


def test_eviction_forgets_the_cached_installed_set(no_protection):
    """The pack resolver caches installed tags for 30s and demotes roles bound to
    anything missing from it. A cache still naming a model we just deleted would
    resolve a role to it for that window — the 404 the protection rules exist to
    prevent, arriving by the back door."""
    from topos.config import model_packs

    adapter = FakeAdapter(FLEET)
    calls = []
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", return_value=1 * GB
    ), patch.object(
        model_packs, "reset_installed_local_models_cache", side_effect=lambda: calls.append(1)
    ):
        mm.reclaim_for(2 * GB, adapter=adapter)

    assert adapter.deleted, "nothing was deleted, so this test proves nothing"
    assert calls == [1], "the installed-model cache still names deleted tags"


def test_a_sweep_that_deletes_nothing_leaves_the_cache_alone(no_protection):
    """Resetting on a no-op sweep would throw away a valid probe and make the
    next resolve open a socket for nothing."""
    from topos.config import model_packs

    adapter = FakeAdapter(FLEET)
    calls = []
    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", return_value=40 * GB
    ), patch.object(
        model_packs, "reset_installed_local_models_cache", side_effect=lambda: calls.append(1)
    ):
        mm.reclaim_for(2 * GB, adapter=adapter)

    assert calls == []


# ---------------------------------------------------------------------------
# Rule 0: a superseded backup goes before a model does
# ---------------------------------------------------------------------------


def _condemned_backups(directory, count=2, keep=3):
    """A directory holding `keep` + `count` backups for one Topos, all superseded."""
    directory.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(keep + count):
        path = directory / f"database-pre-v1.0.0--personaldb-2026081{i}T000000Z.db"
        path.write_bytes(b"x" * 1024)
        os.utime(path, (1e9 + i, 1e9 + i))
        made.append(path)
    return made


@pytest.fixture
def backup_dir(tmp_path, monkeypatch, node_backups_handed_over):
    """A backup directory of this node's own, on the models volume.

    Handed over, because the floor is never given a database path to go find one
    with — see `node_backups_handed_over`.
    """
    directory = tmp_path / "backups"
    monkeypatch.setenv("TOPOS_BACKUP_DIR", str(directory))
    with patch("topos.engine.disk_space.on_same_volume", return_value=True):
        yield directory


def test_a_superseded_backup_is_spent_before_a_model(no_protection, backup_dir):
    """The model is re-downloadable; the backup is deleted by the next migration anyway."""
    made = _condemned_backups(backup_dir)
    adapter = FakeAdapter(FLEET)

    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", side_effect=[5 * GB, 13 * GB]
    ):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert adapter.deleted == [], "no model should have been touched"
    assert result.satisfied is True
    assert result.reason == "backups_pruned"
    assert set(result.removed_backups) == {made[0].name, made[1].name}
    assert [path.is_file() for path in made[2:]] == [True] * 3


def test_the_ladder_is_not_spent_even_under_the_floor(no_protection, backup_dir):
    """Three rungs stay whatever the disk says; models are what gives way instead."""
    made = _condemned_backups(backup_dir, count=0)
    adapter = FakeAdapter(FLEET)

    with patch.object(mm, "min_free_bytes", return_value=10 * GB), patch.object(
        mm, "free_bytes", side_effect=[5 * GB, 13 * GB]
    ):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert all(path.is_file() for path in made), "the rollback path is not reclaimable"
    assert result.removed_backups == ()
    assert adapter.deleted, "with no condemned backup, a model is what gives way"


def test_backups_on_another_volume_are_not_spent_for_this_floor(
    no_protection, tmp_path, monkeypatch, node_backups_handed_over
):
    """Deleting on the home volume does not make room on a second drive."""
    directory = tmp_path / "backups"
    monkeypatch.setenv("TOPOS_BACKUP_DIR", str(directory))
    made = _condemned_backups(directory)
    adapter = FakeAdapter(FLEET)

    with patch("topos.engine.disk_space.on_same_volume", return_value=False), patch.object(
        mm, "min_free_bytes", return_value=10 * GB
    ), patch.object(mm, "free_bytes", return_value=5 * GB):
        result = mm.reclaim_for(2 * GB, adapter=adapter)

    assert all(path.is_file() for path in made)
    assert result.removed_backups == ()
    assert adapter.deleted, "the models volume is still the one that has to clear"
