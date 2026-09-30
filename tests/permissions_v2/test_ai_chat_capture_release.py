"""OD-39 end to end: the owner's stamped extension prompt releases under the grant that withheld it.

``test_chatgpt_owner_snapshot_extension_control`` shows the extension's row
withheld as ``not_owner_authored`` with nothing else in the way: new text, the
owner's conversation, a grant over both sources. That row was written under the
owner's frontend stamp. Here the same flow runs with the stamp the CP gives the
owner's capture app (rule C: requester == owner, app in OWNER_CAPTURE_APP_IDS),
and the prompt now leaves as its exact text; the owner's other app still does not.
"""
from __future__ import annotations

import sqlite3

import pytest

from tests.permissions_v2.test_chatgpt_owner_snapshot_canary import (
    PROMPT_ID, qualify, released_text, review, run_chatgpt_lane, seed_locator)
from tests.permissions_v2.test_chatgpt_owner_snapshot_extension_control import (
    EXTENSION, EXTENSION_TEXT, grant_over_lane_and_extension)
from tests.permissions_v2.test_ingest_snapshot_work_canary import (  # noqa: F401 (fixtures)
    FRONTEND, OWNER_ID, _next, canonical, corpus, lane, paired_runtime, projection_runtime, protocol_call)
from tests.permissions_v2.test_source_release_sibling_lane import source_adapter_read
from topos.core.handlers import handle_control_plane_request
from topos.principal import OWNER_APP, Principal

CAPTURE_APP = "chatgpt-shadow-extension"


async def _app_ingest_as(lane, monkeypatch, record, *, client_id):
    import topos.core.handlers as handlers
    import topos.core.state as state
    conn = sqlite3.connect(str(lane.canonical), check_same_thread=False)
    monkeypatch.setattr(state, "get_db_connection", lambda: conn)
    monkeypatch.setattr(handlers, "get_db_connection", lambda: conn)
    monkeypatch.setenv("TOPOS_PIPELINE_WORKER", "off")
    try:
        response = await handle_control_plane_request({"id": _next(lane, "app-ingest"), "type": "app_ingest",
            "payload": {"user_id": OWNER_ID, "dataset_id": f"{OWNER_ID}:chatgpt", "source_id": EXTENSION,
                        "records": [record]}},
            principal=Principal(cls=OWNER_APP, channel="cp_relay", acting_user=OWNER_ID, client_id=client_id))
    finally:
        conn.close()
    assert response["status"] == "ok", response
    return response


@pytest.mark.asyncio
@pytest.mark.parametrize(("client_id", "released"), [(CAPTURE_APP, True), (FRONTEND, False)])
async def test_the_owners_capture_app_prompt_releases_and_another_owner_app_does_not(
        lane, monkeypatch, client_id, released):
    monkeypatch.delenv("TOPOS_OWNER_CAPTURE_APP_IDS", raising=False)
    run = await run_chatgpt_lane(lane)
    assert run.result.status == "ok"
    message_id = f"capture-prompt-{int(released)}"
    await _app_ingest_as(lane, monkeypatch, {"id": message_id, "thread_id": "capture-thread-1", "role": "user",
                                             "content": EXTENSION_TEXT, "created_at": 1757000400.5}, client_id=client_id)
    with canonical(lane) as conn:
        writer = conn.execute("SELECT writer_class, writer_app_id FROM ai_chat_messages WHERE message_id=?",
                              (message_id,)).fetchone()
    assert tuple(writer) == ("owner_app", client_id)

    lane_fact = seed_locator(lane, PROMPT_ID, "Contoso oolong")
    await review(lane_fact, review_id="capture-lane-review")
    capture_fact = seed_locator(lane, message_id, "Fabrikam espresso", source_id=EXTENSION)
    _snapshot, recorded = await review(capture_fact, review_id="capture-extension-review")
    authority = await grant_over_lane_and_extension(lane)

    outputs, error = source_adapter_read(lane, authority, capture_fact, request_id=f"capture-read-{int(released)}")
    if released:
        assert qualify(lane, capture_fact).verdict == "qualified"
        assert error is None and released_text(outputs) == [(message_id, "ai_chat_messages", EXTENSION_TEXT)]
    else:
        assert (outputs, error) == ([], "not_owner_authored")
        assert recorded["state"]["qualification"]["reason_code"] == "not_owner_authored"
