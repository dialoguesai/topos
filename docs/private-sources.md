# Private sources through a coding agent

A running local Topos can install a source definition directly from JSON. No Dialogues
account, developer API key, Sheaf registration or app registration is needed. This flow
has no public/publish option. The definition remains unregistered; imported data follows
the node's existing access and sharing rules.

The Topos plugin bundles `topos-add-source`, which prepares the definition from an
example file, validates it and opens the owner's approval prompt. Setup installs the
plugin's skills together; private source creation does not need the Dialogues developer
skill package.

```sh
topos-node sources infer sample.json --json
topos-node sources add definition.json --sample sample.json --check --json
topos-node sources add definition.json --sample sample.json --open-terminal --json
topos-node sources inspect my_source --json
```

`infer` is local and needs no running node. `--check` uses the owner socket and does not
install or import. The owner approves in a real terminal; there is no unattended `--yes`.
On macOS `--open-terminal` opens that prompt. Elsewhere run the printed command in your
own terminal. The prompt binds the definition, sample and target Topos to approval.

Use `--sample` only for UTF-8 JSON, JSONL/NDJSON or CSV files up to 2 MiB. Preview checks
at most 200 records, reports truncation, and returns normalized example records. Stable,
unique record ids are required. Definition files are also limited to 2 MiB. Without
`--sample`, installation writes only the definition. New sync adapters still need code;
a definition does not fetch data or schedule collection.

For definition fields and runnable examples, see the plugin's
[source definition guide](https://github.com/dialoguesai/topos-plugin/blob/main/skills/topos-add-source/references/source-definition.md).
Custom extract maps support chat lanes or raw tables. Other lanes can reuse compatible
bundled parsers. Raw tables are not automatically searchable or available as facts.
Private raw table names must start with `private_`, the source id (dots/hyphens replaced
by underscores), then `_`, and cannot reuse unrelated existing tables. Bundled sources and incompatible
existing definitions cannot be replaced by this create workflow.

## Receipts and verification

- `ready`: parser preparation passed; no data was written. This does not prove retrieval.
- `approval_pending` / `needs_human` (exit 2): waiting for the owner; not installed.
- `installed`: definition committed, no sample imported.
- `ingested`: sample processing returned success with records and no reported errors.
- `import_failed` (exit 1): definition remains installed; sample may be partially imported.

After approval, `sources inspect` verifies the persisted definition. For canonical data,
ask a known question through the matching Topos scope before calling it answer-ready.
Automatic processing and agent access to a scope are separate from installation. Raw-only
sources should be reported as stored, never answer-ready.

A repeated unchanged definition-only install is idempotent. Do not blindly retry an import
after a timeout or partial failure: inspect its receipt and existing records first. Later
files use `topos-node import <file> --source <source_id>` in the owner's terminal; this
reads custom sources from the running node rather than a fresh process's bundled registry.

Stream definitions can be installed without importing a sample. Producer enrollment and
its local write credential remain separate owner actions. Registering a reusable/public
connector uses the Dialogues developer workflow and is outside this command.

## Local API

`POST /api/local/owner/sources/prepare` accepts `source_definition_json` plus an optional
`sample` with `file_name` and UTF-8 `content`. It returns a normalized definition, parser
preview, warnings, definition hash and plan hash. Owner identity is resolved locally.

`POST /api/local/owner/sources/install-private` accepts that input and `plan_sha256`.
The existing owner door requires an owner-app principal over the socket with a live
single-use owner confirmation. Agents may prepare, but cannot call this write route as
agents. The node re-prepares the payload, checks the target-bound hash, installs using
the persistent install service and imports through the existing file ingestion pipeline.
A failed sample import does not roll back a persisted definition.

`GET /api/local/owner/private-sources/{source_id}` returns active persisted definitions
scoped to this node's resolved owner. It accepts owner-agent reads. No new MCP write tools,
TCP fallback, cloud calls or developer credentials are introduced.
