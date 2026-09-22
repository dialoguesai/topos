"""Suite-wide switch for the retired ordinal-id capabilities (bookkeeping batch 3, F1).

The node refuses to release under p2a-v1 and p2a-v2: their view's record ids count the
owner's whole message store. Most of this suite predates that and uses those
capabilities to exercise floors, evidence and transports whose code p2a-v3 shares, so
it runs with the retirement lifted. A test marked `ordinal_ids_retired` runs with the
node's real setting; `test_bk3_opaque_ids.py` pins the refusal that way.
"""
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "ordinal_ids_retired: run with p2a-v1/v2 releases refused, as the node does")


@pytest.fixture(autouse=True)
def _ordinal_capabilities_for_legacy_tests(request, monkeypatch):
    if request.node.get_closest_marker("ordinal_ids_retired") is None:
        from topos.permissions_v2 import release
        monkeypatch.setattr(release, "RETIRED_SOURCE_CAPABILITIES", frozenset())
    yield


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
