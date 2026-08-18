"""The engine's half of the query field contract.

Every seam between a client and this engine rebuilds its payload from a hand-written
allow-list. A field can be declared at one end, sent faithfully, and disappear in the
middle with nothing failing and nothing logged — twice in one day on 2026-08-17
(`sourceRefs` in the front end, `retrieval_text` in the control plane), and the second
was found only because someone went looking for it.

`protocol/query_field_contract.json` is the declaration those seams are tested against.
This file guards the engine end: a field the contract promises must actually be read
here, and one the contract promises to return must actually be emitted. The control
plane and the front end each guard their own seam against the same file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "topos" / "protocol" / "query_field_contract.json").read_text())
HANDLER = (ROOT / "topos" / "core" / "handlers" / "query.py").read_text()


def test_the_contract_is_well_formed() -> None:
    assert CONTRACT["version"] >= 1
    assert CONTRACT["request"]["required_forward"]
    assert CONTRACT["response"]["required_return"]


@pytest.mark.parametrize("field", CONTRACT["request"]["required_forward"])
def test_every_forwarded_request_field_is_read_by_the_handler(field: str) -> None:
    """A field nobody reads is a field the contract should not be promising.

    `dataset_id` is the one legitimate exception — it is consumed by the transport layer
    to pick the database before the handler runs.
    """
    if field == "dataset_id":
        pytest.skip("consumed by the transport when selecting the dataset, not by the handler")
    assert re.search(rf'payload\.get\("{re.escape(field)}"', HANDLER), (
        f"{field!r} is promised by the contract but never read in handlers/query.py — "
        "either the handler regressed or the contract is aspirational"
    )


def test_query_outranks_intent_and_retrieval_text_outranks_neither() -> None:
    """The precedence that keeps the classifier and planner on a sentence.

    `query` (the owner's words) must win over `intent` (a keyword digest); sending the
    digest as the query text was measured on 2026-08-16 to lose time windows and make
    the scope classifier abstain on keyword soup. `retrieval_text` is a companion for
    needle matching, never a substitute for either.
    """
    assert re.search(r'payload\.get\("query"\)\s*or\s*payload\.get\("intent"\)', HANDLER)
    assert "retrieval_text" in HANDLER
    assert not re.search(r'query_text\s*=\s*.*payload\.get\("retrieval_text"\)', HANDLER)


@pytest.mark.parametrize("field", ["turn_outcome", "public_result"])
def test_core_response_fields_are_emitted(field: str) -> None:
    assert f'"{field}"' in HANDLER
