from __future__ import annotations

from typing import Any, Dict, List


class ToolCallsJob:
    def get_job_name(self) -> str:
        return "tool_calls"

    async def run(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        _ = raw_records
        return []
