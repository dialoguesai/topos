#!/usr/bin/env python3
"""Open an upgrade fixture with *current* checkout code and assert catch-up.

PLAN_NODE_RELEASE_MIGRATIONS M4 — upgrade matrix runner.

  1. ensure_migrations_applied
  2. run_pending_upgrades (auto steps; consent → pending_consent)
  3. Assert: no failed ledger rows; every planned step reached 'done'; the
     executable steps actually RAN (steps_run > 0, sources walked, derived rows
     written); baseline == shipped OR only pending_consent remaining; schema
     has spec_version when current code ships that migration

The third group is deliberately about EFFECT, not the absence of errors. This
job spent 1.3.4–1.3.6 red because TOPOS_KEY was unset (settings validation
rejected every step before it started), and simply supplying the key would have
turned it green over an empty run: the fixture carried no `timeline` rows, so
every enrichment step walked zero sources and ledgered "done" regardless. A
check that cannot distinguish "did the work" from "found nothing to do" is not
a safety net. See KNOWN_NO_OP_STEPS for what this job does NOT cover.

The 1.5.0 step ``carry-contact-excludes-to-off-limits`` has assertions of its
own (``_assert_carry_step``): it is an ``engine_endpoint`` step that walks no
source, so none of the checks above can tell it from a step that did nothing.

A step still filed under the manifest's ``"unreleased"`` entry is never planned
(``steps_between`` skips that entry), so before a release is cut this job would
not run it at all. ``--stage-unreleased`` rehearses the cut: when that entry
holds steps, it is stamped as the next version by ``scripts/cut_release.py``'s
own ``_stamp_manifest``, in a scratch COPY of the manifest that the run reads
instead; the repository's manifest and version files are not touched. When the
entry holds no step (a tagged checkout, or a branch that stages nothing) the
flag changes nothing and says so, so one command line serves both.

Exit non-zero on failure.

Usage:
  python scripts/run_upgrade_matrix.py --db /tmp/upgrade-fixture.db
  python scripts/run_upgrade_matrix.py --db /tmp/upgrade-fixture.db --stage-unreleased
  python scripts/run_upgrade_matrix.py --print-previous-release [--stage-unreleased]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
# The sibling scripts this one reads: the fixture's declared contacts, and the
# release cut's own manifest stamping.
_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

CARRY_STEP_ID = "carry-contact-excludes-to-off-limits"
EXCLUDE = "exclude_from_grants"


def _shipped_version() -> str:
    from topos.__version__ import __version__

    return __version__


def _has_spec_version_migration() -> bool:
    try:
        from topos.storage.db.migrations.enrichment_spec_version_v1 import (  # noqa: F401
            MIGRATION_ID,
        )

        return True
    except ImportError:
        return False


def _assert_spec_version_column(conn: sqlite3.Connection) -> None:
    if not _has_spec_version_migration():
        print("spec_version migration not in this build; skipping column assert")
        return
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(message_entities)").fetchall()
    }
    if "spec_version" not in cols:
        raise AssertionError(
            "message_entities missing spec_version after ensure_migrations_applied"
        )
    print("ok: message_entities.spec_version present")


def _failed_ledger(conn: sqlite3.Connection) -> list[tuple]:
    try:
        return list(
            conn.execute(
                "SELECT version, step_id, status, detail_json FROM derivation_ledger "
                "WHERE status='failed'"
            ).fetchall()
        )
    except sqlite3.Error:
        return []


# Steps proven to perform no work in ANY environment (not a CI limitation).
# Excluded from the effect assertions below so this job does not sit
# permanently red for a defect it cannot fix on its own — but printed loudly on
# every run so the gap stays visible instead of decaying into background noise.
# Remove an entry the moment the underlying defect is fixed.
KNOWN_NO_OP_STEPS: dict[str, str] = {}


def _table_count(conn: sqlite3.Connection, table: str, where: str = "", params: tuple = ()) -> int:
    try:
        sql = f"SELECT COUNT(*) FROM {table}"
        if where:
            sql += f" WHERE {where}"
        return int(conn.execute(sql, params).fetchone()[0])
    except sqlite3.Error:
        return -1


def _ledger_detail(conn: sqlite3.Connection, step_id: str) -> dict:
    try:
        row = conn.execute(
            "SELECT detail_json FROM derivation_ledger WHERE step_id=?", (step_id,)
        ).fetchone()
    except sqlite3.Error:
        return {}
    if not row or not row[0]:
        return {}
    try:
        loaded = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _assert_steps_did_work(
    conn: sqlite3.Connection, plan: dict, result: dict, shipped: str
) -> None:
    """Assert the steps EXECUTED, not merely that nothing failed.

    An absence of 'failed' rows is not evidence of coverage: before the fixture
    carried canonical rows, every enrichment step ledgered "done" with
    {"sources": {}} and the matrix reported success over an empty run.
    """
    steps = list(plan.get("steps") or [])
    if not steps:
        # Two different empty-plan cases share this branch:
        #   1. Fixture baseline is not below shipped → the hop is invisible and
        #      this run proves nothing (fail).
        #   2. Real code-only hop (baseline < shipped, steps deliberately [])
        #      → RELEASING.md allows empty steps; runner still stamps baseline.
        #      Effect asserts have nothing to check (ok).
        baseline = plan.get("baseline")
        shipped_plan = str(plan.get("shipped") or shipped)
        from topos.upgrades import _version_key

        hop_visible = (
            baseline is not None
            and _version_key(str(baseline)) < _version_key(shipped_plan)
        )
        if not hop_visible:
            raise AssertionError(
                "no upgrade steps planned — the fixture baseline is not below "
                "the shipped version, so this run proves nothing. Check the "
                "fixture's engine.upgrade.baseline stamp against the manifest "
                f"ladder (baseline={baseline!r}, shipped={shipped_plan!r})."
            )
        print(
            f"note: code-only hop {baseline!r} → {shipped_plan!r} "
            "(0 derived steps); skipping effect asserts"
        )
        return

    # Every planned step must have reached a terminal success state.
    # 'pending_consent' is a legitimate resting place (a consent-gated step
    # waits for POST /v1/upgrade/consent), so it is accepted here and excluded
    # from the effect assertions below rather than treated as a failure.
    from topos.upgrades import declaring_versions
    from topos.upgrades.runner import _effective_status

    declared = declaring_versions()
    # Ledger rows are keyed by the release that DECLARED the step, not by
    # whatever was shipping when it ran — a step declared in 1.3.7 keeps one
    # row across every later hop.
    status_by_id = {
        str(s["id"]): _effective_status(
            conn, str(s["id"]), declared.get(str(s["id"])) or shipped
        )
        for s in steps
    }

    # A consent-gated step rests at 'pending_consent' until
    # POST /v1/upgrade/consent, and the matrix runs non-interactively, so that
    # is expected rather than a failure.
    #
    # So is a step waiting BEHIND one. The runner defers a step whose
    # dependency is not yet 'done' without writing a ledger row at all, so its
    # status is None — indistinguishable, to the old check, from a step that
    # should have run and did not. 1.3.16 is the first release to ship a
    # `consent: prompt` step (every earlier one is auto or unset), so this is
    # the first time the difference could be observed, and it failed the
    # release install smoke rather than the release.
    #
    # Computed to a fixpoint: dependency chains are arbitrarily deep and the
    # step list is not in dependency order.
    by_id = {str(s["id"]): s for s in steps}
    awaiting_consent = {
        sid for sid, status in status_by_id.items() if status == "pending_consent"
    }
    deferred = set(awaiting_consent)
    while True:
        newly = {
            sid
            for sid, step in by_id.items()
            if sid not in deferred
            and any(str(dep) in deferred for dep in (step.get("depends_on") or []))
        }
        if not newly:
            break
        deferred |= newly

    for step_id, status in status_by_id.items():
        if step_id in deferred:
            continue
        if status != "done":
            raise AssertionError(
                f"step {step_id!r} (kind={by_id[step_id].get('kind')!r}) ledger "
                f"status is {status!r}, expected 'done'"
            )

    executable = [
        s
        for s in steps
        if str(s.get("kind")) != "none" and str(s["id"]) not in deferred
    ]
    if executable and int(result.get("steps_run") or 0) <= 0:
        raise AssertionError(
            f"steps_run={result.get('steps_run')} with "
            f"{len(executable)} executable step(s) planned — nothing ran"
        )
    if not executable:
        print(
            "note: no executable steps this run "
            f"(pending_consent={sorted(awaiting_consent)}, "
            f"blocked behind consent={sorted(deferred - awaiting_consent)}); "
            "skipping effect asserts"
        )
        return

    # The runner discovers work through exactly this call, so reuse it rather
    # than re-deriving the source list and risking a different answer.
    from topos.upgrades.runner import _real_source_ids

    sources = _real_source_ids(conn)
    if not sources:
        raise AssertionError(
            "fixture advertises no real sources in `timeline` — every "
            "enrichment_reprocess step would no-op while still ledgering 'done'. "
            "Rebuild the fixture with scripts/build_upgrade_fixture.py."
        )
    print(f"ok: fixture advertises source(s) {sources}")

    skipped_note = []
    for step in executable:
        step_id = str(step["id"])
        detail = _ledger_detail(conn, step_id)
        if step_id in KNOWN_NO_OP_STEPS:
            skipped_note.append(step_id)
            continue
        if str(step.get("kind")) == "enrichment_reprocess":
            walked = detail.get("sources") or {}
            if not walked:
                raise AssertionError(
                    f"step {step_id!r} walked ZERO sources (detail={detail}) — it "
                    f"ledgered 'done' without processing anything"
                )
            bad = {s: v for s, v in walked.items() if str(v) != "ok"}
            if bad:
                raise AssertionError(f"step {step_id!r} had non-ok sources: {bad}")
            print(f"ok: {step_id} walked {len(walked)} source(s) -> all ok")

    # Effect assertions: derived rows that only exist if the step really ran.
    # Each is gated on its step's ledger row: a from-current fixture (publish's
    # prior-patch leg) plans only the steps between adjacent versions, so a
    # ladder step that never ran must not be asserted on (v1.3.7's publish
    # failed exactly this way — reextract-entities wasn't in a 1.3.6→1.3.7
    # plan, and the assert could never pass).
    reextract_detail = _ledger_detail(conn, "reextract-entities")
    if reextract_detail:
        placeholders = ",".join("?" for _ in sources)
        extracted = _table_count(
            conn, "message_entities", f"source_id IN ({placeholders})", tuple(sources)
        )
        if extracted <= 0:
            raise AssertionError(
                f"reextract-entities produced no message_entities rows for {sources} "
                f"(count={extracted}) — the NER pass did not run over the fixture"
            )
        mentions = _table_count(conn, "entity_mentions")
        print(f"ok: extraction wrote {extracted} message_entities, {mentions} entity_mentions")

    # backfill-attention-triage: attention_triage is a SIGNAL job, so the step
    # only does anything if the runner routes it down the signal lane. It spent
    # 1.3.0–1.3.6 ledgering "done" with jobs_run=0 and zero verdicts, which is
    # why this asserts on the verdict rows and not on the step's status.
    triage_detail = _ledger_detail(conn, "backfill-attention-triage")
    if triage_detail:
        verdicts = _table_count(conn, "triage_verdicts")
        if verdicts <= 0:
            raise AssertionError(
                f"backfill-attention-triage wrote 0 triage_verdicts "
                f"(detail={triage_detail}) — the step ledgered 'done' without "
                f"running the triage job. Check that the runner routes "
                f"SIGNAL_JOB_REGISTRY jobs through signal_job_names; "
                f"run_canonical() silently drops them."
            )
        print(f"ok: backfill-attention-triage wrote {verdicts} triage_verdicts")

    graph_detail = _ledger_detail(conn, "rebuild-entity-graph")
    if graph_detail:
        edges_after = int(graph_detail.get("edges_after") or 0)
        if edges_after <= 0:
            raise AssertionError(
                f"rebuild-entity-graph left 0 edges (detail={graph_detail}) — the "
                f"rebuild ran against an empty mention set"
            )
        print(
            f"ok: entity graph rebuilt to {edges_after} edges "
            f"({graph_detail.get('communities')} communities)"
        )

    for step_id in skipped_note:
        print(
            f"KNOWN GAP: step {step_id!r} is NOT covered by this job.\n"
            f"    {KNOWN_NO_OP_STEPS[step_id]}"
        )


def _pending_consent(conn: sqlite3.Connection) -> list[str]:
    try:
        rows = conn.execute(
            "SELECT step_id FROM derivation_ledger WHERE status='pending_consent'"
        ).fetchall()
        return [str(r[0]) for r in rows]
    except sqlite3.Error:
        return []


# --- the staging entry, rehearsed as a release -------------------------------

_DEFAULT_MANIFEST = REPO_ROOT / "topos" / "upgrades" / "manifests.json"
_UNRELEASED = "unreleased"
#: ``--stage-unreleased`` given without a version: the next patch after the package version.
NEXT = "next"


def _key(version: str) -> tuple:
    return tuple(int(part) for part in str(version).strip().split("."))


def _manifest_versions(manifest: Path) -> List[str]:
    """Stamped release versions in a manifest file, oldest first; the staging entry is left out."""
    releases = json.loads(Path(manifest).read_text(encoding="utf-8"))["releases"]
    return sorted((str(r["version"]) for r in releases if r.get("version") != _UNRELEASED), key=_key)


def _staged_steps(manifest: Optional[Path] = None) -> List[str]:
    """Ids of the steps waiting under the staging entry (none after a cut, until a branch stages one)."""
    releases = json.loads(Path(manifest or _DEFAULT_MANIFEST).read_text(encoding="utf-8"))["releases"]
    return [
        str(step.get("id")) for release in releases if release.get("version") == _UNRELEASED
        for step in release.get("steps") or []
    ]


def _staged_version(as_version: Optional[str]) -> str:
    """The version ``--stage-unreleased`` stamps: the one given, else the next patch."""
    import cut_release  # the sibling script; reads topos/__version__.py as text, imports nothing of topos

    version = str(as_version or "").strip().lstrip("v")
    if not version or version == NEXT:
        version = cut_release._bump("patch")
    cut_release._parse_version(version)
    return version


def shipped_for_run(stage_unreleased: Optional[str], manifest: Optional[Path] = None) -> tuple:
    """(the version a run treats as shipped, whether the staging entry is rehearsed to get there).

    Staging happens only when asked for AND something is staged. Otherwise the
    run is against the manifest as it stands, at the package version.
    """
    import cut_release

    if stage_unreleased and _staged_steps(manifest):
        return _staged_version(stage_unreleased), True
    return cut_release._read_current_version(), False


def previous_release(shipped: str, manifest: Optional[Path] = None) -> str:
    """The newest stamped release below ``shipped``: what a node upgrading to it most likely runs.

    Read from the manifest ladder rather than worked out from the number. The
    tag build used to subtract one from the patch, or one from the minor with
    the patch set to 0 — which for 1.5.0 is 1.4.0, four releases behind the
    1.4.4 that nodes run.
    """
    below = [v for v in _manifest_versions(manifest or _DEFAULT_MANIFEST) if _key(v) < _key(shipped)]
    if not below:
        raise SystemExit(f"no release below {shipped} in the upgrade manifest")
    return below[-1]


@contextlib.contextmanager
def staged_unreleased(as_version: Optional[str] = None) -> Iterator[str]:
    """Run with the manifest's staging entry stamped as a release. Yields that version.

    The stamping is ``cut_release._stamp_manifest`` itself, pointed at a
    scratch copy, so this is what the cut will do to the manifest and not a
    second description of it. ``topos.upgrades`` reads the copy until the block
    ends. Nothing in the repository is written: not the manifest, not the
    version files, not the changelog.
    """
    import io

    import cut_release
    import topos.upgrades as upgrades

    version = _staged_version(as_version)
    original = Path(upgrades._MANIFESTS_PATH)
    stamped = _manifest_versions(original)
    if stamped and _key(version) <= _key(stamped[-1]):
        raise SystemExit(
            f"--stage-unreleased {version}: not above the newest release in the manifest "
            f"({stamped[-1]}). After a cut the manifest is already stamped: run without the flag."
        )
    scratch_dir = Path(tempfile.mkdtemp(prefix="upgrade-matrix-manifest-"))
    scratch = scratch_dir / "manifests.json"
    shutil.copyfile(original, scratch)
    cut_manifest = cut_release.MANIFESTS
    cut_release.MANIFESTS = scratch
    try:
        # Its one line says "stamped manifests.json", which would read as the
        # repository's file; the line printed below says what was stamped.
        with contextlib.redirect_stdout(io.StringIO()):
            cut_release._stamp_manifest(version)
    finally:
        cut_release.MANIFESTS = cut_manifest
    staged = next(
        r for r in json.loads(scratch.read_text(encoding="utf-8"))["releases"] if r.get("version") == version
    )
    upgrades._MANIFESTS_PATH = scratch
    try:
        print(
            f"staged: the manifest's {_UNRELEASED!r} entry runs as release {version} from a scratch copy "
            f"({len(staged.get('steps') or [])} step(s): "
            f"{', '.join(str(s['id']) for s in staged.get('steps') or []) or 'none'}); "
            "the repository's manifest and version files are unchanged"
        )
        yield version
    finally:
        upgrades._MANIFESTS_PATH = original
        shutil.rmtree(scratch_dir, ignore_errors=True)


# --- carry-contact-excludes-to-off-limits -----------------------------------
#
# The step is one pass over `contacts` and a write per explicit exclude. It
# walks no source and rebuilds no layer, so every check above passes whether it
# did its work or not: a fixture without an excluded contact, or a step whose
# body was lost in a merge, both ledger "done". These assertions are about the
# Off-limits list (`entity_blackholes`) before and after, against the contacts
# scripts/build_upgrade_fixture.py declares.


def _carry_fail(message: str) -> AssertionError:
    return AssertionError(f"step {CARRY_STEP_ID!r} {message}")


def _off_limits(conn: sqlite3.Connection) -> Dict[str, Dict[str, Any]]:
    """Every Off-limits entry, every column, keyed by the entry's id."""
    cursor = conn.execute("SELECT * FROM entity_blackholes")
    names = [column[0] for column in cursor.description]
    return {str(row[names.index("blackhole_id")]): dict(zip(names, row)) for row in cursor.fetchall()}


