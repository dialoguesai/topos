"""Suite-wide switch for the retired ordinal-id capabilities (bookkeeping batch 3, F1).

The locator door refused to release under p2a-v1 and p2a-v2: their view's record ids count the owner's whole
message store. The door itself is removed from the node (N8) and survives only as a test driver
(`tests/permissions_v2/retired_doors.py`). Most of this suite predates the refusal and uses those capabilities
to exercise floors, evidence and ledger code the search door shares, so it drives the test adapter with the
retirement lifted. A test marked `ordinal_ids_retired` runs the adapter with the refusal in place.
"""
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "ordinal_ids_retired: drive the retired locator adapter with p2a-v1/v2 refused")


@pytest.fixture(autouse=True)
def _ordinal_capabilities_for_legacy_tests(request, monkeypatch):
    if request.node.get_closest_marker("ordinal_ids_retired") is None:
        from tests.permissions_v2 import retired_doors
        monkeypatch.setattr(retired_doors, "RETIRED_SOURCE_CAPABILITIES", frozenset())
    yield


def _lift_retired_search_profiles(monkeypatch):
    from topos.permissions_v2 import (index_rebuilds, refresh_loop, search_contract, search_index, search_release,
                                      search_transport)
    for module in (search_contract, search_index, search_release, index_rebuilds, refresh_loop):
        monkeypatch.setattr(module, "RELEASABLE_SEARCH_CAPABILITIES", search_contract.SEARCH_CAPABILITIES)
    monkeypatch.setattr(search_transport, "BATCH_CAPABILITIES", frozenset(search_contract.SEARCH_CAPABILITIES))


@pytest.fixture(scope="module")
def retired_search_profile_module():
    """`retired_search_profile` for a suite whose node is built once per module."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        _lift_retired_search_profiles(monkeypatch)
        yield


@pytest.fixture
def retired_search_profile(monkeypatch):
    """Lift the retirement of the p2c-v1 and p2c-v2 search profiles for one test.

    The node builds an index and answers only under p2c-v3 (N8); under the older profiles every door and the
    index builder refuse. Their branches are still in `search_index.py` and `search_release.py`, and the suites
    written against them also pin code the v3 path shares (the index's states, the ledger's order, the windows,
    the record ids). Until those suites are re-homed onto p2c-v3 and the branches deleted, a suite takes this
    fixture to run as it did before the retirement. Nothing in `topos/` can do what this fixture does.
    """
    _lift_retired_search_profiles(monkeypatch)


# --- the fuzz lane's Hypothesis profiles (confidence program C5; tests/permissions_v2/fuzz_support.py) -------------
# `lane` is deterministic and modest, the permanent lane; `deep` is the recorded confidence run.
# Neither writes an example database into the tree. Chosen with TOPOS_FUZZ_PROFILE.
try:
    from hypothesis import HealthCheck as _HealthCheck, settings as _hypothesis_settings
except ImportError:  # the lane skips itself when Hypothesis is not installed
    _hypothesis_settings = None

if _hypothesis_settings is not None:
    import os as _os
    _suppressed = [_HealthCheck.too_slow, _HealthCheck.function_scoped_fixture, _HealthCheck.data_too_large]
    _hypothesis_settings.register_profile("lane", max_examples=80, deadline=None, derandomize=True, database=None,
                                          print_blob=True, suppress_health_check=_suppressed)
    _hypothesis_settings.register_profile("deep", max_examples=500, deadline=None, derandomize=False, database=None,
                                          print_blob=True, suppress_health_check=_suppressed)
    _hypothesis_settings.load_profile(_os.environ.get("TOPOS_FUZZ_PROFILE", "lane"))
