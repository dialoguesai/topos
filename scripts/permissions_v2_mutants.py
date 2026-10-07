"""Mutation battery over the permissions v2 enforcement path (confidence program C5).

Each mutant is a named, semantic change to one file of the enforcement path -- the
evaluator, the raw-message and fact decisions, the label-free floors, admission, the
three doors and the opaque ids -- applied in place in THIS checkout, run against the
tests that should notice it (the fuzz lane first, then the existing suites), and
restored. A mutant is `killed` when the targeted run fails, `survived` when it passes;
survivors can be re-run against the whole permissions lane with `--full-lane`. Every
survivor must then be killed by a new test or argued equivalent by name in the report.

A mutation run is only meaningful on a green base: `--check` proves every patch applies
exactly once and the tree is clean, and the caller supplies the base's known reds as
`--deselect` node ids (a pre-existing failure would read as a kill).

    python scripts/permissions_v2_mutants.py --check
    python scripts/permissions_v2_mutants.py --out mutants.json [--only NAME ...] [--full-lane]

The checkout must be a private worktree: the runner refuses to start if any target file
is dirty, and restores every file from its exact original bytes on any exit.
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
P = "topos/permissions_v2/"
T = "tests/permissions_v2/"
FUZZ = {"evaluator": T + "test_fuzz_evaluator.py", "encoding": T + "test_fuzz_encoding.py",
        "admission": T + "test_fuzz_admission.py", "transports": T + "test_fuzz_transports.py",
        "facts": T + "test_fuzz_fact_decisions.py", "floors": T + "test_fuzz_floors.py",
        "discovery": T + "test_fuzz_discovery.py"}
EXISTING = {
    # test_message_search_contract.py holds the malformed-policy cases (a source outside the pinned universe among
    # them) for the search grammars, p2c-v3 included.
    "contract": [T + "test_contract_and_ledger.py", T + "test_bk5_read_budget_in_policy.py",
                 T + "test_message_search_contract.py"],
    # The raw-message decision is product code; the suites that drive it do so through the test-only locator
    # adapter (tests/permissions_v2/retired_doors.py) and, door-free, through the reconciliation facts.
    "release": [T + "test_release.py", T + "test_source_release_attested.py", T + "test_reconciliation_facts.py"],
    "facts": [T + "test_fact_policy.py", T + "test_fact_eligibility.py", T + "test_fact_stated_day.py"],
    # The clock's own suites: its cache, the ingest-source clock, and the identity/attestation lanes that its
    # v4 ledger and subject registry belong to.
    "ingest": [T + "test_ingest_provenance.py", T + "test_ingest_owner_boundaries.py",
               T + "test_ingest_origin_evidence.py", T + "test_ingest_snapshot_supersession.py",
               T + "test_bk3_ingest_source_clock.py"],
    "protection_clock": [T + "test_protection_revision_cache.py", T + "test_bk3_ingest_source_clock.py",
                         T + "test_identity_attestation.py", T + "test_owner_identity_binding.py"],
    # The last three are where the Off-limits boundary is pinned on the search path (its checks moved from a
    # table probe to `entity_boundary`, which the first four never build).
    "floors": [T + "test_evidence.py", T + "test_source_release_sibling_facts.py", T + "test_exclusion_floor.py",
               T + "test_evidence_quote_metadata.py", T + "test_direct_message_evidence.py",
               T + "test_direct_search_twins.py", T + "test_entity_boundary_search.py"],
    "canonical_floor": [T + "test_canonical_floor.py", T + "test_canonical_floor_binding.py"],
    # test_fact_release.py reaches the ledger through the test-only fact door adapter; it holds the one test of
    # an envelope issued past its policy's validity.
    "ledger": [T + "test_contract_and_ledger.py", T + "test_bk5_admission_before_the_floor.py",
               T + "test_fact_release.py"],
    "transports": [T + "test_message_search_refusals.py", T + "test_recipient_fabric_refusal_uniformity.py"],
    # The p2c-v3 suites first; then the suites written on the retired p2c-v1 profile, which run with its
    # retirement lifted (tests/permissions_v2/conftest.py `retired_search_profile`).
    # The answers rule at the two search doors, with the control on a share that gives records.
    "answers_share": [T + "test_knowledge_search.py"],
    "search": [T + "test_message_search_refusals.py", T + "test_direct_search_twins.py",
               T + "test_direct_message_search.py", T + "test_knowledge_search.py", T + "test_message_search_batch.py",
               T + "test_message_search_invariant.py", T + "test_message_search_review_fixes.py",
               T + "test_nightA_discovery_subset_access.py"],
    "opaque": [T + "test_bk3_opaque_ids.py"],
    # S1, the isolation battery's node half (isolation charter S0 §6), and the suites its node mutants also answer to.
    "s1": [T + "test_s1_isolation_node.py"],
    # The vector's behaviour tests, not its sha256 pin (which any edit of the module fails, so it kills nothing).
    "s1_bind": [T + "test_bind_protocol.py::test_the_node_accepts_the_vector_bind_and_reproduces_the_vector_proof",
                T + "test_bind_protocol.py::test_the_control_plane_half_of_the_vector_holds_against_this_copy",
                T + "test_bind_protocol.py::test_each_refusal_of_the_vector_raises_its_code",
                T + "test_s1_isolation_node.py"],
    "s1_handler": [T + "test_self_bind.py", T + "test_s1_isolation_node.py"],
    "answer": [T + "test_answer_checks.py", T + "test_answer_generation.py", T + "test_answer_release.py"],
    # R1, the review's three node findings (review S4 H1, M1, M2): the relay dispatcher's rule and the bind's.
    "r1_dispatch": ["tests/core/test_relay_non_owner_gate.py"],
    "r1_bind": [T + "test_bind_over_an_older_sharing_folder.py"],
    # The bind's step 16: the load needs its review store, and what a failed load does with one (Q6).
    "r1_load": [T + "test_bind_load_needs_its_review_store.py"],
    # The first stamp-key pin and its retry (review S4 follow-up, Q8), with the pin's older two tests.
    "r1_pin": ["tests/core/test_first_stamp_pin_retry.py", "tests/core/test_uds_and_convergence.py"],
    # R2, the second review's findings (REVIEW_R1_NODE): the carry step, the clean-up's matching, identifiers at the
    # boundary and in the store, the owner's start, the relay narrowing, the stamp's window and pin, the wiring.
    "r2_carry": ["tests/topos/test_carry_protects_without_destroying.py", "tests/topos/test_carry_step_review_r1.py",
                 "tests/topos/test_carry_contact_excludes_step.py"],
    "r2_runner": ["tests/topos/test_carry_protects_without_destroying.py", "tests/topos/test_upgrade_runner.py"],
    "r2_pending": [T + "test_carried_entry_withholds_while_pending.py"],
    "r2_identifiers": [T + "test_entity_boundary_identifier_aliases.py",
                       "tests/topos/test_off_limits_store_identifiers.py"],
    "r2_start": ["tests/topos/test_owner_starts_the_clean_up.py"],
    "r2_narrowing": ["tests/core/test_relay_named_user_narrowing.py", "tests/core/test_relay_non_owner_gate.py"],
    "r2_stamp": ["tests/core/test_relay_stamp_clock_and_key.py", "tests/core/test_relay_stamp.py",
                 "tests/core/test_first_stamp_pin_retry.py"],
    "r2_wiring": ["tests/test_app_relay_wiring.py"],
    # R3, the third fix round (the re-check of REVIEW_R1_NODE, and WS0's rulings P and M): a carried entry waits; the
    # owner's own paths and the paths toward other people; identifiers as themselves, at read time and at the
    # boundary; the keyless handle; the step first and the hold; scripts without spaces; the relay; the doors.
    "r3_waits": ["tests/topos/test_carried_entry_waits.py", "tests/topos/test_carry_step_review_r1.py"],
    "r3_owner": ["tests/topos/test_carried_entry_owner_paths.py"],
    "r3_outward": ["tests/topos/test_carried_entry_outward_paths.py"],
    "r3_at_the_doors": [T + "test_carried_person_withheld_at_the_doors.py"],
    "r3_read_time": ["tests/topos/test_off_limits_identifiers_at_read_time.py"],
    "r3_boundary": [T + "test_entity_boundary_identifiers_as_themselves.py", T + "test_entity_boundary_keyless_handle.py"],
    "r3_step": ["tests/topos/test_carry_step_builds_the_boundary.py"],
    "r3_first": ["tests/topos/test_exclude_carry_runs_first_and_sharing_waits.py"],
    "r3_bind": [T + "test_bind_waits_for_the_exclude_carry.py"],
    "r3_unspaced": ["tests/topos/test_clean_up_scripts_without_spaces.py"],
    "r3_relay": ["tests/core/test_relay_named_user_narrowing.py"],
    "r3_bind_load": [T + "test_bind_load_needs_its_review_store.py"],
    "r3_store": ["tests/topos/test_off_limits_store_identifiers.py"],
    "r3_doors": ["tests/topos/test_off_limits_doors_for_carried_entries.py"],
    # R4, the fourth round (WS0's ruling on the routine lane; the third round's own B9 and B10; names written without
    # spaces at the share boundary, B1).
    "r4_routine": ["tests/topos/test_routine_lane_carried_items.py"],
    "r4_answers": ["tests/core/test_routine_lane_answers.py"],
    "r4_row_tools": ["tests/core/test_routine_lane_row_tools.py"],
    "r4_own": ["tests/topos/test_off_limits_own_fixes_r4.py"],
    "r4_unspaced": [T + "test_entity_boundary_unspaced_scripts.py"],
    # R5, the fifth round (the second re-check's findings as WS0 ruled them): the row veto and the item rule on a
    # node with one message table; an item that carries an id of theirs; a failure that names its entry, the hold,
    # the pre-flight; the schema step and the downgrade guard; an exclude written after the step; a key that is a
    # name; text that is nearly an entry id; the runner switched off; a node that never turned sharing on.
    "r5_one_table": [T + "test_row_veto_one_message_table.py"],
    "r5_routine": ["tests/topos/test_routine_lane_carried_items.py", "tests/core/test_routine_lane_row_tools.py"],
    "r5_names_it": ["tests/topos/test_carry_step_names_what_it_cannot_read.py"],
    "r5_preflight": [T + "test_carry_preflight.py"],
    "r5_schema": ["tests/storage/test_off_limits_carried_waiting_migration.py",
                  "tests/storage/test_migration_registry.py"],
    "r5_hold": ["tests/topos/test_exclude_carry_runs_first_and_sharing_waits.py"],
    "r5_doors": ["tests/topos/test_off_limits_doors_for_carried_entries.py"],
    "r5_step": ["tests/topos/test_carry_step_builds_the_boundary.py"],
    # R6, the sixth round (the third re-check): the hold starts the step again by itself; the pre-flight and a hard
    # link; the upgrade tools and a real home; the id rule's lookup of a conversation's id.
    "r6_again": ["tests/topos/test_the_hold_starts_the_carry_again.py"],
    "r6_preflight": [T + "test_carry_preflight.py"],
    "r6_tools": ["tests/scripts/test_upgrade_tools_refuse_a_real_home.py"],
    "r6_items": ["tests/topos/test_routine_lane_carried_items.py"],
}


def mutant(name, file, edits, *, fuzz, existing, note=""):
    """`edits` is a list of (old, new) pairs, each of which must match exactly once in `file`."""
    return {"name": name, "file": file, "edits": edits, "tests": [FUZZ[f] for f in fuzz] + [t for e in existing for t in EXISTING[e]],
            "note": note}


# The 4-space indentation below is the files' own; every `old` is checked to occur exactly once.
MUTANTS = [
    # --- contract.py: the three-valued evaluator and the grammar -------------------------------------------------
    mutant("kleene_not_unknown_is_true", P + "contract.py",
           [("        return None if value is None else not value", "        return True if value is None else not value")],
           fuzz=["evaluator"], existing=["contract"]),
    mutant("kleene_all_of_drops_unknown", P + "contract.py",
           [("        return False if False in values else None if None in values else True",
             "        return False if False in values else True")], fuzz=["evaluator"], existing=["contract"]),
    mutant("kleene_any_of_unknown_is_true", P + "contract.py",
           [("    return True if True in values else None if None in values else False",
             "    return True if True in values else True if None in values else False")], fuzz=["evaluator"], existing=["contract"]),
    mutant("atom_unknown_is_false", P + "contract.py",
           [("        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):\n            return None",
             "        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):\n            return False")],
           fuzz=["evaluator"], existing=["contract"]),
    mutant("atom_ignores_malformed_items", P + "contract.py",
           [("        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):\n            return None\n        return bool(set(value).intersection(predicate.values))",
             "        if not isinstance(value, list):\n            return None\n        return bool(set(str(v) for v in value).intersection(predicate.values))")],
           fuzz=["evaluator"], existing=["contract"]),
    mutant("budget_explicit_null_accepted", P + "contract.py",
           [('        if isinstance(value, dict) and "read_budget_per_day" in value and value["read_budget_per_day"] is None:\n            raise ValueError("read budget present but undeclared")\n        return value',
             "        return value")], fuzz=["encoding"], existing=["contract"]),
    mutant("budget_undeclared_serialised_as_null", P + "contract.py",
           [('        if encoded.get("read_budget_per_day") is None:\n            encoded.pop("read_budget_per_day", None)\n        return encoded',
             "        return encoded")], fuzz=["encoding"], existing=["contract"]),
    mutant("validity_may_be_empty", P + "contract.py",
           [('        if self.expires_at <= self.starts_at:\n            raise ValueError("empty validity")',
             '        if self.expires_at < self.starts_at:\n            raise ValueError("empty validity")')],
           fuzz=["admission", "encoding"], existing=["contract"]),
    mutant("source_outside_universe_accepted", P + "contract.py",
           # `pass`, not nothing: the check is the whole body of its branch, and an empty branch is a syntax error
           # that every test "kills" at import. Until 6 Oct 2026 this mutant had never run.
           [('                if not set(sources.values).issubset(universe.source_ids):\n                    raise ValueError("source outside pinned universe")\n',
             "                pass\n")], fuzz=["evaluator"], existing=["contract"]),
    mutant("strict_model_ignores_unknown_keys", P + "contract.py",
           [('    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)',
             '    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)')],
           fuzz=["encoding"], existing=["contract"]),
    # --- release.py: the raw-message decision (the locator door that also lived here is removed, N8) ------------
    mutant("decision_unknown_allow_permits", P + "release.py",
           [("            elif False not in values and None in values:\n                unknown_allow = True",
             "            elif False not in values and None in values:\n                allows.append(rule.rule_id)")],
           fuzz=["evaluator"], existing=["release"],
           note="expected equivalent under the review vocabulary: Unknown is unreachable on p2a (K3); reachable only once the label layer emits unresolved domains"),
    mutant("decision_permit_beats_deny", P + "release.py",
           [('    verdict = "deny" if denies else "indeterminate" if unknown_deny else "permit" if allows else "indeterminate" if unknown_allow else "deny"',
             '    verdict = "permit" if allows else "deny" if denies else "indeterminate" if unknown_deny else "indeterminate" if unknown_allow else "deny"')],
           fuzz=["evaluator"], existing=["release"]),
    mutant("decision_default_is_permit", P + "release.py",
           [('    verdict = "deny" if denies else "indeterminate" if unknown_deny else "permit" if allows else "indeterminate" if unknown_allow else "deny"',
             '    verdict = "deny" if denies else "indeterminate" if unknown_deny else "permit" if allows else "indeterminate" if unknown_allow else "permit"')],
           fuzz=["evaluator"], existing=["release"]),
    mutant("permit_ignores_ceiling", P + "release.py",
           [('            if (rule.release.ceiling != "raw" or not sources or not tables\n                or not all_sources <= sources or not all_tables <= tables):',
             "            if (not sources or not tables\n                or not all_sources <= sources or not all_tables <= tables):")],
           fuzz=["evaluator"], existing=["release"]),
    mutant("permit_ignores_uncovered_sources", P + "release.py",
           [('            if (rule.release.ceiling != "raw" or not sources or not tables\n                or not all_sources <= sources or not all_tables <= tables):',
             '            if (rule.release.ceiling != "raw" or not sources or not tables\n                or not all_tables <= tables):')],
           fuzz=["evaluator"], existing=["release"]),
    mutant("permit_ignores_uncovered_tables", P + "release.py",
           [('            if (rule.release.ceiling != "raw" or not sources or not tables\n                or not all_sources <= sources or not all_tables <= tables):',
             '            if (rule.release.ceiling != "raw" or not sources or not tables\n                or not all_sources <= sources):')],
           fuzz=["evaluator"], existing=["release"]),
    mutant("deny_ignores_its_selection", P + "release.py",
           [("            selected = [item for item in snapshot.leaves if item.identity.source_id in sources and item.identity.table in tables]",
             "            selected = list(snapshot.leaves)")], fuzz=["evaluator"], existing=["release"]),
    mutant("classification_set_unchecked", P + "release.py",
           [('    if not snapshot.leaves or len(labels) != len(closure):\n        raise PolicyError("classification_incomplete")\n    selected_keys = {_key(item.identity) for item in closure}\n    if set(labels) != selected_keys:\n        raise PolicyError("classification_incomplete")\n',
             "    selected_keys = {_key(item.identity) for item in closure}\n")], fuzz=["evaluator"], existing=["release"]),
    mutant("subject_contract_unchecked", P + "release.py",
           [('    if evidence.subject_contract != SUBJECT_CONTRACT_BY_CAPABILITY[capability]:\n        raise PolicyError("subject_contract_mismatch")\n',
             "")], fuzz=["evaluator"], existing=["release"]),
    mutant("vocabulary_unchecked", P + "release.py",
           [('    if policy.versions.vocabulary != VOCABULARY:\n        raise PolicyError("unsupported_vocabulary")\n', "")],
           fuzz=["evaluator"], existing=["release"]),
    # --- fact_policy.py / fact_eligibility.py: the p2b decision --------------------------------------------------
    mutant("fact_unknown_validity_permits", P + "fact_policy.py",
           [("            values += list(clause.leaf_times) + ([None] if structure.fact_times_unknown else [])",
             "            values += list(clause.leaf_times)")], fuzz=["facts"], existing=["facts"],
           note="equivalent: the verdict checks `structure.fact_times_unknown` directly before `allows` is read, so the "
                "appended None only sets `unknown_allow`, which is never reached when fact times are unknown; missing "
                "codes and matched ids are the same on both sides (survived the full lane; argued here, not killed)"),
    mutant("fact_permit_beats_deny", P + "fact_policy.py",
           [('    if denies:\n        return result("deny", "rule_deny", denies=denies)\n    if unknown_deny or structure.fact_times_unknown:\n        return result("indeterminate", "unknown_context", missing=sorted(missing))\n    if allows:\n        return result("permit", "rule_permit", allows=allows)',
             '    if allows:\n        return result("permit", "rule_permit", allows=allows)\n    if denies:\n        return result("deny", "rule_deny", denies=denies)\n    if unknown_deny or structure.fact_times_unknown:\n        return result("indeterminate", "unknown_context", missing=sorted(missing))')],
           fuzz=["facts"], existing=["facts"]),
    mutant("stale_authority_window_widened", P + "fact_eligibility.py",
           [("    if (clock.now < clock.request_as_of or clock.now - clock.request_as_of > 120",
             "    if (clock.now < clock.request_as_of or clock.now - clock.request_as_of > 120000")],
           fuzz=["facts"], existing=["facts"]),
    mutant("future_event_counts_as_in_window", P + "fact_eligibility.py",
           [("            return None if event is None or event > anchor else lower <= event",
             "            return None if event is None else lower <= event")], fuzz=["facts"], existing=["facts"]),
    mutant("window_lower_bound_exclusive", P + "fact_eligibility.py",
           [("            return None if event is None or event > anchor else lower <= event",
             "            return None if event is None or event > anchor else lower < event")], fuzz=["facts"], existing=["facts"]),
    mutant("inference_ceiling_permits_fact", P + "fact_eligibility.py",
           [('            if rule.release.ceiling == "inference":\n                unsupported = True\n                continue',
             '            if rule.release.ceiling == "inference":\n                unsupported = True')], fuzz=["facts"], existing=["facts"]),
    mutant("output_sensitivity_may_attenuate", P + "fact_eligibility.py",
           [('    if _SENSITIVITY[projection.classification.sensitivity] < max(_SENSITIVITY[item.sensitivity] for item in evidence.classifications):\n        raise PolicyError("output_sensitivity_attenuation_unsupported")\n',
             "")], fuzz=["facts"], existing=["facts"]),
    # --- evidence.py: the label-free floors ------------------------------------------------------------------------
    mutant("blackhole_floor_removed", P + "evidence.py",
           [('            if boundary is not None and enforce_floor:\n                boundary.check(table=identity.table, record_id=identity.record_id,\n                    source_id=identity.source_id, dataset_id=identity.dataset_id, row=row)\n', ""),
            ('        boundary = self.entity_boundary(conn)\n        for reference in expected.values():\n            identity = reference.identity\n            boundary.check(table=identity.table, record_id=identity.record_id,\n                source_id=identity.source_id, dataset_id=identity.dataset_id, row=rows[_key(identity)])\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("entity_tombstone_floor_removed", P + "evidence.py",
           [('        if enforce_floor and tombstones["entity"]:\n            raise PolicyError("entity_exclusion_lineage_unavailable")\n', ""),
            ('        if tombstones["entity"]:\n            raise PolicyError("entity_exclusion_lineage_unavailable")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("owner_only_record_floor_removed", P + "evidence.py",
           [('            if enforce_floor and conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=? LIMIT 1",\n                (identity.table, identity.record_id)).fetchone():\n                raise PolicyError("owner_only")\n', ""),
            ('            if conn.execute("SELECT 1 FROM owner_only_records WHERE canonical_table=? AND record_id=? LIMIT 1",\n                            (identity.table, identity.record_id)).fetchone():\n                raise PolicyError("owner_only")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("record_tombstone_floor_removed", P + "evidence.py",
           [('            if enforce_floor and identity.record_id in tombstones["record"]:\n                raise PolicyError("intelligence_excluded")\n', ""),
            ('            if identity.record_id in tombstones["record"]:\n                raise PolicyError("intelligence_excluded")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("fact_tombstone_floor_removed", P + "evidence.py",
           [('            if enforce_floor and identity.table == "signal_objects" and fact_excluded(\n                _json(row.get("payload_json"), dict), tombstones["fact"], restriction_subjects(conn)):\n                raise PolicyError("intelligence_excluded")\n', ""),
            ('                if fact_excluded(payload, tombstones["fact"], restrictions):\n                    raise PolicyError("intelligence_excluded")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("owner_only_disclosure_released", P + "evidence.py",
           [('                if enforce_floor and _json(row.get("payload_json"), dict).get("disclosure") not in SHAREABLE_DISCLOSURES:\n                    raise PolicyError("owner_only")\n', ""),
            ('                if payload.get("disclosure") not in SHAREABLE_DISCLOSURES:\n                    raise PolicyError("owner_only")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("sibling_fact_floor_removed", P + "evidence.py",
           [("            self._source_sibling_floor(conn, snapshot, opted_out=opted_out)\n", "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("unknown_sensitivity_released", P + "evidence.py",
           [('            if (not item.domains or len(item.domains) != len(set(item.domains)) or item.sensitivity == "unknown"',
             '            if (not item.domains or len(item.domains) != len(set(item.domains)) or item.sensitivity == "never"')],
           fuzz=["floors"], existing=["floors"]),
    mutant("empty_domains_released", P + "evidence.py",
           [('            if (not item.domains or len(item.domains) != len(set(item.domains)) or item.sensitivity == "unknown"',
             '            if (len(item.domains) != len(set(item.domains)) or item.sensitivity == "unknown"')],
           fuzz=["floors"], existing=["floors"]),
    mutant("quoted_speech_released", P + "evidence.py",
           [('            if item.authorship != "owner_authored" or item.speech != "direct_self_statement":\n                raise PolicyError("not_owner_self_statement")',
             '            if item.authorship != "owner_authored":\n                raise PolicyError("not_owner_self_statement")')],
           fuzz=["floors"], existing=["floors"]),
    mutant("known_copies_label_ignored", P + "evidence.py",
           [('            if item.independent_copies != "none_known":\n                raise PolicyError("independent_copy_lineage")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("not_from_self_released", P + "evidence.py",
           # `pass`, as above: removing the whole body of the branch never compiled, so this mutant had never run.
           [('                    if type(row.get("is_from_self")) is not int or row["is_from_self"] != 1:\n                        raise PolicyError("not_owner_authored")\n',
             "                    pass\n")],
           fuzz=["floors"], existing=["floors"]),
    mutant("quote_metadata_released", P + "evidence.py",
           [('                    if any(metadata.get(field) not in (None, False, 0, "", [], {}) for field in',
             '                    if False and any(metadata.get(field) not in (None, False, 0, "", [], {}) for field in')],
           fuzz=["floors"], existing=["floors"]),
    mutant("independent_copy_released", P + "evidence.py",
           [('                if self._known_copies(conn, identity, row):\n                    raise PolicyError("independent_copy_lineage")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("stale_review_served", P + "evidence.py",
           [('        if not isinstance(review, OwnerEvidenceReview) or review.owner_id != self.binding.owner_id or review.snapshot != snapshot:\n            raise PolicyError("review_stale")\n', ""),
            ('            if item.evidence != reference:\n                raise PolicyError("review_stale")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("deleted_row_served", P + "evidence.py",
           [('        if _deleted(row):\n            raise PolicyError("evidence_deleted")\n', "")], fuzz=["floors"], existing=["floors"]),
    mutant("revoked_review_served", P + "evidence.py",
           [('        found = db.execute("SELECT rowid FROM fact_reviews WHERE fact_id=? AND active=1", (fact_id,)).fetchmany(2)',
             '        found = db.execute("SELECT rowid FROM fact_reviews WHERE fact_id=?", (fact_id,)).fetchmany(2)')],
           fuzz=["floors"], existing=["floors"], note="a revoked review is inactive; serving it again is the mutation"),
    # --- exclusion_floor.py ---------------------------------------------------------------------------------------
    mutant("fact_tombstone_value_key_dropped", P + "exclusion_floor.py",
           [('        keys = {prefix, prefix + ":" + value.strip().lower(), prefix + ":" + _normalize_value(value)}',
             '        keys = {prefix, prefix + ":" + _normalize_value(value)}')], fuzz=["floors"], existing=["floors"],
           note="equivalent: `fact_excluded` also intersects the whitespace-collapsed view of the tombstone set, and "
                "collapsing the writer's strip-and-lower spelling IS `_normalize_value`, so the dropped key is matched by "
                "the fallback; test_F5 pins that both spellings veto (survived the full lane; argued here, not killed)"),
    mutant("fact_tombstone_prefix_key_dropped", P + "exclusion_floor.py",
           [('        keys = {prefix, prefix + ":" + value.strip().lower(), prefix + ":" + _normalize_value(value)}',
             '        keys = {prefix + ":" + value.strip().lower(), prefix + ":" + _normalize_value(value)}')],
           fuzz=["floors"], existing=["floors"]),
    mutant("malformed_tombstone_tolerated", P + "exclusion_floor.py",
           [('            raise PolicyError("exclusion_state_unknown")\n        if kind == "fact" and (key != key.lower()',
             '            continue\n        if kind == "fact" and (key != key.lower()')], fuzz=["floors"], existing=["floors"]),
    # --- canonical_floor.py ---------------------------------------------------------------------------------------
    mutant("floor_generation_rollback_unchecked", P + "canonical_floor.py",
           [("        if observed.generation < floor.generation or observed.event_sequence < floor.event_sequence:",
             "        if observed.event_sequence < floor.event_sequence:")], fuzz=[], existing=["canonical_floor"]),
    mutant("floor_ledger_digest_unpinned", P + "canonical_floor.py",
           [("        if observed.ledger_digest != floor.ledger_digest or observed.ledger_sequence != floor.ledger_sequence:",
             "        if observed.ledger_sequence != floor.ledger_sequence:")], fuzz=[], existing=["canonical_floor"]),
    # --- ledger.py: admission --------------------------------------------------------------------------------------
    mutant("replay_unchecked_at_verify", P + "ledger.py",
           [('            raise PolicyError("request_replay")\n        encoded = canonical_bytes(envelope.model_dump()).decode("ascii")',
             '            pass\n        encoded = canonical_bytes(envelope.model_dump()).decode("ascii")')],
           fuzz=["admission"], existing=["ledger"]),
    mutant("replay_unchecked_at_claim", P + "ledger.py",
           [('        if conn.execute("SELECT 1 FROM p2a_requests WHERE request_id=?", (admission.lease.request_id,)).fetchone():\n            raise PolicyError("request_replay")\n        conn.execute("INSERT INTO p2a_requests VALUES (?, ?, ?, ?)",',
             '        conn.execute("INSERT OR REPLACE INTO p2a_requests VALUES (?, ?, ?, ?)",')],
           fuzz=["admission"], existing=["ledger"]),
    mutant("envelope_outside_policy_validity_admitted", P + "ledger.py",
           [('        if (envelope.issued_at < policy.validity.starts_at\n            or envelope.expires_at > policy.validity.expires_at):\n            raise PolicyError("envelope_policy_time")\n', "")],
           fuzz=["admission"], existing=["ledger"]),
    mutant("refusal_stores_the_envelope", P + "ledger.py",
           [('            self._claim(conn, admission, envelope_json="", status="refused", now=now)',
             '            self._claim(conn, admission, envelope_json=admission.encoded, status="refused", now=now)')],
           fuzz=[], existing=["ledger"]),
    mutant("request_node_binding_unchecked", P + "ledger.py",
           [('        if any(getattr(request, key) != value for key, value in self.identity.model_dump().items()):\n            raise PolicyError("request_binding")\n', "")],
           fuzz=["admission"], existing=["ledger"]),
    # --- signing.py -------------------------------------------------------------------------------------------------
    mutant("signature_never_verified", P + "signing.py",
           [("        Ed25519PublicKey.from_public_bytes(key).verify(sig, signing_bytes(envelope))",
             "        Ed25519PublicKey.from_public_bytes(key)")], fuzz=["admission"], existing=["ledger"]),
    mutant("ttl_unbounded", P + "signing.py",
           [("    if not 0 < envelope.expires_at - envelope.issued_at <= MAX_TTL_SECONDS:",
             "    if not 0 < envelope.expires_at - envelope.issued_at <= MAX_TTL_SECONDS * 1000:")],
           fuzz=["admission"], existing=["ledger"]),
    mutant("expiry_instant_still_valid", P + "signing.py",
           [("    if envelope.issued_at > now or envelope.expires_at <= now:", "    if envelope.issued_at > now or envelope.expires_at < now:")],
           fuzz=["admission"], existing=["ledger"]),
    mutant("authority_binding_unchecked", P + "signing.py",
           [('    for field, value in expected_authority.model_dump().items():\n        if getattr(envelope, field) != value:\n            raise PolicyError("authority_binding")\n', "")],
           fuzz=["admission"], existing=["ledger"]),
    mutant("request_hash_unchecked", P + "signing.py",
           [('    if envelope.request_hash != request_digest(request.request_type, payload):\n        raise PolicyError("request_hash")\n', "")],
           fuzz=["admission"], existing=["ledger"]),
    # --- the search transport (the locator and fact transports are removed, N8) -------------------------------------
    mutant("search_transport_none_stamp_unchecked", P + "search_transport.py",
           [('        if not _enabled() or message.get("type") != MESSAGE_TYPE:\n            raise PolicyError("message_search_disabled")\n        principal = verify_relay_stamp(message)\n        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":',
             '        if not _enabled() or message.get("type") != MESSAGE_TYPE:\n            raise PolicyError("message_search_disabled")\n        principal = verify_relay_stamp(message)\n        if principal.cls != THIRD_PARTY or principal.channel != "cp_relay":'),
            ('        if not _batch_enabled() or message.get("type") != BATCH_MESSAGE_TYPE:\n            raise PolicyError("message_search_disabled")\n        principal = verify_relay_stamp(message)\n        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":',
             '        if not _batch_enabled() or message.get("type") != BATCH_MESSAGE_TYPE:\n            raise PolicyError("message_search_disabled")\n        principal = verify_relay_stamp(message)\n        if principal.cls != THIRD_PARTY or principal.channel != "cp_relay":')],
           fuzz=["transports"], existing=["transports"],
           note="equivalent: the dispatcher's blanket `except Exception` turns the AttributeError an unstamped frame "
                "raises into the identical error frame, and logs no diagnostics in either branch, so no party sees a "
                "difference. The equivalence RESTS on that catch-all: adding exception logging there would make this "
                "mutant observable to the operator and it would need a test"),
    # --- search_release.py: discovery ----------------------------------------------------------------------------------
    mutant("search_trusts_the_index", P + "search_release.py",
           [('                    decided[cache_key] = ((qualified, rows, decision)\n                                        if decision.verdict == "permit"\n                                        and (direct or _locator_disclosable(qualified, rows, key, grant_id)) else None)',
             "                    decided[cache_key] = (qualified, rows, decision)")], fuzz=["discovery"], existing=["search"]),
    mutant("search_ignores_locator_budget", P + "search_release.py",
           [('                                        if decision.verdict == "permit"\n                                        and (direct or _locator_disclosable(qualified, rows, key, grant_id)) else None)',
             '                                        if decision.verdict == "permit" else None)')], fuzz=["discovery"], existing=["search"]),
    mutant("search_answers_share_searched", P + "search_release.py",
           [('        if effective_mode(policy, frontend_client_id=self.protocol.frontend_client_id) != "records":\n            raise PolicyError("answers_only_share")\n        window = policy.search.window\n',
             "        window = policy.search.window\n"),
            ('        window = policy.search.window\n        if effective_mode(policy, frontend_client_id=self.protocol.frontend_client_id) != "records":\n            raise PolicyError("answers_only_share")\n        lower_us, upper_us',
             "        window = policy.search.window\n        lower_us, upper_us"),
            ('                if effective_mode(policy, frontend_client_id=self.protocol.frontend_client_id) != "records":\n                    raise PolicyError("answers_only_share")\n                laps = {}',
             "                laps = {}"),
            ('                if effective_mode(policy, frontend_client_id=self.protocol.frontend_client_id) != "records":\n                    raise PolicyError("answers_only_share")\n                # Alias/contact/context changes',
             "                # Alias/contact/context changes")],
           fuzz=[], existing=["answers_share"],
           note="A2A-4: a share that gives answers releases no record through either search door. All four copies "
                "of the check go together (each door re-reads the policy under the gate), so the mutant is the "
                "rule's removal and not one of its copies."),
    mutant("search_window_ignored", P + "search_release.py",
           [("            if (event_us is None or not lower_us <= event_us <= upper_us or not native_time_within(row, lower_us, upper_us) or is_record_nsfw(row)",
             "            if (event_us is None or is_record_nsfw(row)")], fuzz=["discovery"], existing=["search"]),
    mutant("search_nsfw_ignored", P + "search_release.py",
           [("            if (event_us is None or not lower_us <= event_us <= upper_us or not native_time_within(row, lower_us, upper_us) or is_record_nsfw(row)",
             "            if (event_us is None or not lower_us <= event_us <= upper_us or not native_time_within(row, lower_us, upper_us)")], fuzz=["discovery"], existing=["search"],
           note="equivalent, re-read under the corrected standard (no observable difference to ANY party, not merely "
                "identical recipient bytes). search_index.py excludes a flagged row when the index is built, so the "
                "door's check is a backstop for the window the index cannot cover: a row flagged AFTER indexing and "
                "before the next rebuild. test_D5 constructs exactly that window and the record still does not come "
                "back with this mutant applied, because the door refuses the request whole once its membership no "
                "longer matches the index. D4 pins the index half, D5 the door half"),
    mutant("search_k_unbounded", P + "search_release.py",
           [("            if len(records) == k:\n                break\n", "")], fuzz=["discovery"], existing=["search"]),
    # --- opaque_ids.py -------------------------------------------------------------------------------------------------
    mutant("opaque_key_length_unchecked", P + "opaque_ids.py",
           [('        raise PolicyError("record_key_invalid")\n    body = canonical_bytes({"grant_id"', '        pass\n    body = canonical_bytes({"grant_id"')],
           fuzz=["encoding"], existing=["opaque"]),
    mutant("opaque_id_without_domain", P + "opaque_ids.py",
           [('    return "r." + hmac.new(key, DOMAIN + body, hashlib.sha256).hexdigest()',
             '    return "r." + hmac.new(key, body, hashlib.sha256).hexdigest()')], fuzz=["encoding"], existing=["opaque"],
           note="the pinned vector in the boundary battery is the cross-repo witness; in-repo only bk3 pins it"),
    mutant("opaque_id_ignores_grant", P + "opaque_ids.py",
           [('    body = canonical_bytes({"grant_id": grant_id, "table": table, "source_id": source_id,',
             '    body = canonical_bytes({"grant_id": "", "table": table, "source_id": source_id,')], fuzz=["encoding"], existing=["opaque"]),
    # --- protection_clock.py: the floor under whether reads happen at all ---------------------------------------
    # Added after the control-plane battery, on the design session's call. The module carried no mutants while
    # deciding twice in one evening whether any read could proceed: `clock_state` refuses on ANY mismatch and
    # takes every read down with it, and its coverage rule is what makes a new identity table fail closed. Each
    # mutant below names what its removal would let through, in the comment beside it.
    mutant("clock_trigger_set_not_compared", P + "protection_clock.py",
           [("or not 0 <= row[1] <= MAX_INTEGER or row[2] != version or found != _triggers(version, coverage)",
             "or not 0 <= row[1] <= MAX_INTEGER or row[2] != version")],
           fuzz=["floors"], existing=["floors", "protection_clock"],
           note="lets a dropped or altered owner-mutation trigger pass: the generation stops advancing on an owner's "
                "narrowing, so a stale protection revision reads as current and the narrowing is never seen"),
    mutant("clock_coverage_frozen_at_install", P + "protection_clock.py",
           [("    coverage = identity_coverage(conn) if version >= 4 else ()",
             "    coverage = IDENTITY_TABLES if version >= 4 else ()")],
           fuzz=["floors"], existing=["floors", "protection_clock"],
           note="lets an identity table that appears AFTER install go unwatched instead of failing closed, which is "
                "the exact rule identity_coverage's docstring states"),
    mutant("clock_id_form_unchecked", P + "protection_clock.py",
           [('if (row is None or type(row[0]) is not str or re.fullmatch(r"[0-9a-f]{64}", row[0]) is None or type(row[1]) is not int',
             "if (row is None or type(row[0]) is not str or type(row[1]) is not int")],
           fuzz=["floors"], existing=["floors", "protection_clock"],
           note="lets a malformed clock id into the node-wide protection revision every signed authority binds to"),
    mutant("clock_contract_version_unchecked", P + "protection_clock.py",
           [("or not 0 <= row[1] <= MAX_INTEGER or row[2] != version or found != _triggers(version, coverage)",
             "or not 0 <= row[1] <= MAX_INTEGER or found != _triggers(version, coverage)")],
           fuzz=["floors"], existing=["floors", "protection_clock"],
           note="equivalent: the state table carries a CHECK on contract_version, so a row naming another contract "
                "cannot be written while the table has its own schema, and replacing the table to get around it is "
                "caught by the trigger-and-table comparison instead. test_clock_state_refuses_a_clock_row_or_table_"
                "set_that_was_tampered_with[contract_version] pins that the write is refused by the CHECK"),
    mutant("clock_identity_tables_unchecked", P + "protection_clock.py",
           [("        or identity != expected_identity):", "        or False):")],
           fuzz=["floors"], existing=["floors", "protection_clock"],
           note="lets the attestation ledger or the subject registry be missing or altered while reads continue"),
    mutant("clock_install_over_existing_triggers", P + "protection_clock.py",
           [("if not allow_install or conn.execute(\"SELECT 1 FROM sqlite_master WHERE type='trigger' AND name LIKE 'permissions_v2_%'\").fetchone():",
             "if not allow_install:")],
           fuzz=[], existing=["floors", "protection_clock"],
           note="installs a fresh clock over a database that already carries permissions triggers, which is the "
                "silent repair the docstring forbids: the new clock starts at generation 0 and every authority "
                "issued under the old one reads as current"),

    # --- ingest_provenance.py: the ledger that says which enrollment a canonical row came from ------------------
    # The second module the battery covered nothing of. `_check_locked` is its `clock_state`: one comparison of the
    # ledger's schema, binding, clock generation and authority digest against the out-of-band marker, and every
    # ingest path runs through it. Each mutant names what its removal would admit.
    mutant("ingest_ledger_binding_unchecked", P + "ingest_provenance.py",
           [('        if len(rows) != 1 or tuple(rows[0][:3]) != (marker.get("store_id"), _json(self.binding.model_dump()), self.resolver._file_revision()):',
             "        if len(rows) != 1:")],
           fuzz=[], existing=["ingest"],
           note="admits a provenance ledger belonging to another node, another binding or another incarnation of "
                "the database file, so rows attributed to this owner's enrollment may have come from elsewhere"),
    mutant("ingest_schema_digest_unchecked", P + "ingest_provenance.py",
           [('        if (version not in (1, 2) or found != self._schema(conn, version)\n'
             '                or digest(found) != marker.get("schema_digest")):',
             "        if (version not in (1, 2) or found != self._schema(conn, version)):")],
           fuzz=[], existing=["ingest"],
           note="stops binding the ledger's schema to the marker that recorded it, so a ledger rebuilt to today's "
                "shape passes as the one the marker was written for"),
    mutant("ingest_source_clock_rollback_allowed", P + "ingest_provenance.py",
           [('        if type(generation) is not int or generation < marker["generation"]:',
             "        if type(generation) is not int:")],
           fuzz=[], existing=["ingest"],
           note="admits a source clock that has gone backwards, so a revocation the marker already observed is "
                "forgotten and the enrollment reads as live again"),
    mutant("ingest_authority_rollback_unchecked", P + "ingest_provenance.py",
           [('            if self._authority_digest(conn) != marker.get("authority_digest"):\n'
             '                raise PolicyError("ingest_ledger_rollback")\n', "")],
           fuzz=[], existing=["ingest"],
           note="admits a database whose ingest authority rows were rolled back under a marker that recorded the "
                "later state"),
    mutant("ingest_observed_revocation_not_persisted", P + "ingest_provenance.py",
           [('                self._publish_marker({**marker, "generation": generation, "revision": marker["revision"] + 1})',
             "                pass")],
           fuzz=[], existing=["ingest"],
           note="sees a newer source-clock generation and does not write it down, so the next reader compares "
                "against the stale marker and the observed revocation is lost on the withheld path"),
    mutant("ingest_record_insert_skips_current_check", P + "ingest_provenance.py",
           [("    def record_insert(self, conn, message_id):\n        self.require_batch(conn)\n"
             "        self.assert_current(conn, source_id=self.source_id, dataset_id=self.dataset_id)\n",
             "    def record_insert(self, conn, message_id):\n        self.require_batch(conn)\n")],
           fuzz=[], existing=["ingest"],
           note="writes a provenance row under an enrollment that may have been superseded or disabled since the "
                "batch opened, which is the whole claim the ledger exists to make"),
    mutant("ingest_canonical_collision_tolerated", P + "ingest_provenance.py",
           [('            raise PolicyError("ingest_canonical_collision")', "            return True")],
           fuzz=[], existing=["ingest"],
           note="treats a message id whose canonical row identity has changed as the same record, so a replaced "
                "row inherits the provenance of the one it replaced"),

    # --- answer release (A2A-4 §16): the model must not weaken the node's release checks ------------------------
    mutant("answer_copy_check_disabled", P + "answer_generation.py",
           [("            if copied_sentence(sentence, prompt.raw_texts):",
             "            if False and copied_sentence(sentence, prompt.raw_texts):")],
           fuzz=[], existing=["answer"]),
    mutant("answer_uncited_sentence_kept", P + "answer_checks.py",
           [("            if not numbers or any(number < 1 or number > record_count for number in numbers):",
             "            if False:")],
           fuzz=[], existing=["answer"]),
    mutant("answer_question_protection_removed", P + "answer_release.py",
           [("                if adapter.resolver.entity_boundary(conn).mentions_protected(question):",
             "                if False:")],
           fuzz=[], existing=["answer"]),
    mutant("answer_output_protection_removed", P + "answer_generation.py",
           [("    if before_scrub and boundary.mentions_protected(before_scrub):",
             "    if False:")],
           fuzz=[], existing=["answer"]),
    mutant("answer_scrub_removed", P + "answer_generation.py",
           [("    sentences, scrub_drops = scrub_sentences(sentences)",
             "    sentences, scrub_drops = sentences, 0")],
           fuzz=[], existing=["answer"]),
    mutant("answer_fetch_authority_check_removed", P + "answer_release.py",
           [("                if (not same_answer_authority(current, job.admitted_authority)\n"
             "                        or effective_mode(policy, frontend_client_id=self.runtime.protocol.frontend_client_id) != job.mode):",
             "                if False:")],
           fuzz=[], existing=["answer"]),
    mutant("answer_fetch_charges_question", P + "answer_release.py",
           [("                ledger.admit_answer(admission, now=self.clock(), charge=False)",
             "                ledger.admit_answer(admission, now=self.clock(), charge=True)")],
           fuzz=[], existing=["answer"]),
    mutant("answer_second_fetch_allowed", P + "answer_release.py",
           [("                if job.state == \"ended\":\n                    job.question = None\n"
             "                    job.body = None\n                    self._jobs.pop(intent.answer_id, None)\n",
             "                if job.state == \"ended\":\n                    job.question = None\n")],
           fuzz=[], existing=["answer"]),
]

KNOWN_REDS = [
    T + "test_night_b_recipient_surface.py::test_b2_expired_admissions_are_never_removed_from_the_node_ledger",
    T + "test_night_b_recipient_surface.py::test_b4_engine_local_truth_doors_admit_a_third_party_principal[/api/local/truth_prompts]",
    T + "test_night_b_recipient_surface.py::test_b4_engine_local_truth_doors_admit_a_third_party_principal[/api/local/truth_seed_fact]",
    T + "test_night_b_recipient_surface.py::test_b4_engine_local_truth_doors_admit_a_third_party_principal[/api/local/verify_claim]",
]


# --- S1, the isolation battery (isolation charter S0 §6): the node's mutants -------------------------------------------
# Run with `--group s1`. The control plane's own S1 group is in its catalog (scripts/permissions_v2_mutants.py there).
BIND = P + "bind_protocol.py"
S1_MUTANTS = [
    mutant("s1_M13_bind_accepted_with_a_wrong_owner", P + "self_bind.py",
           [("    _check_engine_owner(served, bind.owner_id)                       # 6\n", "")],
           fuzz=[], existing=["s1_handler"]),
    mutant("s1_M14_bind_accepted_for_a_wrong_topos", P + "self_bind.py",
           [("    _check_install_scopes(served, bind)                              # 7\n", "")],
           fuzz=[], existing=["s1_handler"]),
    mutant("s1_M15_expired_bind_accepted", BIND,
           [("    if bind.issued_at > now + CLOCK_SKEW_SECONDS or bind.expires_at <= now:\n",
             "    if bind.issued_at > now + CLOCK_SKEW_SECONDS:\n")], fuzz=[], existing=["s1_bind"]),
    mutant("s1_M16_unsigned_bind_accepted", BIND,
           [("        Ed25519PublicKey.from_public_bytes(stamp_public_key).verify(_decode(bind.signature), signing_bytes(bind))\n",
             "        pass\n")], fuzz=[], existing=["s1_bind"]),
    mutant("s1_M17_bind_not_tied_to_its_frame", BIND,
           [('    if bind.request_id != message_id:\n        raise PolicyError("bind_frame_mismatch")\n', "")],
           fuzz=[], existing=["s1_bind"]),
    mutant("s1_M18_proof_for_another_bind_accepted", BIND,
           [("            or proof.nonce != bind.nonce or proof.bind_hash != bind_hash(bind)\n", "")],
           fuzz=[], existing=["s1_bind"]),
    mutant("s1_M33_node_takes_its_identity_from_the_envelope", P + "search_release.py",
           [('\n        request = SearchRequestContext.parse({**ledger.identity.model_dump(), "actor_id": principal.acting_user,',
             '\n        request = SearchRequestContext.parse({**{field: getattr(signed, field) for field in ("environment_id", '
             '"node_id", "resource_id", "owner_id")}, "actor_id": principal.acting_user,')],
           fuzz=[], existing=["s1"],
           note="EQUIVALENT: the ledger binds every request to its own identity before it verifies an envelope "
                "(PolicyLedger._bound_request, topos/permissions_v2/ledger.py:313-318), so an envelope naming another "
                "node is refused (request_binding) wherever its request came from; that guard is s1_M33_guard"),
    mutant("s1_M33_guard_ledger_takes_any_request_identity", P + "ledger.py",
           [('        if any(getattr(request, key) != value for key, value in self.identity.model_dump().items()):\n'
             '            raise PolicyError("request_binding")\n', "")],
           fuzz=[], existing=["s1"], note="the guard that makes s1_M33 equivalent"),
    mutant("s1_M34_node_obeys_any_owners_stamp", P + "evidence.py",
           [("        or principal.acting_user != binding.owner_id):\n", "        ):\n")], fuzz=[], existing=["s1"]),
]
MUTANTS = MUTANTS + S1_MUTANTS


# --- R1, the review fixes made before the closed list opened (review S4: H1, M1, M2) ----------------------------------
# Run with `--group r1`. H1 and M1 are the relay dispatcher's (topos/core/handlers/__init__.py); M2 is the bind's.
DISPATCHER = "topos/core/handlers/__init__.py"
STAMP = "topos/relay_stamp.py"
R1_MUTANTS = [
    mutant("r1_H1_a_non_owner_third_party_reaches_every_type", DISPATCHER,
           [("    if msg_type in NON_OWNER_RELAY_TYPES:\n        return None\n    return _owner_mode_refusal(message)\n",
             "    return None\n")], fuzz=[], existing=["r1_dispatch"],
           note="the state before the rule: only the control plane's routing kept a recipient from the other types"),
    mutant("r1_H1_a_node_with_no_owner_on_record_serves_any_third_party", DISPATCHER,
           [('        if owner is not None and acting == owner:\n',
             '        if owner is None or acting == owner:\n')],
           fuzz=[], existing=["r1_dispatch"], note="the open direction: a node that cannot name its owner"),
    mutant("r1_H1_a_bound_node_falls_back_to_the_engine_config_owner", DISPATCHER,
           [("            return _bound_owner_id() or None\n", "            pass\n")],
           fuzz=[], existing=["r1_dispatch"]),
    mutant("r1_H1_the_allow_list_gains_a_type", DISPATCHER,
           [('    "permissions_v2_answer_fetch",\n})', '    "permissions_v2_answer_fetch",\n    "query",\n})')],
           fuzz=[], existing=["r1_dispatch"], note="a type joins the list unnoticed"),
    mutant("r1_H1_the_rule_reads_only_some_third_parties", DISPATCHER,
           [('        if owner is not None and acting == owner:\n',
             '        if not acting or (owner is not None and acting == owner):\n')],
           fuzz=[], existing=["r1_dispatch"], note="a stamp that names nobody is let through as if it were the owner's"),
    mutant("r1_M1_a_stamp_that_does_not_verify_is_no_stamp", DISPATCHER,
           [("        if reason != NO_STAMP:\n", "        if False:\n")],
           fuzz=[], existing=["r1_dispatch"], note="the state before the rule: the relay deferral, above a third party"),
    mutant("r1_M1_only_a_well_formed_stamp_counts_as_a_stamp", DISPATCHER,
           [("        if reason != NO_STAMP:\n", '        if reason not in (NO_STAMP, "malformed"):\n')],
           fuzz=[], existing=["r1_dispatch"], note="a malformed stamp field reads as no stamp"),
    mutant("r1_M2_a_bind_goes_ahead_over_another_identitys_review_store", P + "self_bind.py",
           [("    had_review_store = _check_review_store(durable, bind)            # 10a: nothing is written before this\n",
             "    had_review_store = False\n")], fuzz=[], existing=["r1_bind"],
           note="with the check gone the store is met later, at step 14a or at the load, after the backup, the key "
                "and the clock were written: the killer is the test that nothing was written"),
    mutant("r1_M2_the_review_store_check_reads_no_identity", P + "self_bind.py",
           [("        if node_id is None or enrolled.review_store_path != str(store) or enrolled.binding != EvidenceBinding.parse({\n"
             '                "environment_id": bind.environment_id, "node_id": node_id, "resource_id": bind.resource_id,\n'
             '                "owner_id": bind.owner_id}):\n'
             "            raise PolicyError(cause)\n", "")],
           fuzz=[], existing=["r1_bind"], note="any readable enrollment passes, whoever it names"),
    mutant("r1_M2_already_bound_vouches_for_a_node_whose_review_store_refuses", P + "self_bind.py",
           [("        runtime.evidence_reviews(require_existing=True)\n", "")], fuzz=[], existing=["r1_bind"]),
    # Q6: the load of step 16 needs its review store; a store the failed bind's own load made goes with it, and no
    # other store is ever moved.
    mutant("r1_Q6_the_load_check_is_skipped", P + "self_bind.py",
           [("        runtime.evidence_reviews(require_existing=True)              # 16: the store opens as this identity's\n",
             "")], fuzz=[], existing=["r1_load"], note="the state before: bound over a store that does not open"),
    mutant("r1_Q6_a_failed_bind_leaves_the_store_it_began", P + "self_bind.py",
           [("        if made:\n            _set_aside_new_review_store(durable, expected)\n", "")],
           fuzz=[], existing=["r1_load"], note="the next first bind is then refused at step 10a, for good"),
    mutant("r1_Q6_the_move_also_takes_a_store_that_was_there_before", P + "self_bind.py",
           [("        if made:\n            _set_aside_new_review_store(durable, expected)\n",
             "        if True:\n            _set_aside_new_review_store(durable, expected)\n")],
           fuzz=[], existing=["r1_load"], note="the owner's own store, with its deselections, set aside"),
    mutant("r1_Q6_made_by_this_bind_is_decided_by_the_name_on_the_store", P + "self_bind.py",
           [('getattr(enrollment, "created_enrollment", False) is True', "True")],
           fuzz=[], existing=["r1_load"],
           note="a store that appeared from elsewhere under this bind's identity is taken for the bind's own"),
    mutant("r1_Q6_the_enrollment_never_says_it_made_the_store", P + "evidence_review_runtime.py",
           [("                self.created_enrollment = True\n", "")], fuzz=[], existing=["r1_load"]),
    mutant("r1_Q6_a_store_that_appeared_before_the_commit_goes_unnoticed", P + "self_bind.py",
           [("        _check_review_store_unchanged(durable, had_review_store)     # 14a: what step 10a saw is still what is there\n",
             "")], fuzz=[], existing=["r1_load"]),
    mutant("r1_Q6_what_is_on_disk_is_moved_whoever_it_names", P + "self_bind.py",
           [("        if ReviewEnrollment.parse(marker.read_bytes()).binding != EvidenceBinding.parse(expected):\n"
             "            return\n", "")], fuzz=[], existing=["r1_load"]),
    mutant("r1_Q6_the_stores_of_failed_binds_pile_up", P + "self_bind.py",
           [("    for old in found[:-FAILED_BIND_REVIEWS_KEPT]:\n        shutil.rmtree(old)\n", "")],
           fuzz=[], existing=["r1_load"]),
    # Q8: a node that holds no stamp key tries the first pin again, and a pinned key is never asked for again.
    mutant("r1_Q8_the_first_pin_is_tried_once", STAMP,
           [("        if stop.wait(delay):\n            return False\n        delay = min(delay * 2, FIRST_PIN_RETRY_MAX_S)\n",
             "        return False\n")], fuzz=[], existing=["r1_pin"],
           note="the state before: no key until the next restart"),
    mutant("r1_Q8_the_wait_between_tries_does_not_grow", STAMP,
           [("        delay = min(delay * 2, FIRST_PIN_RETRY_MAX_S)\n", "        pass\n")],
           fuzz=[], existing=["r1_pin"]),
    mutant("r1_Q8_the_wait_between_tries_has_no_ceiling", STAMP,
           [("        delay = min(delay * 2, FIRST_PIN_RETRY_MAX_S)\n", "        delay = delay * 2\n")],
           fuzz=[], existing=["r1_pin"]),
    mutant("r1_Q8_the_retry_asks_again_over_a_pinned_key", STAMP,
           [("        if _load_public_key_bytes() is not None:\n            return True\n", ""),
            ("    if _load_public_key_bytes() is not None:\n        return False\n    try:\n", "    try:\n"),
            ("        if _load_public_key_bytes() is not None or _file_holds_a_key(path):\n            return False\n", "")],
           fuzz=[], existing=["r1_pin"],
           note="the rejected idea: a key asked for again once one is pinned, so a swapped control plane rotates "
                "itself into trust"),
    mutant("r1_Q8_a_key_pinned_meanwhile_is_replaced", STAMP,
           [("        if _load_public_key_bytes() is not None or _file_holds_a_key(path):\n            return False\n", ""),
            ("            if _file_holds_a_key(path):\n"
             "                return False                      # pinned meanwhile, by anyone: never replaced\n", "")],
           fuzz=[], existing=["r1_pin"],
           note="both guards: since review R1 (R-M3) the pin is also linked into place exclusively, so the look "
                "before the write alone no longer decides it (that one edit survived, an equivalent mutant)"),
    mutant("r1_Q8_an_unusable_environment_value_is_asked_over", STAMP,
           [('        if (os.environ.get(_ENV_KEY) or "").strip():\n            return False\n', "")],
           fuzz=[], existing=["r1_pin"]),
    mutant("r1_Q8_a_late_answer_pins_after_shutdown", STAMP,
           [("        if stop is not None and stop.is_set():\n            return False\n", "")],
           fuzz=[], existing=["r1_pin"]),
    mutant("r1_Q8_a_node_with_no_control_plane_waits_for_ever", STAMP,
           [('        if cp_http_base_from_ws_url(getattr(settings, "topos_control_plane_url", "") or "") is None:\n'
             "            return False\n        if autopin_stamp_key(stop):\n", "        if autopin_stamp_key(stop):\n")],
           fuzz=[], existing=["r1_pin"]),
    mutant("r1_Q8_shutdown_does_not_end_the_tries", STAMP,
           [("    if stop is not None:\n        stop.set()\n", "    if stop is not None:\n        pass\n")],
           fuzz=[], existing=["r1_pin"]),
    mutant("r1_Q8_the_start_up_thread_is_never_started", STAMP,
           [("        thread.start()\n        return thread\n", "        return thread\n")],
           fuzz=[], existing=["r1_pin"]),
]
MUTANTS = MUTANTS + R1_MUTANTS


class Patcher:
    """Applies one mutant's edits in place and always restores the exact original bytes."""

    def __init__(self):
        self.originals: dict[Path, bytes] = {}
        atexit.register(self.restore_all)
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: (self.restore_all(), sys.exit(130)))

    def apply(self, spec) -> str | None:
        path = ROOT / spec["file"]
        original = path.read_bytes()
        text = original.decode("utf-8")
        for old, new in spec["edits"]:
            if text.count(old) != 1:
                return "patch_not_applicable(%d matches)" % text.count(old)
            text = text.replace(old, new)
        # A mutant that does not compile fails every test at import: that is a broken mutant, not a kill.
        try:
            compile(text, str(path), "exec")
        except SyntaxError as exc:
            return "patch_invalid_python(%s line %s)" % (type(exc).__name__, exc.lineno)
        self.originals[path] = original
        path.write_bytes(text.encode("utf-8"))
        return None

    def restore_all(self):
        for path, original in list(self.originals.items()):
            path.write_bytes(original)
            self.originals.pop(path, None)


