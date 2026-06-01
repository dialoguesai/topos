from __future__ import annotations

from typing import Any, Dict, List


class LanguageJob:
    def get_job_name(self) -> str:
        return "language"

    async def run(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        _ = raw_records
        return []