def _notifications(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM blackhole_notifications").fetchone()[0])


def _stored_exclude(raw: Any) -> bool:
    """This job's own reading of one stored choice: an object whose row choice is the exclude."""
    if not isinstance(raw, str) or not raw.strip():
        return False
    try:
        value = json.loads(raw)
    except ValueError:
        return False
    return isinstance(value, dict) and value.get("row_visibility") == EXCLUDE


def _carry_before(
    conn: sqlite3.Connection, step: Dict[str, Any], plan_steps: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """Before the upgrade runs: prove the fixture holds what the step must act on, and record the list.

    Refuses a fixture that does not carry the declared contacts. Without them
    the step has nothing to do, and "it added the 0 entries the fixture asked
    for" is the vacuous pass this job exists to prevent.
    """
    import build_upgrade_fixture as fixture
    from topos.upgrades.runner import DEFAULT_EXECUTORS

    rebuild = "Rebuild the fixture with scripts/build_upgrade_fixture.py."
    declared = {choice.contact_id: choice for choice in fixture.CONTACT_CHOICES}
    try:
        stored = {
            str(contact_id): raw
            for contact_id, raw in conn.execute("SELECT contact_id, sharing_policy_json FROM contacts")
        }
    except sqlite3.Error as exc:
        raise _carry_fail(
            f"has nothing to act on: the fixture holds no contact with a stored sharing choice ({exc}), "
            f"so it would ledger 'done' having done nothing. {rebuild}"
        ) from None
    wrong = sorted(
        contact_id for contact_id, choice in declared.items()
        if contact_id not in stored or stored[contact_id] != choice.stored_choice
    )
    undeclared = sorted(
        contact_id for contact_id, raw in stored.items() if contact_id not in declared and raw is not None
    )
    if wrong or undeclared:
        raise _carry_fail(
            f"cannot be judged: the fixture does not hold the contacts this job declares "
            f"({len(wrong)} of {len(declared)} missing or different, {len(undeclared)} undeclared with a stored "
            f"choice). {rebuild}"
        )
    unlinked = sorted(
        entity_id
        for choice in declared.values()
        for entity_id, _name, _aliases, _mentions in choice.entities
        if conn.execute(
            "SELECT 1 FROM entities WHERE entity_id=? AND contact_id=?", (entity_id, choice.contact_id)
        ).fetchone() is None
    )
    if unlinked:
        raise _carry_fail(
            f"cannot be judged: {len(unlinked)} of the fixture's linked entities are missing, so the "
            f"linked-entity naming branch would not run. {rebuild}"
        )
    carried = [choice for choice in declared.values() if choice.case == fixture.CARRIED]
    others = [choice for choice in declared.values() if choice.case != fixture.CARRIED]
    # The declared cases against this job's own reading of the stored values:
    # the number asserted below is the number of explicit excludes IN the fixture.
    excludes = sorted(contact_id for contact_id, raw in stored.items() if _stored_exclude(raw))
    if not excludes or excludes != sorted(choice.contact_id for choice in carried):
        raise _carry_fail(
            f"cannot be judged: the fixture stores {len(excludes)} explicit exclude(s) and declares "
            f"{len(carried)} as carried. {rebuild}"
        )
    branches = {choice.named_by for choice in carried}
    if branches != set(fixture.NAMING_BRANCHES):
        raise _carry_fail(
            f"cannot be judged: the fixture's excluded contacts cover the naming branches {sorted(branches)}, "
            f"not all of {list(fixture.NAMING_BRANCHES)}."
        )
    before = _off_limits(conn)
    # The step's own dry run, read before anything is written (A2A-6 amendment 8,
    # item 7: on a real node the gain is judged against this number). Judged
    # last, after the effect itself.
    dry_step = dict(step)
    dry_step["params"] = {**(step.get("params") or {}), "dry_run": True}
    dry_run = DEFAULT_EXECUTORS[str(step["kind"])](dry_step, conn)
    print(
        f"ok: fixture holds {len(declared)} contacts: {len(carried)} with an explicit exclude "
        f"(naming branches {sorted(branches)}), {len(others)} with another stored value or none; "
        f"{len(before)} Off-limits entr{'y' if len(before) == 1 else 'ies'} before the upgrade"
    )
    return {
        "carried": carried,
        "others": others,
        # Whether the step is the first thing this plan runs, and so sees the
        # contacts exactly as the fixture was built (see _assert_carry_step, 4).
        "as_built": bool(plan_steps) and str(plan_steps[0].get("id")) == CARRY_STEP_ID,
        "branches": tuple(fixture.NAMING_BRANCHES),
        "before": before,
        "dry_run": dry_run if isinstance(dry_run, dict) else {},
        "after_dry_run": _off_limits(conn),
    }


def _assert_carry_step(
    conn: sqlite3.Connection, step: Dict[str, Any], state: Dict[str, Any], shipped: str
) -> None:
    """The step turned every explicit exclude into an Off-limits entry, and nothing else into one."""
    from topos.features.lifecycle.blackhole import normalize_entity_name
    from topos.features.lifecycle.contact_excludes import NOTE
    from topos.upgrades import declaring_versions
    from topos.upgrades.runner import DEFAULT_EXECUTORS

    carried, others = state["carried"], state["others"]
    expected = len(carried)
    before, after = state["before"], _off_limits(conn)

    # 0. The dry run taken before the upgrade wrote nothing. First, because
    # every count below is a difference from `before`, and a dry run that
    # writes would be counted as the step's own work.
    if state["after_dry_run"] != before:
        raise _carry_fail("wrote to the Off-limits list in a dry run")

    # 1. The list gained exactly one entry per explicit exclude, and lost nothing.
    disturbed = sorted(key for key, entry in before.items() if after.get(key) != entry)
    if disturbed:
        raise _carry_fail(
            f"changed or removed {len(disturbed)} Off-limits entr{'y' if len(disturbed) == 1 else 'ies'} "
            f"that were there before it ran"
        )
    gained = {key: entry for key, entry in after.items() if key not in before}
    if len(gained) != expected:
        raise _carry_fail(
            f"added {len(gained)} Off-limits entries, expected {expected}: one for each explicit exclude in "
            f"the fixture ({len(before)} before, {len(after)} after). A step that carries none, or only some, "
            f"still ledgers 'done'."
        )

    # 2. Each new entry carries the step's note: it is how the owner learns where the entry came from.
    unnoted = sorted(key for key, entry in gained.items() if entry.get("note") != NOTE)
    if unnoted:
        raise _carry_fail(f"wrote {len(unnoted)} of its {expected} Off-limits entries without the step's note")

    # 3. One entry for each excluded contact (the contact id is always among an
    # entry's aliases, whatever names it), and none that reaches anyone else.
    def reach(entry: Dict[str, Any]) -> set:
        try:
            aliases = json.loads(entry.get("aliases_json") or "[]")
        except ValueError:
            aliases = []
        names = {str(entry.get("normalized_name") or ""), normalize_entity_name(str(entry.get("canonical_name") or "")),
                 str(entry.get("entity_id") or "")}
        names.update(str(alias) for alias in aliases if isinstance(alias, str))
        return names - {""}

    reached = {key: reach(entry) for key, entry in after.items()}
    claimed: Dict[str, str] = {}
    for choice in carried:
        holders = [key for key in gained if normalize_entity_name(choice.contact_id) in reached[key]]
        if len(holders) != 1:
            raise _carry_fail(
                f"left the excluded contact {choice.contact_id!r} with {len(holders)} new Off-limits entries, "
                f"expected 1"
            )
        if holders[0] in claimed:
            raise _carry_fail(
                f"folded {choice.contact_id!r} and {claimed[holders[0]]!r} into one Off-limits entry"
            )
        claimed[holders[0]] = choice.contact_id
    for choice in others:
        terms = {choice.contact_id, choice.display_name or "", *choice.usernames}
        terms.update(identifier for identifier, _kind in choice.handles)
        for _entity_id, name, aliases, _mentions in choice.entities:
            terms.update((name, *aliases))
        terms = {normalize_entity_name(term) for term in terms if term} - {""}
        terms.update(entity_id for entity_id, _name, _aliases, _mentions in choice.entities)
        if any(terms & names for names in reached.values()):
            raise _carry_fail(
                f"made an Off-limits entry that reaches {choice.contact_id!r}, whose stored choice is "
                f"{choice.case!r} and was never an exclude"
            )

    # 4. The ledger row: done, filed under the release that declares the step,
    # started and finished, and reporting the same numbers.
    declared_in = declaring_versions().get(CARRY_STEP_ID) or shipped
    rows = conn.execute(
        "SELECT version, status, started_at, finished_at, detail_json, "
        "       (julianday(finished_at) - julianday(started_at)) * 86400.0 "
        "FROM derivation_ledger WHERE step_id=?",
        (CARRY_STEP_ID,),
    ).fetchall()
    if len(rows) != 1:
        raise _carry_fail(f"has {len(rows)} ledger rows, expected 1")
    version, status, started_at, finished_at, detail_json, seconds = rows[0]
    if status != "done" or version != declared_in:
        raise _carry_fail(
            f"ledger row is {status!r} under {version!r}, expected 'done' under {declared_in!r}"
        )
    # Both stamps are SQLite's datetime('now'), whole seconds: a fast step reads
    # 0 s. What is checked is that it was started and then finished, in that order.
    if not started_at or not finished_at or seconds is None or seconds < 0:
        raise _carry_fail(
            f"ledger row is 'done' without a real duration (started_at={started_at!r}, "
            f"finished_at={finished_at!r})"
        )
    try:
        detail = json.loads(detail_json or "{}")
    except ValueError:
        detail = {}
    named_by = detail.get("named_by") if isinstance(detail.get("named_by"), dict) else {}
    reported = {
        "dry_run": detail.get("dry_run"),
        "carried": detail.get("carried"),
        "already_off_limits": detail.get("already_off_limits"),
        "rebuilds_failed": detail.get("rebuilds_failed"),
        "explicit_excludes": (detail.get("counts") or {}).get("explicit_excludes"),
        "named": sum(int(count) for count in named_by.values()),
        "ran_under": detail.get("ran_under"),
    }
    wanted = {
        "dry_run": False, "carried": expected, "already_off_limits": 0, "rebuilds_failed": 0,
        "explicit_excludes": expected, "named": expected, "ran_under": shipped,
    }
    if reported != wanted:
        raise _carry_fail(f"ledger row reports {reported}, expected {wanted}")
    # How each entry was named. Which branches show depends on what ran before
    # the step: a graph rebuild links a person entity to every contact that has
    # a name, a handle or a username (EntityResolver.seed_from_contacts), so
    # after one nearly every entry is named by its linked entity. When the step
    # is the first thing the plan runs it sees the contacts as the fixture was
    # built, and then every declared branch must show, as often as declared.
    declared_branches = {
        branch: sum(1 for choice in carried if choice.named_by == branch) for branch in state["branches"]
    }
    if state["as_built"] and named_by != declared_branches:
        raise _carry_fail(
            f"named its entries by {dict(sorted(named_by.items()))}, expected the fixture's "
            f"{dict(sorted(declared_branches.items()))}"
        )
    if not set(named_by) <= set(state["branches"]):
        raise _carry_fail(f"named entries by {sorted(set(named_by) - set(state['branches']))}, not a known branch")

    # 5. A second run of the step, through the runner's own dispatch, adds no
    # entry, changes no entry and tells the owner nothing new.
    notifications = _notifications(conn)
    again = DEFAULT_EXECUTORS[str(step["kind"])](dict(step), conn)
    again = again if isinstance(again, dict) else {}
    twice = _off_limits(conn)
    added = sorted(set(twice) - set(after))
    changed = sorted(key for key, entry in after.items() if twice.get(key) != entry)
    if added or changed:
        raise _carry_fail(
            f"is not idempotent: a second run added {len(added)} Off-limits entries and changed or removed "
            f"{len(changed)}"
        )
    if again.get("carried") != 0 or again.get("already_off_limits") != expected:
        raise _carry_fail(
            f"is not idempotent: a second run reported carried={again.get('carried')!r}, "
            f"already_off_limits={again.get('already_off_limits')!r}; expected 0 and {expected}"
        )
    if _notifications(conn) != notifications:
        raise _carry_fail(
            f"is not idempotent: a second run raised {_notifications(conn) - notifications} more owner "
            f"notification(s)"
        )

    # 6. Its dry run, taken before the upgrade, reported that number. Judged
    # last so that a step which does nothing fails on what it did not do.
    dry_run = state["dry_run"]
    if dry_run.get("dry_run") is not True or (dry_run.get("counts") or {}).get("explicit_excludes") != expected:
        raise _carry_fail(
            f"dry run reported {(dry_run.get('counts') or {}).get('explicit_excludes')!r} explicit excludes, "
            f"expected {expected}"
        )

    print(
        f"ok: {CARRY_STEP_ID} carried {expected} explicit excludes into Off-limits "
        f"({len(before)} -> {len(after)} entries, each with the step's note, named by "
        f"{dict(sorted(named_by.items()))}"
        + ("" if state["as_built"] else ", after older steps that link entities to contacts")
        + "); "
        f"no entry for the {len(others)} contacts with another stored value or none; "
        f"second run added 0 and changed 0; dry run reported {expected}; "
        f"ledger 'done' under {version} ({started_at} -> {finished_at}, {seconds:.0f} s)"
    )


def run_matrix(
    db_path: Path,
    *,
    stage_unreleased: Optional[str] = None,
    executors: Optional[Dict[str, Any]] = None,
) -> None:
    """Catch the fixture up and assert on what the steps did.

    ``stage_unreleased``: a version, or ``NEXT``, to run the manifest's staging
    entry as that release from a scratch copy (see ``staged_unreleased``).
    ``executors``: for this job's own tests only; the command line always runs
    the product's.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # Avoid accidental live-DB side effects / heavy runners during CI.
    os.environ.setdefault("TOPOS_SKIP_UPDATE_CHECK", "1")
    # NOT setdefault: this harness runs force_reprocess enrichment, so an
    # inherited TOPOS_DATABASE_PATH (a developer shell pointing at ~/.topos)
    # would silently re-derive the LIVE database instead of the fixture.
    os.environ["TOPOS_DATABASE_PATH"] = str(db_path)
    # Run upgrades inline (not background / UI-grace).
    os.environ["TOPOS_UPGRADE_RUNNER"] = "on"
    os.environ.setdefault("TOPOS_UPGRADE_UI_GRACE_SECONDS", "0")
    # Settings validation rejects construction without a key, which used to
    # fail every step before it started (steps_run=0, steps_failed=3 — the
    # whole job was red from 1.3.4 through 1.3.6). The value is never
    # transmitted: upgrade executors dispatch to engine internals rather than
    # HTTP ("no internal dispatch" / "no HTTP self-call" in upgrades/runner.py),
    # and the key's only consumers on this path (privacy_layer, ingestion
    # manager) gate on TOPOS_ENGINE_SERVICE_URL, which CI leaves unset. A real
    # key would buy zero extra coverage and put a live credential in CI.
    os.environ.setdefault("TOPOS_KEY", "ci-upgrade-matrix-dummy-key")

    from topos.storage.db.migrations import ensure_migrations_applied
    from topos.upgrades.runner import (
        plan_upgrade,
        read_baseline,
        run_pending_upgrades,
    )

    if not db_path.is_file():
        raise SystemExit(f"fixture DB not found: {db_path}")

    with contextlib.ExitStack() as stack:
        import topos.upgrades as upgrades

        if stage_unreleased and _staged_steps(Path(upgrades._MANIFESTS_PATH)):
            # Before the plan is read: from here to the end of the run the
            # staging entry is a release like any other.
            shipped = stack.enter_context(staged_unreleased(stage_unreleased))
        else:
            shipped = _shipped_version()
            if stage_unreleased:
                print(
                    f"note: --stage-unreleased: no step is staged under {_UNRELEASED!r}, so the run is "
                    f"against the manifest as it stands (shipped={shipped})"
                )
        conn = sqlite3.connect(str(db_path))
        stack.callback(conn.close)

        print(f"ensure_migrations_applied on {db_path} (shipped={shipped})")
        ensure_migrations_applied(conn, skip_backup=True)
        _assert_spec_version_column(conn)

        plan = plan_upgrade(conn, shipped=shipped)
        print(
            f"plan: baseline={plan.get('baseline')!r} → shipped={plan.get('shipped')!r} "
            f"steps={len(plan.get('steps') or [])} fresh={plan.get('fresh_install')}"
        )

        # Before anything runs: the carry step is judged on what it adds, so
        # the list and the fixture's contacts are read now.
        carry_step = next(
            (step for step in plan.get("steps") or [] if str(step.get("id")) == CARRY_STEP_ID), None
        )
        carry_state = (
            _carry_before(conn, carry_step, list(plan.get("steps") or [])) if carry_step is not None else None
        )

        result = run_pending_upgrades(conn, shipped=shipped, executors=executors)
        print(f"run_pending_upgrades: {result}")

        failed = _failed_ledger(conn)
        if failed:
            raise AssertionError(f"failed derivation_ledger rows: {failed}")

        # Absence of failures is not coverage — prove the steps did work.
        _assert_steps_did_work(conn, plan, result, shipped)

        if carry_step is not None:
            _assert_carry_step(conn, carry_step, carry_state, shipped)
        else:
            from topos.upgrades import load_unreleased

            staged = any(
                str(step.get("id")) == CARRY_STEP_ID for step in (load_unreleased() or {}).get("steps") or []
            )
            # Said out loud, because a check that silently did not run reads as a pass.
            print(
                f"note: {CARRY_STEP_ID} is not in this plan, so its assertions did not run"
                + (
                    f" (it is filed under {_UNRELEASED!r}, which is never planned: "
                    "--stage-unreleased rehearses it)"
                    if staged
                    else ""
                )
            )

        baseline = read_baseline(conn)
        consent = _pending_consent(conn)
        if baseline == shipped:
            print(f"ok: baseline advanced to shipped ({shipped})")
        elif consent and baseline != shipped:
            # Consent-gated steps block baseline advance — acceptable outcome.
            print(
                f"ok: baseline={baseline!r} with pending_consent={consent} "
                f"(shipped={shipped})"
            )
        else:
            raise AssertionError(
                f"baseline {baseline!r} != shipped {shipped!r} and no "
                f"pending_consent remaining"
            )

        # Non-consent pending/running leftover is a failure.
        try:
            stuck = list(
                conn.execute(
                    "SELECT version, step_id, status FROM derivation_ledger "
                    "WHERE status IN ('pending', 'running', 'failed')"
                ).fetchall()
            )
        except sqlite3.Error:
            stuck = []
        if stuck:
            raise AssertionError(f"unresolved ledger rows after upgrade: {stuck}")

        print("upgrade_matrix_ok")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        help="Path to fixture SQLite database (will be mutated in place)",
    )
    parser.add_argument(
        "--stage-unreleased",
        nargs="?",
        const=NEXT,
        default=None,
        metavar="X.Y.Z",
        help=(
            "When steps wait under the manifest's \"unreleased\" entry, run that entry as release "
            "X.Y.Z (default: the next patch after the package version), stamped by cut_release.py's "
            "own code in a scratch copy of the manifest. Nothing in the repository is written. "
            "Before a cut this is the only way a staged step runs here. With nothing staged (a "
            "tagged checkout) the flag changes nothing."
        ),
    )
    parser.add_argument(
        "--print-previous-release",
        action="store_true",
        help=(
            "Print the newest release in the manifest below the version this run would treat as "
            "shipped (the staged one with --stage-unreleased, else the package version), and exit. "
            "It is the from-version of the second fixture."
        ),
    )
    args = parser.parse_args(argv)
    if args.print_previous_release:
        shipped, _staged = shipped_for_run(args.stage_unreleased)
        print(previous_release(shipped))
        return 0
    if args.db is None:
        parser.error("--db is required")
    try:
        run_matrix(args.db.expanduser().resolve(), stage_unreleased=args.stage_unreleased)
    except AssertionError as exc:
        print(f"upgrade_matrix_failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"upgrade_matrix_error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
