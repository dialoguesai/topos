"""A failed owner query leaves a log line that says where it failed, and nothing it was about.

On 2026-10-23 a routine's query came back "database is locked" and the node had logged
nothing: the handler returned the error and dropped the traceback, so nobody could say
which write raised it. The line added for that names the request, the scope, the mode,
the exception class and the code frames. It must never carry the owner's words or the
error's text, which can quote the data it was handling.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict

import pytest

import topos.core.handlers as hub
import topos.query.runtime as runtime
from topos.core.handlers import handle_control_plane_request

PROBE = "how did the dentist appointment with my sister go"
LEAK = "row text that must stay out of the log"


class _FailingOrchestrator:
    """Raises an error whose text carries data. The frames quote source lines, so the
    message is built away from the ``raise`` line, as it is in real code."""

    message = f"database is locked ({LEAK})"

    async def execute(self, **_kwargs: Any) -> Dict[str, Any]:
        raise sqlite3.OperationalError(self.message)


@pytest.mark.asyncio
async def test_a_failed_query_logs_where_and_never_what(monkeypatch, caplog):
    monkeypatch.setattr(runtime, "get_query_orchestrator", lambda **_: _FailingOrchestrator())
    monkeypatch.setattr(hub, "get_db_connection", lambda *_a, **_k: None)
    with caplog.at_level(logging.ERROR, logger="topos.core.handlers"):
        out = await handle_control_plane_request(
            {
                "id": "req-locked",
                "type": "query",
                "payload": {"scope_id": "health:read", "access_mode": "summary", "query": PROBE},
            }
        )

    # The response is unchanged: the control plane still sees the error text it always did.
    assert out == {"id": "req-locked", "status": "error", "error": f"database is locked ({LEAK})"}

    lines = [r for r in caplog.records if "query failed" in r.getMessage()]
    assert len(lines) == 1
    text = lines[0].getMessage()
    assert "id=req-locked" in text
    assert "scope='health:read'" in text
    assert "mode='summary'" in text
    assert "error=OperationalError" in text
    assert "execute" in text, "the frames name the code that raised"
    assert PROBE not in text and "dentist" not in text
    assert LEAK not in text and "database is locked" not in text
    assert lines[0].exc_info is None, "a logger.exception would print the error's text"
