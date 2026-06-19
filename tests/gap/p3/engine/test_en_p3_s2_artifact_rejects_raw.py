"""GT-EN-P3-S2-01d: Artifact rejects forbidden payload keys."""

import pytest

from topos.query.session_utils import validate_public_result
from topos.storage.adapters.fakes import InMemoryQuerySessionStore

pytestmark = pytest.mark.gap


def test_validate_public_result_rejects_evidence() -> None:
    with pytest.raises(ValueError, match="forbidden"):
        validate_public_result({"answer": "yes", "evidence": ["row-1"]})


def test_append_artifact_rejects_forbidden_keys() -> None:
    store = InMemoryQuerySessionStore()
    store.put({"session_id": "qs_bad", "requester_id": "owner", "intent_hash": "h"})
    with pytest.raises(ValueError):
        store.append_artifact(
            "qs_bad",
            {
                "cache_key": "k",
                "public_result_json": {"source_rows": [{"id": 1}]},
            },
        )
