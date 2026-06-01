# Contributing to Topos Node

Thanks for contributing to `topos-node`.

This repository is intentionally consumer-facing. Keep changes focused on local node runtime, user experience, and public-safe tests.

## Local setup

```bash
uv sync --extra dev --extra engine
```

## Test lanes

- Public lane (default for OSS checks):

```bash
pytest tests -m "public and not e2e" -q
```

- Internal/private lane (architecture-heavy, hosted flows, migration/sprint suites):
  - Marked with `@pytest.mark.private`
  - Not part of default public checks

- End-to-end lane:
  - Marked with `@pytest.mark.e2e`
  - Requires external services and explicit environment setup

## Deployment assets ownership

- `topos` (this repo) stays consumer-facing and package-focused.
- Cloud Run deployment/build assets for Topos runtime live in:
  - `topos-control-plane/scripts/gcp/topos-node/`
- Operational maintenance scripts that expose internal deployment details live in:
  - `topos-control-plane/scripts/topos-node-maintenance/`

## Scope guidance

Good fit for this repo:

- CLI and node runtime behavior (`topos-node`)
- local database/engine behavior
- user-facing docs and onboarding
- public-safe tests and fixtures

Keep out of this repo:

- internal cloud rollout scripts
- environment-specific production operations
- private-only diagnostics and runbooks

## Security expectations

- Never commit secrets, keys, or real credentials.
- Use placeholders in examples and fixtures.
