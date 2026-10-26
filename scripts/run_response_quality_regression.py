#!/usr/bin/env python3
"""Paired, offline node + home-chat contract gate. No live DB or model calls.

before --change RQ-ID --note 'hypothesis'; after --baseline PATH --note 'change'
Reports contain test identities/statuses, hashes and timings, never test stdout.
Failures (including pre-existing ones) and skipped coverage prevent a green gate.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CATALOG = Path(__file__).with_name("response_quality_catalog.json")


def digest(data):
    return hashlib.sha256(data).hexdigest()


def source_map(catalog, lane):
    result = {}
    for family, lanes in catalog["families"].items():
        for path in lanes.get(lane, []):
            if path in result:
                raise ValueError(f"Duplicate catalog file: {path}")
            result[path] = family
    return result


def parse_junit(path, lane, files):
    """Normalize pytest and Vitest JUnit; fail closed on unclassified/duplicate cases."""
    rows = {}
    root = ET.parse(path).getroot()
    for suite in root.iter("testsuite"):
        for case in suite.findall("testcase"):
            hints = [case.get("file", ""), case.get("classname", ""), suite.get("name", "")]
            matches = [p for p in files if any(
                h == p or h.endswith("/" + p) or h == p.removesuffix(".py").replace("/", ".")
                or h.startswith(p.removesuffix(".py").replace("/", ".") + ".")
                for h in hints)]
            if len(matches) != 1:
                raise ValueError(f"Cannot classify test: {hints} / {case.get('name')}")
            file = matches[0]
            # A suite's classname can contain a machine-specific absolute path.
            classname = case.get("classname", "").replace("\\", "/")
            if file in classname:
                classname = classname[classname.index(file):]
            key = f"{lane}::{file}::{classname}::{case.get('name')}"
            if key in rows:
                raise ValueError(f"Duplicate test ID: {key}")
            status = ("failed" if case.find("failure") is not None else
                      "error" if case.find("error") is not None else
                      "skipped" if case.find("skipped") is not None else "passed")
            rows[key] = {"family": files[file], "status": status,
                         "seconds": float(case.get("time", "0"))}
    missing = [p for p in files if not any(f"::{p}::" in key for key in rows)]
    if missing:
        raise ValueError(f"No test results for catalog files: {missing}")
    return rows


def compare(before, after):
    reasons = []
    for field in ("catalog_hash", "fixture_hash", "model", "toolchain", "runtime_policy"):
        if before.get(field) != after.get(field):
            reasons.append(f"{field} changed")
    old, new = before["cases"], after["cases"]
    missing = sorted(set(old) - set(new))
    added = sorted(set(new) - set(old))
    if missing or added:
        reasons.append("test inventory changed")
    regressions = sorted(k for k in old.keys() & new.keys()
                         if old[k]["status"] == "passed" and new[k]["status"] != "passed")
    fixes = sorted(k for k in old.keys() & new.keys()
                   if old[k]["status"] in ("failed", "error") and new[k]["status"] == "passed")
    outstanding = sorted(k for k, v in new.items() if v["status"] != "passed")
    if before["errors"] or after["errors"]:
        reasons.append("runner/collection failure")
    verdict = ("NOT_COMPARABLE" if reasons else "FAIL" if outstanding or regressions else "PASS")
    return dict(verdict=verdict, reasons=reasons, regressions=regressions, fixes=fixes,
                missing=missing, added=added, outstanding=outstanding)


def command_output(args, cwd):
    r = subprocess.run(args, cwd=cwd, capture_output=True, timeout=30)
    if r.returncode:
        raise RuntimeError(f"Command failed: {args[0]}")
    return r.stdout


def revision(repo):
    # Include dirty and untracked source in provenance without publishing its contents.
    head = command_output(["git", "rev-parse", "HEAD"], repo).decode().strip()
    delta = command_output(["git", "diff", "HEAD", "--binary"], repo)
    untracked = command_output(["git", "ls-files", "--others", "--exclude-standard", "-z"], repo)
    h = hashlib.sha256(delta)
    for raw in sorted(untracked.split(b"\0")):
        if raw:
            p = repo / raw.decode()
            if p.is_file():
                h.update(raw)
                h.update(p.read_bytes())
    return {"head": head, "dirty": bool(delta or untracked), "working_tree_hash": h.hexdigest()}


def run(catalog, roots, artifact_dir):
    rows, errors, commands, elapsed = {}, [], {}, {}
    for lane, repo in roots.items():
        files = source_map(catalog, lane)
        xml = artifact_dir / f"{lane}.xml"
        if lane == "node":
            cmd = [str(repo / ".venv/bin/python"), "-m", "pytest", *files,
                   "-q", "--tb=short", "-o", "junit_family=xunit1", f"--junitxml={xml}"]
        else:
            cmd = [str(repo / "node_modules/.bin/vitest"), "run", *files,
                   "--reporter=junit", f"--outputFile={xml}"]
        commands[lane] = cmd
        start = time.monotonic()
        try:
            with (artifact_dir / f"{lane}.log").open("w") as log:
                process = subprocess.run(cmd, cwd=repo, stdout=log, stderr=subprocess.STDOUT, timeout=600,
                                         env={**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
            result = parse_junit(xml, lane, files)
            rows.update(result)
            # pytest exit 1 / Vitest exit 1 are assertion failures, not missing output.
            if process.returncode not in (0, 1) or (
                process.returncode and all(r["status"] == "passed" for r in result.values())
            ):
                errors.append(f"{lane}: runner exit {process.returncode}")
        except (OSError, ValueError, ET.ParseError, subprocess.TimeoutExpired) as exc:
            errors.append(f"{lane}: {type(exc).__name__}: {exc}")
        elapsed[lane] = round(time.monotonic() - start, 3)
    return rows, errors, commands, elapsed


def counts(report, family):
    c = Counter(v["status"] for v in report["cases"].values() if v["family"] == family)
    return f"{c['passed']} pass / {c['failed'] + c['error']} fail / {c['skipped']} skip"


def append_log(path, report, report_path, before=None):
    title = f"Regression gate — {report['change']} — {report['phase']}"
    lines = ["", f"### {title}", "", f"- Recorded (UTC): {report['timestamp']}",
             f"- Note: {' '.join(report['note'].splitlines())}",
             f"- Report: [machine-readable results]({report_path.relative_to(path.parent).as_posix()})",
             f"- Catalog: `{report['catalog_version']}`; {report['model']}.",
             f"- Gate: **{report['verdict']}**. Skips and existing failures are not waived.",
             "- Limits: contract/replay coverage, not live-model faithfulness or latency. Test timings are not response timings.",
             "", "| Question family | Before | After |", "| --- | --- | --- |"]
    for family in report["families"]:
        lines.append(f"| {family} | {counts(before or report, family)} | {counts(report, family) if before else 'pending'} |")
    if before:
        diff = report["comparison"]
        lines += ["", f"- Fixed: {len(diff['fixes'])}; regressed: {len(diff['regressions'])}; "
                  f"missing: {len(diff['missing'])}; added: {len(diff['added'])}; "
                  f"remaining non-passes: {len(diff['outstanding'])}."]
        for kind in ("reasons", "regressions", "fixes", "missing"):
            lines += [f"- {kind}: `{item}`" for item in diff[kind]]
    lines += [f"- Runner error: {error}" for error in report["errors"]]
    with path.open("a") as f:
        f.write("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["before", "after"])
    parser.add_argument("--change")
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--note", required=True)
    parser.add_argument("--web-root", type=Path, default=ROOT.parent / "topos-react-app/next")
    args = parser.parse_args()
    before = json.loads(args.baseline.read_text()) if args.baseline else None
    if args.phase == "after" and (not before or before.get("phase") != "before"):
        parser.error("after requires --baseline pointing to a before report")
    if args.phase == "before" and before:
        parser.error("before does not accept --baseline")
    change = args.change or (before or {}).get("change", "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,100}", change):
        parser.error("provide a safe --change ID, e.g. RQ-2026-10-26-04")
    if before and change != before["change"]:
        parser.error("change ID must match baseline")
    catalog = json.loads(CATALOG.read_text())
    roots = {"node": ROOT, "web": args.web_root.resolve()}
    fixture = hashlib.sha256()
    for lane, root in roots.items():
        for file in sorted(source_map(catalog, lane)):
            fixture.update(f"{lane}:{file}".encode())
            fixture.update((root / file).read_bytes())
    now = datetime.now(timezone.utc)
    run_id = f"{now.strftime('%Y%m%dT%H%M%S%fZ')}-{change}-{args.phase}"
    artifacts = ROOT / ".response-quality-artifacts" / run_id
    artifacts.mkdir(parents=True)
    report = {"schema": 1, "phase": args.phase, "change": change, "note": args.note,
              "timestamp": now.isoformat(), "catalog_version": catalog["version"],
              "catalog_hash": digest(CATALOG.read_bytes()), "fixture_hash": fixture.hexdigest(),
              "runtime_policy": {"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
              "model": catalog["model"], "families": list(catalog["families"]),
              "revisions": {lane: revision(root) for lane, root in roots.items()},
              "toolchain": {"python": command_output([str(ROOT / '.venv/bin/python'), '--version'], ROOT).decode().strip(),
                            "node": command_output(['node', '--version'], roots['web']).decode().strip(),
                            "node_lock": digest((ROOT / 'uv.lock').read_bytes()),
                            "web_lock": digest((roots['web'] / 'package-lock.json').read_bytes())}}
    print(f"Running {catalog['version']} across {len(report['families'])} families…", flush=True)
    report["cases"], report["errors"], report["commands"], report["test_seconds"] = run(catalog, roots, artifacts)
    end_fixture = hashlib.sha256()
    for lane, root in roots.items():
        for file in sorted(source_map(catalog, lane)):
            end_fixture.update(f"{lane}:{file}".encode())
            end_fixture.update((root / file).read_bytes())
    if end_fixture.hexdigest() != report["fixture_hash"] or digest(CATALOG.read_bytes()) != report["catalog_hash"]:
        report["errors"].append("Catalog/test files changed during execution; rerun with a frozen snapshot")
    if before:
        report["baseline"] = str(args.baseline.resolve())
        report["comparison"] = compare(before, report)
        report["verdict"] = report["comparison"]["verdict"]
    else:
        report["verdict"] = "FAIL" if report["errors"] or any(v["status"] != "passed" for v in report["cases"].values()) else "PASS"
    out = ROOT / "docs/response-quality-runs" / f"{run_id}.json"
    out.parent.mkdir(exist_ok=True)
    with out.open("x") as f:
        json.dump(report, f, indent=2, sort_keys=True)
        f.write("\n")
    append_log(ROOT / "docs/response-quality-changelog.md", report, out, before)
    print(f"{report['verdict']}: {len(report['cases'])} tests; {len(report['errors'])} runner errors\nReport: {out}")
    for error in report["errors"]:
        print(error)
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
