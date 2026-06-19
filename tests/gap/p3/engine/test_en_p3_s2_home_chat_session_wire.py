"""GT-EN-P3-S2-04: Handler resolves query_session_id."""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_query_handler_forwards_query_session_id() -> None:
    from topos.core.handlers import handle_control_plane_request

    with patch("topos.query.runtime.get_query_orchestrator") as mock_factory:
        mock_orch = mock_factory.return_value
        mock_orch.execute = AsyncMock(
            return_value={"turn_outcome": "live_query", "session_id": "qs_handler"}
        )
        await handle_control_plane_request(
            {
                "type": "query",
                "id": "req-1",
                "payload": {
                    "scope_id": "availability:read",
                    "access_mode": "inference",
                    "query_session_id": "qs_handler",
                    "manifest": {
                        "scope_id": "availability:read",
                        "primary_dimensions": ["Time"],
                    },
                    "intent": "Am I free?",
                },
            }
        )
        mock_orch.execute.assert_awaited_once()
        kwargs = mock_orch.execute.await_args.kwargs
        assert kwargs["query_session_id"] == "qs_handler"
