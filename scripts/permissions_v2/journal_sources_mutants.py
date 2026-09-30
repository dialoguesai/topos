"""Mutation run over the journal-source provenance work (OD-50/OD-52): what a door records on a journal
row, and the rule that decides whether a journal row is the owner's own.

Each mutant weakens one decision: whether the door's app and dataset reach the row, whether a payload
can name them, which writer classes count, whether the install binds the row to the owner, whether a
receipt is the owner's, live and for these exact words, and whether the route is the owner's socket.
Each must be killed by a failing test. Runs in a scratch copy of the engine, one mutant at a time; the
worktree is never modified. A mutant whose text no longer matches counts as a failure, not a pass.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/journal_sources_mutants.py --out journal-mutants.json

Not listed because they are equivalent: recording the dataset without a class in the pipeline (the
store writes app and dataset only beside a class), and merging the payload under the door identity
while the payload's own keys are dropped first (either guard alone suffices; dropping the keys is what
keeps them out of the record handed to derivation, and `payload_keys_reach_the_row` covers both).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STORE = "topos/storage/canonical/canonical_store.py"
PIPELINE = "topos/ingestion/canonical_pipeline.py"
MIGRATION = "topos/storage/db/migrations/entity_mentions_authored_v1.py"
RECEIPTS = "topos/permissions_v2/capture_receipts.py"
ROUTE = "topos/api/permissions_capture_receipts.py"
TIME = "topos/permissions_v2/evidence_time.py"
TESTS = ["tests/ingestion/test_canonical_writer_identity.py", "tests/permissions_v2/test_capture_receipts.py",
         "tests/permissions_v2/test_evidence_time.py", "tests/ingestion/test_journal_declared_time_zone.py",
         "tests/permissions_v2/test_journal_family.py", "tests/permissions_v2/test_knowledge_contract_families.py"]
EVIDENCE = "topos/permissions_v2/evidence.py"
MESSAGE = "topos/permissions_v2/message_evidence.py"
BOUNDARY = "topos/permissions_v2/entity_boundary.py"
REVIEW = "topos/permissions_v2/automatic_message_review.py"
RELEASE = "topos/permissions_v2/search_release.py"
INDEX = "topos/permissions_v2/search_index.py"
GRAMMAR = "topos/permissions_v2/knowledge_contract.py"
RECORDS = "topos/features/temporal/records.py"
DEFINITIONS = "topos/sources/definitions.py"

MUTANTS = [
    # What a door records.
    ("store_skips_the_app_and_dataset", STORE,
     'if identity and table != "ai_chat_messages":', 'if False and identity and table != "ai_chat_messages":'),
    ("store_keeps_a_stale_app_on_a_door_write", STORE,
     '(*[(str(record.get(c) or "").strip() or None) for c in identity], ref.record_id),',
     '(*[(str(record.get(c) or (stored or {}).get(c) or "").strip() or None) for c in identity], ref.record_id),'),
    ("payload_keys_reach_the_row", PIPELINE,
     '                    for key in (*door_identity, "event_time_json", "declared_time_zone"):\n'
     "                        canonical_payload.pop(key, None)\n", ""),
    ("location_child_without_the_door", PIPELINE,
     'loc_ref = store.upsert("location_events", {**loc_row, **door_identity},',
     'loc_ref = store.upsert("location_events", {**loc_row},'),
    ("migration_adds_only_the_class", MIGRATION,
     '_WRITER_COLUMNS = ("writer_class", "writer_app_id", "writer_dataset_id")', '_WRITER_COLUMNS = ("writer_class",)'),
    ("who_sent_it_counts_as_a_rewrite", STORE,
     '"writer_class", "writer_app_id", "writer_dataset_id", "ingested_at",', '"writer_class", "ingested_at",'),
    # Whose row it is.
    ("any_source_may_be_named", RECEIPTS, " or source_id != identity_source_id\n", "\n"),
    ("no_install_still_proves", RECEIPTS,
     "    if dataset is None:\n        return False\n    writer = normalize_writer_class", "    writer = normalize_writer_class"),
    ("pre_stamp_rows_are_the_owners", RECEIPTS,
     "        return content_revision(table, row) in attested_revisions(\n"
     "            conn, owner_id=owner_id, table=table, source_id=source_id, record_id=row[family.id_column])",
     "        return True"),
    ("any_dataset_will_do", RECEIPTS, '    if row.get("writer_dataset_id") != dataset:\n        return False\n', ""),
    ("any_owner_app_counts", RECEIPTS,
     "        return app is not None and app in capture_apps(conn, owner_id=owner_id, table=table, source_id=source_id)",
     "        return True"),
    ("every_class_counts", RECEIPTS, "        return app is not None and app in capture_apps(conn, owner_id=owner_id, "
     "table=table, source_id=source_id)\n    return False", "        return app is not None and app in capture_apps(conn, "
     "owner_id=owner_id, table=table, source_id=source_id)\n    return True"),
    ("a_revoked_receipt_still_names_its_app", RECEIPTS,
     'f"SELECT app_id FROM {RECEIPTS} WHERE owner_id=? AND canonical_table=? AND source_id=? AND revoked_at IS NULL",',
     'f"SELECT app_id FROM {RECEIPTS} WHERE owner_id=? AND canonical_table=? AND source_id=?",'),
    ("another_owners_app_counts", RECEIPTS,
     'f"SELECT app_id FROM {RECEIPTS} WHERE owner_id=? AND canonical_table=? AND source_id=? AND revoked_at IS NULL",',
     'f"SELECT app_id FROM {RECEIPTS} WHERE ? IS NOT NULL AND canonical_table=? AND source_id=? AND revoked_at IS NULL",'),
    ("a_revoked_receipt_still_lists_its_rows", RECEIPTS,
     '"WHERE r.canonical_table=? AND r.record_id=? AND t.owner_id=? AND t.canonical_table=? AND t.source_id=? "\n'
     '        "AND t.revoked_at IS NULL", (table, record_id, owner_id, table, source_id)))',
     '"WHERE r.canonical_table=? AND r.record_id=? AND t.owner_id=? AND t.canonical_table=? AND t.source_id=? "\n'
     '        , (table, record_id, owner_id, table, source_id)))'),
    ("another_owners_receipt_lists_the_row", RECEIPTS,
     '"WHERE r.canonical_table=? AND r.record_id=? AND t.owner_id=? AND t.canonical_table=? AND t.source_id=? "\n'
     '        "AND t.revoked_at IS NULL", (table, record_id, owner_id, table, source_id)))',
     '"WHERE r.canonical_table=? AND r.record_id=? AND ? IS NOT NULL AND t.canonical_table=? AND t.source_id=? "\n'
     '        "AND t.revoked_at IS NULL", (table, record_id, owner_id, table, source_id)))'),
    ("the_words_are_not_attested", RECEIPTS,
     '        table="journal_entries", id_column="entry_id", revision_columns=("source_id", "content"),',
     '        table="journal_entries", id_column="entry_id", revision_columns=("source_id",),'),
    # The owner's attestation.
    ("attest_without_confirmation", RECEIPTS,
     '    if confirm is not True:\n        raise PolicyError("capture_attestation_unconfirmed")\n', ""),
    ("attest_whatever_the_preview_said", RECEIPTS,
     '    if preview_digest != summary["preview_digest"]:\n        raise PolicyError("capture_attestation_preview_stale")\n', ""),
    ("attest_without_an_owner_binding", RECEIPTS,
     '    if dataset is None:\n        raise PolicyError("capture_attestation_invalid")\n    install(conn)', "    install(conn)"),
    ("stamped_rows_are_swept_into_a_receipt", RECEIPTS, "WHERE source_id=? AND writer_class IS NULL \"", "WHERE source_id=? \""),
    ("anyone_may_revoke", RECEIPTS,
     'f"SELECT revoked_at FROM {RECEIPTS} WHERE receipt_id=? AND owner_id=?",\n                         (receipt_id, owner_id)).fetchone()',
     'f"SELECT revoked_at FROM {RECEIPTS} WHERE receipt_id=? AND ? IS NOT NULL",\n                         (receipt_id, owner_id)).fetchone()'),
    ("any_table_is_a_family", RECEIPTS,
     '    found = FAMILIES.get(table) if isinstance(table, str) else None\n    if found is None:\n'
     '        raise PolicyError("capture_attestation_invalid")\n    return found',
     '    return FAMILIES["journal_entries"]'),
    ("a_receipt_can_be_edited", RECEIPTS,
     "        attested_at, dataset_id ON {RECEIPTS} BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END",
     "        attested_at, dataset_id ON {RECEIPTS} BEGIN SELECT 1; END"),
    ("a_receipt_can_be_unrevoked", RECEIPTS,
     "        WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL\n"
     "        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END",
     "        WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL\n        BEGIN SELECT 1; END"),
    ("a_revoked_receipt_takes_rows", RECEIPTS,
     "              AND canonical_table=NEW.canonical_table) != 1\n"
     "        BEGIN SELECT RAISE(ABORT, 'capture_receipt_immutable'); END",
     "              AND canonical_table=NEW.canonical_table) != 1\n        BEGIN SELECT 1; END"),
    ("a_door_written_row_names_no_dataset", RECEIPTS,
     '        return _text(row.get("writer_dataset_id"))\n    if not installed(conn)', '        return None\n    if not installed(conn)'),
    # When a zone-less row happened.
    ("naive_text_keeps_its_written_time", TIME,
     '    if point.precision == "instant" and point.basis == "unrecorded":\n'
     '        point = parse_point(point.text[:10], provenance=_PROVENANCE)   # the day it states, not the time it wrote\n', ""),
    ("naive_text_passes_the_message_rule", TIME,
     "        return parse_point(value, provenance=_PROVENANCE) if canonical_utc_microseconds(value) is not None else None",
     "        return parse_point(value, provenance=_PROVENANCE)"),
    ("a_month_or_a_year_places_a_row", TIME,
     '    return point if point.precision in ("instant", "day") else None', "    return point if point.known else None"),
    ("a_window_needs_only_the_spans_start", TIME,
     "    return bounds is not None and lower_us <= bounds[0] and bounds[1] <= upper_us",
     "    return bounds is not None and lower_us <= bounds[0] and bounds[0] <= upper_us"),
    ("a_window_needs_only_the_spans_end", TIME,
     "    return bounds is not None and lower_us <= bounds[0] and bounds[1] <= upper_us",
     "    return bounds is not None and lower_us <= bounds[1] and bounds[1] <= upper_us"),
    ("a_stated_day_releases_an_instant", TIME, '    if precision != "day":\n        return None\n    stated_day', "    stated_day"),
    ("a_stated_day_releases_its_earliest_instant", TIME,
     "    return (span(stated_day).lo + 14 * 3_600 * 1_000_000) // 1_000_000", "    return span(stated_day).lo // 1_000_000"),
    ("precision_none_releases_a_time", TIME, '    if point is None or precision == "none":\n        return None\n',
     "    if point is None:\n        return None\n"),
    # When a row of a source that declares its zone happened.
    ("a_payload_names_its_own_zone", PIPELINE,
     '                    for key in (*door_identity, "event_time_json", "declared_time_zone"):',
     "                    for key in door_identity:"),
    ("a_replay_dates_rows_under_a_later_declaration", PIPELINE,
     '                                     if target_table == "journal_entries" and writer_class is not None else {})',
     '                                     if target_table == "journal_entries" else {})'),
    ("a_record_outlives_its_time", STORE,
     '            self._conn.execute(\n'
     '                "UPDATE journal_entries SET event_time_json=NULL WHERE entry_id=? AND entry_at IS NOT ?",\n'
     '                (entry_id, entry_at),\n            )\n', ""),
    ("a_resend_rewrites_the_record", STORE,
     '"UPDATE journal_entries SET event_time_json=? WHERE entry_id=? AND event_time_json IS NULL",',
     '"UPDATE journal_entries SET event_time_json=? WHERE entry_id=?",'),
    ("a_repeated_or_skipped_hour_is_guessed", RECORDS, "    if len(offsets) != 1 or None in offsets:", "    if None in offsets:"),
    ("a_day_gets_a_zone", RECORDS,
     '    if point.precision != "instant" or point.basis != "unrecorded" or type(zone_name) is not str:\n        return None\n',
     "    if type(zone_name) is not str:\n        return None\n"),
    ("a_moved_time_keeps_its_record_on_read", TIME, "            or not point.text.startswith(written)):", "            ):"),
    ("a_damaged_record_reads_as_absent", TIME,
     "    except (ValueError, TypeError):\n        return None\n    written", "    except (ValueError, TypeError):\n        return row.get(column)\n    written"),
    ("a_day_record_reads_as_an_instant", TIME,
     '    if (point.basis == "utc" and suffix == "Z") or (point.basis == "fixed_offset" and len(suffix) == 6):\n        return point.text\n    return None',
     "    return point.text"),
    ("any_text_is_a_zone_name", DEFINITIONS,
     "                type(self.time_zone) is not str or _TIME_ZONE_NAME.fullmatch(self.time_zone) is None):",
     "                type(self.time_zone) is not str):"),
    # The journal as evidence (IF-5 P3).
    ("the_flag_does_not_gate_loading", EVIDENCE,
     "            enabled_family(table)   # a family behind its flag does not exist while the flag is off\n", ""),
    ("an_unproven_row_loads", EVIDENCE,
     '        if table == JOURNAL_TABLE and not self._journal_owner_proven(conn, identity, row):',
     '        if False and table == JOURNAL_TABLE and not self._journal_owner_proven(conn, identity, row):'),
    ("same_source_twins_are_all_members", EVIDENCE,
     '        if own is None or min(same) != own:\n            raise PolicyError("journal_copy_alias")\n', ""),
    ("a_copy_in_another_source_is_ignored", EVIDENCE,
     "        if any(r[1] != identity.source_id for r in rows):\n            return True\n", ""),
    ("an_ambient_posture_does_not_cap_a_journal_row", MESSAGE,
     '    if record_role(row, table=identity.table, posture=posture) != "authored":\n        raise PolicyError("not_owner_authored")\n'
     '    content = row.get("content")\n    if not isinstance(content, str) or not content.strip() or len(content) > 100_000 or is_record_nsfw(row):\n'
     '        raise PolicyError("unsupported_message_content")\n    if resolver._known_copies(conn, identity, row):\n'
     '        raise PolicyError("independent_copy_lineage")\n\n\ndef _source_checks',
     '    content = row.get("content")\n    if not isinstance(content, str) or not content.strip() or len(content) > 100_000 or is_record_nsfw(row):\n'
     '        raise PolicyError("unsupported_message_content")\n    if resolver._known_copies(conn, identity, row):\n'
     '        raise PolicyError("independent_copy_lineage")\n\n\ndef _source_checks'),
    ("nsfw_journal_rows_pass", MESSAGE,
     "    if not isinstance(content, str) or not content.strip() or len(content) > 100_000 or is_record_nsfw(row):\n"
     "        raise PolicyError(\"unsupported_message_content\")\n    if resolver._known_copies(conn, identity, row):\n"
     "        raise PolicyError(\"independent_copy_lineage\")\n\n\ndef _source_checks",
     "    if not isinstance(content, str) or not content.strip() or len(content) > 100_000:\n"
     "        raise PolicyError(\"unsupported_message_content\")\n    if resolver._known_copies(conn, identity, row):\n"
     "        raise PolicyError(\"independent_copy_lineage\")\n\n\ndef _source_checks"),
    ("a_journal_row_needs_a_conversation", BOUNDARY,
     "            elif table in CONTEXTLESS_TABLES:", "            elif False:"),
    ("a_journal_entry_may_be_labelled_none", REVIEW, '    if sensitivity == "none":\n        sensitivity = "personal"\n', ""),
    ("special_cues_are_ignored", REVIEW,
     '    if sensitivity != "unknown" and (SPECIAL & set(words) or SPECIAL & {stem(word) for word in words}):',
     '    if False:'),
    ("journals_share_the_message_rubric_revision", REVIEW,
     '    if table == "journal_entries":\n        return digest({"base": rubric_revision(), "family": "journal_entry/v1", "floors": JOURNAL_FLOORS_VERSION})\n', ""),
    ("the_worker_never_pages_journals", "topos/permissions_v2/automatic_review_worker.py",
     '        if table == "journal_entries":\n            return self._journal_page(after_id, request, ingested_after)\n', ""),
    # Release.
    ("a_journal_entry_releases_without_its_option", RELEASE,
     '        if (not automatic or "journal_entry" not in policy.search.result_types\n', "        if (not automatic\n"),
    ("a_journal_entry_releases_before_its_day_has_ended", RELEASE,
     "                or not within(identity.table, row, lower_us, upper_us) or is_record_nsfw(row)",
     "                or is_record_nsfw(row)"),
    ("a_journal_entry_releases_an_instant", RELEASE,
     "                      event_at=released(identity.table, row, precision))",
     "                      event_at=released(identity.table, row, \"day\"))"),
    ("the_index_keeps_journal_members_without_the_option", INDEX,
     "                         or ('journal_entry' if v['identity'].table == 'journal_entries' else 'message') in kinds}",
     "                         or 'message' in kinds}"),
    ("the_index_admits_an_entry_before_its_day_ends", INDEX,
     "                                or not within(identity.table, row, lower_us, now * 1_000_000)):",
     "                                ):"),
    # Grammar.
    ("a_kind_needs_no_table", GRAMMAR, '                raise ValueError("result type without its table")', "                pass"),
    ("an_interest_may_name_any_month", GRAMMAR,
     'pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"', 'pattern=r"^.+$"'),
    # The owner's read-through.
    ("the_queue_lists_unproven_rows", MESSAGE,
     "                identity = resolver._identity(\"journal_entries\", row[0], row[1])\n"
     "                try:\n                    snapshot, loaded = snapshot_message(resolver, conn, floor, identity)\n"
     "                    _floors(resolver, conn, snapshot, loaded, opted_out - {message_key(identity)})\n                except PolicyError:\n"
     "                    continue\n                labels = _preview_labels",
     "                identity = resolver._identity(\"journal_entries\", row[0], row[1])\n"
     "                try:\n                    snapshot, loaded = snapshot_message(resolver, conn, floor, identity)\n"
     "                except PolicyError:\n                    continue\n                labels = _preview_labels"),
    ("the_queue_is_not_ranked", MESSAGE, "    found.sort(key=lambda item: item[:2])\n", ""),
    ("anyone_reads_the_queue", MESSAGE,
     '    from .message_review_contract import MessageReviewPage\n    _owner(resolver.binding)\n    enabled_family("journal_entries")',
     '    from .message_review_contract import MessageReviewPage\n    enabled_family("journal_entries")'),
    ("the_route_is_open_to_any_owner_key", ROUTE,
     "    _require_owner_socket(principal)\n    return await _respond(lambda owner_id, conn: capture_receipts.attest(",
     "    return await _respond(lambda owner_id, conn: capture_receipts.attest("),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def pytest(base, tests):
        run = subprocess.run([sys.executable, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider"],
                             cwd=base, env=env, capture_output=True, text=True, timeout=1800)
        tail = [line for line in run.stdout.splitlines() if " passed" in line or " failed" in line][-1:]
        failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
        return run.returncode, tail, failing

    results = []
    with tempfile.TemporaryDirectory(prefix="journal-mutants-") as scratch:
        base = Path(scratch) / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "scripts", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        tests = [test for test in TESTS if (base / test).exists()]
        code, tail, failing = pytest(base, tests)
        baseline = {"status": "pass" if code == 0 else "FAIL", "summary": tail, "failing": failing, "tests": tests}
        print({"baseline": baseline}, flush=True)
        if code != 0:
            args.out.write_text(json.dumps({"baseline": baseline}, indent=2) + "\n")
            return 2
        for name, path, old, new in MUTANTS:
            if args.only and name not in args.only:
                continue
            target = base / path
            if not target.exists():
                results.append({"mutant": name, "status": "patch_not_applicable", "count": 0})
                continue
            original = target.read_text()
            if original.count(old) != 1:
                results.append({"mutant": name, "status": "patch_not_applicable", "count": original.count(old)})
                print(results[-1], flush=True)
                continue
            target.write_text(original.replace(old, new))
            try:
                code, tail, failing = pytest(base, tests)
                # Killed only by a failing test: an error before any test ran proves nothing.
                status = "killed" if code != 0 and failing else "SURVIVED" if code == 0 else "ERRORED"
                results.append({"mutant": name, "status": status, "summary": tail, "killed_by": failing})
            finally:
                target.write_text(original)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    report = {"baseline": baseline, "mutants": len(results), "killed": killed, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed}))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
