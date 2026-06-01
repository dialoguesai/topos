"""
URL classification for browser visits. Thin wrapper over the Engine (Sprint 03).

Prefer building a ProcessingTask and calling Engine.run() directly. This module
remains for backward compatibility with any code that still calls classify_url().
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..engine import Engine, build_url_classification_task


def classify_url(url: str, title: Optional[str] = None) -> Dict[str, Any]:
    """
    Classify a URL (and optional title) into a category. Uses the Engine.
    Returns dict with category, confidence, model (same shape as before migration).
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("url must be a non-empty string")
    task = build_url_classification_task(
        task_id="website_classifier",
        url=url.strip(),
        title=title,
    )
    engine = Engine()
    result = engine.run(task)
    if result.status != "completed":
        raise RuntimeError(result.error or f"Engine returned status {result.status}")
    return result.output
