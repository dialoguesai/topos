import pytest

from topos.ingestion.parser import parse_json_stream


async def _bytes_stream(payload: bytes):
    yield payload


@pytest.mark.asyncio
async def test_parse_json_stream_accepts_comment_lines() -> None:
    payload = b"""
{
  "calendar": {
    "events": [
      // first event
      {"id": "evt_001", "title": "Team Strategy Meeting"},
      /* second event */
      {"id": "evt_002", "title": "Therapy Session"}
    ]
  }
}
"""
    records = []
    async for rec in parse_json_stream(_bytes_stream(payload)):
        records.append(rec)

    assert len(records) == 1
    assert records[0]["calendar"]["events"][0]["id"] == "evt_001"
    assert records[0]["calendar"]["events"][1]["id"] == "evt_002"


@pytest.mark.asyncio
async def test_parse_json_stream_surfaces_parse_error_details() -> None:
    payload = b'{"calendar":{"events":[{"id":"evt_001",}]}}'

    with pytest.raises(ValueError, match=r"Failed to parse JSON file: .+line .+column .+"):
        async for _ in parse_json_stream(_bytes_stream(payload)):
            pass
