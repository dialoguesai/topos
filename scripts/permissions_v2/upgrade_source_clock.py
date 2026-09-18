"""Owner-run, node STOPPED: upgrade an ingest snapshot store's source clock from v1 to v2.

v2 stops sync receipts from staling every snapshot enrollment (design §7 W3/ING-3;
`IngestProvenanceService.upgrade_source_clock_v2`). The upgrade advances the store's
generation once, so every current enrollment goes stale once and resumes with a fresh
signed run. Run it with the node stopped: a running node holds the old marker in memory
and refuses the store until it restarts.

Every path is explicit; there is no default database. Example:
    python scripts/permissions_v2/upgrade_source_clock.py --canonical-db <dir>/database.db \\
        --environment-id permissions-beta-local --node-id <node> --resource-id <resource> --owner-id <owner>
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--canonical-db", type=Path, required=True)
    for field in ("environment-id", "node-id", "resource-id", "owner-id"):
        parser.add_argument("--" + field, required=True)
    args = parser.parse_args()
    from topos.permissions_v2.evidence import EvidenceBinding
    from topos.permissions_v2.ingest_provenance import IngestProvenanceService
    from topos.principal import OWNER_APP, Principal, reset_principal, set_principal

    canonical = args.canonical_db.resolve(strict=True)
    binding = EvidenceBinding(environment_id=args.environment_id, node_id=args.node_id,
                              resource_id=args.resource_id, owner_id=args.owner_id)
    service = IngestProvenanceService(canonical_database=canonical, binding=binding,
                                      snapshot_root=canonical.parent / "permissions-v2" / "ingest-snapshots")
    token = set_principal(Principal(cls=OWNER_APP, channel="uds", acting_user=args.owner_id))
    conn = sqlite3.connect(canonical)
    try:
        print(json.dumps(service.upgrade_source_clock_v2(conn)))
    finally:
        conn.close()
        reset_principal(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
