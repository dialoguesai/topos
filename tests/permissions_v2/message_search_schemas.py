"""The p2c-v1 schema exports. Rewrite only after a deliberate contract change, from the engine root:
    .venv/bin/python3 -m tests.permissions_v2.message_search_schemas
test_message_search_contract pins every file byte for byte; the control plane pins the same bytes.
"""
from __future__ import annotations

from pathlib import Path

from tests.permissions_v2.source_attested_schemas import FIXTURES, export
from topos.permissions_v2.search_contract import (MessageSearchResult, SearchIntent, SearchMemberDecision, SearchPolicy,
    SearchSetDecision)
from topos.permissions_v2.signing import SearchAuthorityBinding, SearchEnvelopeBody, SearchRequestContext, SignedSearchEnvelope

SEARCH_FIXTURES = FIXTURES / "message_search"
SEARCH_MODELS = (SearchPolicy, SearchIntent, MessageSearchResult, SearchSetDecision, SearchMemberDecision,
                 SearchAuthorityBinding, SearchEnvelopeBody, SignedSearchEnvelope, SearchRequestContext)


def write() -> list[Path]:
    SEARCH_FIXTURES.mkdir(exist_ok=True)
    written = []
    for model in SEARCH_MODELS:
        path = SEARCH_FIXTURES / f"{model.__name__}.schema.json"
        path.write_bytes(export(model))
        written.append(path)
    return written


if __name__ == "__main__":
    for path in write():
        print("wrote", path)
