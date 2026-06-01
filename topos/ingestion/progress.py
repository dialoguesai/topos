"""Progress tracking for ingestion jobs."""

from __future__ import annotations

import time
from typing import Optional


class IngestionProgress:
    def __init__(self, job_id: str, records_total: Optional[int] = None):
        self.job_id = job_id
        self.records_total = records_total
        self.records_processed = 0
        self.start_time = time.time()
        self.last_update_time = self.start_time
        self.current_step = "parsing"
        self.errors_count = 0

    def update(self, records_processed: int, current_step: Optional[str] = None) -> None:
        self.records_processed = records_processed
        self.last_update_time = time.time()
        if current_step:
            self.current_step = current_step

    def get_progress_percent(self) -> float:
        if not self.records_total:
            return 0.0
        return min(100.0, (self.records_processed / self.records_total) * 100.0)

    def get_estimated_seconds_remaining(self) -> Optional[int]:
        if not self.records_total or self.records_processed == 0:
            return None
        elapsed = time.time() - self.start_time
        if elapsed <= 0:
            return None
        rate = self.records_processed / elapsed
        if rate <= 0:
            return None
        remaining = self.records_total - self.records_processed
        return int(remaining / rate)

    def should_report(self, min_interval: float = 1.0) -> bool:
        return (time.time() - self.last_update_time) >= min_interval

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "progress_percent": self.get_progress_percent(),
            "records_processed": self.records_processed,
            "records_total": self.records_total,
            "estimated_seconds_remaining": self.get_estimated_seconds_remaining(),
            "current_step": self.current_step,
            "errors_count": self.errors_count,
        }
