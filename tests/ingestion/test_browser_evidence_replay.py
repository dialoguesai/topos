"""Optional acceptance test: replay real extension receipts into an isolated node DB.

TOPOS_BROWSER_EVIDENCE_DIR points to local chrome.json / safari.json receipts.
No private receipts or conversation identifiers belong in this repository.
"""
import json
import os
from pathlib import Path

import pytest

from test_activity_writer_class import conn, captured_jobs, _owner_capture, _rows  # noqa: F401
from topos.features.provenance.browser_actor import browser_actor, personal_browser_activity, conversation_activity


@pytest.mark.asyncio
async def test_real_browser_receipts_preserve_identity_and_stay_out_of_personal_interests(conn, captured_jobs):
    evidence_dir = os.environ.get("TOPOS_BROWSER_EVIDENCE_DIR")
    if not evidence_dir:
        pytest.skip("Opt-in: set TOPOS_BROWSER_EVIDENCE_DIR to local browser receipts")
    from topos.features.facts.reads import list_timeline
    from topos.features.timeline_projection import project_timeline_rows
    from topos.enrichment.authored_only import rows_for_job
    report = {"test_kind": "captured-browser-payload replay through the real node app_ingest handler in an isolated database", "browsers": {}}
    seen = set()
    for browser in ("chrome", "safari"):
        evidence = json.loads((Path(evidence_dir) / f"{browser}.json").read_text())
        assert evidence["ack"]["actor"]["conversation_id"] == evidence["expected_actor"]["conversation_id"]
        records = [r for receipt in evidence["receipts"] if receipt["source_id"] == "browser_visits" for r in receipt["records"]]
        assert records, browser
        for record in records:
            assert record["actor"]["conversation_id"] == evidence["expected_actor"]["conversation_id"]
            result = await _owner_capture(f"proof-{record['capture_id']}", record)
            assert result["status"] == "ok", result
            seen.add(record["capture_id"])
        report["browsers"][browser] = {"captures": [r["capture_id"] for r in records], "actor": evidence["ack"]["actor"]}
    rows = _rows(conn)
    assert len(rows) == len(seen)
    for row in rows:
        assert browser_actor(row)["kind"] == "agent"
        assert not personal_browser_activity(row)
        assert browser_actor(row)["conversation_id"] == evidence["expected_actor"]["conversation_id"]
    assert len(conversation_activity(conn, source_id="codex_sessions", conversation_id=evidence["expected_actor"]["conversation_id"])) == len(seen)
    project_timeline_rows(conn, [{**r, "_table": "activity_events"} for r in rows])
    ai = list_timeline(conn, actor_kind="ai")
    assert ai["total"] == len(seen)
    assert list_timeline(conn, actor_kind="human")["total"] == 0
    for job in ("entities", "topics", "facts", "goal_extraction", "stats", "sentiment"):
        assert rows_for_job(job, rows) == []
    report.update(canonical_rows=len(rows), ai_timeline_total=ai["total"], human_timeline_total=0, personal_jobs_eligible_rows=0,
                  parent_lookup_rows=len(rows), timeline=ai)
    (Path(evidence_dir) / "candidate-node-proof.json").write_text(json.dumps(report, indent=2, default=str) + "\n")
