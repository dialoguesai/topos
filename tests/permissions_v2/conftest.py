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
