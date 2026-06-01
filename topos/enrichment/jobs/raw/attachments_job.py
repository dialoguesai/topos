from __future__ import annotations

from typing import Any, Dict, List


class AttachmentsJob:
    def get_job_name(self) -> str:
        return "attachments"

    async def run(self, raw_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        _ = raw_records
        return []
