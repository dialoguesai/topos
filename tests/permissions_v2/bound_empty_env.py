"""Opt-in pytest plugin: run permissions tests as a bound node whose environment sets no sharing switch.

Any-to-any N1, first "done when": a bound node with an empty environment serves search, shown by the existing
door tests run with no switches set where that is possible. With this plugin loaded:

- the node is bound (``switches.is_bound`` answers yes unless the master switch is set off), so every switch the
  environment does not set takes its bound default;
- every test starts with no sharing switch in its environment, and a test's own ``monkeypatch.setenv`` that turns
  ON a switch whose bound default is already on is not applied: the bound default has to supply it. Everything
  else is applied as the test wrote it: an off, a number, a choice, and an on for a switch that stays off when
  bound (facts, entailment, the lab doors).

    python3 -m pytest -p tests.permissions_v2.bound_empty_env tests/permissions_v2/test_message_search_batch.py

``--bound-kinds=1.4.4`` keeps the three kind switches a bound node turns on (journal entries, the journal goal
field, browsing interests) at their 1.4.4 defaults, to show the doors and the loop alone: most fixture corpora
have no ``journal_entries`` table (a migrated node does), and with the journal kind on every message read of such
a corpus refuses as ``evidence_storage_unavailable``.

A test that turns a door off by deleting its switch relies on 1.4.4's "unset means off" and fails under this
plugin by design, as does a test that pins an unbound default: there an empty environment is not possible.
Deleting a switch the plugin never set is not an error here. Not loaded by default; nothing imports it.
"""
from __future__ import annotations

import os

import pytest

KINDS = ("TOPOS_PERMISSIONS_V2_JOURNAL_SOURCES", "TOPOS_PERMISSIONS_V2_JOURNAL_GOAL_FIELD",
         "TOPOS_PERMISSIONS_V2_INTEREST_SOURCES")


def pytest_addoption(parser):
    parser.addoption("--bound-kinds", choices=("d5", "1.4.4"), default="d5",
                     help="the kind switches' defaults on the bound node: D5's (on) or 1.4.4's (off)")


@pytest.fixture(autouse=True)
def _bound_with_no_switch_set(monkeypatch, request):
    # Imported here, not at the top: `-p` loads a plugin before tests/conftest.py pins the ~/.topos defaults, and
    # no topos module may be imported before that.
    from topos.permissions_v2 import switches

    kinds_as_before = request.config.getoption("--bound-kinds") == "1.4.4"

    def bound(env=None):
        env = os.environ if env is None else env
        return switches.parse(switches.ENABLED, env.get(switches.ENABLED.name)) is not False

    def default(switch, env=None):
        item = switches.lookup(switch)
        if item.unbound == item.bound or (kinds_as_before and item.name in KINDS):
            return item.unbound
        return item.bound if bound(env) else item.unbound

    monkeypatch.setattr(switches, "is_bound", bound)
    monkeypatch.setattr(switches, "default", default)
    for name in switches.BY_NAME:
        monkeypatch.delenv(name, raising=False)
    original_setenv, original_delenv = pytest.MonkeyPatch.setenv, pytest.MonkeyPatch.delenv

    def setenv(self, name, value, prepend=None):
        item = switches.BY_NAME.get(name)
        if (item is not None and item.kind == "bool" and default(item) is True
                and switches.parse(item, value) is True):
            original_delenv(self, name, raising=False)    # the bound default supplies it
            return None
        return original_setenv(self, name, value, prepend)

    def delenv(self, name, raising=True):
        return original_delenv(self, name, raising=raising and name not in switches.BY_NAME)

    monkeypatch.setattr(pytest.MonkeyPatch, "setenv", setenv)
    monkeypatch.setattr(pytest.MonkeyPatch, "delenv", delenv)
    yield
