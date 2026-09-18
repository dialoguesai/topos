"""The p2a-v3 schema exports (opaque record ids, canonical.message_disclosure.v2).

Rewrite them only after a deliberate contract change, from the engine root:
    .venv/bin/python3 -m tests.permissions_v2.source_opaque_schemas
It also rewrites the protocol exports whose unions p2a-v3 joined (via
source_attested_schemas). Pure contract modules only: no database, no settings.
test_bk3_p2a_v3_contract pins every file byte for byte.
"""
from __future__ import annotations

from pathlib import Path

from tests.permissions_v2 import source_attested_schemas
from topos.permissions_v2.registry import OpaqueMessageDisclosure, OpaqueSubjectSourceDecision, OpaqueSubjectSourcePolicy
from topos.permissions_v2.signing import OpaqueSourceAuthorityBinding, OpaqueSourceEnvelopeBody, SignedOpaqueSourceEnvelope

OPAQUE_FIXTURES = source_attested_schemas.FIXTURES / "source_opaque"
OPAQUE_MODELS = (OpaqueSubjectSourcePolicy, OpaqueSubjectSourceDecision, OpaqueMessageDisclosure,
                 OpaqueSourceAuthorityBinding, OpaqueSourceEnvelopeBody, SignedOpaqueSourceEnvelope)


def write() -> list[Path]:
    OPAQUE_FIXTURES.mkdir(exist_ok=True)
    written = []
    for model in OPAQUE_MODELS:
        path = OPAQUE_FIXTURES / f"{model.__name__}.schema.json"
        path.write_bytes(source_attested_schemas.export(model))
        written.append(path)
    return written + source_attested_schemas.write()


if __name__ == "__main__":
    for path in write():
        print(path)