# --- R2, the second review of the node (REVIEW_R1_NODE: one blocker, two high, eight medium) ----------------------------
# Run with `--group r2`. One planted fault for each rule the fixes added, each killed by a test that fails by name.
CARRY = "topos/features/lifecycle/contact_excludes.py"
REBUILD = "topos/features/lifecycle/blackhole_rebuild.py"
OFF_LIMITS = "topos/features/lifecycle/blackhole.py"
BOUNDARY = P + "entity_boundary.py"
SIGNAL_HANDLERS = "topos/core/handlers/signal_features.py"
SIGNAL_ROUTES = "topos/api/signal.py"
APP = "topos/app.py"
R2_MUTANTS = [
    # R-B1: the carry step protects and destroys nothing; the clean-up matches whole words
    # Since the third round a clean-up asked for on an entry that is carried and waiting does nothing
    # (blackhole_rebuild; held by r3_P2_a_clean_up_runs_on_an_entry_that_waits), so the step calling the clean-up is
    # no longer a fault by itself: it survived. The same fault now passes both guards, as the step would have to:
    # it makes the entry full and then cleans up.
    mutant("r2_B1_the_step_runs_the_clean_up_again", CARRY,
           [('            outcome = "carried"\n',
             '            outcome = "carried"\n            from .blackhole_rebuild import rebuild_for_blackhole\n'
             '            store.make_full(result["blackhole_id"])\n'
             '            rebuild_for_blackhole(conn, result["normalized_name"])\n')],
           fuzz=[], existing=["r2_carry"], note="the state before: derived text withdrawn unattended at the first start"),
    mutant("r2_B1_a_term_matches_as_a_substring_again", REBUILD,
           [('    return re.compile(rf"(?<![^\\W_])(?:{body})(?![^\\W_])")\n', '    return re.compile(rf"(?:{body})")\n')],
           fuzz=[], existing=["r2_carry"], note='"sam" inside "same", "work" inside "network"'),
    mutant("r2_B1_a_term_under_three_characters_is_searched_for", REBUILD,
           [("MIN_TERM_CHARS = 3\n", "MIN_TERM_CHARS = 1\n")], fuzz=[], existing=["r2_carry"]),
    mutant("r2_B1_an_upgrade_step_rewrites_home_chat", "topos/upgrades/runner.py",
           [("                reports = rerun_all_rebuilds(conn, home_chat=False)\n",
             "                reports = rerun_all_rebuilds(conn)\n")], fuzz=[], existing=["r2_runner"]),
    mutant("r2_B1_the_boundary_reads_only_cleaned_up_entries", BOUNDARY,
           [("            self.active = bool(flags)\n",
             '            flags = [flag for flag in flags if flag.get("rebuild_state") == "complete"]\n'
             "            self.active = bool(flags)\n")], fuzz=[], existing=["r2_pending"],
           note="a waiting entry would then withhold nothing: what leaving entries pending relies on"),
    # R-M5: handles, usernames and ids are identifiers, never names
    mutant("r2_M5_a_listed_identifier_is_read_as_a_name", BOUNDARY,
           [("            if identifiers and skeleton(value) in identifiers:\n", "            if False:\n")],
           fuzz=[], existing=["r2_identifiers"], note='the state before: "contact" and "default" are name parts'),
    mutant("r2_M5_a_list_that_cannot_be_read_is_passed_over", BOUNDARY,
           [("        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):\n"
             "            raise PolicyError(UNAVAILABLE)\n        return set(filter(None, map(skeleton, values)))\n",
             "        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):\n"
             "            return set()\n        return set(filter(None, map(skeleton, values)))\n")],
           fuzz=[], existing=["r2_identifiers"]),
    mutant("r2_M5_a_name_can_be_listed_as_an_identifier", OFF_LIMITS,
           [("        return (was | set(identifiers)) - kept_names\n", "        return was | set(identifiers)\n")],
           fuzz=[], existing=["r2_identifiers"], note="the one way the list could loosen a real name"),
    mutant("r2_M5_the_step_writes_identifiers_as_names", CARRY,
           [('            result = store.blackhole_entity(entity_ref=entry["entity_ref"], note=NOTE, aliases=entry["names"],\n'
             '                                            identifiers=entry["identifiers"], carried=True)\n',
             '            result = store.blackhole_entity(entity_ref=entry["entity_ref"], note=NOTE,\n'
             '                                            aliases=[*entry["names"], *entry["identifiers"]], carried=True)\n')],
           fuzz=[], existing=["r2_carry"]),
    # R-M6
    mutant("r2_M6_the_owners_own_card_is_carried", CARRY,
           [('        if contact.get("is_self"):\n            out["own_card_skipped"] += 1\n            continue\n', "")],
           fuzz=[], existing=["r2_carry"]),
    # R-H2, R-L5, R-L4
    mutant("r2_H2_a_contact_is_named_by_a_saved_name_of_symbols_alone", CARRY,
           [('    elif _can_name(identity["display"]):\n', '    elif identity["display"]:\n')],
           fuzz=[], existing=["r2_carry"]),
    mutant("r2_H2_one_contact_that_fails_stops_the_rest", CARRY,
           [("        except Exception as exc:  # noqa: BLE001 -- one contact must not stop the others; counted, tried again next start\n",
             "        except ZeroDivisionError as exc:  # noqa: BLE001 -- one contact must not stop the others; counted, tried again next start\n")],
           fuzz=[], existing=["r2_carry"]),
    mutant("r2_H2_a_run_with_failures_is_ledgered_done", CARRY,
           [('    if out["failed"]:\n        raise CarryIncomplete(\n', "    if False:\n        raise CarryIncomplete(\n")],
           fuzz=[], existing=["r2_carry"], note="the contacts that failed would then never be tried again"),
    mutant("r2_L5_a_second_run_puts_back_what_the_owner_removed", CARRY,
           [('        if contact_id in remembered:\n            out["carried_before"] += 1\n            continue\n', "")],
           fuzz=[], existing=["r2_carry"]),
    mutant("r2_L4_an_existing_entry_is_written_as_a_new_one", CARRY,
           [("        if found is None:\n", "        if True:\n")], fuzz=[], existing=["r2_carry"],
           note="the owner's tier is reset and their note replaced, and the entry does not wait again"),
    # Since the third round (ruling P; R-L4 as ruled there) the entry is not put back to `pending`: what it gains is
    # marked carried and waiting. The same fault against that rule: what it gains is not marked.
    mutant("r2_L4_an_entry_that_gained_names_does_not_wait_again", OFF_LIMITS,
           [('                                terms=set(record["carried_waiting_aliases"]) | gained,\n',
             '                                terms=set(record["carried_waiting_aliases"]),\n')],
           fuzz=[], existing=["r2_identifiers"]),
    # the notice and the owner's start
    # Since the third round the step writes ONE notice for all it carried (R2-H3), not one per entry. The same
    # fault against that rule: the step's own words are not written.
    mutant("r2_notice_a_carried_entry_says_the_stores_own_words", CARRY,
           [('        if out["waiting"] and (out["carried"] or out["added_to_existing"] or not told_before):\n',
             '        if False:\n')], fuzz=[], existing=["r2_carry"]),
    mutant("r2_start_marking_a_waiting_entry_again_runs_nothing", SIGNAL_HANDLERS,
           [('        if not result.get("already_blackholed") or result.get("rebuild_state") != "complete":\n',
             '        if not result.get("already_blackholed"):\n')], fuzz=[], existing=["r2_start"],
           note="the state before: no way to start a waiting clean-up"),
    mutant("r2_start_starting_a_clean_up_loosens_the_tier", OFF_LIMITS,
           [('    tier = stricter_tier(processing_tier, waiting["processing_tier"])\n', "    tier = processing_tier\n")],
           fuzz=[], existing=["r2_start"],
           note="the control plane sends the default tier with every mark: a stricter entry is reset by starting its "
                "clean-up"),
    mutant("r2_start_the_local_route_runs_nothing_for_a_waiting_entry", SIGNAL_ROUTES,
           [('            if not result.get("already_blackholed") or result.get("rebuild_state") != "complete":\n',
             '            if not result.get("already_blackholed"):\n')], fuzz=[], existing=["r2_start"]),
    # R-H1 and R-M1: the narrowing, on a bound node
    mutant("r2_M1_an_owner_side_stamp_naming_another_user_is_not_compared", DISPATCHER,
           [("        if not acting or not switches.is_bound():\n            return None\n", "        return None\n")],
           fuzz=[], existing=["r2_narrowing"], note="the state before: owner_app for another user reaches every type"),
    mutant("r2_M1_the_comparison_runs_on_an_unbound_node", DISPATCHER,
           [("        if not acting or not switches.is_bound():\n", "        if not acting:\n")],
           fuzz=[], existing=["r2_narrowing"]),
    mutant("r2_M1_a_stamp_that_names_nobody_is_refused", DISPATCHER,
           [("        if not acting or not switches.is_bound():\n", "        if not switches.is_bound():\n")],
           fuzz=[], existing=["r2_narrowing"], note="every owner's routines would stop"),
    mutant("r2_M1_an_unreadable_bound_identity_passes_any_named_user", DISPATCHER,
           [("        if acting == _bound_owner_id():           # None when the identity cannot be read: never the owner\n",
             "        if _bound_owner_id() in (acting, None):\n")], fuzz=[], existing=["r2_narrowing"]),
    mutant("r2_H1_an_unstamped_frame_naming_another_user_is_served", DISPATCHER,
           [("            refusal = _unstamped_naming_refusal(message)\n            if refusal is not None:\n"
             "                return refusal\n", "")], fuzz=[], existing=["r2_narrowing"],
           note="the state before: 195 of 287 types"),
    mutant("r2_H1_the_unstamped_rule_runs_on_an_unbound_node", DISPATCHER,
           [("    if not named or not switches.is_bound():\n", "    if not named:\n")],
           fuzz=[], existing=["r2_narrowing"]),
    mutant("r2_H1_an_exception_joins_the_list", DISPATCHER,
           [('    "connection_info": None,\n}', '    "connection_info": None,\n    "query": None,\n}')],
           fuzz=[], existing=["r2_narrowing"]),
    mutant("r2_H1_the_inbox_exception_covers_the_owner_field_too", DISPATCHER,
           [('    "app_ingest": frozenset({("payload", "requesting_user_id")}),\n', '    "app_ingest": None,\n')],
           fuzz=[], existing=["r2_narrowing"]),
    mutant("r2_H1_one_field_naming_the_owner_passes_the_frame", DISPATCHER,
           [("    if owner is not None and all(value == owner for value in named):\n",
             "    if owner is not None and any(value == owner for value in named):\n")],
           fuzz=[], existing=["r2_narrowing"]),
    # R-M2 and R-M3: the stamp's window, the cause, the pin
    mutant("r2_M2_the_window_is_one_minute_again", STAMP, [("SKEW_S = 300\n", "SKEW_S = 60\n")],
           fuzz=[], existing=["r2_stamp"]),
    mutant("r2_M2_a_stamp_past_its_own_end_is_refused_at_once", STAMP,
           [("    if not iat - SKEW_S <= now <= exp + SKEW_S:\n", "    if not iat - SKEW_S <= now <= exp:\n")],
           fuzz=[], existing=["r2_stamp"]),
    mutant("r2_M2_a_stamp_of_another_key_out_of_time_reads_as_a_clock", STAMP,
           [("        return None, WRONG_KEY, None\n",
             "        return None, (WRONG_KEY if iat - SKEW_S <= _now() <= exp + SKEW_S else WRONG_CLOCK), None\n")],
           fuzz=[], existing=["r2_stamp"], note="the times read before the signature"),
    mutant("r2_M2_a_time_that_is_not_a_number_is_compared", STAMP,
           [("    if len(sig) != _SIGNATURE_BYTES or not exp or not math.isfinite(iat) or not math.isfinite(exp):\n",
             "    if len(sig) != _SIGNATURE_BYTES or not exp:\n")], fuzz=[], existing=["r2_stamp"]),
    mutant("r2_M2_the_log_has_one_text_for_a_clock_and_a_key", DISPATCHER,
           [("    elif reason == relay_stamp.WRONG_CLOCK:\n", "    elif False:\n")], fuzz=[], existing=["r2_stamp"]),
    mutant("r2_M2_the_bind_is_not_told_why", DISPATCHER,
           [("                cause = STAMP_CAUSES.get(reason, STAMP_CAUSE_OTHER)\n", "                cause = None\n")],
           fuzz=[], existing=["r2_stamp"]),
    mutant("r2_M2_every_types_refusal_says_why", DISPATCHER,
           [('            if str(message.get("type") or "").strip().lower() == _BIND_TYPE:\n', "            if True:\n")],
           fuzz=[], existing=["r2_stamp"], note="the one shape for every other refused type"),
    mutant("r2_M3_anything_that_decodes_is_a_key", STAMP,
           [("    return raw if len(raw) == _KEY_BYTES else None\n", "    return raw\n")],
           fuzz=[], existing=["r2_stamp"], note="the state before: an empty pin file is a key for ever"),
    mutant("r2_M3_the_first_pin_replaces_whatever_is_there", STAMP,
           [("            os.link(beside, path)\n", "            os.replace(beside, path)\n")],
           fuzz=[], existing=["r2_stamp"], note="neither exclusive nor safe against a death in the middle"),
    # R-M4, R-L8
    mutant("r2_M4_the_move_takes_every_file_named_like_the_store", P + "self_bind.py",
           [("    return [durable / name for name in REVIEW_STORE_FILES if os.path.lexists(durable / name)]\n",
             "    return sorted(path for path in durable.iterdir() if path.name.startswith(REVIEW_STORE_FILES[0]))\n")],
           fuzz=[], existing=["r1_load"], note="the state before: a copy the owner renamed aside is moved, then deleted"),
    mutant("r2_L8_the_newest_leftovers_are_decided_by_name", P + "self_bind.py",
           [("    found.sort(key=lambda path: (path.stat().st_mtime_ns, path.name))\n", "    found.sort()\n")],
           fuzz=[], existing=["r1_load"]),
    # R-M8: the wiring
    mutant("r2_M8_the_app_hands_frames_straight_to_the_handlers", APP,
           [("            from .core.handlers import dispatch_relay_message\n\n"
             "            return await dispatch_relay_message(message)\n",
             "            from .principal import RELAY_PRINCIPAL\n\n"
             "            return await handle_control_plane_request(message, principal=RELAY_PRINCIPAL)\n")],
           fuzz=[], existing=["r2_wiring"], note="the reviewer's mutant: both relay rules unhooked, 388 nearby tests green"),
    mutant("r2_M8_the_pin_thread_is_never_started", APP, [("        start_first_pin()\n", "        pass\n")],
           fuzz=[], existing=["r2_wiring"]),
    mutant("r2_M8_shutdown_leaves_the_pin_thread_waiting", APP, [("    stop_first_pin()\n", "    pass\n")],
           fuzz=[], existing=["r2_wiring"]),
    # The other direction and the edges: faults under which the tests that say "and nothing else changes" fail.
    # (Each new test was run under every fault above without stopping; these are for the ones none of them failed.)
    mutant("r2_M5_every_value_of_an_entry_with_a_list_is_read_as_a_handle", BOUNDARY,
           [("            if identifiers and skeleton(value) in identifiers:\n", "            if identifiers:\n")],
           fuzz=[], existing=["r2_identifiers"], note="a real name loosened: its parts and forms stop withholding"),
    mutant("r2_M5_an_alias_shaped_like_a_handle_is_read_as_one", BOUNDARY,
           [("            if identifiers and skeleton(value) in identifiers:\n",
             '            if (identifiers and skeleton(value) in identifiers) or " " not in value.strip():\n')],
           fuzz=[], existing=["r2_identifiers"], note="the rejected design: an identifier told by its shape"),
    mutant("r2_M5_a_listed_identifier_no_longer_reaches_its_contact", BOUNDARY,
           [("                        self.terms.update(self._handle(spelling))\n",
             "                        self._handle(spelling)\n")], fuzz=[], existing=["r2_identifiers"],
           note="under-protection: the older exclude's reach, the contact and its threads, is lost"),
    mutant("r2_M5_a_hand_made_entry_gets_the_column", OFF_LIMITS,
           [("        if not marked and not was:\n            return\n", "")], fuzz=[], existing=["r2_identifiers"]),
    mutant("r2_L4_adding_names_resets_the_owners_tier", OFF_LIMITS,
           [("                \"UPDATE entity_blackholes SET aliases_json=?, updated_at=datetime('now') WHERE blackhole_id=?\",\n"
             '                (json.dumps(sorted(merged)), record["blackhole_id"]),\n',
             "                \"UPDATE entity_blackholes SET processing_tier='secure', aliases_json=?, \"\n"
             "                \"updated_at=datetime('now') WHERE blackhole_id=?\",\n"
             '                (json.dumps(sorted(merged)), record["blackhole_id"]),\n')], fuzz=[], existing=["r2_identifiers"]),
    mutant("r2_L4_an_entry_that_gains_nothing_is_written_all_the_same", OFF_LIMITS,
           [('        if merged == set(record["aliases"]) and marked == set(record["identifier_aliases"]):\n',
             "        if False:\n")], fuzz=[], existing=["r2_identifiers"],
           note="every such write moves the protection clock and drops every share index"),
    mutant("r2_notice_the_store_ignores_the_words_it_is_given", OFF_LIMITS,
           [("                    message=notice or (\n                        f\"'{canonical_name or ref}' is now off-limits.",
             "                    message=(\n                        f\"'{canonical_name or ref}' is now off-limits.")],
           fuzz=[], existing=["r2_identifiers"]),
    mutant("r2_L5_a_dry_run_makes_the_memory_table", CARRY,
           [('    if not dry_run and found["excludes"]:\n', '    if found["excludes"]:\n')],
           fuzz=[], existing=["r2_carry"]),
    mutant("r2_start_a_finished_entry_is_cleaned_up_again", SIGNAL_HANDLERS,
           [('        if not result.get("already_blackholed") or result.get("rebuild_state") != "complete":\n',
             "        if True:\n")], fuzz=[], existing=["r2_start"]),
    mutant("r2_M1_the_owners_own_stamp_is_refused_on_a_bound_node", DISPATCHER,
           [("        if acting == _bound_owner_id():           # None when the identity cannot be read: never the owner\n",
             "        if False:\n")], fuzz=[], existing=["r2_narrowing"], note="the rule over-refusing: the owner's app"),
    mutant("r2_H1_an_unstamped_frame_naming_the_owner_is_refused", DISPATCHER,
           [("    if owner is not None and all(value == owner for value in named):\n        return None\n", "")],
           fuzz=[], existing=["r2_narrowing"], note="the rule over-refusing: every owner route that names its caller"),
    mutant("r2_H1_anything_in_an_identity_field_names_a_user", DISPATCHER,
           [("        if isinstance(value, str) and value.strip():\n", "        if value:\n")],
           fuzz=[], existing=["r2_narrowing"]),
    mutant("r2_H1_the_handshake_is_compared", DISPATCHER,
           [('    "connection_info": None,\n}', "}")], fuzz=[], existing=["r2_narrowing"]),
    mutant("r2_M2_a_stamp_may_live_as_long_as_it_says", STAMP,
           [("    if exp - iat > MAX_LIFETIME_S:\n        return None, MALFORMED, None\n", "")],
           fuzz=[], existing=["r2_stamp"]),
    mutant("r2_M2_a_node_with_no_key_blames_the_key", STAMP,
           [("        return None, NO_KEY, None\n", "        return None, WRONG_KEY, None\n")],
           fuzz=[], existing=["r2_stamp"]),
]
MUTANTS = MUTANTS + R2_MUTANTS


