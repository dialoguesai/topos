"""Dump the message types registered in the control-plane handler registry.

Regenerates the committed protocol snapshot:

    uv run python scripts/dump_handled_types.py > topos/protocol/handled_message_types.json

The snapshot lets external tooling (and package consumers) discover which
control-plane message types this engine version dispatches without importing
the package. tests/test_protocol_handled_types.py fails if it goes stale.

Two keys are emitted:

- handled_message_types: every registered type.
- owner_only_message_types: the subset registered with @handles(..., owner_only=True),
  which must never appear on an agent or cross-user surface.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> None:
    from topos.core.handlers import HANDLERS, OWNER_ONLY_MESSAGE_TYPES

    print(
        json.dumps(
            {
                "handled_message_types": sorted(HANDLERS),
                "owner_only_message_types": sorted(OWNER_ONLY_MESSAGE_TYPES),
            },
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
