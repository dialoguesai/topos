# Releasing topos-node

This is the procedure of record for shipping `topos-node` to PyPI.
It implements [PLAN_NODE_RELEASE_MIGRATIONS](../PLAN_NODE_RELEASE_MIGRATIONS.md)
milestones M0–M4: release notes as build artifacts, a single migration
registry with fail-loud + backup + downgrade guard, a release clerk,
per-job `spec_version` stamps for selective reprocessing + consent for
slow upgrade steps, and an upgrade-matrix CI path
(`.github/workflows/upgrade-matrix.yml`).

PyPI publish is **tag-driven only** — pushing `vX.Y.Z` runs
`.github/workflows/publish.yml`.

## Change taxonomy (lane tags)

Every changelog / manifest entry classifies its impact:

| Lane | Meaning | On-node effect |
|------|---------|----------------|
| **S1** | Additive schema | DDL via `wiki_schema_migrations` |
| **S2** | Restructuring schema | Backup → table rebuild |
| **C** | Normalizer change | `canonical_reprocess` (from raw) |
| **E** | Enrichment change | bump `JOB_SPEC_VERSIONS` + `enrichment_reprocess` |
| **D** | Derived-layer algorithm | `derived_rebuild` (or legacy `engine_endpoint`) |
| **V** | Embedding model / dim | `reembed` (embed + ANN) |
| **P** | Protocol / contract | CP/CI only |
| **O** | Code-only | nothing |

## During development (every PR)

1. **Schema change** → new append-only module under
   `topos/storage/db/migrations/`, registered in
   `topos/storage/db/migrations/registry.py` (`MIGRATIONS`).
   Never edit a shipped, non-`always_run` migration in place.
2. **Enrichment / normalizer / derived change** → bump the job's
   `spec_version` in `topos/enrichment/models/mvp_defaults.py`
   (`JOB_SPEC_VERSIONS`), and extend the `"unreleased"` entry in
   `topos/upgrades/manifests.json` with a step (`why`, `cost`,
   `consent: auto|prompt`, `depends_on` as needed). Prefer stale-predicate
   reprocess (omit `force_reprocess`); use `consent: prompt` for LLM /
   full re-embed steps. Mark the commit `no-invalidation` only when the
   diff truly does not invalidate outputs (CI nudge).
3. Update `CHANGELOG.md` under `## [Unreleased]` with lane tags
   (e.g. `[S1] [E:entities]`).
4. After adding a ledger-guarded migration:
   `python scripts/sync_migration_checksums.py --write`.
5. CI enforces: contract snapshots, migration checksums, spec-version
   nudge, public tests.

## At release cut

```bash
cd topos
# TOPOS_KEY must be set: cut_release regenerates the handled-types snapshot by
# importing the app, and Settings refuses to construct without it. Without the
# key the script fails PART WAY THROUGH, leaving the manifest stamped, the
# CHANGELOG stamped and the version bumped but the snapshot stale — `git
# checkout` those four files and start over.
export TOPOS_KEY=$(grep -o '^TOPOS_KEY=.*' ~/.topos/.env | cut -d= -f2)

# Stamp unreleased → X.Y.Z, bump versions, regen checksums + handled types
python scripts/cut_release.py --bump patch   # or --version X.Y.Z

# Review the diff — the stamped manifest entry IS the data-impact review.
# Ask: are steps ordered (depends_on) correctly? Is anything consent:auto
# that could burn an hour of laptop CPU?

