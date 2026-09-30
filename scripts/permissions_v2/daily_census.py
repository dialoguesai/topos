"""OD-20 daily census diff: one keyless, count-only census of the active p2c-v3 grant a day, diffed with the day before.

Design and parameters: audits/2026-09-14-permissions/latency-coverage-2026-09-28/ws1/OD20_DAILY_CENSUS_DIFF_PROPOSAL.md
(owner-delegated picks recorded under OD-20: 06:30 local, free disk after the copy >= 8 GiB, 30-day retention, two
hand dry runs before WS0 stages a LaunchAgent that the owner installs). Nothing here schedules itself.

One run:
  1. preflight: refuse unless free disk after the copy stays >= 8 GiB            -> skipped_disk
  2. quiet gate: the node's permission stores untouched for 5-10 min (stat only);
     retried every 20 min, 3 times                                               -> skipped_busy
  3. engine drift: the mirrored engine functions must match the census's pins    -> void
  4. one consistent copy WITHOUT the grant key (census_copy.make_copy keys=False)-> void when no attempt agrees
  5. the census with an ephemeral key: counts only, no private file, the index compared by count
  6. aggregate + diff against the most recent earlier day, under <out>/<YYYY-MM-DD>/; days older than 30 are removed
  7. the copy and the scratch directory are deleted in `finally`, whatever happened
Exit 0 ok, 1 alert, 2 skipped or void. Prints one line of counts, alert codes and info codes; never a name, id or content.

Run (zsh, each flag its own token), from the engine worktree with the scratch environment exported:
  PYTHONPATH=$PWD <engine venv>/bin/python3 scripts/permissions_v2/daily_census.py \\
      --scratch <job scratch dir> --out <LC>/runs/daily
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import census_support as cs  # noqa: E402

FLOOR_BYTES = 8 * (1 << 30)
QUIET_SECONDS = {"permissions-v2/ledger.db": 600, "permissions-v2/evidence-reviews.db": 300,
                 "permissions-v2/message-search/refresh-state.json": 600}
RETRY_SECONDS, RETRIES = 1200, 3
RETENTION_DAYS = 30
# Alert thresholds (the proposal's table).
DECAY_TOLERANCE = 0.9          # P_impl below 90% of yesterday's members still inside today's window
LOSS_GROWTH = 1.2              # a loss reason up more than 20% ...
LOSS_GROWTH_MIN = 5            # ... and by at least 5 rows, so a handful of rows is not an alert
ASSESSMENT_LAG = 0.05          # unassessed + stale above 5% of owner-shaped in-window rows
CAP_HEADROOM = 0.10            # max_permitted_records headroom below 10% of the cap
PROTECTED_BANDS = ("50_to_90pct", "over_90pct")


def quiet(source: Path, now: float, *, mtime=lambda path: path.stat().st_mtime) -> bool:
    """True when no permission store changed within its quiet time. A stat only: nothing is opened."""
    for rel, seconds in QUIET_SECONDS.items():
        path = Path(source) / rel
        if path.exists() and now - mtime(path) < seconds:
            return False
    return True


def preflight(needed_bytes: int, scratch: Path, *, floor: int = FLOOR_BYTES, disk_usage=shutil.disk_usage) -> bool:
    return disk_usage(scratch).free - needed_bytes >= floor


def _losses(aggregate: dict) -> dict:
    """Real losses by reason: engineering first check, no policy veto."""
    out: dict[str, int] = {}
    for row in aggregate.get("withheld_in_window", []):
        if row["reason_class"] == "engineering" and row["policy_veto"] == "none":
            out[row["reason_code"]] = out.get(row["reason_code"], 0) + row["count"]
    return out


def _grant_change(today: dict, yesterday: dict) -> dict:
    """What moved in the grant between two runs: {} when nothing did, or when either run cannot say."""
    moved = {}
    for key in ("policy_hash", "capability"):
        before, after = yesterday.get(key), today.get(key)
        if before is not None and after is not None and before != after:
            moved[key] = "changed"
    for key in ("max_age_seconds", "release_event_time"):
        before, after = (yesterday.get("window") or {}).get(key), (today.get("window") or {}).get(key)
        if before is not None and after is not None and before != after:
            moved[key] = {"was": before, "now": after}
    return moved


def diff(today: dict, yesterday: dict | None) -> dict:
    """The day's alerts, information and deltas, from two IF-1 aggregates. Pure; counts in, counts out."""
    alerts, info = [], []

    def alert(code, **detail):
        alerts.append({"code": code, **detail})
    gate = today.get("gate", {})
    if gate.get("unknown_reasons", 0) > 0:
        alert("unknown_reasons", count=gate["unknown_reasons"])
    if (today.get("node_source") or {}).get("drift"):
        alert("node_source_drift", functions=len(today["node_source"]["drift"]))
    if gate.get("void_reasons"):
        alert("census_void", reasons=list(gate["void_reasons"]))
    if today.get("index_state") in ("missing", "over_cap", "unreadable"):
        alert("grant_dark", state=today["index_state"])
    elif gate.get("keyless") and not gate.get("census_equals_live_after_aging"):
        comparison = today.get("index_comparison", {})
        alert("index_stale", census=comparison.get("census_members"), live=comparison.get("live_members"),
              aged_out=comparison.get("index_aged_out"))
    caps = today.get("caps", {})
    headroom = caps.get("max_permitted_records")
    if headroom is not None and headroom < CAP_HEADROOM * (headroom + (today.get("census_members") or 0)):
        alert("cap_headroom", headroom=headroom)
    if caps.get("protected_vocabulary_band") in PROTECTED_BANDS:
        alert("protected_vocabulary", band=caps["protected_vocabulary_band"])
    for action, last in ((today.get("job_state") or {}).get("last") or {}).items():
        states = [last.get("state")] + list(last.get("grant_states") or [])
        if "failed" in states and last.get("seconds_before_copy", 1e12) < 86400:
            alert("refresh_failed", action=action)
    losses = _losses(today)
    lag = sum(n for code, n in losses.items() if code == "unassessed" or code.startswith("review_stale"))
    owner_shaped = (today.get("U") or 0) - sum(row["count"] for row in today.get("withheld_in_window", [])
                                                if row["policy_veto"] == "not_owner_authored")
    if owner_shaped > 0 and lag / owner_shaped > ASSESSMENT_LAG:
        alert("assessment_lag", rows=lag, owner_shaped=owner_shaped)
    families = today.get("families") or {}
    if any(families.get(f, 0) for f in ("fact", "goal", "relationship")):
        info.append({"code": "typed_members", **{f: families.get(f, 0) for f in ("fact", "goal", "relationship")}})
    deltas = {"U": today.get("U"), "p_impl": today.get("census_members"), "losses": losses,
              "eligible": (today.get("pool") or {}).get("eligible"),
              "linked_total": (today.get("pool") or {}).get("linked_total")}
    if yesterday is not None:
        moved = _grant_change(today, yesterday)
        against = []

        def compared(code, **detail):
            """A finding that compares today with yesterday: an alert, unless the grant itself moved."""
            if moved:
                against.append({"code": code, **detail})
            else:
                alert(code, **detail)
        lower_day = ((today.get("window") or {}).get("lower_utc") or "")[:10]
        predicted = sum(n for day, n in ((yesterday.get("pool") or {}).get("p_impl_by_event_day") or {}).items()
                        if day >= lower_day)
        if predicted and (today.get("census_members") or 0) < DECAY_TOLERANCE * predicted:
            compared("p_impl_below_decay", p_impl=today.get("census_members"), predicted=predicted)
        zero_today = (today.get("pool") or {}).get("p_impl_zero_on")
        zero_before = (yesterday.get("pool") or {}).get("p_impl_zero_on")
        if zero_today and zero_before and zero_today < zero_before:
            compared("pool_zero_earlier", zero_on=zero_today, was=zero_before)
        before = _losses(yesterday)
        for code, n in sorted(losses.items()):
            if code not in before:
                compared("new_loss_reason", reason=code, count=n)
            elif n > LOSS_GROWTH * before[code] and n - before[code] >= LOSS_GROWTH_MIN:
                compared("loss_growth", reason=code, count=n, was=before[code])
        if (yesterday.get("pool") or {}).get("linked_total") != deltas["linked_total"]:
            info.append({"code": "provenance_changed", "linked_total": deltas["linked_total"]})
        if moved:
            # Yesterday's rows were counted under another grant, so these comparisons measure the change
            # itself. They are reported, not alerted, and tomorrow compares like with like again.
            info.append({"code": "grant_changed", "moved": moved, "comparisons": against})
        deltas.update({"U_change": (today.get("U") or 0) - (yesterday.get("U") or 0),
                       "p_impl_change": (today.get("census_members") or 0) - (yesterday.get("census_members") or 0)})
    else:
        info.append({"code": "first_day"})
    return {"alerts": alerts, "info": info, "deltas": deltas}