# --- R3: the third fix round (the re-check of REVIEW_R1_NODE; WS0's rulings P and M) --------------------------------
VIEW = "topos/features/lifecycle/off_limits_view.py"
GUARD = "topos/features/lifecycle/blackhole_guard.py"
GATE = "topos/features/lifecycle/blackhole_llm.py"
LISTING = "topos/features/lifecycle/off_limits_list.py"
RETRIEVAL = "topos/query/retrieval.py"
RUNNER = "topos/upgrades/runner.py"
MANIFEST = "topos/upgrades/manifests.json"
RUNTIME = P + "runtime.py"
SELF_BIND = P + "self_bind.py"
R3_MUTANTS = [
    # --- P.2: a waiting carried entry feeds an owner path again ---------------------------------------------------
    mutant("r3_P2_the_step_writes_ordinary_entries", CARRY,
           [('                                            identifiers=entry["identifiers"], carried=True)\n',
             '                                            identifiers=entry["identifiers"])\n')],
           fuzz=[], existing=["r3_waits"], note="the state before: every reader in the node sees the carried entry"),
    mutant("r3_P2_the_owner_view_reads_what_waits", OFF_LIMITS,
           [('            if record["carried_waiting"]:\n                continue\n'
             '            waiting = set(record["carried_waiting_aliases"]) if view == OWNER else set()\n',
             '            waiting = set(record["carried_waiting_aliases"]) if view == OWNER else set()\n')],
           fuzz=[], existing=["r3_waits"]),
    mutant("r3_P2_the_owner_view_keeps_the_names_the_step_added", OFF_LIMITS,
           [('                          "aliases": [alias for alias in record["aliases"] if alias not in waiting],\n',
             '                          "aliases": list(record["aliases"]),\n')],
           fuzz=[], existing=["r3_waits"], note="R-L4: what an owner-made entry gains is read by his own tools at once"),
    mutant("r3_P2_the_query_exit_reads_every_entry", RETRIEVAL,
           [("    from ..features.lifecycle.off_limits_view import for_request\n\n    return for_request()\n",
             '    return "everyone"\n')],
           fuzz=[], existing=["r3_owner"], note="R2-H1: the outside client loses its query items; his app gets them stamped"),
    mutant("r3_P2_the_derived_mode_floor_reads_every_entry", RETRIEVAL,
           [("    guard = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE, view=_off_limits_view())\n"
             "    if not guard.active:\n",
             "    guard = BlackholeGuard(conn, caller_class=CallerClass.GRANTEE)\n    if not guard.active:\n")],
           fuzz=[], existing=["r3_owner"],
           note="what the re-check did not measure: one carried entry empties every summary-mode query"),
    mutant("r3_P2_the_model_gate_reads_every_entry", GATE,
           [("    return BlackholeStore(conn).list(view=for_own_processing())\n", "    return BlackholeStore(conn).list()\n")],
           fuzz=[], existing=["r3_owner"], note="R2-H1: the owner's model calls are moved or blocked"),
    mutant("r3_P2_the_routing_status_counts_what_waits", SIGNAL_HANDLERS,
           [("        view = for_own_processing()\n        store = BlackholeStore(hub.get_db_connection())\n",
             '        view = "everyone"\n        store = BlackholeStore(hub.get_db_connection())\n')],
           fuzz=[], existing=["r3_owner"]),
    mutant("r3_P2_the_guard_reads_every_entry_for_the_owners_client", GUARD,
           [("        if caller_class in (CallerClass.OWNER_UI, CallerClass.OWNER_AGENT):\n            return OWNER\n",
             "        if caller_class == CallerClass.OWNER_UI:\n            return OWNER\n")],
           fuzz=[], existing=["r3_owner"], note="R2-H3: every summary withheld from the outside client while an entry waits"),
    mutant("r3_P2_the_row_filter_builds_the_shares_boundary_for_the_owners_client", GUARD,
           [("                boundary = EntityBoundary(conn, waiting=self._view == EVERYONE)\n",
             "                boundary = EntityBoundary(conn)\n")], fuzz=[], existing=["r3_owner"]),
    mutant("r3_P2_the_labeler_reads_every_entry", "topos/features/signal/topic_clustering.py",
           [("        return off_limits_terms(conn, view=for_own_processing())\n", "        return off_limits_terms(conn)\n")],
           fuzz=[], existing=["r3_owner"]),
    mutant("r3_P2_the_graph_fingerprint_moves_with_a_waiting_entry", "topos/features/entities/graph_inputs.py",
           [('            if isinstance(mark, dict) and mark.get("whole") is True:\n                continue\n            yield row\n',
             "            yield row\n")], fuzz=[], existing=["r3_owner"], note="a graph rebuild on every node that carried someone"),
    mutant("r3_P2_a_clean_up_runs_on_an_entry_that_waits", REBUILD,
           [('    if record["carried_waiting"]:\n', "    if False:\n")], fuzz=[], existing=["r3_waits"],
           note="R2-N4: any caller of the rebuild cleans up unattended"),
    # --- P.3: the owner's act ----------------------------------------------------------------------------------------
    mutant("r3_P3_the_owners_mark_leaves_the_entry_waiting", OFF_LIMITS,
           [("        store.make_full(waiting[\"blackhole_id\"])\n        waiting = store.get(entity_ref)\n", "        pass\n")],
           fuzz=[], existing=["r3_waits"], note="a carried entry the owner made fully Off-limits hides nothing from his tools"),
    mutant("r3_P3_a_mark_by_name_leaves_the_entry_waiting", OFF_LIMITS,
           [("                if not carried and has_waiting(existing):\n", "                if False:\n")],
           fuzz=[], existing=["r3_waits"]),
    # --- P.1: a path toward other people takes the owner's view --------------------------------------------------------
    mutant("r3_P1_the_share_boundary_skips_what_waits", BOUNDARY,
           [("    def __init__(self, conn, *, waiting=True):\n", "    def __init__(self, conn, *, waiting=False):\n")],
           fuzz=[], existing=["r3_at_the_doors"], note="every door case releases the carried person again"),
    mutant("r3_P1_a_relayed_third_party_is_taken_for_the_owner", VIEW,
           [("    return bool(acting) and owner is not None and acting == owner\n", "    return bool(acting)\n")],
           fuzz=[], existing=["r3_outward"], note="a recipient, and a node that cannot say who its owner is"),
    mutant("r3_P1_a_caller_the_node_cannot_place_reads_the_owners_view", VIEW,
           [("    return OWNER if is_owner_himself(principal) else EVERYONE\n", "    return OWNER\n")],
           fuzz=[], existing=["r3_outward"], note="a frame with no stamp, a request with no principal"),
    mutant("r3_P1_the_routine_lane_reads_the_owners_view", VIEW,
           [("ROUTINE_LANE = EVERYONE\n", "ROUTINE_LANE = OWNER\n")], fuzz=[], existing=["r3_outward"],
           note="a carried person in routine mail addressed to other people; WS0's decision, not a fix"),
    mutant("r3_P1_the_guards_default_class_reads_the_owners_view", GUARD,
           [("        if caller_class == CallerClass.ROUTINE:\n            return ROUTINE_LANE\n        return EVERYONE\n",
             "        if caller_class == CallerClass.ROUTINE:\n            return ROUTINE_LANE\n        return OWNER\n")],
           fuzz=[], existing=["r3_outward"], note="a grantee, a plugin, a caller the node cannot place, the inspection floor"),
    mutant("r3_P1_the_gate_reads_the_owners_view_for_a_recipient", VIEW,
           [("    return EVERYONE if is_another_person(principal) else OWNER\n", "    return OWNER\n")],
           fuzz=[], existing=["r3_outward"]),
    # --- M: identifiers match only as themselves, and never against a key ------------------------------------------
    mutant("r3_M_a_bare_word_identifier_is_found_anywhere_at_read_time", OFF_LIMITS,
           [("        self._anywhere = frozenset(term for term in self.identifiers\n"
             "                                   if \"@\" in term or any(ch.isdigit() for ch in term) or _in_a_run(term))\n",
             "        self._anywhere = frozenset(self.identifiers)\n")], fuzz=[], existing=["r3_read_time"],
           note='the state before: the username "al" in "also", the handle "work" in "network"'),
    mutant("r3_M_an_identifier_is_looked_for_in_keys_at_read_time", RETRIEVAL,
           [("            hit = bool(blob) and terms.found_in(blob, values=normalize_entity_name(_values_text(item)))\n",
             "            hit = bool(blob) and terms.found_in(blob)\n")], fuzz=[], existing=["r3_read_time"],
           note='the key `retrieval_source` of every item holds "al"'),
    mutant("r3_M_a_bare_word_identifier_is_found_anywhere_at_the_boundary", BOUNDARY,
           [("        whole = frozenset(identifiers & self.whole_identifiers)\n", "        whole = frozenset()\n")],
           fuzz=[], existing=["r3_boundary"], note='R2-M3: the handle "work" withholds "network" from every share'),
    mutant("r3_M_an_identifier_is_read_as_a_name_at_the_boundary", BOUNDARY,
           [("        return frozenset(self.terms - identifiers), frozenset(identifiers - whole), whole\n",
             "        return frozenset(self.terms), frozenset(), frozenset()\n")], fuzz=[], existing=["r3_boundary"],
           note="the reading of 841e4706: forms and keys too"),
    mutant("r3_M_an_identifier_is_looked_for_in_keys_at_the_boundary", BOUNDARY,
           [("                   for _key, texts in keyed_surfaces(row, keys=False) for text in texts)\n",
             "                   for _key, texts in keyed_surfaces(row) for text in texts)\n")],
           fuzz=[], existing=["r3_boundary"]),
    mutant("r3_M_a_name_listed_as_a_handle_stops_being_a_name_at_the_boundary", BOUNDARY,
           [("        identifiers = (self.identifier_terms & self.terms) - self.name_terms\n",
             "        identifiers = self.identifier_terms & self.terms\n")], fuzz=[], existing=["r3_boundary"],
           note="the one way the rule could narrow a real name"),
    # --- R2-H2: the keyless handle, and nothing else ----------------------------------------------------------------
    mutant("r3_H2_a_keyless_handle_refuses_the_whole_boundary_again", BOUNDARY,
           [("                        if isinstance(handle, str) and not skeleton(handle):\n                            continue\n", "")],
           fuzz=[], existing=["r3_boundary"], note="the state before: every share on the node off"),
    mutant("r3_H2_the_pass_is_widened_to_every_value_the_boundary_cannot_key", BOUNDARY,
           [("        keys = _handle_keys(value)\n        if not keys:\n            raise PolicyError(UNAVAILABLE)\n        self.handles.update(keys)\n",
             "        keys = _handle_keys(value)\n        if not keys:\n            return set()\n        self.handles.update(keys)\n")],
           fuzz=[], existing=["r3_boundary"], note="a linked entity's unkeyable identifier, a handle that is not text"),
    mutant("r3_H2_the_step_does_not_build_the_boundary", CARRY,
           [('        out["boundary"] = _boundary_state(conn)\n', '        out["boundary"] = "built"\n')],
           fuzz=[], existing=["r3_step"]),
    mutant("r3_H2_a_boundary_that_refuses_is_ledgered_done", CARRY,
           [('    if out["boundary"] not in ("built", "not_built"):\n        found = out.get("unreadable") or {}\n',
             '    if False:\n        found = out.get("unreadable") or {}\n')], fuzz=[], existing=["r3_step"]),
    # --- R2-M2: the step first, and the hold -------------------------------------------------------------------------
    mutant("r3_M2_the_step_runs_in_declaring_order", RUNNER,
           [("    planned.sort(key=lambda step: 0 if runs_first(step) else 1)\n", "")],
           fuzz=[], existing=["r3_first"], note="a node from 1.3.x has every older step queued ahead of it"),
    mutant("r3_M2_the_manifest_does_not_declare_the_step_first", MANIFEST,
           [('          "runs_first": true\n', '          "runs_first": false\n')], fuzz=[], existing=["r3_first"]),
    mutant("r3_M2_start_up_waits_for_the_app_before_the_step", RUNNER,
           [("                run_pending_upgrades(conn, stop_event=stop_event, only_first=True)\n", "                pass\n")],
           fuzz=[], existing=["r3_first"]),
    mutant("r3_M2_a_search_read_does_not_wait_for_the_step", RUNTIME,
           [('        """A fresh adapter over the one index service; request payloads never select anything here."""\n'
             "        self.hold_for_the_exclude_carry()\n",
             '        """A fresh adapter over the one index service; request payloads never select anything here."""\n')],
           fuzz=[], existing=["r3_first"]),
    mutant("r3_M2_an_answer_read_does_not_wait_for_the_step", RUNTIME,
           [('        """One process-local answer queue for every share on this node."""\n'
             "        self.hold_for_the_exclude_carry()\n",
             '        """One process-local answer queue for every share on this node."""\n')],
           fuzz=[], existing=["r3_first"]),
    mutant("r3_M2_a_new_bind_does_not_wait_for_the_step", SELF_BIND,
           [("    _hold_for_the_exclude_carry(served)                              # 9a: nothing is written before this either\n", "")],
           fuzz=[], existing=["r3_bind"]),
    mutant("r3_M2_a_bound_node_that_holds_is_vouched_for", SELF_BIND,
           [("        runtime.hold_for_the_exclude_carry()\n", "")], fuzz=[], existing=["r3_bind"]),
    mutant("r3_M2_a_node_with_nobody_excluded_is_held", CARRY,
           [("        if not uncarried(conn):\n            return None\n", "")], fuzz=[], existing=["r3_first"],
           note="a node with its runner off and nothing to carry never shares"),
    # Since the fifth round two lines hold this rule, each enough alone: the read that raises first, and the read
    # of the contacts table, which `owed` no longer returns before (it stopped at a plan without the step, and a
    # database that cannot be read plans as a fresh install). The fault takes both away; with only the first gone
    # it is equivalent, and the round's run found it alive.
    mutant("r3_M2_a_database_that_cannot_be_read_now_plans_as_a_fresh_install", CARRY,
           [('        conn.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()\n', ""),
            ('        if "no such table: contacts" in str(exc).lower():\n            return 0\n        raise\n',
             "        return 0\n")],
           fuzz=[], existing=["r3_first"], note="a locked database reads as nothing owed, remembered for half a minute"),
    mutant("r3_M2_start_up_runs_a_first_pass_for_every_plan", RUNNER,
           [('            if any(runs_first(step) for step in plan["steps"]):\n', "            if True:\n")],
           fuzz=[], existing=["r3_first"], note="a stop during the wait no longer leaves the database alone"),
    mutant("r3_M2_what_cannot_be_read_does_not_hold", CARRY,
           [("    except Exception:  # noqa: BLE001 -- unreadable: hold, and say failed\n        return FAILED\n",
             "    except Exception:  # noqa: BLE001 -- unreadable: hold, and say failed\n        return None\n")],
           fuzz=[], existing=["r3_first"]),
    # --- R2-M1: scripts written without spaces ----------------------------------------------------------------------
    mutant("r3_M1_a_name_in_an_unspaced_script_needs_a_word_boundary", REBUILD,
           [("    return bool(letters) and all(UNSPACED.match(ch) for ch in letters)\n", "    return False\n")],
           fuzz=[], existing=["r3_unspaced"], note="the state before: a two-character Han name is never found"),
    mutant("r3_M1_the_unspaced_floor_is_one_character", REBUILD,
           [("MIN_UNSPACED_TERM_CHARS = 2\n", "MIN_UNSPACED_TERM_CHARS = 1\n")], fuzz=[], existing=["r3_unspaced"]),
    # --- R2-L1 and R2-L5: the relay, the bind's enrollment, the purge (the re-check's own faults) ---------------------
    mutant("r3_L1_a_stamp_that_names_nobody_is_not_read_with_its_payload", DISPATCHER,
           [('    elif principal.cls != THIRD_PARTY and not (principal.acting_user or ""):\n', "    elif False:\n")],
           fuzz=[], existing=["r3_relay"]),
    mutant("r3_L5_the_stamp_rules_reach_local_third_parties", DISPATCHER,
           [('    if getattr(principal, "channel", None) != "cp_relay" or cls == CP_RELAY:   # not the relay\'s, or no stamp at all\n',
             "    if cls == CP_RELAY:\n")], fuzz=[], existing=["r3_relay"],
           note="the re-check's own2_the_stamp_rules_reach_local_third_parties, which survived 100 tests"),
    mutant("r3_L5_the_enrollment_may_name_another_store_path", SELF_BIND,
           [("        if node_id is None or enrolled.review_store_path != str(store) or enrolled.binding != EvidenceBinding.parse({\n",
             "        if node_id is None or enrolled.binding != EvidenceBinding.parse({\n")],
           fuzz=[], existing=["r3_bind_load"], note="the re-check's own2_the_enrollment_may_name_another_store_path (102 green)"),
    mutant("r3_L5_added_names_do_not_drop_the_search_indexes", OFF_LIMITS,
           [("            commit_connection(self._conn)\n            _purge_message_search(self._conn)\n"
             '        return {**(self.get(entity_ref) or {}), "grew": True, "notification_id": None}\n',
             "            commit_connection(self._conn)\n"
             '        return {**(self.get(entity_ref) or {}), "grew": True, "notification_id": None}\n')],
           fuzz=[], existing=["r3_store"], note="the re-check's own2_added_names_do_not_drop_the_search_indexes (995 green)"),
    # --- R2-H3, R2-L3, R2-L4: the doors -----------------------------------------------------------------------------
    mutant("r3_H3_an_entrys_own_id_is_read_as_a_name", OFF_LIMITS,
           [("        if ENTRY_ID.match(ref):\n            by_id = self.get(ref)\n", "        if False:\n            by_id = self.get(ref)\n")],
           fuzz=[], existing=["r3_doors"], note="the entry is renamed to its id, and an id that is no entry makes one"),
    mutant("r3_H3_an_entry_is_shown_under_its_stored_name", LISTING,
           [("    return NAMELESS if not label or _is_contact_id(label) else label\n", "    return own or NAMELESS\n")],
           fuzz=[], existing=["r3_doors"], note="a contact id as the entry's name in the owner's list"),
    mutant("r3_H3_the_contact_id_is_listed_among_the_identifiers", LISTING,
           [("    return {term for term in terms if term not in contact_ids and not _is_contact_id(term)}\n",
             "    return set(terms)\n")], fuzz=[], existing=["r3_doors"]),
    mutant("r3_L3_the_preview_writes", REBUILD,
           [('            "goals": _withdraw_goals(conn, terms, dry=True),\n', '            "goals": _withdraw_goals(conn, terms),\n')],
           fuzz=[], existing=["r3_doors"], note="a dry run that deletes"),
    mutant("r3_L3_the_preview_blanks_chat_turns", REBUILD,
           [("    sessions = _withdraw_home_chat_sessions(conn, terms, dry=True, counts=chat)\n",
             "    sessions = _withdraw_home_chat_sessions(conn, terms, counts=chat)\n")], fuzz=[], existing=["r3_doors"]),
    mutant("r3_L4_a_clean_up_that_missed_names_says_fully_hidden", OFF_LIMITS,
           [('                    if too_short else\n', '                    if False else\n')], fuzz=[], existing=["r3_doors"]),
]
MUTANTS = MUTANTS + R3_MUTANTS


