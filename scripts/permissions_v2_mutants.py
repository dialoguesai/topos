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
    "contract": [T + "test_contract_and_ledger.py", T + "test_bk5_read_budget_in_policy.py"],
    "release": [T + "test_release.py", T + "test_recipient_fabric_refusal_uniformity.py"],
    "release_door": [T + "test_release.py", T + "test_bk3_opaque_ids.py", T + "test_nightA_discovery_subset_access.py",
                     T + "test_bk5_admission_before_the_floor.py", T + "test_bk3_gate_release.py"],
    "facts": [T + "test_fact_policy.py", T + "test_fact_eligibility.py", T + "test_fact_stated_day.py"],
    # The clock's own suites: its cache, the ingest-source clock, and the identity/attestation lanes that its
    # v4 ledger and subject registry belong to.
    "ingest": [T + "test_ingest_provenance.py", T + "test_ingest_owner_boundaries.py",
               T + "test_ingest_origin_evidence.py", T + "test_ingest_snapshot_supersession.py",
               T + "test_bk3_ingest_source_clock.py"],
    "protection_clock": [T + "test_protection_revision_cache.py", T + "test_bk3_ingest_source_clock.py",
                         T + "test_identity_attestation.py", T + "test_owner_identity_binding.py"],
    "floors": [T + "test_evidence.py", T + "test_source_release_sibling_facts.py", T + "test_exclusion_floor.py",
               T + "test_evidence_quote_metadata.py"],
    "canonical_floor": [T + "test_canonical_floor.py", T + "test_canonical_floor_binding.py"],
    "ledger": [T + "test_contract_and_ledger.py", T + "test_bk5_admission_before_the_floor.py"],
    "transports": [T + "test_recipient_fabric_refusal_uniformity.py", T + "test_release_transport.py"],
    "search": [T + "test_message_search_invariant.py", T + "test_message_search_refusals.py",
               T + "test_message_search_review_fixes.py", T + "test_nightA_discovery_subset_access.py"],
    "opaque": [T + "test_bk3_opaque_ids.py"],
    "fact_release": [T + "test_fact_release.py", T + "test_recipient_fabric_refusal_uniformity.py"],
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
           [('                if not set(sources.values).issubset(universe.source_ids):\n                    raise ValueError("source outside pinned universe")\n',
             "")], fuzz=["evaluator"], existing=["contract"]),
    mutant("strict_model_ignores_unknown_keys", P + "contract.py",
           [('    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)',
             '    model_config = ConfigDict(extra="ignore", strict=True, frozen=True)')],
           fuzz=["encoding"], existing=["contract"]),
    # --- release.py: the raw-message decision and the locator door ---------------------------------------------
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
    mutant("retired_capability_released", P + "release.py",
           [('        if signed.capability_version in self.retired:\n            raise PolicyError("capability_retired")\n', "")],
           fuzz=[], existing=["release_door"]),
    mutant("opaque_ids_skipped_under_v3", P + "release.py",
           [("                key = self._record_key(signed) if signed.capability_version == CAPABILITY_OPAQUE else None",
             "                key = None")], fuzz=["discovery"], existing=["release_door"]),
    mutant("disclosure_budget_unbounded", P + "release.py",
           [("                if not records or len(canonical_bytes(output.model_dump())) > MAX_DISCLOSURE_BYTES:",
             "                if not records or len(canonical_bytes(output.model_dump())) > MAX_DISCLOSURE_BYTES * 1000:")],
           fuzz=["discovery"], existing=["release_door"]),
    mutant("deny_leaves_no_tombstone", P + "release.py",
           [('                if decision.verdict != "permit":\n                    ledger.refuse(admission, decision.model_dump(), candidate_revision=decision.candidate_revision,\n                                  now=self.clock())\n                    raise PolicyError("permission_denied")',
             '                if decision.verdict != "permit":\n                    raise PolicyError("permission_denied")')],
           fuzz=[], existing=["release_door"]),
    mutant("deny_releases_anyway", P + "release.py",
           [('                if decision.verdict != "permit":\n                    ledger.refuse(admission, decision.model_dump(), candidate_revision=decision.candidate_revision,\n                                  now=self.clock())\n                    raise PolicyError("permission_denied")',
             '                if False:\n                    raise PolicyError("permission_denied")')],
           fuzz=["floors", "discovery"], existing=["release_door"]),
    mutant("send_without_authority_recheck", P + "release.py",
           [('        if self._authority_after_checkpoint(signed) != checkpointed:\n            raise PolicyError("authority_stale")\n        send(result.model_dump(), output.model_dump())',
             "        send(result.model_dump(), output.model_dump())")], fuzz=[], existing=["release_door"]),
    mutant("locator_door_skips_principal_check", P + "release.py",
           [('        if (principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay"\n            or not principal.acting_user or not principal.client_id):\n            raise PolicyError("recipient_relay_required")\n        intent = SourceMessageIntent.parse(payload)',
             "        intent = SourceMessageIntent.parse(payload)")], fuzz=[], existing=["release", "release_door"],
           note="defence in depth behind the transport's stamp check; may only be killed by a direct adapter test"),
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
           [('        if enforce_floor and conn.execute("SELECT 1 FROM entity_blackholes LIMIT 1").fetchone():\n            raise PolicyError("entity_protection_lineage_unavailable")\n', ""),
            ('        if conn.execute("SELECT 1 FROM entity_blackholes LIMIT 1").fetchone():\n            raise PolicyError("entity_protection_lineage_unavailable")\n', "")],
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
           [('                if enforce_floor and _json(row.get("payload_json"), dict).get("disclosure") != "scoped":\n                    raise PolicyError("owner_only")\n', ""),
            ('                if payload.get("disclosure") != "scoped":\n                    raise PolicyError("owner_only")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("sibling_fact_floor_removed", P + "evidence.py",
           [("        if discloses_sources:\n            self._source_sibling_floor(conn, snapshot)\n", "")],
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
           [('                    if type(row.get("is_from_self")) is not int or row["is_from_self"] != 1:\n                        raise PolicyError("not_owner_authored")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("quote_metadata_released", P + "evidence.py",
           [('                    if any(metadata.get(field) not in (None, False, 0, "", [], {}) for field in',
             '                    if False and any(metadata.get(field) not in (None, False, 0, "", [], {}) for field in')],
           fuzz=["floors"], existing=["floors"]),
    mutant("independent_copy_released", P + "evidence.py",
           [('                if self._known_copies(conn, identity, row):\n                    raise PolicyError("independent_copy_lineage")\n', "")],
           fuzz=["floors"], existing=["floors"]),
    mutant("stale_review_served", P + "evidence.py",
           [('        if review.owner_id != self.binding.owner_id or review.snapshot != snapshot:\n            raise PolicyError("review_stale")\n', ""),
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
    # --- the three transports ---------------------------------------------------------------------------------------
    mutant("source_transport_frame_names_the_code", P + "release_transport.py",
           [('    except Exception:\n        # Recipient errors reveal no fact existence, review/protection state,\n        # credential/config paths, source text, or exception diagnostics.\n        error = {"id": request_id, "type": MESSAGE_TYPE, "status": "error", "code": 403, "error": "permission_denied"}',
             '    except Exception as exc:\n        error = {"id": request_id, "type": MESSAGE_TYPE, "status": "error", "code": 403, "error": getattr(exc, "code", "permission_denied")}')],
           fuzz=["transports"], existing=["transports"]),
    mutant("source_transport_flag_ignored", P + "release_transport.py",
           [('        if (os.environ.get("TOPOS_PERMISSIONS_V2_SOURCE_RELEASE_ENABLED", "").lower() != "true"\n            or message.get("type") != MESSAGE_TYPE):\n            raise PolicyError("source_release_disabled")',
             '        if message.get("type") != MESSAGE_TYPE:\n            raise PolicyError("source_release_disabled")')],
           fuzz=["transports"], existing=["transports"]),
    mutant("fact_transport_admits_owner_stamp", P + "fact_release_transport.py",
           [('        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":\n            raise PolicyError("recipient_relay_required")',
             '        if principal is None or principal.channel != "cp_relay":\n            raise PolicyError("recipient_relay_required")')],
           fuzz=["transports"], existing=["transports"]),
    mutant("search_transport_none_stamp_unchecked", P + "search_transport.py",
           [('        if principal is None or principal.cls != THIRD_PARTY or principal.channel != "cp_relay":',
             '        if principal.cls != THIRD_PARTY or principal.channel != "cp_relay":')],
           fuzz=["transports"], existing=["transports"],
           note="equivalent: the dispatcher's blanket `except Exception` turns the AttributeError an unstamped frame "
                "raises into the identical error frame, and logs no diagnostics in either branch, so no party sees a "
                "difference. The equivalence RESTS on that catch-all: adding exception logging there would make this "
                "mutant observable to the operator and it would need a test"),
    # --- search_release.py: discovery ----------------------------------------------------------------------------------
    mutant("search_trusts_the_index", P + "search_release.py",
           [('                    decided[fact_id] = ((qualified, rows, decision)\n                                        if decision.verdict == "permit"\n                                        and _locator_disclosable(qualified, rows, key, grant_id) else None)',
             "                    decided[fact_id] = (qualified, rows, decision)")], fuzz=["discovery"], existing=["search"]),
    mutant("search_ignores_locator_budget", P + "search_release.py",
           [('                                        if decision.verdict == "permit"\n                                        and _locator_disclosable(qualified, rows, key, grant_id) else None)',
             '                                        if decision.verdict == "permit" else None)')], fuzz=["discovery"], existing=["search"]),
    mutant("search_window_ignored", P + "search_release.py",
           [("            if (event_us is None or not lower_us <= event_us <= upper_us or is_record_nsfw(row)",
             "            if (event_us is None or is_record_nsfw(row)")], fuzz=["discovery"], existing=["search"]),
    mutant("search_nsfw_ignored", P + "search_release.py",
           [("            if (event_us is None or not lower_us <= event_us <= upper_us or is_record_nsfw(row)",
             "            if (event_us is None or not lower_us <= event_us <= upper_us")], fuzz=["discovery"], existing=["search"],
           note="equivalent, re-read under the corrected standard (no observable difference to ANY party, not merely "
                "identical recipient bytes). search_index.py excludes a flagged row when the index is built, so the "
                "door's check is a backstop for the window the index cannot cover: a row flagged AFTER indexing and "
                "before the next rebuild. test_D5 constructs exactly that window and the record still does not come "
                "back with this mutant applied, because the door refuses the request whole once its membership no "
                "longer matches the index. D4 pins the index half, D5 the door half"),
    mutant("search_k_unbounded", P + "search_release.py",
           [("                        if len(records) == intent.k:\n                            break\n", "")], fuzz=["discovery"], existing=["search"]),
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
    # --- fact_release.py: the fact door ---------------------------------------------------------------------------------
    mutant("fact_door_releases_a_deny", P + "fact_release.py",
           [('                if decision.verdict != "permit":\n                    ledger.refuse(admission, decision.model_dump(), candidate_revision=decision.candidate_revision,\n                                  now=self.clock())\n                    raise PolicyError("permission_denied")',
             '                if False:\n                    raise PolicyError("permission_denied")')], fuzz=[], existing=["fact_release"]),
    mutant("fact_door_budget_unbounded", P + "fact_release.py",
           [("                if len(canonical_bytes(output.model_dump())) > MAX_FACT_DISCLOSURE_BYTES:",
             "                if len(canonical_bytes(output.model_dump())) > MAX_FACT_DISCLOSURE_BYTES * 1000:")],
           fuzz=["facts"], existing=["fact_release"],
           note="equivalent: both output families are six bounded fields (a 256-character scalar at most), so no parsed "
                "output reaches the budget; test_T5 pins the largest admissible output under an eighth of it"),

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
]

KNOWN_REDS = [
    T + "test_night_b_recipient_surface.py::test_b2_expired_admissions_are_never_removed_from_the_node_ledger",
    T + "test_night_b_recipient_surface.py::test_b4_engine_local_truth_doors_admit_a_third_party_principal[/api/local/truth_prompts]",
    T + "test_night_b_recipient_surface.py::test_b4_engine_local_truth_doors_admit_a_third_party_principal[/api/local/truth_seed_fact]",
    T + "test_night_b_recipient_surface.py::test_b4_engine_local_truth_doors_admit_a_third_party_principal[/api/local/verify_claim]",
]


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
        self.originals[path] = original
        path.write_bytes(text.encode("utf-8"))
        return None

    def restore_all(self):
        for path, original in list(self.originals.items()):
            path.write_bytes(original)
            self.originals.pop(path, None)


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


def run_tests(tests, deselect, *, timeout) -> tuple[int, list[str], str, float]:
    command = [sys.executable, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider", "--tb=line"]
    for node in deselect:
        command += ["--deselect", node]
    started = time.monotonic()
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, env=env, timeout=timeout)
    except subprocess.TimeoutExpired:
        return -1, [], "timeout", time.monotonic() - started
    failing = [line.split(" ", 1)[1].split(" - ")[0] for line in run.stdout.splitlines() if line.startswith("FAILED ")]
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
    args = parser.parse_args(argv)
    specs = [m for m in MUTANTS if not args.only or m["name"] in args.only]
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
    for spec in specs:
        problem = patcher.apply(spec)
        if problem:
            results.append({"mutant": spec["name"], "file": spec["file"], "status": problem, "note": spec["note"]})
            print(json.dumps(results[-1]), flush=True)
            continue
        try:
            code, failing, summary, seconds = run_tests(spec["tests"], args.deselect, timeout=args.timeout)
        finally:
            patcher.restore_all()
        status = "killed" if code != 0 else "SURVIVED"
        killed_by_fuzz = bool(failing) and "test_fuzz_" in failing[0]
        results.append({"mutant": spec["name"], "file": spec["file"], "status": status, "killed_by": failing[:3],
                        "killed_by_fuzz_lane": killed_by_fuzz, "summary": summary, "seconds": round(seconds, 1),
                        "tests": spec["tests"], "note": spec["note"]})
        print(json.dumps({k: results[-1][k] for k in ("mutant", "status", "killed_by", "seconds")}), flush=True)
        if status == "SURVIVED" and args.full_lane:
            problem = patcher.apply(spec)
            try:
                code, failing, summary, seconds = run_tests([args.lane], args.deselect, timeout=args.timeout * 2)
            finally:
                patcher.restore_all()
            results[-1].update({"full_lane": {"status": "killed" if code != 0 else "SURVIVED", "killed_by": failing[:3],
                                              "summary": summary, "seconds": round(seconds, 1)}})
            if code != 0:
                results[-1]["status"] = "killed_by_full_lane"
            print(json.dumps({"mutant": spec["name"], "full_lane": results[-1]["full_lane"]}), flush=True)
    report = {"version": "permissions-v2-mutation-battery/v1", "base_commit": base, "mutants": len(results),
              "killed": sum(r["status"] in ("killed", "killed_by_full_lane") for r in results),
              "survived": [r["mutant"] for r in results if r["status"] == "SURVIVED"],
              "not_applied": [r["mutant"] for r in results if r["status"].startswith("patch")],
              "killed_by_fuzz_lane": sum(bool(r.get("killed_by_fuzz_lane")) for r in results),
              "results": results}
    if args.out:
        args.out.write_text(json.dumps(report, indent=1) + "\n")
    print(json.dumps({k: report[k] for k in ("mutants", "killed", "survived", "not_applied", "killed_by_fuzz_lane")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
