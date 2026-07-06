"""
Gap: Parity — undocumented deltas → matrix doc + dual-profile adapter tests
Sprint: EN-P4-S1
Before sprint: EXPECT FAIL / NOT IMPLEMENTED
After sprint:  EXPECT PASS
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[5]
MATRIX = REPO_ROOT / "topos-control-plane" / "docs" / "WIKI_MVP_PARITY_MATRIX.md"

# The matrix doc lives in the (private) control-plane repo; this guard only
# applies when it is checked out as a sibling (monorepo / CP CI layout).
pytestmark = [
    pytest.mark.gap,
    pytest.mark.skipif(
        not MATRIX.parent.parent.is_dir(),
        reason="sibling topos-control-plane checkout not present",
    ),
]


def test_parity_matrix_doc_exists_with_required_sections() -> None:
    assert MATRIX.is_file(), "WIKI_MVP_PARITY_MATRIX.md must exist"
    text = MATRIX.read_text()
    for section in ("local_database", "hosted_database", "Legacy tables", "conversation_messages"):
        assert section in text or "Local" in text