# --- The fourth round (R4N): the routine lane, two own fixes, names written without spaces ------------------------
# Run with `--group r4`. WS0's four for the routine lane come first (the floor tripped again by a waiting entry; the
# item filter skipped; the matcher widened back to a substring; the rule given to a caller that is not a verified
# routine), then one fault for each other thing a test of this round holds.
TABLES = "topos/core/handlers/database_explorer.py"
MESSAGES = "topos/core/handlers/messages.py"
R4_MUTANTS = [
    # ----- the routine lane: the floor
    mutant("r4_Q1_a_carried_entry_trips_the_derived_mode_floor_again", RETRIEVAL,
           [("        return _carried_items(conn) is None or guard.active_apart_from_what_is_carried()\n",
             "        return True\n")],
           fuzz=[], existing=["r4_routine"], note="one carried contact empties every routine's summary-mode query"),
    mutant("r4_Q1_the_floor_stays_open_beside_an_entry_the_owner_made", RETRIEVAL,
           [("        return _carried_items(conn) is None or guard.active_apart_from_what_is_carried()\n",
             "        return _carried_items(conn) is None\n")],
           fuzz=[], existing=["r4_routine"], note="a full entry no longer closes the derived modes to a routine"),
    mutant("r4_Q1_a_rule_that_cannot_be_built_opens_the_floor", RETRIEVAL,
           [("    except Exception:  # noqa: BLE001 -- what cannot be applied item by item closes the modes, as before\n"
             "        return True\n",
             "    except Exception:  # noqa: BLE001 -- what cannot be applied item by item closes the modes, as before\n"
             "        return False\n")], fuzz=[], existing=["r4_routine"]),
    mutant("r4_Q1_the_whole_list_view_takes_the_upgrades_additions_off_his_own_entry", OFF_LIMITS,
           [('            waiting = set(record["carried_waiting_aliases"]) if view == OWNER else set()\n',
             '            waiting = set(record["carried_waiting_aliases"])\n')],
           fuzz=[], existing=["r4_routine"], note="names the step added to an owner-made entry leave every whole-list rule"),
    # ----- the routine lane: the item filter
    mutant("r4_Q1_the_exit_filter_skips_the_item_rule", RETRIEVAL,
           [("            hit = carried.names(item)\n", "            hit = False\n")],
           fuzz=[], existing=["r4_routine"], note="the item that names a carried person is released to a routine"),
    mutant("r4_Q1_the_cluster_filter_skips_the_item_rule", RETRIEVAL,
           [("            hit = carried.names(cluster)\n", "            hit = False\n")], fuzz=[], existing=["r4_routine"]),
    mutant("r4_Q1_the_roster_skips_the_item_rule", RETRIEVAL,
           [('        if carried is not None and carried.names([entity_id or "", label, identifier]):\n',
             "        if False:\n")], fuzz=[], existing=["r4_routine"]),
    mutant("r4_Q1_the_packet_is_not_walked", RETRIEVAL,
           [("            packet = carried.withhold_from(packet, text=False)\n", "            pass\n")],
           fuzz=[], existing=["r4_routine"], note="inference scores and the lanes the exit filter never sees"),
    mutant("r4_Q1_an_item_that_cannot_be_judged_is_kept", GUARD,
           [("        except Exception:  # noqa: BLE001 -- an item that cannot be judged is withheld\n            return True\n",
             "        except Exception:  # noqa: BLE001 -- an item that cannot be judged is withheld\n            return False\n")],
           fuzz=[], existing=["r4_routine"]),
    mutant("r4_Q1_the_item_rule_is_built_over_every_entry", BOUNDARY,
           [("                flags = [flag for flag in flags if self._waits_whole(flag)]\n", "                pass\n")],
           fuzz=[], existing=["r4_answers"], note="an owner-made entry read the boundary's way on the routine lane"),
    # ----- the routine lane: the matcher
    mutant("r4_Q1_the_name_scan_reads_a_carried_entry_as_a_substring_again", RETRIEVAL,
           [("    return FULL\n", "    return _off_limits_view()\n")],
           fuzz=[], existing=["r4_routine"], note='"Ed" in "Edited", "J" in "Just": the third round\'s routine numbers'),
    mutant("r4_Q1_an_items_keys_are_read", BOUNDARY,
           [("            yield from _item_values(child, str(name), depth + 1)\n",
             "            yield key, str(name)\n            yield from _item_values(child, str(name), depth + 1)\n")],
           fuzz=[], existing=["r4_routine"], note="the username in a field name of every item"),
    # ----- the routine lane: who it is for
    mutant("r4_Q1_the_rule_is_for_the_class_from_any_door", VIEW,
           [('    return (getattr(principal, "cls", None) == _ROUTINE_CLASS\n'
             '            and getattr(principal, "channel", None) == _RELAY_CHANNEL)\n',
             '    return getattr(principal, "cls", None) == _ROUTINE_CLASS\n')],
           fuzz=[], existing=["r4_routine"], note="a caller that is not a verified routine frame"),
    mutant("r4_Q1_the_rule_is_for_every_caller", GUARD,
           [("    if conn is None or not is_routine_lane(principal, current=current):\n        return None\n",
             "    if conn is None:\n        return None\n")],
           fuzz=[], existing=["r4_routine"], note="a recipient and an unstamped frame get the routine's reading"),
    # ----- the routine lane: the one filter on the way out
    mutant("r4_Q1_a_routines_answer_leaves_as_the_handler_gave_it", DISPATCHER,
           [("            response = await asyncio.to_thread(_withhold_what_is_carried, message, msg_type, response)\n",
             "            pass\n")], fuzz=[], existing=["r4_answers"]),
    mutant("r4_Q1_an_answer_that_cannot_be_filtered_is_sent", DISPATCHER,
           [("    except Exception:  # noqa: BLE001 -- what cannot be filtered is not sent\n        return _owner_mode_refusal(message)\n",
             "    except Exception:  # noqa: BLE001 -- what cannot be filtered is not sent\n        return response\n")],
           fuzz=[], existing=["r4_answers"]),
    mutant("r4_Q1_the_turns_bookkeeping_is_walked_too", DISPATCHER,
           [('        if isinstance(payload, dict) and "public_result" in payload:\n', "        if False:\n")],
           fuzz=[], existing=["r4_answers"], note="a contact saved under one of the node's own words empties the envelope"),
    mutant("r4_Q1_the_sentence_the_node_composed_is_left_in", GUARD,
           [("                if not (text and self.names(child)):\n", "                if True:\n")],
           fuzz=[], existing=["r4_answers"], note='"answer": a sentence written from the owner\'s data, naming the person'),
    mutant("r4_Q1_the_model_call_is_filtered", DISPATCHER,
           [('ROUTINE_ANSWERS_NOT_READ_OUT = frozenset({"llm_generation"})\n', "ROUTINE_ANSWERS_NOT_READ_OUT = frozenset()\n")],
           fuzz=[], existing=["r4_answers"]),
    mutant("r4_Q1_the_notice_says_nothing_else_changed", CARRY,
           [("are now never shared, and \"\n          \"your routines leave out anything that names them. Your routines may",
             "are now never shared. \"\n          \"Your routines may")],
           fuzz=[], existing=["r3_waits"], note="untrue of routines since this round"),
    # ----- the routine's own tools
    mutant("r4_T_a_carried_entry_closes_a_routines_tools_again", DISPATCHER,
           [("    if msg_type in ROUTINE_BRIDGE_INSPECTION_TOOLS and _only_what_is_carried_closes_it(conn, guard):\n",
             "    if False:\n")],
           fuzz=[], existing=["r4_row_tools"], note="every routine that reads messages, a table or the analytics fails its run"),
    mutant("r4_T_every_inspection_tool_is_opened", DISPATCHER,
           [("    if msg_type in ROUTINE_BRIDGE_INSPECTION_TOOLS and _only_what_is_carried_closes_it(conn, guard):\n",
             "    if _only_what_is_carried_closes_it(conn, guard):\n")], fuzz=[], existing=["r4_row_tools"]),
    mutant("r4_T_the_floor_opens_beside_an_entry_the_owner_made", DISPATCHER,
           [("        return (is_routine_lane() and anything_is_carried(conn)\n"
             "                and not guard.active_apart_from_what_is_carried())\n",
             "        return (is_routine_lane() and anything_is_carried(conn))\n")], fuzz=[], existing=["r4_row_tools"]),
    mutant("r4_T_the_floor_opens_for_every_caller", DISPATCHER,
           [("        return (is_routine_lane() and anything_is_carried(conn)\n", "        return (anything_is_carried(conn)\n")],
           fuzz=[], existing=["r4_row_tools", "r3_outward"]),
    mutant("r4_T_a_table_is_read_without_the_row_veto", TABLES,
           [("            rows = carried.veto_rows(table_name, rows)\n", "            pass\n")],
           fuzz=[], existing=["r4_row_tools"], note="the owner's own messages in the carried person's thread"),
    mutant("r4_T_the_ai_chat_lane_is_read_without_the_row_veto", MESSAGES,
           [('                messages = carried.veto_rows("ai_chat_messages", messages)\n', "                pass\n")],
           fuzz=[], existing=["r4_row_tools"], note="a chat whose title names the person"),
    mutant("r4_T_a_row_that_cannot_be_judged_is_kept", GUARD,
           [("            except Exception:  # noqa: BLE001 -- unavailable context withholds\n                continue\n",
             "            except Exception:  # noqa: BLE001 -- unavailable context withholds\n                kept.append(row)\n")],
           fuzz=[], existing=["r4_row_tools"]),
    # ----- the third round's own B9 and B10
    mutant("r4_B9_the_tier_is_checked_after_the_entry_is_made_full", OFF_LIMITS,
           [("        if processing_tier not in PROCESSING_TIERS:\n"
             '            raise ValueError(f"unknown processing_tier: {processing_tier}")\n'
             "        # The owner's act on an entry the upgrade carried (ruling P.3): from here it is an ordinary entry.\n",
             "        # The owner's act on an entry the upgrade carried (ruling P.3): from here it is an ordinary entry.\n")],
           fuzz=[], existing=["r4_own"], note="a refused mark leaves the entry full with its clean-up never run"),
    mutant("r4_B10_the_label_forgets_the_entrys_own_name", LISTING,
           [('        label = contact["saved_name"] or next(iter(contact["handles"]), "") or own\n',
             '        label = contact["saved_name"] or next(iter(contact["handles"]), "") or (own if record.get("entity_id") else "")\n')],
           fuzz=[], existing=["r4_own"]),
    # ----- names written without spaces, at the share boundary (B1)
    mutant("r4_B1_the_rule_is_removed", BOUNDARY,
           [("    if in_a_run and any(term in plain for term in in_a_run):\n", "    if False:\n")],
           fuzz=[], existing=["r4_unspaced"], note="a two-character name in running text is released again"),
    mutant("r4_B1_the_floor_is_one_character", BOUNDARY,
           [("UNSPACED_TERM_CHARS = 2\n", "UNSPACED_TERM_CHARS = 1\n")], fuzz=[], existing=["r4_unspaced"],
           note="one character found anywhere: every sentence with a king in it"),
    mutant("r4_B1_the_script_list_is_empty", BOUNDARY,
           [('UNSPACED = re.compile(\n    "[', 'UNSPACED = re.compile(\n    "(?!)[')], fuzz=[], existing=["r4_unspaced"]),
    mutant("r4_B1_an_identifier_in_such_a_script_is_a_whole_token_again", BOUNDARY,
           [("    in_a_run = unspaced_terms(frozenset(short_terms) | frozenset(whole_terms))\n",
             "    in_a_run = unspaced_terms(frozenset(short_terms))\n")], fuzz=[], existing=["r4_unspaced"]),
    mutant("r4_B1_an_identifier_in_such_a_script_needs_a_word_boundary_at_read_time", OFF_LIMITS,
           [('                                   if "@" in term or any(ch.isdigit() for ch in term) or _in_a_run(term))\n',
             '                                   if "@" in term or any(ch.isdigit() for ch in term))\n')],
           fuzz=[], existing=["r3_read_time"], note="the model gate does not see a contact named by such a username"),
    mutant("r4_B1_the_revision_does_not_move", BOUNDARY,
           [('                **({"unspaced_terms": sorted(self._in_a_run())} if self._in_a_run() else {}),\n', "")],
           fuzz=[], existing=["r4_unspaced"], note="an index built before keeps a record the boundary now withholds"),
]
MUTANTS = MUTANTS + R4_MUTANTS


