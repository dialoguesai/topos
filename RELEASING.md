# Releasing topos-node

This is the procedure of record for shipping `topos-node` to PyPI.
It implements [PLAN_NODE_RELEASE_MIGRATIONS](../PLAN_NODE_RELEASE_MIGRATIONS.md)
milestones M0–M2: release notes as build artifacts, a single migration
registry with fail-loud + backup + downgrade guard, and a release clerk.

PyPI publish is **tag-driven only** — pushing `vX.Y.Z` runs
`.github/workflows/publish.yml`.

## Change taxonomy (lane tags)

Every changelog / manifest entry classifies its impact:

| Lane | Meaning | On-node effect |
|------|---------|----------------|
| **S1** | Additive schema | DDL via `wiki_schema_migrations` |
| **S2** | Restructuring schema | Backup → table rebuild |
| **C** | Normalizer change | `canonical_reprocess` (from raw) |
| **E** | Enrichment change | `enrichment_reprocess` (prefer `spec_version`) |
| **D** | Derived-layer algorithm | `engine_endpoint` / derived rebuild |
| **V** | Embedding model / dim | re-embed + ANN + FTS |
| **P** | Protocol / contract | CP/CI only |
| **O** | Code-only | nothing |

## During development (every PR)

1. **Schema change** → new append-only module under
   `topos/storage/db/migrations/`, registered in
   `topos/storage/db/migrations/registry.py` (`MIGRATIONS`).
   Never edit a shipped, non-`always_run` migration in place.
2. **Enrichment / normalizer / derived change** → extend the
   `"unreleased"` entry in `topos/upgrades/manifests.json` with a step
   (`why`, cost, `depends_on` as needed).
3. Update `CHANGELOG.md` under `## [Unreleased]` with lane tags
   (e.g. `[S1] [E:entities]`).
4. After adding a ledger-guarded migration:
   `python scripts/sync_migration_checksums.py --write`.
5. CI enforces: contract snapshots, migration checksums, public tests.

## At release cut

```bash
cd topos
# Stamp unreleased → X.Y.Z, bump versions, regen checksums + handled types
python scripts/cut_release.py --bump patch   # or --version X.Y.Z

# Review the diff — the stamped manifest entry IS the data-impact review.
# Ask: are steps ordered (depends_on) correctly? Is anything consent:auto
# that could burn an hour of laptop CPU?

just eval-release     # privacy scorecard + privacy pytest gate
just gate             # dep pins + public tests + build + smoke

# Then commit, push main, tag, push tag (see .cursor/skills/release-topos):
git add -A && git commit -m "Release X.Y.Z: <why>."
git push origin main
git tag vX.Y.Z && git push origin vX.Y.Z
```

`publish.yml` then verifies tag==pyproject, release artifacts
(manifest + changelog + checksums), fresh-install + upgrade smoke, and
Trusted Publishing to PyPI.

## Post-release

- Nodes self-update (or are nudged). First boot:
  1. Downgrade guard (`PRAGMA user_version`)
  2. Pre-migration backup to `~/.topos/backups/` when ledger-pending
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
| `scripts/sync_migration_checksums.py` | Checksum write/check |