# Commit the candidate on a release branch and open a PR.
git add -A && git commit -m "Release X.Y.Z: <why>."
git push -u origin HEAD
```

Require every selected PR CI check to pass on the candidate commit. The
`Select node checks` job computes a plan from the complete PR diff and explains
its choices in the Actions summary. `test-and-package` always reports and fails
if selection failed or any required job failed, was cancelled, or was skipped.
The plan is also saved as the `node-check-plan` Actions artifact.
Branch rules should require the stable `test-and-package` aggregate, rather
than individual shard names that are intentionally skipped on focused plans.

Before tagging, use `just release-plan` to preview the cumulative release plan
and `just release-checks` to apply its sharing-rig requirement. Both compare
against the previous reachable stable **node** tag, not the last commit or
the tray app's `app-v*` tag. Version stamping cannot hide earlier runtime changes.
Run `just gate` only when CI is unavailable or a local reproduction is needed;
running it after green CI repeats the same long suite.

For sharing-impact releases set the five `A2A_RIG_*` paths. `just release-checks`
runs the isolated rig once, saves `node-release-gate.json` next to its verdict,
and records `release/sharing-rig` on the candidate commit. After merging, reuse
that evidence on the identical merged tree; a different tree requires a new run.
For other releases this command automatically reports that no rig is needed.

After the required checks pass, merge the candidate to `main`, verify its tree
matches the tested candidate, then record the applicable gate and tag it:

```bash
git switch main && git pull --ff-only origin main
# Sharing-impact release: reuse the candidate's identical-tree evidence.
just release-checks --evidence /absolute/rig-output/node-release-gate.json
# Other releases: just release-checks (automatically skips Rig D).
git tag vX.Y.Z && git push origin vX.Y.Z
```

### Automatic selection

`scripts/select_release_checks.py` owns the conservative allowlist. A new or
unmapped runtime file, shared data/auth/query/lifecycle code, dependency change,
or runtime removal selects the full suite and the sharing rig. Classification
or Git-range errors fail the check; they never select a fast route.

| Changed code | Automatic PR checks |
|---|---|
| Documentation only | Planner self-tests and secret scanning; no public suite, package, or upgrade fixture |
| Version stamping (including only this package's version in `uv.lock`) and empty upgrade-manifest entries | Planner plus package/install smoke |
| Existing owner status CLI, or only known tray drawing functions | Mapped regression tests, privacy, package/install smoke |
| Added/changed isolated `tests/cli/test_*.py` or `tests/topos/test_*.py` | Changed public test files and selection guards |
| Other code, shared fixtures, CI/tooling, removed tests | All seven public shards, privacy, package/install smoke, upgrade fixtures |

Source allowlists are deliberately narrow. Changing tray shutdown/update/profile
handling selects full checks even when the same file also changes a drawing.
An additive field inside `topos/api/shell.py` is not automatically considered a
display-only change: that module also owns mutating update endpoints. Broaden
the mapping only after reviewing the module's callers and regression coverage.

Targeted runs collect the normal suite before deselecting, retaining the same
imports and session-level leak/owner-database guards. Full CI runs weekly and
on manual dispatch regardless of changed paths. The nightly/tag upgrade matrix
also stays full; only PR fixture work follows the plan.

### Sharing-impact requirement

Record the generated plan and passing CI run in the release PR. Run the full
six-pair Rig D gate for a change that can affect sharing, consent, permissions,
authentication, recipient-facing reads or search, disclosure/refusal behavior
or timing, the sharing ledger, shared-data storage or migrations, the
control-plane protocol, or node lifecycle around those paths. Run it when the
impact is uncertain. A failed weekly Rig D run also blocks a release until
investigated.

For a change the automatic allowlist confines to presentation, documentation,
owner status, or release stamping, passing selected CI checks and tag-driven
publish checks are sufficient. Unmapped packaging/dependency/runtime changes
remain full checks. Run the full rig on current
`main` at least weekly as a separate backstop, and record its verdict.

The public tests are already parallelized across seven isolated CI runners.
Do not substitute `pytest-xdist` on one runner: the suite's session-level
leak, thread-lifetime, and owner-database guards can fail in a worker without
making the controller fail (see `tests/lane_shards.py`). The isolated Rig D
timing battery also relies on paced requests; threads or processes sharing
that rig would alter the latency measurement. Parallelize independent work
on separate machines only after validating the resulting verdict.

`publish.yml` independently recomputes the entire release range and refuses a
sharing-impact release without a passing `release/sharing-rig` status for the
exact tagged tree. It then verifies tag==pyproject, release artifacts
(manifest + changelog + checksums), fresh-install + upgrade smoke, and
Trusted Publishing to PyPI.

This policy applies to `dialoguesai/topos` (`topos-node`). The separate tray
repository's signed/notarized app release workflow is unaffected.

## Post-release

- Nodes self-update (or are nudged). First boot:
  1. Downgrade guard (`PRAGMA user_version`)
  2. Pre-migration backup to `~/.topos/backups/` when ledger-pending or
     when the boot will raise `user_version` (an `always_run`-only release too)
  3. Schema tail via `ensure_migrations_applied`
  4. Upgrade runner executes `steps_between(baseline, shipped)`
- Rollback is **not** package downgrade. Recovery =
  restore `~/.topos/backups/database-pre-v{X}-*.db` +
  `uv tool install topos-node==<old>`.
- Forward path for bad derived changes: a hotfix release whose manifest
  step re-derives correctly.

## Support floor

Floor = **1.1.0** (`_BOOTSTRAP_BASELINE`). Databases below the floor use
the escape hatch: backup, then `reprocess_source(from_stage="raw")`.

## Key files

| File | Role |
|------|------|
| `topos/upgrades/manifests.json` | Per-release derived invalidation (+ `unreleased` staging) |
| `CHANGELOG.md` | Human prose, keep-a-changelog |
| `topos/storage/db/migrations/registry.py` | Single `MIGRATIONS` list |
| `topos/storage/db/migrations/registry_checksums.json` | Append-only guard |
| `scripts/cut_release.py` | Release clerk |
| `scripts/check_release_artifacts.py` | Publish/CI guards |
| `scripts/build_upgrade_fixture.py` | Upgrade-matrix fixture (`--from-current` for CI) |
| `scripts/run_upgrade_matrix.py` | Catch-up assert on a fixture DB |
| `.github/workflows/upgrade-matrix.yml` | Nightly / tag / dispatch upgrade matrix |
| `scripts/sync_migration_checksums.py` | Checksum write/check |
