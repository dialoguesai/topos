"""`get_sources` without a device lists the owner's installs from every device (OD-51, self-serve sources).

The control plane builds the grant editor's source catalog by asking the node for its installed sources. It knows
the owner, the Topos and the dataset, never the device an install came from. `list_installs` matched a concrete
scope exactly, device included, so an install made with a device never reached the catalog. Pinned here:

* with no device, every device's install under the same owner, Topos and dataset is listed (newest first, one per
  source), and nothing under another owner, Topos or dataset;
* a caller that names a device keeps the exact match;
* the owner, Topos and dataset are still required.
"""
from __future__ import annotations

import asyncio

import pytest

from topos.api import source_install
from topos.sources import install_service
from topos.sources.install_service import InstallRecord

OWNER, TOPOS, DATASET = "owner-1", "topos-1", "owner-1:default:dev"


def record(source_id, *, device="*", user=OWNER, topos=TOPOS, dataset=DATASET, active=True, stamp="2026-09-30T00:00:00"):
    return InstallRecord(install_id=f"{source_id}-{device}-{user}-{topos}-{dataset}", source_id=source_id,
                         scope={"user_id": user, "device_id": device, "topos_id": topos, "dataset_id": dataset},
                         version_id=None, status="installed", is_active=active,
                         source_definition_json={"source_id": source_id, "canonical_group_id": "journal",
                                                 "display_name": source_id, "source_type": "ui_stream",
                                                 "schema_id": "s", "parser_id": "p"},
                         source_version_row_json=None, failure_reason=None, created_at=stamp, updated_at=stamp)


ROWS = [
    record("grow_journal", device="laptop-7"),        # installed with a device: the case the exact match missed
    record("browser_visits"),                          # installed without one
    record("grow_journal", device="phone-2"),         # the same source from a second device: listed once
    record("other_owner_source", user="owner-2"),
    record("other_topos_source", topos="topos-2"),
    record("other_dataset_source", dataset="owner-1:default:other"),
    record("retired_source", active=False),
]


@pytest.fixture
def installs(monkeypatch):
    calls = []

    def fake_list_installs(*, scope=None, source_id=None):
        calls.append(scope)
        if scope is None:
            return list(ROWS)
        wanted = install_service._scope_key(scope)
        return [row for row in ROWS if install_service._scope_key(row.scope) == wanted]
    monkeypatch.setattr(install_service, "list_installs", fake_list_installs)
    monkeypatch.setattr(install_service, "rehydrate_active_installs_runtime", lambda: None)
    monkeypatch.setattr(source_install, "_scope_supply", lambda ids: {})
    return calls


def test_no_device_lists_every_device_under_the_same_owner_topos_and_dataset(installs):
    listed = install_service.list_installs_any_device(scope={"user_id": OWNER, "topos_id": TOPOS, "dataset_id": DATASET})
    assert sorted({r.source_id for r in listed}) == ["browser_visits", "grow_journal", "retired_source"]
    result = asyncio.run(source_install._list_sources_core({"user_id": OWNER, "topos_id": TOPOS, "dataset_id": DATASET}))
    assert sorted(s["source_id"] for s in result["sources"]) == ["browser_visits", "grow_journal"]


def test_a_named_device_keeps_the_exact_match(installs):
    result = asyncio.run(source_install._list_sources_core(
        {"user_id": OWNER, "topos_id": TOPOS, "dataset_id": DATASET, "device_id": "phone-2"}))
    assert [s["source_id"] for s in result["sources"]] == ["grow_journal"]
    assert installs[-1]["device_id"] == "phone-2"


@pytest.mark.parametrize("missing", ["user_id", "topos_id", "dataset_id"])
def test_owner_topos_and_dataset_are_still_required(installs, missing):
    scope = {"user_id": OWNER, "topos_id": TOPOS, "dataset_id": DATASET}
    scope[missing] = "*"
    with pytest.raises(ValueError):
        install_service.list_installs_any_device(scope=scope)
    scope.pop(missing)
    with pytest.raises(ValueError):
        asyncio.run(source_install._list_sources_core(scope))
