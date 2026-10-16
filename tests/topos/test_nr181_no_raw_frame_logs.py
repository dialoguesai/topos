"""NR181: the node's log never quotes a relay frame, a handler's error text or a stored message's content.

A frame from the control plane can carry a recipient's question, an answer or shared records, and the owner's own
messages travel the same way. Pinned here, with an invented marker string inside each frame or text:

- an invalid frame is logged by its length and the first twelve hex characters of its SHA-256, never quoted;
- a handler that raises is logged by the request's type and id, the error's class and the code locations it passed
  through, never the error's text (a validation error repeats its input);
- ingestion's content preview (`field_preview`, which a relayed `store_message` reaches at INFO) is the same length
  and digest;
- no logging call on the relay's own paths passes a whole frame, body, payload, question or answer.

No network, no model, no database of the owner's.
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import logging
from pathlib import Path

import pytest

import topos.control_plane_client as control_plane_client
from topos.control_plane_client import ControlPlaneClient
from topos.core.log_fingerprint import DIGEST_HEX, fingerprint

pytestmark = pytest.mark.public

MARKER = "zq-invented-marker-7f3k"
ROOT = Path(__file__).resolve().parents[2]


class FakeWebSocket:
    def __init__(self, frames):
        self._frames = list(frames)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, message):
        self.sent.append(message)

    async def close(self, code=1000):
        _ = code


class FakeConnect:
    def __init__(self, ws):
        self.ws = ws

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __call__(self, url, additional_headers=None, ssl=None, **kwargs):
        return self


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:DIGEST_HEX]


def _everything_logged(records) -> str:
    """Each record's rendered line, its raw arguments and any exception text it would print."""
    parts = []
    for record in records:
        parts.append(record.getMessage())
        parts.append(repr(record.args))
        if record.exc_info:
            parts.append(logging.Formatter().formatException(record.exc_info))
        if record.exc_text:
            parts.append(record.exc_text)
    return "\n".join(parts)


async def _run_client(monkeypatch, frames, handler):
    """Feed `frames` through the client's real receive loop; stop once the handler has answered one."""
    ws = FakeWebSocket(frames)
    monkeypatch.setattr(control_plane_client, "connect", FakeConnect(ws))
    done = asyncio.Event()
    client = None

    async def wrapped(message):
        try:
            return await handler(message)
        finally:
            done.set()
            client._stop.set()

    client = ControlPlaneClient(control_plane_url="ws://cp.invalid/ws/engine", api_key="test-key", handler=wrapped,
                                verify_ssl=False)
    task = asyncio.create_task(client._run())
    await asyncio.wait_for(done.wait(), timeout=5)
    client._stop.set()
    await asyncio.wait_for(task, timeout=5)
    return ws


# -- the invalid frame (control_plane_client.py:442 before NR181) -----------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("as_bytes", [False, True], ids=["text", "bytes"])
async def test_an_invalid_frame_is_logged_by_length_and_digest_never_quoted(as_bytes, monkeypatch, caplog):
    invalid = '{"id": "req-invented", "type": "permissions_v2_answer_submit", "payload": {"question": "' + MARKER
    frame = invalid.encode("utf-8") if as_bytes else invalid
    after = json.dumps({"id": "req-after", "type": "healthcheck", "payload": {}})
    seen = []

    async def handler(message):
        seen.append(message["id"])
        return {"id": message["id"], "status": "ok", "payload": {"status": "ok"}}

    with caplog.at_level(logging.DEBUG):
        ws = await _run_client(monkeypatch, [frame, after], handler)

    assert seen == ["req-after"] and ws.sent                      # the loop went on to the next frame
    lines = [record for record in caplog.records if "not valid JSON" in record.getMessage()]
    assert len(lines) == 1
    encoded = invalid.encode("utf-8")
    assert lines[0].getMessage() == (
        f"Relay message is not valid JSON: length={len(encoded)} sha256={_digest(encoded)}")
    assert MARKER not in _everything_logged(caplog.records)


def test_the_fingerprint_is_length_and_a_short_digest_of_any_value():
    assert fingerprint("a frame") == f"length=7 sha256={_digest(b'a frame')}"
    assert fingerprint(b"a frame") == fingerprint("a frame") == fingerprint(bytearray(b"a frame"))
    assert fingerprint("é") == f"length=2 sha256={_digest('é'.encode())}"             # bytes as they travel
    assert fingerprint(None) == f"length=0 sha256={_digest(b'')}"
    assert fingerprint("\ud800" + MARKER).startswith("length=")                          # a lone surrogate
    for value in ({"q": MARKER}, [MARKER], 12345, object()):
        line = fingerprint(value)
        assert MARKER not in line and line.startswith("length=") and len(line.split("sha256=")[1]) == DIGEST_HEX

    class Unreadable:
        def __repr__(self):
            raise RuntimeError(MARKER)

    assert fingerprint(Unreadable()) == "length=? type=Unreadable"


# -- a handler's error (control_plane_client.py:782 before NR181) -----------------------------------------------------

def _raise_with_the_frame_in_its_text(message):
    raise ValueError("input_value=" + repr(message["payload"]))


