"""OD-52 P7: how many browsing interests (cluster-months) qualify, before and after each guard. Counts only.

Owner-local, on a keyless census copy (`census_copy.py`), read-only and immutable. For the 30 / 90 / 365-day
windows it counts the (topic cluster, month) pairs `permissions_v2.interest_family` would build, and how many
survive each guard in the order the node applies them:

    candidates (at least one browser visit in the cluster-month)
    threshold on every visit (5 visits on 3 distinct days, no guard yet)
    after each visit guard: incognito, nsfw, excluded, provenance (threshold recomputed on what is left)
    after each label guard: browsing (per month), label_form, label_host, label_title, label_person,
        excluded_label, opted_out, offlimits
    after assessment (a current, releasable machine assessment of the label; none exist until the node runs one)

A month counts in a window when the whole period is inside it: the elapsed part of the current month, or a whole
past month (`interest_family.period_inside`). `windows` is what a grant that releases day-level time sees;
`windows_whole_months_only` is what a grant that releases no time sees (IF-5 Q&A I1: whole months only).

Nothing but integers and fixed codes leaves this script: no label, cluster id, URL, title, host, name or record
id is printed or written. Nothing is written to any store.

Run (zsh; every flag its own token):
  export TOPOS_DATABASE_PATH=<scratch>/throwaway.db TOPOS_ENV_FILE=<scratch>/topos.env
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/interest_family_measure.py \\
      --copy <candidates>/census-copy/<run-id> --out <LC>/runs/<run-id>-p7/interest-measure.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

VERSION = "interest-family-measure/v1"
WINDOWS = {"d30": 30, "d90": 90, "d365": 365}


def _stages():
    from topos.permissions_v2 import interest_family as fam
    return (["candidates", "threshold_all"] + [f"after_{check}" for check in fam.VISIT_CHECKS]
            + [f"after_{check}" for check in fam.LABEL_CHECKS] + ["after_assessment"])


def _inside(candidate, now_us: int, days: int, *, strict: bool) -> bool:
    from topos.permissions_v2 import interest_family as fam
    if strict and not candidate.complete:
        return False
    return fam.period_inside(period_start_us=candidate.period_start_us, period_end_us=candidate.period_end_us,
                             now_us=now_us, max_age_seconds=days * 86_400)


def measure(conn, *, owner_id: str, now_us: int, opt_outs: frozenset = frozenset(), boundary=None) -> dict:
    """The count-only report for one read snapshot. Pure: reads ``conn``, returns integers and codes."""
    from topos.permissions_v2 import capture_receipts as cr
    from topos.permissions_v2 import interest_family as fam
    from topos.permissions_v2 import interest_review as ir
    from topos.permissions_v2.entity_boundary import EntityBoundary

    boundary = boundary if boundary is not None else EntityBoundary(conn)
    result = fam.build(conn, owner_id=owner_id, now_us=now_us, boundary=boundary, opt_outs=opt_outs)
    objects = {obj.interest_id: obj for obj in result.objects}
    context_revision, _terms = ir.context(boundary)
    qualifies = {key: ir.qualifies(ir.current(conn, owner_id=owner_id, obj=obj, context_revision=context_revision))
                 for key, obj in objects.items()}
    label_order = list(fam.LABEL_CHECKS)

    def funnel(days: int, *, strict: bool) -> dict:
        rows = {stage: 0 for stage in _stages()}
        for c in result.candidates:
            if not _inside(c, now_us, days, strict=strict):
                continue
            rows["candidates"] += 1
            if not c.qualifies("all"):
                continue
            rows["threshold_all"] += 1
            # Visit guards are cumulative: the threshold is recomputed on the visits every guard so far left.
            survived = 0
            for check in fam.VISIT_CHECKS:
                if not c.qualifies(check):
                    break
                rows[f"after_{check}"] += 1
                survived += 1
            if survived < len(fam.VISIT_CHECKS):
                continue
            failed_at = label_order.index(c.label_withheld) if c.label_withheld else len(label_order)
            for index, check in enumerate(label_order):
                if index >= failed_at:
                    break
                rows[f"after_{check}"] += 1
            if failed_at == len(label_order) and qualifies.get(fam.interest_id(c.cluster_id, c.month)):
                rows["after_assessment"] += 1
        return rows

    visits = {key: int(value) for key, value in result.visit_counts.items()}
    visits["placed_in_months"] = sum(c.visits["all"] for c in result.candidates)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(activity_events)")}
    report = {
        "schema": VERSION,
        "build_schema": result.schema,
        "rule": {"min_visits": fam.MIN_VISITS, "min_days": fam.MIN_DAYS, "bands": [list(b) for b in fam.BANDS]},
        "preconditions": {
            "activity_writer_columns": {"writer_class", "writer_app_id", "writer_dataset_id"} <= columns,
            "browser_install_certified": cr.install_dataset(conn, owner_id=owner_id, source_id=fam.SOURCE_ID) is not None,
            "live_receipts_activity": len([r for r in cr.receipts(conn, owner_id=owner_id)
                                           if r["table"] == fam.TABLE and r["revoked_at"] is None]),
            "offlimits_active": bool(boundary.active),
            "opt_outs_read": len(opt_outs),
        },
        "visits_in_clusters": visits,
        "clusters_with_browser_members": len({c.cluster_id for c in result.candidates}),
        "labels_pending_assessment": len({obj.label_revision for key, obj in objects.items() if not qualifies[key]}),
        "windows": {name: funnel(days, strict=False) for name, days in WINDOWS.items()},
        "windows_whole_months_only": {name: funnel(days, strict=True) for name, days in WINDOWS.items()},
        "band_mix_deterministic": {band: sum(1 for obj in result.objects if obj.band == band) for band, _ in fam.BANDS},
    }
    return report


def table(report: dict) -> str:
    """The funnel as a Markdown table: stages down, windows across."""
    names = list(WINDOWS)
    lines = ["| stage | " + " | ".join(f"{WINDOWS[n]} d" for n in names) + " |",
             "|---|" + "---:|" * len(names)]
    for stage in _stages():
        lines.append(f"| {stage} | " + " | ".join(str(report["windows"][n][stage]) for n in names) + " |")
    lines.append("| whole months only, after_assessment | " + " | ".join(
        str(report["windows_whole_months_only"][n]["after_assessment"]) for n in names) + " |")
    lines.append("| whole months only, after_offlimits | " + " | ".join(
        str(report["windows_whole_months_only"][n]["after_offlimits"]) for n in names) + " |")
    return "\n".join(lines)


def _opt_outs(reviews: Path) -> frozenset:
    import census_support as cs
    if not reviews.exists():
        return frozenset()
    conn = cs.ro(reviews, immutable=True)
    try:
        found = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fact_opt_outs'").fetchone()
        return frozenset(r[0] for r in conn.execute("SELECT fact_id FROM fact_opt_outs")) if found else frozenset()
    finally:
        conn.close()


def main(argv=None) -> int:
    import census_support as cs

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--copy", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    cs.require_scratch_environment()
    copy_root = cs.refuse_live(args.copy.expanduser().absolute())
    out = cs.refuse_live(args.out.expanduser().absolute())
    manifest = json.loads((copy_root / "census-copy-manifest.json").read_text())
    if not manifest["consistency"]["consistent"]:
        raise cs.CensusRefused("copy_not_consistent")
    owner_id = cs.load_config(copy_root)["identity"]["owner_id"]
    opt_outs = _opt_outs(copy_root / "permissions-v2" / "evidence-reviews.db")
    conn = cs.ro(copy_root / "database.db", immutable=True)
    try:
        report = measure(conn, owner_id=owner_id, now_us=int(manifest["copied_at"]) * 1_000_000, opt_outs=opt_outs)
    finally:
        conn.close()
    report["copy"] = {"run_id": manifest["run_id"], "copied_at_utc": manifest["copied_at_utc"]}
    cs.write_private(out, json.dumps(report, indent=2, sort_keys=True).encode("utf-8"))
    print(table(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
