"""Mutation run over the guards bookkeeping batch 5 adds (E1 and E2).

Each mutant is one edit to a scratch copy of this tree, exported from git so the copy is
the committed code. Its targeted tests then run: a mutant is KILLED when they fail. A
survivor is either a missing test or an equivalent mutant, and has to be argued for by
name in the report.

    TOPOS_ENV_FILE=<scratch> TMPDIR=<non-symlinked> python scripts/permissions_v2/bk5_mutants.py [--only NAME]
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
CONTRACT = "topos/permissions_v2/contract.py"
LEDGER = "topos/permissions_v2/ledger.py"
RELEASE = "topos/permissions_v2/release.py"
FACT_RELEASE = "topos/permissions_v2/fact_release.py"
SEARCH_RELEASE = "topos/permissions_v2/search_release.py"

BUDGET = "tests/permissions_v2/test_bk5_read_budget_in_policy.py"
ADMISSION = "tests/permissions_v2/test_bk5_admission_before_the_floor.py"
ATTESTED = "tests/permissions_v2/test_source_release_attested.py"
LEDGER_TESTS = "tests/permissions_v2/test_contract_and_ledger.py"
SEARCH_TESTS = "tests/permissions_v2/test_message_search_refusals.py"

# name -> (file, find, replace, targeted tests)
MUTANTS: dict[str, tuple] = {
    # --- E1: the declaration is optional, bounded, and invisible when undeclared ----
    "e1_serializer_keeps_the_undeclared_key": (
        CONTRACT, 'if encoded.get("read_budget_per_day") is None:\n            encoded.pop("read_budget_per_day", None)',
        "pass", [BUDGET, ATTESTED]),
    "e1_serializer_drops_a_declared_budget": (
        CONTRACT, 'if encoded.get("read_budget_per_day") is None:',
        'if True:', [BUDGET]),
    "e1_budget_may_be_zero": (
        CONTRACT, "ReadBudget = Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]",
        "ReadBudget = Annotated[int, Field(strict=True, ge=0, le=MAX_INTEGER)]", [BUDGET]),
    "e1_budget_unbounded_above": (
        CONTRACT, "ReadBudget = Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]",
        "ReadBudget = Annotated[int, Field(strict=True, ge=1)]", [BUDGET]),
    "e1_budget_not_strict": (
        CONTRACT, "ReadBudget = Annotated[int, Field(strict=True, ge=1, le=MAX_INTEGER)]",
        "ReadBudget = Annotated[int, Field(ge=1, le=MAX_INTEGER)]", [BUDGET]),
    "e1_explicit_null_is_accepted": (
        CONTRACT, 'if isinstance(value, dict) and "read_budget_per_day" in value and value["read_budget_per_day"] is None:\n            raise ValueError("read budget present but undeclared")',
        "pass", [BUDGET]),

    # --- E2: the tombstone, the claim, and its terminality -------------------------
    "e2_refusal_stores_the_envelope": (
        LEDGER, 'self._claim(conn, admission, envelope_json="", status="refused", now=now)',
        'self._claim(conn, admission, envelope_json=admission.encoded, status="refused", now=now)', [ADMISSION]),
    "e2_refusal_claims_as_admitted": (
        LEDGER, 'self._claim(conn, admission, envelope_json="", status="refused", now=now)',
        'self._claim(conn, admission, envelope_json="", status="admitted", now=now)', [ADMISSION]),
    "e2_claim_drops_the_replay_select": (
        LEDGER, 'if conn.execute("SELECT 1 FROM p2a_requests WHERE request_id=?", (admission.lease.request_id,)).fetchone():\n            raise PolicyError("request_replay")',
        "pass", [ADMISSION, LEDGER_TESTS]),
    "e2_a_refused_row_may_be_checkpointed": (
        LEDGER, 'if row["status"] != "admitted":', 'if row["status"] not in ("admitted", "refused"):',
        [ADMISSION, LEDGER_TESTS]),
    "e2_refuse_never_claims_without_a_decision": (
        LEDGER, "if admission.status is not None:\n            return None",
        "if admission.status is not None or raw_decision is None:\n            return None", [ADMISSION]),
    "e2_refuse_writes_no_receipt": (
        LEDGER, "if raw_decision is not None:", "if False:", [ADMISSION, SEARCH_TESTS]),
    "e2_locator_claims_before_its_floors": (
        RELEASE, "admission = ledger.verify(envelope, request=request, payload=intent.model_dump(), now=self.clock())",
        "admission = ledger.verify(envelope, request=request, payload=intent.model_dump(), now=self.clock())\n"
        "            ledger.admit_verified(admission, now=self.clock())", [ADMISSION]),
    "e2_locator_drops_the_handler_claim": (
        RELEASE, "                try:\n                    ledger.refuse(admission, now=self.clock())\n                except Exception:  # noqa: BLE001\n                    pass\n                raise",
        "                raise", [ADMISSION]),
    "e2_fact_door_keeps_the_envelope_on_refusal": (
        FACT_RELEASE, "ledger.refuse(admission, decision.model_dump(), candidate_revision=decision.candidate_revision,\n                                  now=self.clock())",
        "ledger.checkpoint_decision(ledger.admit_verified(admission, now=self.clock()), decision.model_dump(),\n"
        "                        candidate_revision=decision.candidate_revision, output=None, now=self.clock())", [ADMISSION]),
    "e2_search_door_keeps_the_envelope_on_refusal": (
        SEARCH_RELEASE, "admission = ledger.verify(envelope, request=request, payload=signed_payload(intent), now=self.clock())",
        "admission = ledger.verify(envelope, request=request, payload=signed_payload(intent), now=self.clock())\n"
        "            ledger.admit_verified(admission, now=self.clock())", [ADMISSION]),
    "e2_search_refusal_never_spends_the_id": (
        SEARCH_RELEASE, "        except Exception:  # noqa: BLE001 -- the receipt rolled back with its row; spend the id alone\n            self._tombstone(admission)",
        "        except Exception:  # noqa: BLE001\n            pass", [ADMISSION, SEARCH_TESTS]),
}


def run(name: str, spec: tuple, export: Path, interpreter: str, env: dict) -> dict:
    target, find, replace, tests = spec
    with tempfile.TemporaryDirectory(prefix=f"bk5-mutant-{name}-", dir=str(export.parent)) as scratch:
        tree = Path(scratch) / "tree"
        shutil.copytree(export, tree, symlinks=True)
        path = tree / target
        source = path.read_text()
        if find not in source:
            return {"mutant": name, "status": "NOT_APPLIED", "reason": "pattern absent", "file": target}
        path.write_text(source.replace(find, replace, 1))
        result = subprocess.run([interpreter, "-m", "pytest", *tests, "-x", "-q", "-p", "no:cacheprovider"],
                                cwd=tree, env={**env, "PYTHONPATH": str(tree)}, capture_output=True, text=True)
        killed = result.returncode != 0
        tail = [line for line in result.stdout.splitlines() if line.startswith(("FAILED", "ERROR"))][:3]
        return {"mutant": name, "file": target, "tests": tests, "status": "KILLED" if killed else "SURVIVED",
                "first_failures": tail}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--interpreter", default=sys.executable)
    args = parser.parse_args()
    env = {key: value for key, value in os.environ.items()}
    env.setdefault("TOPOS_KEY", "synthetic-mutation-key")
    with tempfile.TemporaryDirectory(prefix="bk5-mutants-") as staging:
        export = Path(staging) / "export"
        export.mkdir()
        subprocess.run(f"git -C {ROOT} archive HEAD | tar -x -C {export}", shell=True, check=True)
        results = []
        for name, spec in MUTANTS.items():
            if args.only and name not in args.only:
                continue
            outcome = run(name, spec, export, args.interpreter, env)
            results.append(outcome)
            print(f"{outcome['status']:12} {name}", flush=True)
    summary = {"total": len(results), "killed": sum(1 for r in results if r["status"] == "KILLED"),
               "survived": [r["mutant"] for r in results if r["status"] == "SURVIVED"],
               "not_applied": [r["mutant"] for r in results if r["status"] == "NOT_APPLIED"], "results": results}
    print(json.dumps({k: summary[k] for k in ("total", "killed", "survived", "not_applied")}, indent=2))
    if args.out:
        args.out.write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if not summary["survived"] and not summary["not_applied"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
