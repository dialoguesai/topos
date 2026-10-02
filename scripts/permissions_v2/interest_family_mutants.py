"""Mutation run over the browser-interests family (OD-52 P7): the object store, the visit proof, the label
assessment, the second try at a bad label, and index membership and release.

Each mutant weakens one decision: the threshold, the window at month granularity, which visits count (private
window, NSFW, exclusions, provenance, time), the per-month browsing share, each label guard, the assessment floors
and currency, who is owed a second label and what a second label is held to (its tries, its checks, what is stored
and shown, its currency, its pruning, and the refresh loop's budget and wait), the grant decision, and every
re-check at release. Each must be killed by a failing test. Runs in a scratch copy of the engine, one mutant at a
time; the worktree is never modified. A mutant whose text no longer matches counts as a failure, not a pass.

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
RELABEL = "topos/permissions_v2/interest_relabel.py"
LOOP = "topos/permissions_v2/refresh_loop.py"
INDEX = "topos/permissions_v2/interest_index.py"
RECEIPTS = "topos/permissions_v2/capture_receipts.py"
MEASURE = "scripts/permissions_v2/interest_family_measure.py"
TESTS = ["tests/permissions_v2/test_interest_family.py", "tests/permissions_v2/test_interest_review.py",
         "tests/permissions_v2/test_interest_relabel.py", "tests/permissions_v2/test_interest_index.py",
         "tests/permissions_v2/test_interest_family_measure.py", "tests/permissions_v2/test_capture_receipts.py",
         "tests/permissions_v2/test_refresh_loop.py"]

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
    ("label_form_skipped", FAMILY, "    if not label_form_ok(label):\n        yield \"label_form\"",
     "    if False:\n        yield \"label_form\""),
    ("urlish_allowed", FAMILY, "    return _URLISH.search(label) is None", "    return True"),
    ("long_label_allowed", FAMILY, "    if len(label) > MAX_LABEL_CHARS or len(label.split()) > MAX_LABEL_WORDS:",
     "    if False:"),
    ("fallback_label_allowed", FAMILY, 'if skeleton(label) in {"", "topiccluster"} or', 'if skeleton(label) in {""} or'),
    ("host_guard_skipped", FAMILY, "    if names_host(label, _cluster_host_keys(rows_all)):", "    if False:"),
    ("title_guard_skipped", FAMILY, "    if echoes_title(label, [r.get(\"title\") for r in rows_all] + list(previews)):",
     "    if False:"),
    ("title_prefix_rule_off", FAMILY, "        if (len(words) >= 2 and len(other) >= len(words)",
     "        if (False and len(other) >= len(words)"),
    ("person_guard_skipped", FAMILY, "    if names_person(label, person_keys):", "    if False:"),
    ("one_cluster_built_reads_only_its_own_mentions", FAMILY, "            if record_id in clustered:",
     "            if record_id in visits_by_id:"),
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
    # Who is owed a second label (interest_relabel), and when a stored one stands in.
    ("person_alone_earns_a_second_label", FAMILY, 'RETRY_CHECKS = ("label_form", "label_host", "label_title")',
     'RETRY_CHECKS = ("label_form", "label_host", "label_title", "label_person")'),
    ("offlimits_label_gets_a_second_label", FAMILY, 'NEVER_RETRIED = ("excluded_label", "opted_out", "offlimits")',
     'NEVER_RETRIED = ("excluded_label", "opted_out")'),
    ("opted_out_cluster_gets_a_second_label", FAMILY, 'NEVER_RETRIED = ("excluded_label", "opted_out", "offlimits")',
     'NEVER_RETRIED = ("excluded_label", "offlimits")'),
    ("excluded_label_gets_a_second_label", FAMILY, 'NEVER_RETRIED = ("excluded_label", "opted_out", "offlimits")',
     'NEVER_RETRIED = ("opted_out", "offlimits")'),
    ("explicit_exclusion_not_read_before_a_retry", FAMILY,
     "            if cluster_check in RETRY_CHECKS and not set(own_rules) & set(NEVER_RETRIED) and _topic_text(label):",
     "            if cluster_check in RETRY_CHECKS and _topic_text(label):"),
    ("stored_second_label_not_checked_again", FAMILY, "                if again is not None and not again_rules:",
     "                if again is not None:"),
    ("second_label_for_a_good_own_label", FAMILY,
     "            if cluster_check in RETRY_CHECKS and not set(own_rules) & set(NEVER_RETRIED) and _topic_text(label):",
     "            if not set(own_rules) & set(NEVER_RETRIED) and _topic_text(label):"),
    ("fallback_label_asked_about", FAMILY,
     '            and any(ch.isalpha() for ch in label) and skeleton(label) != "topiccluster")',
     "            and any(ch.isalpha() for ch in label))"),
    ("digits_only_label_asked_about", FAMILY,
     '            and any(ch.isalpha() for ch in label) and skeleton(label) != "topiccluster")',
     '            and skeleton(label) != "topiccluster")'),
    ("long_own_label_asked_about", FAMILY,
     "    return (isinstance(label, str) and 0 < len(label) <= MAX_RETRY_SOURCE_CHARS",
     "    return (isinstance(label, str) and 0 < len(label)"),
    ("any_month_earns_a_try", FAMILY,
     "            if retry is not None and withheld == cluster_check and candidate.qualifies():",
     "            if retry is not None:"),
    ("browsing_month_not_required_for_a_try", FAMILY,
     "            if retry is not None and withheld == cluster_check and candidate.qualifies():",
     "            if retry is not None and candidate.qualifies():"),
    ("offlimits_months_earn_a_try", FAMILY,
     "            retry.clear = lambda serves=serves: any(not _visits_protected(boundary, rows) for rows in serves)",
     "            retry.clear = lambda serves=serves: True"),
    ("excluded_second_label_kept_as_text", FAMILY,
     "            excluded = [rule for rule in (*own_rules, *again_rules) if rule in NEVER_RETRIED]",
     "            excluded = [rule for rule in own_rules if rule in NEVER_RETRIED]"),
    ("second_label_of_an_excluded_cluster_kept_as_text", FAMILY,
     "            excluded = [rule for rule in (*own_rules, *again_rules) if rule in NEVER_RETRIED]",
     "            excluded = [rule for rule in again_rules if rule in NEVER_RETRIED]"),
    ("second_label_naming_a_person_erased", FAMILY,
     "            excluded = [rule for rule in (*own_rules, *again_rules) if rule in NEVER_RETRIED]",
     "            excluded = [rule for rule in (*own_rules, *again_rules) if rule in (*NEVER_RETRIED, \"label_person\")]"),
    ("partial_build_reads_as_whole", FAMILY, "    out.whole = only is None\n", ""),
    # The second label itself: its tries, its checks, what is stored and shown, its currency, its pruning.
    ("a_third_try", RELABEL, "RETRIES = 2\n", "RETRIES = 3\n"),
    ("second_label_never_checked", RELABEL,
     '    broken = retry.check(answer) if isinstance(answer, str) else ("label_form",)', "    broken = ()"),
    ("non_label_answer_accepted", RELABEL,
     '    broken = retry.check(answer) if isinstance(answer, str) else ("label_form",)',
     "    broken = retry.check(answer) if isinstance(answer, str) else ()"),
    ("refused_answer_stored", RELABEL,
     "                      label=None if broken else answer, refused=broken[0] if broken else None)",
     "                      label=answer if isinstance(answer, str) and answer else None,\n"
     "                      refused=broken[0] if broken else None)"),
    ("excluded_answer_shown_again", RELABEL,
     "    return label if isinstance(label, str) and rules and not set(rules) & set(fam.NEVER_RETRIED) else None",
     "    return label if isinstance(label, str) and rules else None"),
    ("only_the_first_broken_rule_decides_what_is_shown", RELABEL,
     "    return label if isinstance(label, str) and rules and not set(rules) & set(fam.NEVER_RETRIED) else None",
     "    return label if isinstance(label, str) and rules and rules[0] not in fam.NEVER_RETRIED else None"),
    ("refused_answer_never_shown", RELABEL,
     "    return label if isinstance(label, str) and rules and not set(rules) & set(fam.NEVER_RETRIED) else None",
     "    return None"),
    ("publish_when_the_tries_moved", RELABEL,
     '            or (result.tries if result is not None else 0) != prepared["tries"] or prepared["tries"] >= RETRIES):',
     '            or prepared["tries"] >= RETRIES):'),
    ("publish_for_another_label", RELABEL,
     '    if (retry is None or retry.base_revision != prepared["base_revision"]', "    if (retry is None"),
    ("publish_under_another_revision", RELABEL,
     '    if prepared.get("rule_revision") != revision():\n        raise PolicyError("machine_review_conflict")\n', ""),
    ("a_call_after_the_last_try", RELABEL, "    if relabel.label is not None or relabel.tries >= RETRIES:",
     "    if relabel.label is not None:"),
    ("a_call_after_an_accepted_label", RELABEL, "    if relabel.label is not None or relabel.tries >= RETRIES:",
     "    if relabel.tries >= RETRIES:"),
    ("answer_whitespace_kept", RELABEL, '    return _SPACE.sub(" ", value["label"]).strip()', '    return value["label"]'),
    ("spent_cluster_asked_again", RELABEL, "        if tries >= RETRIES or not retry.clear():",
     "        if not retry.clear():"),
    ("offlimits_only_cluster_asked", RELABEL, "        if tries >= RETRIES or not retry.clear():",
     "        if tries >= RETRIES:"),
    ("unfinished_answer_judged", RELABEL,
     '        if not isinstance(body, dict) or body.get("model") != MODEL or body.get("done") is not True:\n',
     "        if not isinstance(body, dict):\n"),
    ("relabel_thinks", RELABEL, '"model": MODEL, "stream": False, "think": False, "format": "json",\n'
     '            "options": {"temperature": 0, "num_predict": 96},',
     '"model": MODEL, "stream": False, "think": True, "format": "json",\n'
     '            "options": {"temperature": 0, "num_predict": 96},'),
    ("relabel_samples", RELABEL, '            "options": {"temperature": 0, "num_predict": 96},',
     '            "options": {"temperature": 0.7, "num_predict": 96},'),
    ("stale_relabel_revision_current", RELABEL,
     "            or relabel.rule_revision != revision() or relabel.model_revision != _model_revision()",
     "            or relabel.model_revision != _model_revision()"),
    ("stale_relabel_model_current", RELABEL,
     "            or relabel.rule_revision != revision() or relabel.model_revision != _model_revision()",
     "            or relabel.rule_revision != revision()"),
    ("relabel_body_of_another_owner", RELABEL,
     "    if (relabel.owner_id != owner_id or relabel.base_revision != base_revision",
     "    if (relabel.base_revision != base_revision"),
    ("relabel_filed_under_another_label", RELABEL,
     "    if (relabel.owner_id != owner_id or relabel.base_revision != base_revision",
     "    if (relabel.owner_id != owner_id"),
    ("relabel_row_of_another_owner", RELABEL,
     'for base_revision, raw in conn.execute(f"SELECT base_revision, relabel_json FROM {TABLE} WHERE owner_id=?",\n'
     "                                           (owner_id,)).fetchall():\n"
     "        relabel = _current(raw, owner_id=owner_id, base_revision=base_revision)\n"
     "        if relabel is not None and relabel.label is not None:",
     'for base_revision, raw in conn.execute(f"SELECT base_revision, relabel_json FROM {TABLE} WHERE ? IS NOT NULL",\n'
     "                                           (owner_id,)).fetchall():\n"
     "        relabel = _current(raw, owner_id=owner_id, base_revision=base_revision)\n"
     "        if relabel is not None and relabel.label is not None:"),
    ("prune_keeps_a_gone_label", RELABEL, "        if relabel is None or base_revision not in built.own_revisions:",
     "        if relabel is None:"),
    ("prune_on_a_partial_build", RELABEL, '    if built.schema != "ok" or not built.whole or not installed(conn):',
     '    if built.schema != "ok" or not installed(conn):'),
    ("prune_never_erases", RELABEL,
     "        elif relabel.label is not None and base_revision in built.second_unusable:", "        elif False:"),
    # The refresh loop: the budget, the wait for an owed restore, the second store, the pruning.
    ("second_tries_outside_the_budget", LOOP,
     '            while prepared is not None:\n                if spent["calls"] >= budget:\n'
     '                    counts["short"] = True\n                    return "complete"\n',
     "            while prepared is not None:\n"),
    ("second_tries_never_wait_for_a_restore", LOOP, "            if restore_owed and not waited:", "            if False:"),
    ("second_tries_wait_every_round", LOOP, "            if restore_owed and not waited:", "            if restore_owed:"),
    ("a_wait_keeps_the_interval", LOOP, "                self._relabels_waited, self._last_interest_at = True, None",
     "                self._relabels_waited = True"),
    ("new_objects_not_stored_after_a_second_label", LOOP,
     '            if state != "complete" or not counts["relabelled"]:\n                return state',
     "            if True:\n                return state"),
    ("prune_never_run", LOOP, "                if self.settings.relabels:\n"
     "                    relabel.prune(conn, owner_id=self.owner_id, built=built)\n", ""),
    # The switch: off, the node is what it was before second labels.
    ("switch_cannot_be_set_off", RELABEL, '    return str(env.get(FLAG, "")).strip().lower() not in _OFF',
     "    return True"),
    ("second_labels_read_with_the_switch_off", FAMILY,
     "    second = interest_relabel.accepted(conn, owner_id=owner_id) if relabels else {}",
     "    second = interest_relabel.accepted(conn, owner_id=owner_id)"),
    ("clusters_listed_as_owed_with_the_switch_off", FAMILY,
     "        base = label_revision(cluster_id, label) if relabels and isinstance(label, str) else None",
     "        base = label_revision(cluster_id, label) if isinstance(label, str) else None"),
    ("second_tries_without_the_interest_flag", LOOP, "            relabels = interests and relabel_enabled(env)",
     "            relabels = relabel_enabled(env)"),
    ("second_tries_with_the_switch_off", LOOP, "            relabels = interests and relabel_enabled(env)",
     "            relabels = interests"),
    ("switched_off_refresh_reads_what_is_owed", LOOP,
     "                    relabel.pending(conn, owner_id=self.owner_id, built=built) if self.settings.relabels else [])",
     "                    relabel.pending(conn, owner_id=self.owner_id, built=built))"),
    ("switched_off_refresh_prunes", LOOP, "                if self.settings.relabels:\n"
     "                    relabel.prune(conn, owner_id=self.owner_id, built=built)\n",
     "                relabel.prune(conn, owner_id=self.owner_id, built=built)\n"),
    ("switched_off_refresh_goes_on_to_second_labels", LOOP,
     "            if not self.settings.relabels:\n                return state", "            if False:\n                return state"),
    ("switched_off_receipt_carries_the_counts", LOOP,
     "        if self.settings.relabels:\n            counts.update(relabel_pending=0, relabel_calls=0, relabelled=0)",
     "        if True:\n            counts.update(relabel_pending=0, relabel_calls=0, relabelled=0)"),
    ("counts_not_taken_are_stored_as_null", LOOP,
     "                if data.get(key) is None:\n                    data.pop(key, None)", "                pass"),
    ("failed_label_asked_again_in_the_same_refresh", LOOP,
     '            fresh = [prepared for prepared in pending if prepared["label_revision"] not in seen]',
     "            fresh = pending"),
    ("unreached_label_not_reported", LOOP,
     '            if spent["calls"] >= budget:\n                counts["short"] = True\n                break',
     '            if spent["calls"] >= budget:\n                break'),
    ("unfinished_call_ends_nothing", LOOP,
     '                    spent["failures"] += 1\n                    if spent["failures"] >= MAX_CONSECUTIVE_FAILURES:\n'
     '                        return "failed"\n                    break',
     "                    break"),
    ("owners_pass_does_not_stop_second_tries", LOOP,
     '                    return "complete"\n                if self._stop.is_set() or self._worker_object().running():\n'
     '                    return "cancelled"',
     '                    return "complete"'),
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
    # The person rule: a whole name as whole words, in order (interest-label-rules/v2).
    ("person_name_matched_inside_a_word", FAMILY,
     "    return not whole.isdisjoint(_word_runs(label, max(map(len, whole), default=0)))",
     "    return any(key in skeleton(label) if len(key) >= 4 else key in tokens for key in whole)"),
    ("person_name_words_not_joined", FAMILY,
     "                if len(joined) >= MIN_RUN_LETTERS:\n                    runs.add(joined)\n",
     "                pass\n"),
    ("short_names_joined_across_words", FAMILY, "MIN_RUN_LETTERS = 4\n", "MIN_RUN_LETTERS = 1\n"),
    ("apostrophe_letter_not_an_apostrophe", FAMILY,
     "    if any(ch in plain for ch in APOSTROPHE_LETTERS):\n", "    if False:\n"),
    ("mentioned_name_word_read_once", FAMILY,
     "    if any(not parts.isdisjoint(words) for words in _label_readings(label)):",
     "    if not parts.isdisjoint(_label_readings(label)[0]):"),
    ("excluded_name_whole_words_only", FAMILY,
     '    if cluster_id in tombstones["record"] or names_any(label, excluded_keys):',
     '    if cluster_id in tombstones["record"] or names_person(label, excluded_keys):'),
    ("refusal_current_under_any_label_checks", RELABEL,
     "    expected = revision() if relabel.label is not None else refusal_revision()",
     "    expected = revision() if relabel.label is not None else relabel.rule_revision"),
    ("refusal_stored_under_the_prompt_revision_alone", RELABEL,
     "                      rule_revision=refusal_revision() if broken else revision(), tries=prepared[\"tries\"] + 1,",
     "                      rule_revision=revision(), tries=prepared[\"tries\"] + 1,"),
    ("erased_label_keeps_the_acceptance_revision", RELABEL,
     '            erased = relabel.model_copy(update={"label": None, "refused": built.second_unusable[base_revision],\n'
     '                                                "rule_revision": refusal_revision()})',
     '            erased = relabel.model_copy(update={"label": None, "refused": built.second_unusable[base_revision]})'),
    ("label_checks_not_in_the_refusal_revision", RELABEL,
     '    return digest({"revision": revision(), "label_rules": fam.LABEL_RULES})',
     '    return digest({"revision": revision(), "label_rules": "fixed"})'),
    # Repeat visits count (interest-visit-count/v2): a visit with no vector of its own counts where its page text was
    # placed, in every such cluster, once, after every visit check; and it is read by every check a visit is.
    ("repeats_not_counted", FAMILY, "    repeats = _repeat_visits(conn, placed, activity)\n", "    repeats = {}\n"),
    ("repeat_joins_one_cluster", FAMILY, "            clusters.update(holders.get(text, ()))",
     "            clusters.update(sorted(holders.get(text, ()))[:1])"),
    ("placed_visit_counted_through_its_text", FAMILY,
     "        if not isinstance(event_id, str) or event_id in placed:\n            continue\n        clusters = set()",
     "        if not isinstance(event_id, str):\n            continue\n        clusters = set()"),
    ("visit_with_a_vector_counted_through_its_text", FAMILY,
     '"EXISTS (SELECT 1 FROM signal_embeddings e WHERE e.record_id = a.event_id)"',
     '"EXISTS (SELECT 1 FROM signal_embeddings e WHERE 0)"'),
    ("title_alone_never_a_page_text", FAMILY, '        texts.add(str(row.get("title") or "").strip())',
     "        pass"),
    ("title_alone_a_page_text_beside_content", FAMILY,
     '    if not str(row.get("content") or "").strip():\n        texts.add', "    if True:\n        texts.add"),
    ("title_and_url_never_a_page_text", FAMILY, "    texts = {embeddable_content(row)}", "    texts = set()"),
    ("repeat_mentions_not_read", FAMILY, "        clustered = set(placed) | set(repeats)\n",
     "        clustered = set(placed)\n"),
    ("repeats_outside_the_scope_built", FAMILY,
     "            for cluster_id in sorted(repeats[row[\"event_id\"]]):\n                if in_scope is None or cluster_id in in_scope:",
     "            for cluster_id in sorted(repeats[row[\"event_id\"]]):\n                if True:"),
    ("repeat_counted_unproven", FAMILY, "            proven=event_id in proven_ids,",
     "            proven=event_id in proven_ids or event_id in repeats,"),
    ("repeat_not_read_by_off_limits", FAMILY,
     "_visits_protected(boundary, [visits_by_id[v.event_id] for v in month_visits])",
     "_visits_protected(boundary, [visits_by_id[v.event_id] for v in month_visits if v.event_id in placed])"),
    ("counting_not_in_member_revision", FAMILY, '            member_rev = digest({"version": VERSION, "counting": COUNTING,',
     '            member_rev = digest({"version": VERSION,'),
    ("content_not_read_by_off_limits", FAMILY, '"writer_app_id", "writer_dataset_id", "content") if c in activity]',
     '"writer_app_id", "writer_dataset_id") if c in activity]'),
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
