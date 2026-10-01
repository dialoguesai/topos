"""Mutation run over the browser-interests family (OD-52 P7): the object store, the visit proof, the label
assessment, and index membership and release.

Each mutant weakens one decision: the threshold, the window at month granularity, which visits count (private
window, NSFW, exclusions, provenance, time), the per-month browsing share, each label guard, the assessment floors
and currency, the grant decision, and every re-check at release. Each must be killed by a failing test. Runs in a
scratch copy of the engine, one mutant at a time; the worktree is never modified. A mutant whose text no longer
matches counts as a failure, not a pass.

    export TOPOS_DATABASE_PATH=<scratch>/db.sqlite TOPOS_ENV_FILE=<scratch>/.env
    .venv/bin/python3 scripts/permissions_v2/interest_family_mutants.py --out interest-mutants.json

Not listed because it is equivalent: letting `parse_assessment` accept extra keys (a `speech` answer).
`InterestClassification` is a strict model that forbids extra fields, so the answer is refused with the same
code either way.
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
FAMILY = "topos/permissions_v2/interest_family.py"
REVIEW = "topos/permissions_v2/interest_review.py"
INDEX = "topos/permissions_v2/interest_index.py"
RECEIPTS = "topos/permissions_v2/capture_receipts.py"
MEASURE = "scripts/permissions_v2/interest_family_measure.py"
TESTS = ["tests/permissions_v2/test_interest_family.py", "tests/permissions_v2/test_interest_review.py",
         "tests/permissions_v2/test_interest_index.py", "tests/permissions_v2/test_interest_family_measure.py",
         "tests/permissions_v2/test_capture_receipts.py"]

MUTANTS = [
    # The threshold, the bands and the period.
    ("min_visits_4", FAMILY, "MIN_VISITS = 5\n", "MIN_VISITS = 4\n"),
    ("min_days_2", FAMILY, "MIN_DAYS = 3\n", "MIN_DAYS = 2\n"),
    ("threshold_or", FAMILY, "return self.visits[stage] >= MIN_VISITS and self.days[stage] >= MIN_DAYS",
     "return self.visits[stage] >= MIN_VISITS or self.days[stage] >= MIN_DAYS"),
    ("band_medium_at_14", FAMILY, '("medium", 15)', '("medium", 14)'),
    ("window_start_unchecked", FAMILY,
     "return now_us - max_age_seconds * 1_000_000 <= period_start_us and period_end_us <= now_us + 1",
     "return period_end_us <= now_us + 1"),
    ("window_end_unchecked", FAMILY,
     "return now_us - max_age_seconds * 1_000_000 <= period_start_us and period_end_us <= now_us + 1",
     "return now_us - max_age_seconds * 1_000_000 <= period_start_us"),
    ("current_month_whole", FAMILY, "period_end_us=end if complete else now_us, complete=complete",
     "period_end_us=end, complete=complete"),
    # Which visits count.
    ("future_visits_count", FAMILY, "        if at_us > now_us:\n", "        if at_us > now_us + 10**15:\n"),
    ("flat_incognito_ignored", FAMILY,
     'incognito=row.get("source_record_id") in flat_incognito or _metadata_incognito(row.get("metadata_json")),',
     'incognito=_metadata_incognito(row.get("metadata_json")),'),
    ("metadata_incognito_ignored", FAMILY,
     'incognito=row.get("source_record_id") in flat_incognito or _metadata_incognito(row.get("metadata_json")),',
     'incognito=row.get("source_record_id") in flat_incognito,'),
    ("unreadable_metadata_is_public", FAMILY,
     "        return True  # unreadable metadata cannot prove the visit was not private",
     "        return False"),
    ("nsfw_ignored", FAMILY, "            nsfw=is_record_nsfw(row),", "            nsfw=False,"),
    ("record_exclusion_ignored", FAMILY,
     'excluded=event_id in tombstones["record"] or bool(mentions.get(event_id, set()) & tombstones["entity"]),',
     'excluded=bool(mentions.get(event_id, set()) & tombstones["entity"]),'),
    ("entity_exclusion_ignored", FAMILY,
     'excluded=event_id in tombstones["record"] or bool(mentions.get(event_id, set()) & tombstones["entity"]),',
     'excluded=event_id in tombstones["record"],'),
    ("provenance_ignored", FAMILY, '"provenance": lambda v: v.proven}', '"provenance": lambda v: True}'),
    # The per-month browsing share and each label guard.
    ("browsing_half_is_enough", FAMILY, "browsing[cluster_id][month] * 2 <= members_in_month",
     "browsing[cluster_id][month] * 2 < members_in_month"),
    ("unplaced_members_ignored", FAMILY,
     "            unplaced[cluster_id] += 1  # unknown, or ambiguous across tables: might be in any month",
     "            pass"),
    ("label_form_skipped", FAMILY, "    if not label_form_ok(label):\n        return \"label_form\"",
     "    if False:\n        return \"label_form\""),
    ("urlish_allowed", FAMILY, "    return _URLISH.search(label) is None", "    return True"),
    ("long_label_allowed", FAMILY, "    if len(label) > MAX_LABEL_CHARS or len(label.split()) > MAX_LABEL_WORDS:",
     "    if False:"),
    ("fallback_label_allowed", FAMILY, 'if skeleton(label) in {"", "topiccluster"} or', 'if skeleton(label) in {""} or'),
    ("host_guard_skipped", FAMILY, "    if names_host(label, _host_keys(", "    if False and names_host(label, _host_keys("),
    ("title_guard_skipped", FAMILY, "    if echoes_title(label, [r.get(\"title\") for r in rows_all] + list(previews)):",
     "    if False:"),
    ("title_prefix_rule_off", FAMILY, "        if (len(words) >= 2 and len(other) >= len(words)",
     "        if (False and len(other) >= len(words)"),
    ("person_guard_skipped", FAMILY, "    if names_any(label, person_keys):", "    if False:"),
    ("mentioned_name_parts_off", FAMILY,
     "    person_keys = (_name_keys(person_names)[0], _name_keys(mentioned_names)[1])",
     "    person_keys = (_name_keys(person_names)[0], frozenset())"),
    ("excluded_cluster_allowed", FAMILY,
     '    if cluster_id in tombstones["record"] or names_any(label, excluded_keys):',
     "    if names_any(label, excluded_keys):"),
    ("opt_out_ignored", FAMILY, "    if opt_out_key(cluster_id) in opt_outs:", "    if False:"),
    ("offlimits_label_ignored", FAMILY,
     "    if boundary.mentions_protected(label) or _offlimits_name_part(boundary, label):",
     "    if _offlimits_name_part(boundary, label):"),
    # Floors v2 took the model's veto on protected content away, so a bare part of an Off-limits name in the label
    # is caught by this deterministic check or not at all.
    ("offlimits_name_part_ignored", FAMILY,
     "    if boundary.mentions_protected(label) or _offlimits_name_part(boundary, label):",
     "    if boundary.mentions_protected(label):"),
    ("name_part_rule_of_another_family", FAMILY, 'NAME_PART_FAMILY = "journal_entries"',
     'NAME_PART_FAMILY = "conversation_messages"'),
    ("offlimits_visits_ignored", FAMILY, "            if withheld is None and _visits_protected(",
     "            if False and _visits_protected("),
    ("undecidable_boundary_releases", FAMILY, "    except PolicyError:\n        return True\n",
     "    except PolicyError:\n        return False\n"),
    ("member_revision_ignores_visits", FAMILY,
     'member_rev = digest({"version": VERSION, "visits": sorted([v.event_id, v.revision] for v in counted)})',
     'member_rev = digest({"version": VERSION})'),
    # The visit proof, batched.
    ("batch_any_dataset", RECEIPTS, '        elif row.get("writer_dataset_id") == dataset and (',
     "        elif (\n"),
    ("batch_any_app", RECEIPTS,
     "                or (writer == WRITER_OWNER_APP and _text(row.get(\"writer_app_id\")) in apps)):",
     "                or writer == WRITER_OWNER_APP):"),
    ("batch_revoked_receipts_count", RECEIPTS,
     '"AND t.source_id=? AND t.revoked_at IS NULL", (table, owner_id, table, source_id)):',
     '"AND t.source_id=?", (table, owner_id, table, source_id)):'),
    ("batch_other_source", RECEIPTS,
     '        if row.get("source_id") != source_id or not isinstance(record_id, str) or not record_id:',
     '        if not isinstance(record_id, str) or not record_id:'),
    ("visit_revision_ignores_url", RECEIPTS, 'revision_columns=("source_id", "url", "occurred_at"),',
     'revision_columns=("source_id", "occurred_at"),'),
    # The label assessment.
    ("special_cue_floor_off", REVIEW, '    if words & SPECIAL and labels.sensitivity != "unknown":', "    if False:"),
    ("message_floors_off", REVIEW, "    labels = message_floors(labels, inputs)\n", ""),
    ("special_qualifies", REVIEW, 'assessment.classification.sensitivity in ("none", "personal")',
     'assessment.classification.sensitivity in ("none", "personal", "special")'),
    ("protected_unknown_qualifies", REVIEW, 'and assessment.classification.protected_content == "none")',
     'and assessment.classification.protected_content != "present")'),
    # Floors v2: only the model's own protected-content `unknown` is read as `none`.
    ("model_unknown_still_withholds", REVIEW,
     '    if labels.protected_content == "unknown":\n        labels = labels.model_copy(update={"protected_content": "none"})\n',
     ""),
    ("model_present_read_as_none", REVIEW, '    if labels.protected_content == "unknown":\n',
     '    if labels.protected_content != "none":\n'),
    ("unknown_sensitivity_lowered_with_it", REVIEW,
     '        labels = labels.model_copy(update={"protected_content": "none"})\n',
     '        labels = labels.model_copy(update={"protected_content": "none", "sensitivity": "none"})\n'),
    ("a_floors_unknown_lowered_too", REVIEW,
     '    if labels.protected_content == "unknown":\n        labels = labels.model_copy(update={"protected_content": "none"})\n'
     '    labels = message_floors(labels, inputs)\n',
     '    labels = message_floors(labels, inputs)\n'
     '    if labels.protected_content == "unknown":\n        labels = labels.model_copy(update={"protected_content": "none"})\n'),
    ("stale_label_current", REVIEW,
     "if (assessment.cluster_id != obj.cluster_id or assessment.classification.label_revision != obj.label_revision",
     "if (assessment.cluster_id != obj.cluster_id"),
    ("stale_rubric_current", REVIEW, "or assessment.model_revision != _model_revision() or assessment.rubric_revision != rubric_revision()",
     "or assessment.model_revision != _model_revision()"),
    ("stale_vocabulary_current", REVIEW, "            or assessment.context_revision != context_revision):",
     "            ):"),
    ("other_owner_counts", REVIEW, 'f"SELECT assessment_json FROM {TABLE} WHERE label_revision=? AND owner_id=?",\n'
     '                       (obj.label_revision, owner_id)).fetchone()',
     'f"SELECT assessment_json FROM {TABLE} WHERE label_revision=? AND ? IS NOT NULL",\n'
     '                       (obj.label_revision, owner_id)).fetchone()'),
    ("publish_skips_floors", REVIEW, "    classification = apply_floors(classification, prepared[\"input\"])\n", ""),
    ("publish_moved_vocabulary", REVIEW, '    if revision != prepared["context_revision"]:\n        raise PolicyError("machine_review_conflict")\n', ""),
    # Membership and release.
    ("flag_ignored", INDEX, "    return str(env.get(FLAG, \"\")).strip().lower() in _TRUE", "    return True"),
    ("kind_not_required", INDEX, "    if KIND not in (getattr(search, \"result_types\", None) or ()) or TABLE not in (search.tables or ()):",
     "    if TABLE not in (search.tables or ()):"),
    ("table_not_required", INDEX, "    if KIND not in (getattr(search, \"result_types\", None) or ()) or TABLE not in (search.tables or ()):",
     "    if KIND not in (getattr(search, \"result_types\", None) or ()):"),
    ("capability_not_required", INDEX,
     "    if search is None or versions is None or getattr(versions, \"capability\", None) != CAPABILITY:",
     "    if search is None or versions is None:"),
    ("authored_role", INDEX, 'return {"domain": list(classification.domains), "actor_role": ["ambient"],',
     'return {"domain": list(classification.domains), "actor_role": ["authored", "ambient"],'),
    ("any_domain_permits", INDEX, "            if values and all(value is True for value in values):",
     "            if values and any(value is True for value in values):"),
    ("deny_ignored", INDEX, "            if True in values:\n                denies.append(rule.rule_id)",
     "            if False:\n                denies.append(rule.rule_id)"),
    ("unknown_deny_permits", INDEX, 'verdict = ("deny" if denies else "indeterminate" if unknown_deny else "permit" if allows',
     'verdict = ("deny" if denies else "permit" if allows'),
    ("window_not_rechecked", INDEX,
     "    if not fam.period_inside(period_start_us=obj.period_start_us, period_end_us=obj.period_end_us,",
     "    if False and fam.period_inside(period_start_us=obj.period_start_us, period_end_us=obj.period_end_us,"),
    ("assessment_not_required", INDEX, "    if not ir.qualifies(assessment):\n        return None\n",
     "    if assessment is None:\n        return None\n"),
    ("content_revision_not_rechecked", INDEX,
     "    if len(found) != 1 or found[0].content_revision != binding.get(\"content_revision\"):",
     "    if len(found) != 1:"),
    ("assessment_revision_not_rechecked", INDEX,
     "    if (digest(assessment.model_dump()) != binding.get(\"assessment_revision\")\n"
     "            or rule_id != binding.get(\"allow_clause_id\")):",
     "    if (rule_id != binding.get(\"allow_clause_id\")):"),
    ("sealed_table_trusted", INDEX, "            or sealed.get(\"dataset_id\") is not None or not isinstance(binding.get(\"cluster_id\"), str)",
     "            or not isinstance(binding.get(\"cluster_id\"), str)"),
    ("record_id_not_bound", INDEX,
     "            or sealed.get(\"record_id\") != fam.interest_id(binding[\"cluster_id\"], binding.get(\"month\"))):",
     "            ):"),
    ("second_precision_releases_time", INDEX, 'event_at = obj.period_start_us // 1_000_000 if precision == "day" else None',
     'event_at = obj.period_start_us // 1_000_000 if precision != "none" else None'),
    ("open_month_under_any_precision", INDEX, "    if not obj.complete and not open_month_allowed(policy):\n        return None\n",
     ""),
    ("release_opt_outs_dropped", INDEX,
     "    obj = _current_object(conn, sealed, owner_id=owner_id, now=now, boundary=boundary, opt_outs=opt_outs)",
     "    obj = _current_object(conn, sealed, owner_id=owner_id, now=now, boundary=boundary, opt_outs=frozenset())"),
    # The measurement's funnel.
    ("measure_visit_guards_not_cumulative", MEASURE, "                if not c.qualifies(check):\n                    break",
     "                if not c.qualifies(\"all\"):\n                    break"),
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
    with tempfile.TemporaryDirectory(prefix="interest-mutants-") as scratch:
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
            original = target.read_text() if target.exists() else ""
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
