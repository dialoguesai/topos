from __future__ import annotations

from typing import Any, Dict, List


class TimeNormalizationJob:
    def get_job_name(self) -> str:
        return "time_normalization"

    async def run(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        _ = raw_records
        return []
