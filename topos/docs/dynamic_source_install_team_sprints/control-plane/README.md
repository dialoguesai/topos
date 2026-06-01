# Control-plane team plan

## Mission
Expose secure install/test APIs that proxy engine dynamic source flows, enforce authorization, and preserve auditable behavior.

## Product goal
Users can trigger install and instant validation from frontend surfaces without direct engine coupling, with consistent access control and diagnostics.

## Engineering requirements
1. Proxy endpoints for install/test/list/trigger map cleanly to engine contracts.
2. `version_id` resolution path pulls canonical source payload from app registry.
3. Authorization and audit records cover install/update/uninstall/test actions.
4. Response envelopes stay stable for frontend consumption.

## Sprint index

| Sprint | Theme |
|---|---|
| [sprint-01-install-test-proxy-and-version-resolver.md](./sprint-01-install-test-proxy-and-version-resolver.md) | Control-plane proxy routes and version resolver |
| [sprint-02-authz-audit-and-contract-hardening.md](./sprint-02-authz-audit-and-contract-hardening.md) | Authz, audit trails, and contract hardening |
