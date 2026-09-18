"""Mutation run over p2c-v1's guards: every guard must be killed by at least one test.

Each mutant is one textual patch to a module under topos/permissions_v2 (or a
lifecycle hook). The script copies the engine's `topos/`, `tests/` and `fixtures/`
into a scratch directory, applies one mutant at a time there, and runs the
message-search tests against the scratch copy. The worktree is never modified.

    TOPOS_KEY=synthetic .venv/bin/python3 scripts/permissions_v2/p2c_mutants.py --out mutants.json
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
P = "topos/permissions_v2/"
TESTS = ["tests/permissions_v2/" + name for name in (
    "test_message_search_invariant.py", "test_message_search_twins.py", "test_message_search_index.py",
    "test_message_search_contract.py", "test_message_search_refusals.py", "test_message_search_state.py",
    "test_message_search_ledger.py", "test_message_search_boundary.py", "test_message_search_review_fixes.py")]

MUTANTS = [
    ("no_release_recheck", P + "search_release.py",
     'if decision.verdict == "permit" and _locator_disclosable(qualified, rows) else None)',
     'if True else None)'),
    ("recheck_trusts_the_index", P + "search_release.py",
     '                    decision = source_message_decision(policy, qualified)\n                    # The locator door',
     '                    decision = source_message_decision(policy, qualified)\n                    decision = decision.model_copy(update={"verdict": "permit", "matched_allow_clause_ids": ["permit-work-not-personal"]})\n                    # The locator door'),
    ("skip_window", P + "search_release.py",
     "if (event_us is None or not lower_us <= event_us <= upper_us or is_record_nsfw(row)",
     "if (event_us is None or is_record_nsfw(row)"),
    ("skip_nsfw_at_release_and_build", P + "search_release.py",
     "or is_record_nsfw(row)\n", "\n"),
    ("skip_nsfw_at_build", P + "search_index.py",
     "if (identity.table not in tables or is_record_nsfw(row) or event_us is None",
     "if (identity.table not in tables or event_us is None"),
    ("rank_over_all_of_p_not_window", P + "search_lanes.py",
     "    index = within(index, lower_us, upper_us)\n", "\n"),
    ("truncate_instead_of_cap_refusal", P + "search_index.py",
     "over_cap = len(members) > policy.search.max_permitted_records",
     "over_cap = False"),
    ("no_k_bound", P + "search_release.py",
     "if intent.k > policy.search.max_k:", "if False:"),
    ("no_window_inside_grant", P + "search_release.py",
     "if intent.window.after < now - window.max_age_seconds or intent.window.before > now + 1:", "if False:"),
    ("one_key_for_all_grants", P + "opaque_ids.py",
     "key = secrets.token_bytes(32)", "key = bytes(32)"),
    ("id_from_rowid", P + "opaque_ids.py",
     'return "r." + hmac.new(key, DOMAIN + body, hashlib.sha256).hexdigest()',
     'return "r." + (record_id.split(":")[-1].rjust(64, "0"))[-64:] if record_id.split(":")[-1].isdigit() else "r." + hmac.new(key, DOMAIN + body, hashlib.sha256).hexdigest()'),
    ("no_key_rotation_on_revoke", P + "search_index.py",
     "        purge(self.root, grant_id)\n        self.keys.delete(grant_id)", "        purge(self.root, grant_id)"),
    ("no_own_index_check_in_request", P + "search_release.py",
     "        self.index.check_own(signed.grant_id, authority, now=now)\n", "\n"),
    ("sweep_ignores_clock", P + "search_index.py",
     '"clock_id": clock[0], "clock_generation": clock[1]}', '"clock_id": clock[0], "clock_generation": 0}'),
    ("sweep_ignores_row_changes", P + "search_index.py",
     'if _member_fingerprint(rows, facts, table=member["table"]) != member["fingerprint"]:', "if False:"),
    ("sweep_ignores_lineage", P + "search_index.py",
     'if deep and _lineage_fingerprint(conn, member, dict(rows[0]).get("content")) != member["lineage"]:', "if False:"),
    ("fingerprint_every_column", P + "search_index.py",
     '    parts = [_row_revision(dict(rows[0]), table=table)] + [_row_revision(dict(found[0]), table="signal_objects")',
     '    parts = [row_digest(rows[0])] + [row_digest(found[0])'),
    ("no_locator_disclosability", P + "search_release.py",
     'if decision.verdict == "permit" and _locator_disclosable(qualified, rows) else None)',
     'if decision.verdict == "permit" else None)'),
    ("request_sweeps_every_grant", P + "search_release.py",
     "        self.index.check_own(signed.grant_id, authority, now=now)\n",
     "        self.index.sweep(now=now)\n"),
    ("transport_skips_authority_recheck", P + "search_transport.py",
     '        if authority.model_dump() != result["authority"]:', "        if False:"),
    ("rebuild_all_stops_on_first_error", P + "search_index.py",
     "            except Exception:  # noqa: BLE001 -- a failed rebuild leaves no index for that grant\n                purge(self.root, grant_id)\n                states[grant_id] = \"failed\"",
     "            except KeyboardInterrupt:\n                raise"),
    ("forget_rotates_any_grant_key", P + "search_index.py",
     "            purge(self.root, grant_id)       # never rotate another capability's record-id key",
     "            self.forget(grant_id)"),
    ("refusal_without_receipt", P + "search_release.py",
     "            self._refuse(lease, signed.grant_id)\n",
     "            raise PolicyError(\"permission_denied\")\n"),
    ("no_blackhole_purge_hook", "topos/features/lifecycle/blackhole.py",
     "    purge_for_database(conn)\n", "    pass\n"),
    ("load_ignores_basis", P + "search_index.py",
     '                if basis.get(field) != getattr(authority, field):', '                if False:'),
    ("hold_gate_through_send", P + "search_transport.py",
     "        await asyncio.wait_for(ws.send(canonical_bytes(frame).decode(\"ascii\")), SEND_TIMEOUT_SECONDS)",
     "        from topos.storage.db.write_gate import _WRITE_LOCK\n        with _WRITE_LOCK:\n            await asyncio.wait_for(ws.send(canonical_bytes(frame).decode(\"ascii\")), SEND_TIMEOUT_SECONDS)"),
    ("summary_ceiling_allowed", P + "search_contract.py",
     '    ceiling: Literal["raw"]', '    ceiling: Literal["summary", "inference", "raw"]'),
    ("score_field_in_view", P + "search_contract.py",
     "    content: Annotated[str, StringConstraints(strict=True, max_length=MAX_RECORD_CHARS)]\n\n\nclass MessageSearchResult",
     "    content: Annotated[str, StringConstraints(strict=True, max_length=MAX_RECORD_CHARS)]\n    score: float = 0.0\n\n\nclass MessageSearchResult"),
    ("p2a_checkpoint_accepts_search", P + "ledger.py",
     '            if envelope.capability_version == "permissions-beta/p2c-v1":\n                raise PolicyError("unsupported_capability")  # a search is checkpointed only as a set\n', ""),
    ("set_shape_unchecked", P + "ledger.py",
     "                self._search_shape(policy, decision, parsed_output, members)\n", ""),
    ("index_keeps_raw_identity_unsealed", P + "search_index.py",
     '            built.append((Member(opaque, event_us, len(tokens), terms, sealed), opaque, identity, vectors))',
     '            terms[identity.record_id] = 1\n            built.append((Member(opaque, event_us, len(tokens), terms, sealed), opaque, identity, vectors))'),
    ("old_records_kept_in_index", P + "search_index.py",
     "                                    or event_us < (now - policy.search.window.max_age_seconds) * 1_000_000):",
     "                                    ):"),
    ("global_df_bm25", P + "search_lanes.py",
     "    frequency = {term: sum(1 for member in members if term in member.terms) for term in terms}",
     "    frequency = {term: 1 for term in terms}"),
    ("query_logged", P + "search_release.py",
     "        intent = SearchIntent.parse(payload)\n",
     "        intent = SearchIntent.parse(payload)\n        import logging; logging.getLogger(__name__).info('search %s', intent.query)\n"),
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--only", nargs="*")
    args = parser.parse_args()
    python = sys.executable
    results = []
    with tempfile.TemporaryDirectory(prefix="p2c-mutants-") as scratch:
        base = Path(scratch) / "engine"
        base.mkdir()
        for part in ("topos", "tests", "fixtures", "pyproject.toml", "shared"):
            source = ROOT / part
            if source.is_dir():
                shutil.copytree(source, base / part, ignore=shutil.ignore_patterns("__pycache__"))
            elif source.exists():
                shutil.copy2(source, base / part)
        for name, path, old, new in MUTANTS:
            if args.only and name not in args.only:
                continue
            target = base / path
            original = target.read_text()
            if original.count(old) != 1:
                results.append({"mutant": name, "status": "patch_not_applicable", "count": original.count(old)})
                continue
            target.write_text(original.replace(old, new))
            try:
                env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "P2C_SEEDS": "8", "P2C_FAMILY_N": "20"}
                run = subprocess.run([python, "-m", "pytest", *TESTS, "-q", "-x", "-p", "no:cacheprovider"],
                                     cwd=base, env=env, capture_output=True, text=True, timeout=1800)
                tail = [line for line in run.stdout.splitlines() if "passed" in line or "failed" in line][-1:]
                killed = run.returncode != 0
                failing = [line.split(" ")[1] for line in run.stdout.splitlines() if line.startswith("FAILED ")][:3]
                results.append({"mutant": name, "status": "killed" if killed else "SURVIVED", "summary": tail,
                                "killed_by": failing})
            finally:
                target.write_text(original)
            print(results[-1], flush=True)
    killed = sum(result["status"] == "killed" for result in results)
    report = {"mutants": len(results), "killed": killed, "results": results}
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"mutants": len(results), "killed": killed}))
    return 0 if killed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
