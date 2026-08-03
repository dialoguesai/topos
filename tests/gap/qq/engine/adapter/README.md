# topos-eval node adapter (engine side)

This is the **engine-coupled half** of the eval suite (see
`../../../../../../topos-control-plane/topos-eval/ARCHITECTURE.md` for the seam). It
implements the `topos-eval` protocols against a *real* Topos node — the part that
necessarily imports engine internals (`QueryPipelineOrchestrator`, the manifest resolver,
the disclosure layer, the storage migrations) and therefore lives here, versioned with the
engine, not in the portable package.

## What's here

| File | Implements |
|---|---|
| `target_engine.py` | `QueryTarget` via the in-process pipeline; `EvidencePacket` from the DDR (`TOPOS_QUERY_DDR=1`); `normalize_result` reused by the runner |
| `target_mcp.py` | `QueryTarget` over the CP MCP gateway (owner `query_scope`, persistent session, `X-Topos-Client: topos-home-chat/1`); reuses `target_engine.normalize_result` — one normalizer, two transports. Grantee/`shared_*` parity is a later lane (refused loudly, not half-measured) |
| `sql_oracle.py` | `Oracle` — needle groups + topic terms + class hooks (browse membership, recency window) → graded 0/1/2; truth computed once from SQL |
| `llm_judge.py` | `Judge` on local Ollama (`qwen3.5:9b-mlx`, think-off JSON verdicts); localhost-only, enforced |

The **catalog data** (concrete cases referencing real scopes) is the existing
`../query_eval_cases.py` / `../composition_eval_cases.py` / `../composition_seed_corpus.py`
plus `../negative_eval_cases.py` (N-series) and `../generative_eval_cases.py` (G-series).

## Future work (created when implemented — no stub files)

- `target_mcp.py` grantee lane — parity over the `shared_*` tool surface with UMA auth
  (v1 is owner-only; see the module docstring).
- `corpus_builder.py` — the `qq-seeded-3` persona pack via engine migrations (Phase 2):
  therapy-adjacent journal canary, validity-windowed employment fact, warm/cold contact
  pair, NSFW canary per layer, code-switched needles.

## The contract

Both targets normalize the node's response into `topos_eval.Response(+EvidencePacket)` so
the **same** catalog and the **same** analysis (`import topos_eval`) run against either an
in-process node or a remote one over MCP. That's the whole point of the protocol seam:
write the adapter once, measure any node.

## Import direction (do not violate)

`topos-eval` (the package) must NEVER import this adapter or any `topos.*` module — the CI
guard `topos-eval/tests/test_no_node_dependency.py` enforces it. This adapter imports *from*
`topos-eval` (the protocols) and *from* the engine. One-way: adapter → {package, engine}.
