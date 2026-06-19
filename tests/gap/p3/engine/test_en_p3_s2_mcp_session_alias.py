"""GT-EN-P3-S2-04b: session_id alias for query_session_id."""

from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.gap


@pytest.mark.asyncio
async def test_query_handler_accepts_session_id_alias() -> None:
    from topos.core.handlers import handle_control_plane_request

    with patch("topos.query.runtime.get_query_orchestrator") as mock_factory:
        mock_orch = mock_factory.return_value
        mock_orch.execute = AsyncMock(return_value={"turn_outcome": "live_query"})
        await handle_control_plane_request(
            {
                "type": "query_live",
                "id": "req-2",
                "payload": {
                    "scope_id": "messages:read",
                    "access_mode": "summary",
                    "session_id": "qs_alias",
                    "manifest": {
                        "scope_id": "messages:read",
                        "primary_dimensions": ["messages"],
                    },
                },
            }
        )
        kwargs = mock_orch.execute.await_args.kwargs
        assert kwargs["query_session_id"] == "qs_alias"