def previous(out_root: Path, day: str) -> dict | None:
    """The most recent earlier day's aggregate, if any."""
    days = sorted(p.name for p in Path(out_root).glob("????-??-??") if p.is_dir() and p.name < day
                  and (p / "if1-aggregate.json").exists())
    return json.loads((Path(out_root) / days[-1] / "if1-aggregate.json").read_text()) if days else None


def prune(out_root: Path, today: date) -> int:
    """Remove day directories older than the retention (count-only files; nothing private lives here)."""
    removed = 0
    for path in Path(out_root).glob("????-??-??"):
        try:
            day = date.fromisoformat(path.name)
        except ValueError:
            continue
        if path.is_dir() and day < today - timedelta(days=RETENTION_DAYS):
            shutil.rmtree(path)
            removed += 1
    return removed


def daily(source: Path, scratch: Path, out_root: Path, *, now: float | None = None, sleep=time.sleep,
          make_copy=None, census_run=None, mtime=None, disk_usage=shutil.disk_usage,
          node_root: Path | None = None) -> tuple[str, dict]:
    import census_copy
    import grant_census as gc
    make_copy = make_copy or census_copy.make_copy
    census_run = census_run or gc.run
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    scratch = cs.refuse_live(Path(scratch).expanduser().absolute())
    out_root = cs.refuse_live(Path(out_root).expanduser().absolute())
    copy_root = scratch / "copy"
    try:
        stores, _config = census_copy._stores(Path(source))
        needed = sum(census_copy._size(paths) for role, paths in stores.items() if role != "record_keys")
        cs.private_dir(scratch)
        if not preflight(needed, scratch, disk_usage=disk_usage):
            return "skipped_disk", {"needed_bytes": needed}
        for attempt in range(1 + RETRIES):
            if quiet(Path(source), time.time() if attempt else now, **({"mtime": mtime} if mtime else {})):
                break
            if attempt == RETRIES:
                return "skipped_busy", {"attempts": attempt + 1}
            sleep(RETRY_SECONDS)
        drift = sorted(name for name, digest in gc.mirrored_sources().items() if gc.PINNED.get(name) != digest)
        if drift:
            return "void", {"alerts": [{"code": "engine_source_drift", "functions": len(drift)}]}
        made = make_copy(Path(source), copy_root, None, keys=False, stores=stores)
        if made.get("void"):
            return "void", {"alerts": [{"code": "copy_void", "attempts": len(made.get("attempts", []))}]}
        copied = Path(made["path"])
        manifest = json.loads((copied / "census-copy-manifest.json").read_text())
        census = census_run(canonical=copied / "database.db", reviews=copied / "permissions-v2" / "evidence-reviews.db",
                            ledger=copied / "permissions-v2" / "ledger.db",
                            index_root=copied / "permissions-v2" / "message-search", keys=None,
                            binding=cs.binding_from_config(cs.load_config(copied)),
                            live_canonical=manifest["live_canonical_path"], now=manifest["copied_at"], keyless=True)
        aggregate = gc.aggregate(census, run_at=datetime.now(timezone.utc).isoformat(),
                                 copy_meta={"method": manifest["method"], "run_id": manifest["run_id"],
                                            "copied_at_utc": manifest["copied_at_utc"],
                                            "files": [{"role": f["role"], "bytes": f["bytes"]} for f in manifest["files"]]},
                                 job_state=gc.job_state(copied, manifest["copied_at"]),
                                 node_source=gc.node_source_check(node_root or gc.installed_package_root()))
        result = diff(aggregate, previous(out_root, today.isoformat()))
        day_dir = out_root / today.isoformat()
        day_dir.mkdir(parents=True, exist_ok=True)
        (day_dir / "if1-aggregate.json").write_text(json.dumps(aggregate, sort_keys=True, indent=1) + "\n")
        (day_dir / "diff.json").write_text(json.dumps(result, sort_keys=True, indent=1) + "\n")
        result["pruned"] = prune(out_root, today)
        result["summary"] = {"U": aggregate["U"], "p_impl": aggregate["census_members"],
                             "live_index": aggregate["live_index_members"], "unknown": aggregate["gate"]["unknown_reasons"]}
        return ("alert" if result["alerts"] else "ok"), result
    finally:
        if copy_root.exists():
            shutil.rmtree(copy_root)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source-root", type=Path, default=cs.LIVE_HOME)
    parser.add_argument("--scratch", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--node-source", type=Path,
                        help="the installed node's topos package directory (default: the uv tool install)")
    args = parser.parse_args(argv)
    cs.require_scratch_environment()
    status, result = daily(args.source_root.expanduser().resolve(), args.scratch, args.out, node_root=args.node_source)
    line = {"status": status, "alerts": [a["code"] for a in result.get("alerts", [])],
            "info": [i["code"] for i in result.get("info", [])], **result.get("summary", {})}
    print(json.dumps(line, sort_keys=True))
    return {"ok": 0, "alert": 1}.get(status, 2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except cs.CensusRefused as exc:
        print(json.dumps({"status": "refused", "reason": str(exc)}), file=sys.stderr)
        sys.exit(2)
