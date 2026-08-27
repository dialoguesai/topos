"""What the model manager may delete, and what it must never delete.

The floor is the owner's number; eviction is what the node does to honour it.
The tests that matter here are the refusals — a manager that frees space by
removing the model the node answers with has traded a visible problem (a full
disk) for an invisible one (a 404 on the next question).
"""

from __future__ import annotations

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