# --- The fifth round (R5N): what the second re-check found, as WS0 ruled it -------------------------------------------
# Run with `--group r5`. The brief's six come first (items 1, 2, 3, 4 "the guard", 5 and 7), then one fault for each
# other thing a test of this round holds, the re-check's own surviving fault among them (re-expressed: the line it
# patched is gone, the rule it broke is not).
DIAGNOSIS = "topos/features/lifecycle/carry_diagnosis.py"
PREFLIGHT = "scripts/permissions_v2/carry_preflight.py"
REGISTRY = "topos/storage/db/migrations/registry.py"
MIGRATIONS_INIT = "topos/storage/db/migrations/__init__.py"
R5_MUTANTS = [
    # ----- item 1 (R3-M1): the row veto on a node that never made one of the message tables
    mutant("r5_M1_a_message_table_never_made_withholds_every_row_again", BOUNDARY,
           [("                if not self._never_made(native_table):\n                    raise\n                rows = []\n",
             "                raise\n")],
           fuzz=[], existing=["r5_one_table"], note="a routine's get_messages returns 0 of 10 once anyone is carried"),
    mutant("r5_M1_any_table_that_fails_reads_as_never_made", BOUNDARY,
           [('            return _copy_count(self.conn, table, "") == 0\n', "            return True\n")],
           fuzz=[], existing=["r5_one_table"], note="a view by the name, or no read transaction, releases the row"),
    # ----- item 2 (R3-M2): an item that carries the id of a message from their conversation
    mutant("r5_M2_the_item_rule_looks_no_id_up", BOUNDARY,
           [("                or self.carries_a_reached_id(text))\n", "                or False)\n")],
           fuzz=[], existing=["r5_routine"], note="the index row that holds their own message word for word passes"),
    mutant("r5_M2_a_table_that_cannot_be_read_holds_nobodys_message", BOUNDARY,
           [("            if not self._never_made(table):\n                raise\n            self._absent_tables[table] = True\n",
             "            self._absent_tables[table] = True\n")],
           fuzz=[], existing=["r5_one_table"], note="a fault in the lookup releases the item"),
    mutant("r5_M2_the_ids_inside_a_stored_json_column_are_not_read", BOUNDARY,
           [("                if any(self._an_id_of_theirs(inner) for inner in _strings(_decode(text), keys=False)):\n",
             "                if False:\n")],
           fuzz=[], existing=["r5_one_table", "r5_routine"], note="a fact that cites their message in its evidence"),
    # ----- item 3 (R3-M3): the failure names its entry; the hold; the pre-flight
    mutant("r5_M3_the_notice_names_an_entry_whose_removal_is_not_enough", CARRY,
           [('    if out["failed"] or not found.get("enough") or not found.get("entries"):\n',
             '    if out["failed"] or not found.get("entries"):\n')],
           fuzz=[], existing=["r5_names_it"], note="the owner removes the entry and sharing does not come back"),
    mutant("r5_M3_removal_is_called_enough_without_asking_the_boundary", DIAGNOSIS,
           [("    enough = bool(suspects) and builds(conn, without=suspects)\n", "    enough = bool(suspects)\n")],
           fuzz=[], existing=["r5_names_it"]),
    mutant("r5_M3_the_ledger_row_does_not_hold_the_entry", RUNNER,
           [('                        {**(more if isinstance(more, dict) else {}), "error": str(exc), "ran_under": shipped_v})\n',
             '                        {"error": str(exc), "ran_under": shipped_v})\n')],
           fuzz=[], existing=["r5_names_it"]),
    mutant("r5_M3_a_step_that_ended_failed_with_nobody_left_holds_nothing", CARRY,
           [('        if status == "failed":\n            # The step ended failed:', '        if False:\n            # The step ended failed:')],
           fuzz=[], existing=["r5_names_it"], note="nothing refuses a new bind on a node whose boundary cannot be built"),
    mutant("r5_M3_the_preflight_runs_on_a_folder_it_was_not_told_is_a_copy", PREFLIGHT,
           [("    if this_is_a_copy is not True:\n", "    if False:\n")], fuzz=[], existing=["r5_preflight"]),
    mutant("r5_M3_the_preflight_does_not_refuse_a_live_home", PREFLIGHT,
           [("    root = census.refuse_live_home(Path(copy_root))\n",
             "    root = Path(os.path.realpath(Path(copy_root)))\n"),
            ("import argparse\nimport json\n", "import argparse\nimport json\nimport os\n")],
           fuzz=[], existing=["r5_preflight"], note="the real step would run on the real home"),
    mutant("r5_M3_the_preflight_prints_the_entrys_id", PREFLIGHT,
           [('                "entries": [{"position": _count(entry["position"]),\n',
             '                "entries": [{"position": _count(entry["position"]), "entry": entry["blackhole_id"],\n')],
           fuzz=[], existing=["r5_preflight"]),
    mutant("r5_M3_the_preflight_builds_no_boundary", PREFLIGHT,
           [("        boundary = contact_excludes._boundary_state(conn)\n", '        boundary = "built"\n')],
           fuzz=[], existing=["r5_preflight"], note="as blind as the dry run it replaces"),
    # ----- item 4 (R3-M4): the schema step, and the guard it is for
    mutant("r5_M4_the_mark_is_not_a_schema_step", REGISTRY,
           [("    _spec(81, OFF_LIMITS_CARRIED_WAITING_V1_ID, apply_off_limits_carried_waiting_v1_up, always_run=True),\n", "")],
           fuzz=[], existing=["r5_schema"], note="the stamp stays at 80 and an older build opens the database"),
    mutant("r5_M4_the_downgrade_guard_lets_an_older_build_in", MIGRATIONS_INIT,
           [("    if current > max_order:\n        raise DowngradeGuardError(\n",
             "    if False:\n        raise DowngradeGuardError(\n")],
           fuzz=[], existing=["r5_schema"]),
    # ----- item 5 (R3-L1), with the re-check's own surviving fault (item 10)
    mutant("r5_L1_a_step_that_is_done_owes_nothing_ever_again", CARRY,
           [('        if not uncarried(conn):\n            return None\n    except Exception:  # noqa: BLE001 -- unreadable: hold, and say failed\n',
             '        if status == "done" or not uncarried(conn):\n            return None\n    except Exception:  # noqa: BLE001 -- unreadable: hold, and say failed\n')],
           fuzz=[], existing=["r5_hold"], note="an exclude an older app writes after the upgrade is shared"),
    mutant("r5_L1_the_runner_never_asks_whether_the_step_is_owed_again", RUNNER,
           [("        return bool(owe_again(conn, shipped))\n", "        return False\n")],
           fuzz=[], existing=["r5_hold"], note="held for, and no start ever carries it"),
    mutant("r5_L1_no_hold_is_remembered_for_good", CARRY,
           [("    if cached is not None and now - cached[0] < (_HOLD_SECONDS if cached[1] else _NO_HOLD_SECONDS):\n",
             '    if cached is not None and now - cached[0] < (_HOLD_SECONDS if cached[1] else float("inf")):\n')],
           fuzz=[], existing=["r5_hold"], note="the re-check's own3 fault, on the line that now holds the rule"),
    mutant("r5_L1_a_build_without_the_step_holds_for_it", CARRY,
           [("        if not _declared(shipped):\n            return None\n        status =", "        status =")],
           fuzz=[], existing=["r5_hold"], note="a tree that is not cut holds sharing for a step no start runs"),
    # ----- item 7 (R3-L3): a name that is a key
    mutant("r5_L3_a_key_is_not_read_for_names", BOUNDARY,
           [('        if keys:\n            row["item_keys_json"] = json.dumps(dict.fromkeys(keys))\n',
             "        if False:\n            pass\n")],
           fuzz=[], existing=["r5_routine"], note='{"message_counts": {"<her name>": 12}} passes a routine'),
    mutant("r5_L3_an_identifier_is_matched_against_a_key", BOUNDARY,
           [('            row["item_keys_json"] = json.dumps(dict.fromkeys(keys))\n',
             '            row["item_keys"] = "\\n".join(keys)\n')],
           fuzz=[], existing=["r5_routine"], note="ruling M: never against a key"),
    # ----- the other items, one each
    mutant("r5_L2_text_that_starts_like_an_entry_id_makes_an_entry", OFF_LIMITS,
           [("        if by_id is None and starts_like_an_entry_id(ref) and (\n", "        if False and (\n")],
           fuzz=[], existing=["r5_doors"]),
    mutant("r5_L2_a_contact_saved_under_such_a_name_strands_the_step", CARRY,
           [("    return _usable(value) and not starts_like_an_entry_id(value)\n", "    return _usable(value)\n")],
           fuzz=[], existing=["r5_doors"]),
    mutant("r5_L4_a_node_switched_off_that_holds_says_nothing", RUNNER,
           [('        logger.info("upgrade runner disabled (TOPOS_UPGRADE_RUNNER=off)")\n        _say_what_waits_while_switched_off(conn)\n',
             '        logger.info("upgrade runner disabled (TOPOS_UPGRADE_RUNNER=off)")\n')],
           fuzz=[], existing=["r5_hold"]),
    mutant("r5_a_node_that_never_turned_sharing_on_cannot_finish_the_step", CARRY,
           [("            conn.execute(TOMBSTONES_SQL)\n            commit_connection(conn)\n", "            commit_connection(conn)\n")],
           fuzz=[], existing=["r5_step", "r5_hold"], note="with the hold on a failed step, such a node can never bind"),
]
MUTANTS = MUTANTS + R5_MUTANTS


