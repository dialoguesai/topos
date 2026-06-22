#!/usr/bin/env python3
"""Backfill ingest-time NSFW tags on canonical message tables."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

if "--nsfw-only" not in sys.argv:
    sys.argv.insert(1, "--nsfw-only")

runpy.run_path(str(Path(__file__).resolve().parent / "backfill_disclosure.py"), run_name="__main__")
