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
    mutant("r2_B1_the_step_runs_the_clean_up_again", CARRY,
           [('            outcome = "carried"\n',
             '            outcome = "carried"\n            from .blackhole_rebuild import rebuild_for_blackhole\n'
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
             '                                            identifiers=entry["identifiers"],\n',
             '            result = store.blackhole_entity(entity_ref=entry["entity_ref"], note=NOTE,\n'
             '                                            aliases=[*entry["names"], *entry["identifiers"]],\n')],
           fuzz=[], existing=["r2_carry"]),
    # R-M6
    mutant("r2_M6_the_owners_own_card_is_carried", CARRY,
           [('        if contact.get("is_self"):\n            out["own_card_skipped"] += 1\n            continue\n', "")],
           fuzz=[], existing=["r2_carry"]),
    # R-H2, R-L5, R-L4
    mutant("r2_H2_a_contact_is_named_by_a_saved_name_of_symbols_alone", CARRY,
           [('    elif _usable(identity["display"]):\n', '    elif identity["display"]:\n')],
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
    mutant("r2_L4_an_entry_that_gained_names_does_not_wait_again", OFF_LIMITS,
           [('        requeued = record["rebuild_state"] == "complete"\n', "        requeued = False\n")],
           fuzz=[], existing=["r2_identifiers"]),
    # the notice and the owner's start
    mutant("r2_notice_a_carried_entry_says_the_stores_own_words", CARRY,
           [('                                            notice=NOTICE.format(name=entry["saved_name"]))\n',
             "                                            notice=None)\n")], fuzz=[], existing=["r2_carry"]),
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
           [("                \"UPDATE entity_blackholes SET aliases_json=?, rebuild_state=?, updated_at=datetime('now') \"\n",
             "                \"UPDATE entity_blackholes SET processing_tier='secure', aliases_json=?, rebuild_state=?, \"\n"
             "                \"updated_at=datetime('now') \"\n")], fuzz=[], existing=["r2_identifiers"]),
    mutant("r2_L4_an_entry_that_gains_nothing_is_written_all_the_same", OFF_LIMITS,
           [('        if merged == set(record["aliases"]) and marked == set(record["identifier_aliases"]):\n',
             "        if False:\n")], fuzz=[], existing=["r2_identifiers"],
           note="every such write moves the protection clock and drops every share index"),
    mutant("r2_notice_the_store_ignores_the_words_it_is_given", OFF_LIMITS,
           [("                message=notice or (\n                    f\"'{canonical_name or ref}' is now off-limits.",
             "                message=(\n                    f\"'{canonical_name or ref}' is now off-limits.")],
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
    parser.add_argument("--group", choices=["s1", "r1", "r2"],
                        help="only the isolation battery's node mutants (s1), or the review fixes' (r1, r2)")
    args = parser.parse_args(argv)
    pool = {"s1": S1_MUTANTS, "r1": R1_MUTANTS, "r2": R2_MUTANTS}.get(args.group, MUTANTS)
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
