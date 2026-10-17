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

Require every PR CI check to pass on the candidate commit. CI runs the public
suite in seven parallel, separate processes, followed by privacy tests, build,
and fresh-install smoke. Run `just gate` only when CI is unavailable or a local
reproduction is needed; running it after green CI repeats the same long suite.

If the change has sharing impact (rules below), also run `just eval-release`
once on isolated Rig D with the five `A2A_RIG_*` paths. When CI is unavailable,
`just gate-sharing` runs the core local gate and Rig D once each. Do not run
`just eval-release` and then `just gate-sharing`.

After the required checks pass, merge the reviewed candidate to `main`, verify
the merged tree matches the tested candidate, then tag that merged commit:

```bash
git switch main && git pull --ff-only origin main
git tag vX.Y.Z && git push origin vX.Y.Z
```

### Sharing-impact decision

Record the decision and the passing CI run in the release PR. Run the full
six-pair Rig D gate for a change that can affect sharing, consent, permissions,
authentication, recipient-facing reads or search, disclosure/refusal behavior
or timing, the sharing ledger, shared-data storage or migrations, the
control-plane protocol, or node lifecycle around those paths. Run it when the
impact is uncertain. A failed weekly Rig D run also blocks a release until
investigated.

For a change confined to presentation, packaging, docs, or an additive
owner-only status field with no effect on those paths, passing targeted tests,
all PR CI checks, and the tag-driven publish checks are sufficient. The
release PR must say why Rig D was not required. Run the full rig on current
`main` at least weekly as a separate backstop, and record its verdict.

The public tests are already parallelized across seven isolated CI runners.
Do not substitute `pytest-xdist` on one runner: the suite's session-level
leak, thread-lifetime, and owner-database guards can fail in a worker without
making the controller fail (see `tests/lane_shards.py`). The isolated Rig D
timing battery also relies on paced requests; threads or processes sharing
that rig would alter the latency measurement. Parallelize independent work
on separate machines only after validating the resulting verdict.

`publish.yml` then verifies tag==pyproject, release artifacts
(manifest + changelog + checksums), fresh-install + upgrade smoke, and
Trusted Publishing to PyPI.

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