@pytest.mark.asyncio
async def test_a_handlers_error_is_logged_by_class_and_location_never_its_text(caplog):
    async def handler(message):
        _raise_with_the_frame_in_its_text(message)

    client = ControlPlaneClient(control_plane_url="ws://cp.invalid/ws/engine", api_key="test-key", handler=handler,
                                verify_ssl=False)
    ws = FakeWebSocket([])
    frame = {"id": "req-invented", "type": "query", "payload": {"question": MARKER}}
    with caplog.at_level(logging.DEBUG, logger="topos.control_plane_client"):
        await client._handle_message(ws, frame)
    [line] = [record for record in caplog.records if "Handler raised exception" in record.getMessage()]
    text = line.getMessage()
    assert text.startswith("Handler raised exception: type=query id=req-invented error=ValueError\n")
    assert "_raise_with_the_frame_in_its_text" in text and "test_nr181_no_raw_frame_logs.py" in text
    assert line.exc_info is None
    assert MARKER not in _everything_logged(caplog.records)
    # What goes back to the control plane is not a log line and is unchanged here (NR181 report, backlog).
    assert json.loads(ws.sent[0])["status"] == "error"


# -- ingestion's preview, which a relayed store_message reaches --------------------------------------------------------

@pytest.mark.asyncio
async def test_a_relayed_store_message_never_logs_its_content(monkeypatch, caplog):
    from topos.core.handlers import messages
    from topos.ingestion import ingest_helpers
    from topos.ingestion.log_preview import field_preview

    async def ingest_ui_payload(**kwargs):
        logging.getLogger("topos.ingestion.ingest_helpers").info(
            "content_preview=%s", field_preview(kwargs["payload"].get("content")))
        return {"status": "ok", "job_id": "job-invented", "records_processed": 1}

    monkeypatch.setattr(ingest_helpers, "ingest_ui_payload", ingest_ui_payload)
    content = "An invented note " + MARKER
    message = {"id": "req-invented", "type": "store_message",
               "payload": {"dataset_id": "owner-invented:default", "sender_type": "user", "content": content}}
    with caplog.at_level(logging.DEBUG):
        response = await messages.handle_store_message(message)
    assert response["status"] == "ok"
    rendered = _everything_logged(caplog.records)
    assert MARKER not in rendered
    assert f"length={len(content.encode())} sha256={_digest(content.encode())}" in rendered


# -- no whole frame reaches a logging call on the relay's paths -------------------------------------------------------

RELAY_PATHS = ("topos/control_plane_client.py", "topos/core/handlers/__init__.py",
               "topos/permissions_v2/answer_transport.py", "topos/permissions_v2/search_transport.py",
               "topos/permissions_v2/answer_release.py", "topos/permissions_v2/search_release.py",
               "topos/core/handlers/messages.py", "topos/ingestion/ingest_helpers.py")
WHOLE = {"raw", "frame", "data", "message", "msg", "payload", "body", "question", "answer", "prompt", "content",
         "text", "envelope", "output", "request", "response", "resp", "record"}
LEVELS = {"debug", "info", "warning", "warn", "error", "exception", "critical", "log"}


def _logging_calls(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in LEVELS
                and "log" in ast.unparse(node.func.value).lower()):
            yield node


def _routing_key(key) -> bool:
    """`frame["id"]`, `record["message_id"]`, `data["type"]`: a routing field, not content."""
    return (isinstance(key, ast.Constant) and isinstance(key.value, str)
            and (key.value in ("id", "type", "status") or key.value.endswith("_id")))


def test_no_logging_call_on_the_relay_paths_passes_a_whole_frame_or_body():
    found, seen = [], {}
    for rel in RELAY_PATHS:
        tree = ast.parse((ROOT / rel).read_text(), rel)
        calls = list(_logging_calls(tree))
        seen[rel] = len(calls)
        for call in calls:
            for arg in call.args[1:]:
                if isinstance(arg, ast.Name) and arg.id in WHOLE:
                    found.append(f"{rel}:{call.lineno} {arg.id}")
                if (isinstance(arg, ast.Subscript) and isinstance(arg.value, ast.Name) and arg.value.id in WHOLE
                        and not _routing_key(arg.slice)):
                    found.append(f"{rel}:{call.lineno} {ast.unparse(arg)}")
    assert found == []
    # The walk sees the logging there is (the search door logs nothing of its own; its refusals go through
    # search_timing, which writes fixed words only).
    assert seen["topos/control_plane_client.py"] >= 15 and seen["topos/ingestion/ingest_helpers.py"] >= 3
    assert seen["topos/permissions_v2/search_transport.py"] == 0


def test_the_walk_finds_a_whole_frame_when_there_is_one():
    """The positive control: the shape that was at control_plane_client.py:442 is found."""
    tree = ast.parse('logger.warning("Relay message is not valid JSON: %s", raw)\n')
    [call] = list(_logging_calls(tree))
    assert isinstance(call.args[1], ast.Name) and call.args[1].id in WHOLE
    [content, routing] = [ast.parse(text).body[0].value for text in ('payload["content"]', 'payload["message_id"]')]
    assert not _routing_key(content.slice) and _routing_key(routing.slice)
