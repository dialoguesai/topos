"""E2E: ChatGPT conversations.json → canonical + embeddings + graph."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "e2e" / "chatgpt_conversations_ingest.py"
CONVERSATIONS = ROOT.parent / "conversations.json"

pytestmark = pytest.mark.e2e


@pytest.mark.skipif(not CONVERSATIONS.is_file(), reason="conversations.json not present at repo root")
def test_chatgpt_conversations_ingest_e2e() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--input",
            str(CONVERSATIONS),
            "--max-conversations",
            "2",
            "--embedding-fallback",
            "stub",
            "--json",
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    report = json.loads(proc.stdout)
    verify = report["verify"]
    assert verify["ai_chat_messages"] > 0
    assert verify["ai_chat_conversations"] > 0
    assert verify["embedding_metadata_total"] > 0
    assert verify["graph_edges"] > 0
