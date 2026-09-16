"""The p2a-v2 schema exports, and the protocol exports whose unions p2a-v2 joined.

Rewrite them only after a deliberate contract change, from the engine root:
    .venv/bin/python3 -m tests.permissions_v2.source_attested_schemas
It imports the pure contract modules alone, so it opens no database and loads no
settings. test_source_release_attested pins every file it writes, byte for byte.
"""
from __future__ import annotations

import json
from pathlib import Path

from topos.permissions_v2.forwarding import SignedNodeResult
from topos.permissions_v2.protocol import AppliedCommandReceipt, NodeGrantState, SignedAck, SignedMutation
from topos.permissions_v2.registry import AttestedSubjectSourceDecision, AttestedSubjectSourcePolicy
from topos.permissions_v2.signing import (AttestedSourceAuthorityBinding, AttestedSourceEnvelopeBody,
    SignedAttestedSourceEnvelope)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "permissions_v2"
ATTESTED_FIXTURES = FIXTURES / "source_attested"
ATTESTED_MODELS = (AttestedSubjectSourcePolicy, AttestedSubjectSourceDecision, AttestedSourceAuthorityBinding,
                   AttestedSourceEnvelopeBody, SignedAttestedSourceEnvelope)
# Exports whose unions name every capability, so adding one moves them on purpose.
PROTOCOL_MODELS = (SignedMutation, SignedAck, NodeGrantState, AppliedCommandReceipt, SignedNodeResult)


def export(model) -> bytes:
    """The checked-in byte form of every permissions_v2 schema fixture."""
    return (json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n").encode("ascii")


def write() -> list[Path]:
    ATTESTED_FIXTURES.mkdir(exist_ok=True)
    written = []
    for directory, models in ((ATTESTED_FIXTURES, ATTESTED_MODELS), (FIXTURES, PROTOCOL_MODELS)):
        for model in models:
            path = directory / f"{model.__name__}.schema.json"
            path.write_bytes(export(model))
            written.append(path)
    return written


if __name__ == "__main__":
    for path in write():
        print("wrote", path)
