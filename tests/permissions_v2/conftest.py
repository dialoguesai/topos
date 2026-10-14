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
    config.addinivalue_line("markers", "real_support_judge: run the real `_judge_local` body against a transport the "
                            "test replaces; the model host is still never contacted")
    _install_support_judge_guard()


def pytest_unconfigure(config):
    _remove_support_judge_guard()


# --- the support judge never reaches the model host from this folder (DF160) ----------------------------------------
# From 1.5.7 `ANSWER_SUPPORT_JUDGE` is on by default, and `AnswerService` binds `answer_release._judge_local` at
# construction: a service built by any test (through `runtime.answers()` or by hand) would otherwise post to the
# local model host once per kept sentence. The module attribute is replaced for the whole session by a guard that
# answers "yes" (every sentence stands: the outcome of 1.5.6) unless the test hands the service a judge of its own,
# as the aq158 suite does. A test marked `real_support_judge` gets the real body (it replaces the transport). A call
# that reaches the real body in any other test is recorded and fails that test at teardown.
_JUDGE = {"original": None, "mode": None, "test": None, "trips": [], "real_calls": 0}


def _install_support_judge_guard():
    from topos.permissions_v2 import answer_release
    if _JUDGE["original"] is not None:
        return
    original = answer_release._judge_local
    _JUDGE["original"] = original

    async def _guarded_judge_local(sentence, items, *, deadline, question=None, context=None):
        # TR162: `context` (the prompt's lines about time) is passed only when the prompt carried some.
        if _JUDGE["mode"] == "real":
            _JUDGE["real_calls"] += 1
            extra = {"context": context} if context else {}
            return await original(sentence, items, deadline=deadline, question=question, **extra)
        if _JUDGE["mode"] == "yes":
            return True
        _JUDGE["trips"].append(_JUDGE["test"] or "<outside any test>")
        raise AssertionError("the real support judge was reached outside the hermetic stand-in (DF160 guard)")

    _guarded_judge_local.__wrapped__ = original      # the guard test reads these; nothing in `topos/` does
    _guarded_judge_local.state = _JUDGE
    answer_release._judge_local = _guarded_judge_local


def _remove_support_judge_guard():
    from topos.permissions_v2 import answer_release
    if _JUDGE["original"] is not None:
        answer_release._judge_local = _JUDGE["original"]
        _JUDGE["original"] = None


# --- the word rules of 1.5.6, one fixture (DF160) --------------------------------------------------------------------
# From 1.5.7 the topic-word rule (amendment 3), the anchor rule's abstention (amendment 2) and the lower-case binding
# of a person's short word (4.3a) are off, and the support judge is the check in their place (A2A-4 amendment 11).
# A test that pinned the old outcome runs both ways: once under the candidate default, once with the three word
# switches set as 1.5.6 read them. The judge is the "yes" stand-in either way (above), so what each case shows is the
# deterministic checks' own outcome.
WORD_RULES_1_5_6 = {"TOPOS_PERMISSIONS_V2_ANSWER_SUBJECT_WORDS": "true",
                    "TOPOS_PERMISSIONS_V2_ANSWER_QUESTION_ANCHOR": "true",
                    "TOPOS_PERMISSIONS_V2_ANSWER_SHORT_WORD_NAMES": "true"}


@pytest.fixture(params=[False, True], ids=["candidate", "rules-1.5.6"])
def rules_1_5_6(request, monkeypatch):
    """True: 1.5.6's three word rules are set on for this test; False: the 1.5.7 default, nothing set."""
    if request.param:
        for name, value in WORD_RULES_1_5_6.items():
            monkeypatch.setenv(name, value)
    return request.param


@pytest.fixture(autouse=True)
def hermetic_support_judge(request):
    """Every test in this folder: the judge says "yes" unless the test is marked `real_support_judge`."""
    real = request.node.get_closest_marker("real_support_judge") is not None
    _JUDGE["mode"], _JUDGE["test"] = ("real" if real else "yes"), request.node.nodeid
    before = len(_JUDGE["trips"])
    yield
    _JUDGE["mode"], _JUDGE["test"] = None, None
    tripped = _JUDGE["trips"][before:]
    assert not tripped, f"the real support judge was reached in {tripped}"


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
