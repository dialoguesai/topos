#!/usr/bin/env python3
"""Open an upgrade fixture with *current* checkout code and assert catch-up.

PLAN_NODE_RELEASE_MIGRATIONS M4 — upgrade matrix runner.

  1. ensure_migrations_applied
  2. run_pending_upgrades (auto steps; consent → pending_consent)
  3. Assert: no failed ledger rows; every planned step reached 'done'; the
     executable steps actually RAN (steps_run > 0, sources walked, derived rows
     written); baseline == shipped OR only pending_consent remaining; schema
     has spec_version when current code ships that migration

The third group is deliberately about EFFECT, not the absence of errors. This
job spent 1.3.4–1.3.6 red because TOPOS_KEY was unset (settings validation
rejected every step before it started), and simply supplying the key would have
turned it green over an empty run: the fixture carried no `timeline` rows, so
every enrichment step walked zero sources and ledgered "done" regardless. A
check that cannot distinguish "did the work" from "found nothing to do" is not
a safety net. See KNOWN_NO_OP_STEPS for what this job does NOT cover.

Exit non-zero on failure.

Usage:
  python scripts/run_upgrade_matrix.py --db /tmp/upgrade-fixture.db
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _shipped_version() -> str:
    from topos.__version__ import __version__

    return __version__


def _has_spec_version_migration() -> bool:
    try:
        from topos.storage.db.migrations.enrichment_spec_version_v1 import (  # noqa: F401
            MIGRATION_ID,
        )

        return True
    except ImportError:
        return False


def _assert_spec_version_column(conn: sqlite3.Connection) -> None:
    if not _has_spec_version_migration():
        print("spec_version migration not in this build; skipping column assert")
        return
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(message_entities)").fetchall()
    }
    if "spec_version" not in cols:
        raise AssertionError(
            "message_entities missing spec_version after ensure_migrations_applied"
        )
    print("ok: message_entities.spec_version present")


def _failed_ledger(conn: sqlite3.Connection) -> list[tuple]:
    try:
        return list(
            conn.execute(
                "SELECT version, step_id, status, detail_json FROM derivation_ledger "
                "WHERE status='failed'"
            ).fetchall()
        )
    except sqlite3.Error:
        return []


# Steps proven to perform no work in ANY environment (not a CI limitation).
# Excluded from the effect assertions below so this job does not sit
# permanently red for a defect it cannot fix on its own — but printed loudly on
# every run so the gap stays visible instead of decaying into background noise.
# Remove an entry the moment the underlying defect is fixed.
KNOWN_NO_OP_STEPS: dict[str, str] = {}


def _table_count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int:
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return -1


def _ledger_detail(conn: sqlite3.Connection, step_id: str) -> dict:
    try:
        row = conn.execute(
            "SELECT detail_json FROM derivation_ledger WHERE step_id=?", (step_id,)
        ).fetchone()
    except sqlite3.Error:
        return {}
    if not row or not row[0]:
        return {}
    try:
        loaded = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _assert_steps_did_work(
    conn: sqlite3.Connection, plan: dict, result: dict, shipped: str
) -> None:
    """Assert the steps EXECUTED, not merely that nothing failed.

    An absence of 'failed' rows is not evidence of coverage: before the fixture
    carried canonical rows, every enrichment step ledgered "done" with
    {"sources": {}} and the matrix reported success over an empty run.
    """
    steps = list(plan.get("steps") or [])
    if not steps:
        raise AssertionError(
            "no upgrade steps planned — the fixture baseline is not below the "
            "shipped version, so this run proves nothing. Check the fixture's "
            "engine.upgrade.baseline stamp against the manifest ladder."
        )

    # Every planned step must have reached a terminal success state.
    # 'pending_consent' is a legitimate resting place (a consent-gated step
    # waits for POST /v1/upgrade/consent), so it is accepted here and excluded
    # from the effect assertions below rather than treated as a failure.
    awaiting_consent: set[str] = set()
    for step in steps:
        step_id = str(step["id"])
        row = conn.execute(
            "SELECT status FROM derivation_ledger WHERE version=? AND step_id=?",
            (shipped, step_id),
        ).fetchone()
        status = str(row[0]) if row else None
        if status == "pending_consent":
            awaiting_consent.add(step_id)
            continue
        if status != "done":
            raise AssertionError(
                f"step {step_id!r} (kind={step.get('kind')!r}) ledger status is "
                f"{status!r}, expected 'done'"
            )

    executable = [
        s
        for s in steps
        if str(s.get("kind")) != "none" and str(s["id"]) not in awaiting_consent
    ]
    if executable and int(result.get("steps_run") or 0) <= 0:
        raise AssertionError(
            f"steps_run={result.get('steps_run')} with "
            f"{len(executable)} executable step(s) planned — nothing ran"
        )
    if not executable:
        print(
            "note: no executable steps this run "
            f"(pending_consent={sorted(awaiting_consent)}); skipping effect asserts"
        )
        return

    # The runner discovers work through exactly this call, so reuse it rather
    # than re-deriving the source list and risking a different answer.
    from topos.upgrades.runner import _real_source_ids

    sources = _real_source_ids(conn)
    if not sources:
        raise AssertionError(
            "fixture advertises no real sources in `timeline` — every "
            "enrichment_reprocess step would no-op while still ledgering 'done'. "
            "Rebuild the fixture with scripts/build_upgrade_fixture.py."
        )
    print(f"ok: fixture advertises source(s) {sources}")

    skipped_note = []
    for step in executable:
        step_id = str(step["id"])
        detail = _ledger_detail(conn, step_id)
        if step_id in KNOWN_NO_OP_STEPS:
            skipped_note.append(step_id)
            continue
        if str(step.get("kind")) == "enrichment_reprocess":
            walked = detail.get("sources") or {}
            if not walked:
                raise AssertionError(
                    f"step {step_id!r} walked ZERO sources (detail={detail}) — it "
                    f"ledgered 'done' without processing anything"
                )
            bad = {s: v for s, v in walked.items() if str(v) != "ok"}
            if bad:
                raise AssertionError(f"step {step_id!r} had non-ok sources: {bad}")
            print(f"ok: {step_id} walked {len(walked)} source(s) -> all ok")

    # Effect assertions: derived rows that only exist if the step really ran.
    placeholders = ",".join("?" for _ in sources)
    extracted = _table_count(
        conn, "message_entities", f"source_id IN ({placeholders})", tuple(sources)
    )
    if extracted <= 0:
        raise AssertionError(
            f"reextract-entities produced no message_entities rows for {sources} "
            f"(count={extracted}) — the NER pass did not run over the fixture"
        )
    mentions = _table_count(conn, "entity_mentions")
    print(f"ok: extraction wrote {extracted} message_entities, {mentions} entity_mentions")

    # backfill-attention-triage: attention_triage is a SIGNAL job, so the step
    # only does anything if the runner routes it down the signal lane. It spent
    # 1.3.0–1.3.6 ledgering "done" with jobs_run=0 and zero verdicts, which is
    # why this asserts on the verdict rows and not on the step's status.
    triage_detail = _ledger_detail(conn, "backfill-attention-triage")
    if triage_detail:
        verdicts = _table_count(conn, "triage_verdicts")
        if verdicts <= 0:
            raise AssertionError(
                f"backfill-attention-triage wrote 0 triage_verdicts "
                f"(detail={triage_detail}) — the step ledgered 'done' without "
                f"running the triage job. Check that the runner routes "
                f"SIGNAL_JOB_REGISTRY jobs through signal_job_names; "
                f"run_canonical() silently drops them."
            )
        print(f"ok: backfill-attention-triage wrote {verdicts} triage_verdicts")

    graph_detail = _ledger_detail(conn, "rebuild-entity-graph")
    if graph_detail:
        edges_after = int(graph_detail.get("edges_after") or 0)
        if edges_after <= 0:
            raise AssertionError(
                f"rebuild-entity-graph left 0 edges (detail={graph_detail}) — the "
                f"rebuild ran against an empty mention set"
            )
        print(
            f"ok: entity graph rebuilt to {edges_after} edges "
            f"({graph_detail.get('communities')} communities)"
        )

    for step_id in skipped_note:
        print(
            f"KNOWN GAP: step {step_id!r} is NOT covered by this job.\n"
            f"    {KNOWN_NO_OP_STEPS[step_id]}"
        )


def _pending_consent(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT step_id FROM derivation_ledger WHERE status='pending_consent'"
        ).fetchall()
        return [str(r[0]) for r in rows]
    except sqlite3.Error:
        return []


def run_matrix(db_path: Path) -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # Avoid accidental live-DB side effects / heavy runners during CI.
    os.environ.setdefault("TOPOS_SKIP_UPDATE_CHECK", "1")
    # NOT setdefault: this harness runs force_reprocess enrichment, so an
    # inherited TOPOS_DATABASE_PATH (a developer shell pointing at ~/.topos)
    # would silently re-derive the LIVE database instead of the fixture.
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    # Run upgrades inline (not background / UI-grace).
    os.environ["TOPOS_UPGRADE_RUNNER"] = "on"
    os.environ.setdefault("TOPOS_UPGRADE_UI_GRACE_SECONDS", "0")
    # Settings validation rejects construction without a key, which used to
    # fail every step before it started (steps_run=0, steps_failed=3 — the
    # whole job was red from 1.3.4 through 1.3.6). The value is never
    # transmitted: upgrade executors dispatch to engine internals rather than
    # HTTP ("no internal dispatch" / "no HTTP self-call" in upgrades/runner.py),
    # and the key's only consumers on this path (privacy_layer, ingestion
    # manager) gate on TOPOS_ENGINE_SERVICE_URL, which CI leaves unset. A real
    # key would buy zero extra coverage and put a live credential in CI.
    os.environ.setdefault("TOPOS_KEY", "ci-upgrade-matrix-dummy-key")

    from topos.storage.db.migrations import ensure_migrations_applied
    from topos.upgrades.runner import (
        plan_upgrade,
        read_baseline,
        run_pending_upgrades,
    )

    if not db_path.is_file():
        raise SystemExit(f"fixture DB not found: {db_path}")

    shipped = _shipped_version()
    conn = sqlite3.connect(str(db_path))
    try:
        print(f"ensure_migrations_applied on {db_path} (shipped={shipped})")
        ensure_migrations_applied(conn, skip_backup=True)
        _assert_spec_version_column(conn)

        plan = plan_upgrade(conn, shipped=shipped)
        print(
            f"plan: baseline={plan.get('baseline')!r} → shipped={plan.get('shipped')!r} "
            f"steps={len(plan.get('steps') or [])} fresh={plan.get('fresh_install')}"
        )

        result = run_pending_upgrades(conn, shipped=shipped)
        print(f"run_pending_upgrades: {result}")

        failed = _failed_ledger(conn)
        if failed:
            raise AssertionError(f"failed derivation_ledger rows: {failed}")

        # Absence of failures is not coverage — prove the steps did work.
        _assert_steps_did_work(conn, plan, result, shipped)

        baseline = read_baseline(conn)
        consent = _pending_consent(conn)
        if baseline == shipped:
            print(f"ok: baseline advanced to shipped ({shipped})")
        elif consent and baseline != shipped:
            # Consent-gated steps block baseline advance — acceptable outcome.
            print(
                f"ok: baseline={baseline!r} with pending_consent={consent} "
                f"(shipped={shipped})"
            )
        else:
            raise AssertionError(
                f"baseline {baseline!r} != shipped {shipped!r} and no "
                f"pending_consent remaining"
            )

        # Non-consent pending/running leftover is a failure.
        try:
            stuck = list(
                conn.execute(
                    "SELECT version, step_id, status FROM derivation_ledger "
                    "WHERE status IN ('pending', 'running', 'failed')"
                ).fetchall()
            )
        except sqlite3.Error:
            stuck = []
        if stuck:
            raise AssertionError(f"unresolved ledger rows after upgrade: {stuck}")

        print("upgrade_matrix_ok")
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        required=True,
        type=Path,
        help="Path to fixture SQLite database (will be mutated in place)",
    )
    args = parser.parse_args(argv)
    try:
        run_matrix(args.db.expanduser().resolve())
    except AssertionError as exc:
        print(f"upgrade_matrix_failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"upgrade_matrix_error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