# --- The sixth round (R6N): what the third re-check found, as WS0 ruled it -------------------------------------------
# Run with `--group r6`. Item 1 first (the hold starts the step's pass again: what starts it, what bounds it, what it
# may not run beside, what it runs, what it says at the end), then item 2 (the pre-flight and a hard link; its
# backup rule), then one each for the upgrade tools and for the re-check's second surviving fault.
CENSUS_SUPPORT = "scripts/permissions_v2/census_support.py"
MATRIX = "scripts/run_upgrade_matrix.py"
FIXTURE_BUILDER = "scripts/build_upgrade_fixture.py"
R6_MUTANTS = [
    # ----- item 1 (R4-M1 and the start-up race)
    mutant("r6_M1_the_hold_never_starts_the_step_again", CARRY,
           [("    if reason is not None:\n        _start_the_pass_again(key, now)\n",
             "    if False:\n        _start_the_pass_again(key, now)\n")],
           fuzz=[], existing=["r6_again"], note="a dead upgrade thread holds every share until the next start, in silence"),
    mutant("r6_M1_there_is_a_fourth_try_and_a_fifth", CARRY,
           [('            if state["tries"] >= _AGAIN_LIMIT:\n'
             "                return                             # three in a row: nothing more until the next start\n", "")],
           fuzz=[], existing=["r6_again"], note="a home that cannot be carried is run at every look"),
    mutant("r6_M1_the_tries_are_not_half_a_minute_apart", CARRY,
           [('            if state["at"] is not None and now - state["at"] < _AGAIN_SECONDS:\n                return\n', "")],
           fuzz=[], existing=["r6_again"], note="three tries inside six seconds, then the notice"),
    mutant("r6_M1_a_pass_is_started_beside_a_live_upgrade_thread", CARRY,
           [("        if any(thread.is_alive() and thread.name.startswith(runner.UPGRADE_THREADS)\n"
             "               for thread in threading.enumerate()):\n            return\n", "")],
           fuzz=[], existing=["r6_again"]),
    mutant("r6_M1_it_runs_beside_a_pass_that_is_live_on_another_thread", CARRY,
           [("        if not runner.claim_the_only_pass():\n", "        if not (runner.claim_the_only_pass() or True):\n")],
           fuzz=[], existing=["r6_again"]),
    mutant("r6_M1_a_pass_of_the_runner_does_not_wait_for_the_restarted_one", RUNNER,
           [("        _let_a_restarted_carry_finish()\n        return _run_pending_upgrades(",
             "        return _run_pending_upgrades(")], fuzz=[], existing=["r6_again"]),
    mutant("r6_M1_the_restarted_pass_runs_every_planned_step", CARRY,
           [("            runner.run_pending_upgrades(conn, only_first=True)\n", "            runner.run_pending_upgrades(conn)\n")],
           fuzz=[], existing=["r6_again"], note="the older releases' steps, which call models, started by a share read"),
    mutant("r6_M1_after_the_last_try_nothing_is_said", CARRY,
           [("    if conn is not None:\n        _say_it_is_held(conn)\n", "    if False:\n        _say_it_is_held(conn)\n")],
           fuzz=[], existing=["r6_again"]),
    mutant("r6_M1_the_last_tries_notice_is_written_beside_the_steps_own", CARRY,
           [("        if open_notice is None:\n            BlackholeStore(conn)._notify(blackhole_id=CARRY_NOTICE_ID, entity_id=\"\", normalized_name=\"\",\n"
             "                                         kind=\"carry_failed\", message=NOTICE_HELD)\n",
             "        if True:\n            BlackholeStore(conn)._notify(blackhole_id=CARRY_NOTICE_ID, entity_id=\"\", normalized_name=\"\",\n"
             "                                         kind=\"carry_failed\", message=NOTICE_HELD)\n")],
           fuzz=[], existing=["r6_again"], note="two failure notices"),
    mutant("r6_M1_sharing_stays_held_after_the_pass_has_ended_the_hold", CARRY,
           [('            state["tries"] = 0\n            _hold_cache.pop(key, None)\n', '            state["tries"] = 0\n')],
           fuzz=[], existing=["r6_again"]),
    mutant("r6_M1_the_count_does_not_begin_again_after_a_pass_that_ended_the_hold", CARRY,
           [('            state["tries"] = 0\n            _hold_cache.pop(key, None)\n', '            _hold_cache.pop(key, None)\n')],
           fuzz=[], existing=["r6_again"], note="the fourth exclude written from an older app is held until a start"),
    mutant("r6_M1_it_starts_with_the_runner_switched_off", CARRY,
           [("    if _AGAIN_LIMIT <= 0 or not runner._enabled():\n", "    if _AGAIN_LIMIT <= 0:\n")],
           fuzz=[], existing=["r6_again"]),
    mutant("r6_M1_a_step_cut_short_never_writes_its_notice", CARRY,
           [('        if out["waiting"] and (out["carried"] or out["added_to_existing"] or not told_before):\n',
             '        if out["waiting"] and (out["carried"] or out["added_to_existing"]):\n')],
           fuzz=[], existing=["r6_again"]),
    # ----- item 2 (R4-L1): the pre-flight and a hard link; its backup rule
    mutant("r6_L1_a_database_with_two_names_is_taken_for_a_copy", CENSUS_SUPPORT,
           [("    if info.st_nlink != 1:\n", "    if False:\n")],
           fuzz=[], existing=["r6_preflight", "r6_tools"], note="the pre-flight runs for real on the real database"),
    mutant("r6_L1_another_name_for_a_file_of_the_real_home_is_not_called_the_real_home", CENSUS_SUPPORT,
           [("        if _another_name_for_a_file_under(info, LIVE_HOME):\n            raise CensusRefused(\"live_store_refused\")\n", "")],
           fuzz=[], existing=["r6_preflight"]),
    mutant("r6_L1_the_preflight_does_not_ask_the_check", PREFLIGHT,
           [('    canonical = cs.refuse_a_real_database(census._inside(root, census._stores(root)["canonical"]), closed=True)\n',
             '    canonical = census._inside(root, census._stores(root)["canonical"])\n')],
           fuzz=[], existing=["r6_preflight"]),
    mutant("r6_L1_the_preflight_writes_a_backup", PREFLIGHT,
           [("            ensure_migrations_applied(conn, skip_backup=True)\n",
             "            ensure_migrations_applied(conn, skip_backup=False)\n")],
           fuzz=[], existing=["r6_preflight"], note="the third re-check's fault that left every test green"),
    # ----- item 3 (R4-L4): the matrix and the fixture builder
    mutant("r6_L4_the_matrix_takes_a_database_of_the_real_home", MATRIX,
           [("    db_path = census_support.refuse_a_real_database(Path(db_path))\n", "    db_path = Path(db_path)\n")],
           fuzz=[], existing=["r6_tools"]),
    mutant("r6_L4_the_fixture_builder_removes_a_database_of_the_real_home", FIXTURE_BUILDER,
           [("    out = census_support.refuse_a_real_database(Path(out))\n    if str(REPO_ROOT) not in sys.path:\n",
             "    out = Path(out)\n    if str(REPO_ROOT) not in sys.path:\n")],
           fuzz=[], existing=["r6_tools"]),
    mutant("r6_L4_the_builder_that_installs_a_release_removes_it_too", FIXTURE_BUILDER,
           [("    out = census_support.refuse_a_real_database(Path(out))\n    out.parent.mkdir(parents=True, exist_ok=True)\n",
             "    out = Path(out)\n    out.parent.mkdir(parents=True, exist_ok=True)\n")],
           fuzz=[], existing=["r6_tools"]),
    # ----- item 4: the third re-check's second surviving fault
    mutant("r6_the_id_rule_does_not_look_up_a_conversations_id", BOUNDARY,
           [('                for parent in self._rows_or_none_made(parents, parent_columns, "conversation_id", value):\n',
             "                for parent in ():\n")],
           fuzz=[], existing=["r6_items"], note="a count kept under their thread's id passes a routine's query"),
]
MUTANTS = MUTANTS + R6_MUTANTS


