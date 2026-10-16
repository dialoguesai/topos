"""The dispatcher's positive allowlist for the ``owner_agent`` class (on-ramp CONTRACTS §1.2, lane L2).

protects: an enrolled coding agent on the owner socket reaches exactly ``query``, ``aggregate`` and
``get_sources``. Every other type — every ``owner_only`` handler, every ``signal_*`` type, and the four
mutations that are not marked owner-only today (``delete_jsonl_file``, ``delete_source_install``,
``create_routine``, ``ollama_install``) — gets the owner-only gate's refusal shape with the code
``owner_agent_not_allowed``, before the handler runs and before every other gate. ``owner_app`` is
untouched on all of them. Parametrised from the handler registry, so a type registered tomorrow is
covered the day it lands.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest

import topos.core.handlers as hub
from topos.core.handlers import handle_control_plane_request
from topos.core.handlers.registry import HANDLERS, OWNER_ONLY_MESSAGE_TYPES
from topos.principal import OWNER_APP, Principal

OWNER_AGENT = Principal(cls="owner_agent", channel="uds", client_id="agent-claude-code")
OWNER = Principal(cls=OWNER_APP, channel="uds")

ALLOWED = ("query", "aggregate", "get_sources")
UNGATED_MUTATIONS = ("delete_jsonl_file", "delete_source_install", "create_routine", "ollama_install")


def _owner_only() -> List[str]:
    return sorted(OWNER_ONLY_MESSAGE_TYPES)


def _signal() -> List[str]:
    return sorted(t for t in HANDLERS if t.startswith("signal_"))


def _everything_else() -> List[str]:
    return sorted(set(_owner_only()) | set(_signal()) | set(UNGATED_MUTATIONS))


@pytest.fixture()
def probe(monkeypatch):
    """Replace one handler with a recorder, so the test sees the gate and not the handler."""
    seen: List[str] = []

    def install(msg_type: str) -> List[str]:
        assert msg_type in HANDLERS, msg_type

        async def handler(message: Dict[str, Any]) -> Dict[str, Any]:
            seen.append(msg_type)
            return {"id": message.get("id"), "status": "ok", "payload": {"probe": msg_type}}

        monkeypatch.setitem(HANDLERS, msg_type, handler)
        return seen

    return install


async def _dispatch(msg_type: str, principal: Principal) -> Dict[str, Any]:
    return await handle_control_plane_request(
        {"id": f"req-{msg_type}", "type": msg_type, "payload": {"source_id": "imessage", "table_name": "entities"}},
        principal=principal,
    )


def _refusal(msg_type: str) -> Dict[str, Any]:
    return {"id": f"req-{msg_type}", "status": "error", "code": 403, "error": "owner_agent_not_allowed"}


# ----------------------------------------------------------------- the list itself


def test_the_allowlist_is_exactly_the_three_read_types() -> None:
    assert hub.OWNER_AGENT_ALLOWED_TYPES == frozenset({"query", "aggregate", "get_sources"})
    assert isinstance(hub.OWNER_AGENT_ALLOWED_TYPES, frozenset)
    assert not (hub.OWNER_AGENT_ALLOWED_TYPES & OWNER_ONLY_MESSAGE_TYPES)


def test_the_registry_is_not_empty_so_the_matrix_below_is_not_vacuous() -> None:
    assert len(_owner_only()) >= 40, _owner_only()
    assert len(_signal()) >= 40, _signal()
    for msg_type in UNGATED_MUTATIONS:
        assert msg_type in HANDLERS and msg_type not in OWNER_ONLY_MESSAGE_TYPES, msg_type


# --------------------------------------------------------------- owner_agent refused


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", _owner_only())
async def test_owner_agent_is_refused_on_every_owner_only_type(probe, msg_type: str) -> None:
    seen = probe(msg_type)

    out = await _dispatch(msg_type, OWNER_AGENT)

    assert out == _refusal(msg_type), out
    assert seen == [], f"owner_agent reached {msg_type}"


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", _signal())
async def test_owner_agent_is_refused_on_every_signal_type(probe, msg_type: str) -> None:
    seen = probe(msg_type)

    out = await _dispatch(msg_type, OWNER_AGENT)

    assert out == _refusal(msg_type), out
    assert seen == [], f"owner_agent reached {msg_type}"


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", UNGATED_MUTATIONS)
async def test_owner_agent_is_refused_on_mutations_that_are_not_owner_only(probe, msg_type: str) -> None:
    """Not "everything that isn't owner_only": these four are open to owner_app and closed to the agent."""
    seen = probe(msg_type)

    out = await _dispatch(msg_type, OWNER_AGENT)

    assert out == _refusal(msg_type), out
    assert seen == [], f"owner_agent reached {msg_type}"


@pytest.mark.asyncio
async def test_owner_agent_is_refused_on_a_read_off_the_list_too(probe) -> None:
    """``query_live`` and ``get_table_rows`` are reads, and still not on the list."""
    for msg_type in ("query_live", "get_table_rows", "list_database_tables"):
        seen = probe(msg_type)
        out = await _dispatch(msg_type, OWNER_AGENT)
        assert out == _refusal(msg_type), out
        assert seen == [], f"owner_agent reached {msg_type}"


# --------------------------------------------------------------- owner_agent allowed


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", ALLOWED)
async def test_owner_agent_reaches_exactly_the_three_allowed_types(probe, msg_type: str) -> None:
    seen = probe(msg_type)

    out = await _dispatch(msg_type, OWNER_AGENT)

    assert seen == [msg_type], out
    assert out == {"id": f"req-{msg_type}", "status": "ok", "payload": {"probe": msg_type}}


# --------------------------------------------------------------- owner_app unchanged


@pytest.mark.asyncio
@pytest.mark.parametrize("msg_type", _everything_else() + list(ALLOWED))
async def test_owner_app_is_served_on_all_of_them_as_before(probe, msg_type: str) -> None:
    """The in-process owner principal (what every HTTP route and the node's own loops mint) reaches every one."""
    seen = probe(msg_type)

    out = await _dispatch(msg_type, OWNER)

    assert seen == [msg_type], out
    assert out["status"] == "ok"


@pytest.mark.asyncio
async def test_the_refusal_is_one_object_for_every_type(probe) -> None:
    """No type changes the refusal body beyond the frame's id: nothing to fingerprint."""
    bodies = set()
    for msg_type in _everything_else():
        probe(msg_type)
        out = await _dispatch(msg_type, OWNER_AGENT)
        out.pop("id")
        bodies.add(tuple(sorted(out.items())))
    assert bodies == {(("code", 403), ("error", "owner_agent_not_allowed"), ("status", "error"))}


@pytest.mark.asyncio
async def test_the_allowlist_runs_before_the_signal_and_owner_only_gates(probe) -> None:
    """An agent on ``signal_upload`` (owner_only AND signal_) hears the allowlist's code, not ``owner_mode_required``."""
    probe("signal_upload")
    out = await _dispatch("signal_upload", OWNER_AGENT)
    assert out["error"] == "owner_agent_not_allowed"
