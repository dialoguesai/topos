# Bench readiness contract

The bench selects people from ingested evidence for a particular purpose. Selection does not require the owner to nominate people. A shortlist is useful even when the evidence cannot yet establish that a person is ready to help.

`topos/features/derivation/bench_readiness.py` isolates the decision from retrieval, extraction, ranking, prompts and model choice. It is a deterministic evaluator of normalized observations, not an extractor that understands raw messages. Its tests must never be reported as message-understanding or answer-quality accuracy.

## Independent judgments

| Judgment | Required evidence | Insufficient on its own |
|---|---|---|
| Current need | A supported, still-current owner purpose | A recurring topic, old goal, or frequent task |
| Purpose fit | Evidence connecting this person to this particular purpose | Generic keyword or embedding similarity |
| Capability | Demonstrated work relevant to the purpose | Job title, self-promotion, co-presence or discussion |
| Willingness | An attributable offer or open commitment to help the owner with this purpose | Warmth, response frequency, general friendliness or a third-party report |
| Availability | An attributable availability statement with a valid time window | An old offer, silence or assumed free time |

Readiness evaluates willingness and availability. Bench qualification additionally requires current need, purpose fit and demonstrated capability. These are separate fields rather than one opaque score. Unknown means unqualified evidence; it does not mean unwilling or incapable.

## Observation boundary

An adapter supplies person, purpose, dataset, speaker, recipient, assertion mode, dimension/value, canonical source event time, explicit validity end, conditions status and source reference. A single message can support multiple dimensions, but each claim must be supported independently. An offer or promise deadline is not automatically a statement of availability. A condition can be marked met only from evidence establishing its satisfaction.

An adapter must resolve negation, quotations, hypotheticals, instructions embedded in source text, third-party reports, identity, purpose linkage and condition satisfaction before emitting direct observations. The evaluator does not provide these semantic guarantees. Preserve provenance and source clocks through that adapter, and test it independently across models and source families.

Policy `bench-readiness-1` uses `[event_at, valid_until)` validity and a conservative 30-day freshness ceiling. This ceiling is an initial explicit policy, not an empirically calibrated statement about people. Both clocks must include timezone information. Unknown validity cannot be filled with ingestion time or a fabricated expiry. Shorter explicit windows win. The latest observation per dimension supersedes earlier ones; expiry of a newer withdrawal does not revive an older offer. Contradictory latest observations remain conflicted. Duplicates do not increase confidence.

A positive assessment requires a complete adapter read with no rejected observations. Partial coverage, uncertain clocks and rejected rows can hide newer contradictory evidence. Negative and conditional judgments carry their reasons and references; they must not become lasting labels on a person. Every assessment is bound to person, purpose, dataset and reference instant.

## Current integration and limits

`build_bench_slate` attaches `readiness`, `readiness_status` and `bench_qualification` to candidates. `bench_sources.py` now reads explicit owner requests and peer replies in named human direct-message exchanges. It preserves quotations, dates, request context and later messages for inspection. The source adapter uses a bounded English recognizer; it does not claim general semantic coverage. Unknown wording stays unqualified. No model is called, so this component does not depend on a particular model's obedience to a prompt.

A direct reply can support willingness/availability only with a matching purpose, a firm offer and a timezone-qualified `until` timestamp alongside an explicit availability statement. Later unclassified messages prevent promotion. Conditions, tentative language and unrelated timing phrases cannot silently establish readiness. Current need is qualified only for an explicit owner statement of needing help until a still-valid timestamp; other requests remain recorded requests with current status unverified. A ready bench member additionally needs demonstrated capability under the same stable person identity. These deliberately narrow positive rules should be expanded through labeled source evaluations, not relaxed merely to fill the bench.

The read considers up to 32 recent goal purposes before selecting eight displayed purposes; sourced connections take priority over unsupported role guesses. Direct request exchanges take priority over inferred topic discussion. The selected dataset and whether it was resolved automatically are reported. Source reads are bounded and report truncation. Related discussion remains explicitly distinct from capability or willingness.

Identity resolution accepts exact handles and equivalent US ten-digit/+1 spellings. Duplicate contact imports may supply one agreed display name, but they cannot merge contact/entity IDs or transfer capabilities. Conflicting names remain unresolved; international suffixes and extensions do not use fuzzy matching. Bench exclusions are checked across duplicate identity aliases, including when only a display name was recovered.

The October 12 frozen snapshot still has zero active `net.capability` and `net.character` objects. The integrated read recovers three people-purpose connections: two explicit requests with replies and one relevant prior discussion. All three remain candidates, with no assertion of current readiness. Replacing the new identity resolver with the previous one reduces the same read to zero connections; limiting goal purposes to the latest eight reduces it to two. Neither result proves raw source evidence is absent elsewhere.

Existing outward-pack consent and disclosure rules remain intact. This adapter is an owner read over already-ingested messages, does not write personal profiles or enable a pack, and respects excluded identities. Broader capability extraction, unsolicited offers, other source families, implicit dates, conversational inference, hosted transport and generated-answer faithfulness remain separate qualification work. See the source catalog and fixed-data comparison in the control-plane QA history.

## Reproduce

Run contract and integration tests:

```sh
python -m pytest -q tests/features/derivation/test_bench_sources.py tests/analytics/test_bench_identity_resolution.py tests/features/derivation/test_bench_readiness.py tests/features/derivation/test_bench_recalibration.py tests/features/derivation/test_social_bench.py tests/features/derivation/test_bench_work_engagement.py
python scripts/bench_readiness_eval.py --output /tmp/bench-readiness.json
```

The portable catalog is `tests/features/derivation/fixtures/bench_readiness_cases.json`, with stable IDs BR-001 onward. The evaluator emits individual results and a catalog digest. To additionally replay a frozen owner database, supply `--snapshot`, `--snapshot-sha256` and a timezone-aware `--as-of`. Use an offline immutable copy; the runner opens read-only, disables migrations only within its evaluation process, verifies the snapshot before and after, and emits aggregate counts without names, messages or dataset identifiers. No model is invoked. Do not pass a changing live database.