def check(specs) -> list[dict]:
    """Every edit applies exactly once, and no two mutants of one file conflict on their own text."""
    report = []
    for spec in specs:
        text = (ROOT / spec["file"]).read_text("utf-8")
        counts = [text.count(old) for old, _ in spec["edits"]]
        report.append({"mutant": spec["name"], "file": spec["file"], "matches": counts,
                       "applicable": all(count == 1 for count in counts)})
    return report


def git_clean(files) -> bool:
    out = subprocess.run(["git", "status", "--porcelain", "--", *sorted(files)], cwd=ROOT, capture_output=True, text=True)
    return out.returncode == 0 and out.stdout.strip() == ""


LAST_ERRORS: list[str] = []


def run_tests(tests, deselect, *, timeout, stop_at_first=True) -> tuple[int, list[str], str, float]:
    """(exit code, the tests that FAILED, pytest's last summary line, seconds). `LAST_ERRORS` holds the tests that
    ERRORED in this run (a fixture or a collection that raised): they are reported, and are never a kill."""
    command = [sys.executable, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider", "--tb=line"]
    if stop_at_first:
        command.append("-x")
    for node in deselect:
        command += ["--deselect", node]
    started = time.monotonic()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        LAST_ERRORS[:] = []
        return -1, [], "timeout", time.monotonic() - started
    failing = [line.split(" ", 1)[1].split(" - ")[0] for line in run.stdout.splitlines() if line.startswith("FAILED ")]
    LAST_ERRORS[:] = [line.split(" ", 1)[1][:300] for line in run.stdout.splitlines() if line.startswith("ERROR ")]
    summary = next((line for line in reversed(run.stdout.splitlines()) if "passed" in line or "failed" in line or "error" in line), "")
    return run.returncode, failing, summary.strip(), time.monotonic() - started


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="only prove every patch applies exactly once")
    parser.add_argument("--out", type=Path, help="JSON report path")
    parser.add_argument("--only", nargs="*", default=[], help="mutant names to run")
    parser.add_argument("--full-lane", action="store_true", help="re-run survivors against the whole permissions lane")
    parser.add_argument("--lane", default=T, help="the full lane's test path")
    parser.add_argument("--deselect", nargs="*", default=KNOWN_REDS, help="node ids red on the base")
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--group", choices=["s1", "r1", "r2", "r3", "r4", "r5", "r6"],
                        help="only the isolation battery's node mutants (s1), or the review fixes' (r1 to r6)")
    args = parser.parse_args(argv)
    pool = {"s1": S1_MUTANTS, "r1": R1_MUTANTS, "r2": R2_MUTANTS, "r3": R3_MUTANTS,
            "r4": R4_MUTANTS, "r5": R5_MUTANTS, "r6": R6_MUTANTS}.get(args.group, MUTANTS)
    specs = [m for m in pool if not args.only or m["name"] in args.only]
    names = [m["name"] for m in MUTANTS]
    assert len(names) == len(set(names)), "duplicate mutant name"
    applicability = check(specs)
    if args.check:
        print(json.dumps({"mutants": len(specs), "applicable": sum(r["applicable"] for r in applicability),
                          "not_applicable": [r for r in applicability if not r["applicable"]]}, indent=1))
        return 0 if all(r["applicable"] for r in applicability) else 1
    files = {m["file"] for m in specs}
    if not git_clean(files):
        print("refusing: a target file is dirty in this checkout", file=sys.stderr)
        return 2
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    patcher = Patcher()
    results = []
    # A mutant is "killed" when one of its tests FAILS by name. A non-zero exit alone is also what a missing file,
    # an import error, a fixture that raised, a timeout or a test already red on the base produce. So each distinct
    # test set must pass UNMUTATED before a kill counts, and a mutated run that names no failed test is reported as
    # `errored` or `timeout`, never as a kill.
    baselines = {}
    for spec in specs:
        key = tuple(spec["tests"])
        if key not in baselines:
            missing = [test for test in key if not (ROOT / test.split("::")[0]).exists()]
            code, failing, summary, _ = (4, missing, "missing test file", 0.0) if missing else \
                run_tests(spec["tests"], args.deselect, timeout=args.timeout)
            baselines[key] = None if code == 0 else {"failing": failing[:3], "summary": summary}
        if baselines[key] is not None:
            results.append({"mutant": spec["name"], "file": spec["file"], "status": "baseline_red",
                            "baseline": baselines[key], "tests": spec["tests"], "note": spec["note"]})
            print(json.dumps({k: results[-1][k] for k in ("mutant", "status", "baseline")}), flush=True)
            continue
        problem = patcher.apply(spec)
        if problem:
            results.append({"mutant": spec["name"], "file": spec["file"], "status": problem, "note": spec["note"]})
            print(json.dumps(results[-1]), flush=True)
            continue
        try:
            code, failing, summary, seconds = run_tests(spec["tests"], args.deselect, timeout=args.timeout)
            if code not in (0, -1) and not failing:
                # The run stopped at a test that ERRORED (a fixture raised) before any test failed. Run the whole
                # list once more without stopping: a failed test further on is the kill; errors alone are not.
                code, failing, summary, more = run_tests(spec["tests"], args.deselect, timeout=args.timeout,
                                                         stop_at_first=False)
                seconds += more
        finally:
            patcher.restore_all()
        status = ("SURVIVED" if code == 0 else "killed" if failing else "timeout" if code == -1 else "errored")
        killed_by_fuzz = bool(failing) and "test_fuzz_" in failing[0]
        results.append({"mutant": spec["name"], "file": spec["file"], "status": status, "killed_by": failing[:3],
                        "killed_by_fuzz_lane": killed_by_fuzz, "summary": summary, "seconds": round(seconds, 1),
                        "tests": spec["tests"], "note": spec["note"]})
        if status == "errored":
            results[-1]["errors"] = list(LAST_ERRORS[:3])
        print(json.dumps({k: results[-1][k] for k in ("mutant", "status", "killed_by", "seconds")}), flush=True)
        if status == "SURVIVED" and args.full_lane:
            problem = patcher.apply(spec)
            try:
                code, failing, summary, seconds = run_tests([args.lane], args.deselect, timeout=args.timeout * 2)
            finally:
                patcher.restore_all()
            results[-1].update({"full_lane": {"status": "killed" if failing else "SURVIVED" if code == 0 else "errored",
                                              "killed_by": failing[:3], "summary": summary,
                                              "seconds": round(seconds, 1)}})
            if failing:
                results[-1]["status"] = "killed_by_full_lane"
            print(json.dumps({"mutant": spec["name"], "full_lane": results[-1]["full_lane"]}), flush=True)
    report = {"version": "permissions-v2-mutation-battery/v1", "base_commit": base, "mutants": len(results),
              "killed": sum(r["status"] in ("killed", "killed_by_full_lane") for r in results),
              "survived": [r["mutant"] for r in results if r["status"] == "SURVIVED"],
              "not_applied": [r["mutant"] for r in results if r["status"].startswith("patch")],
              "baseline_red": [r["mutant"] for r in results if r["status"] == "baseline_red"],
              "errored": [r["mutant"] for r in results if r["status"] == "errored"],
              "timeout": [r["mutant"] for r in results if r["status"] == "timeout"],
              "killed_by_fuzz_lane": sum(bool(r.get("killed_by_fuzz_lane")) for r in results),
              "results": results}
    if args.out:
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: report[k] for k in ("mutants", "killed", "survived", "not_applied", "baseline_red",
                                              "errored", "timeout", "killed_by_fuzz_lane")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
