"""What the dispatcher refuses an ``owner_agent`` (AI-tool on-ramp, CONTRACTS §1.2; lanes L1 and L2).

protects: the class is refused on every owner-only type and every ``signal_*`` type — parametrised from the
handler registry, so a type marked owner-only later is covered without a change here — and on the mutations
that are not marked owner-only but are off the agent's positive list.

The owner-only and ``signal_*`` refusals hold on the untouched tree already: the dispatcher's gates admit
``owner_app`` alone, and a new class is nobody to them. They are kept here as the regression guard for the
day someone maps the agent onto ``owner_app`` (rev-1 security 5). The four ungated mutations need L2's
positive allowlist (``OWNER_AGENT_ALLOWED_TYPES`` in ``core/handlers/__init__.py``) and are marked strict
xfail until it lands: once it does they pass, the marker fails the run, and L2 removes it.
"""
from __future__ import annotations

import pytest

from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
from topos.principal import Principal

pytestmark = [pytest.mark.public, pytest.mark.asyncio]

AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
REFUSALS = {"owner_mode_required", "owner_agent_not_allowed"}
#: Off the positive list and not owner_only today (rev-1 security 5). `delete_source_install` is in the same
#: list in the critique but is refused already by `sources/install_authority.install_refusal` (owner class only),
#: so it sits with the regression guards below rather than waiting on L2.
UNGATED_MUTATIONS = ("delete_jsonl_file", "create_routine", "ollama_install")
ALREADY_REFUSED_MUTATIONS = ("delete_source_install",)


@pytest.fixture(autouse=True)
def _no_database_and_no_installer(monkeypatch):
    """A refusal happens before any handler runs, so a handler that does run is the finding — it must not
    open a database or start an Ollama install on the way to saying so."""
    import topos.core.handlers as hub
    from topos.engine import ollama_install

    monkeypatch.setattr(hub, "get_db_connection", lambda *a, **k: None)
    monkeypatch.setattr(ollama_install, "start_install", lambda *a, **k: {"stubbed": True})


def _refused(resp) -> bool:
    return (
        isinstance(resp, dict)
        and resp.get("status") == "error"
        and resp.get("code") == 403
        and resp.get("error") in REFUSALS
    )


@pytest.mark.parametrize("msg_type", sorted(OWNER_ONLY_MESSAGE_TYPES))
async def test_owner_agent_is_refused_on_every_owner_only_type(msg_type):
    assert msg_type in HANDLERS
    resp = await handle_control_plane_request({"id": f"oo-{msg_type}", "type": msg_type, "payload": {}}, principal=AGENT)
    assert _refused(resp), resp


@pytest.mark.parametrize("msg_type", sorted(t for t in HANDLERS if t.startswith("signal_")))
async def test_owner_agent_is_refused_on_every_signal_type(msg_type):
    resp = await handle_control_plane_request({"id": f"sig-{msg_type}", "type": msg_type, "payload": {}}, principal=AGENT)
    assert _refused(resp), resp


@pytest.mark.parametrize("msg_type", ALREADY_REFUSED_MUTATIONS)
async def test_owner_agent_is_refused_on_the_mutations_with_their_own_owner_door(msg_type):
    assert msg_type in HANDLERS and msg_type not in OWNER_ONLY_MESSAGE_TYPES
    resp = await handle_control_plane_request({"id": f"od-{msg_type}", "type": msg_type, "payload": {}}, principal=AGENT)
    assert _refused(resp), resp


@pytest.mark.xfail(strict=True, reason="L2: OWNER_AGENT_ALLOWED_TYPES in core/handlers/__init__.py (CONTRACTS §1.2)")
@pytest.mark.parametrize("msg_type", UNGATED_MUTATIONS)
async def test_owner_agent_is_refused_on_the_ungated_mutations(msg_type):
    assert msg_type in HANDLERS and msg_type not in OWNER_ONLY_MESSAGE_TYPES
    resp = await handle_control_plane_request({"id": f"ug-{msg_type}", "type": msg_type, "payload": {}}, principal=AGENT)
    assert _refused(resp), resp
