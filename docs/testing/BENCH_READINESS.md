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

## Current integration and gaps

`build_bench_slate` attaches `readiness`, `readiness_status` and `bench_qualification` to candidates. The current adapter reads demonstrated skill facts and discussion overlap. It **does not yet read readiness observations**, and explicitly emits `coverage=not_integrated`. Existing inferred purpose/fit judgments cannot produce a qualified ready member. The UI distinguishes this missing assessment from proof that no offers exist.

Existing `net.capability` and `net.character` packs are draft, owner-only, outward derivations. Their enablement/consent rules remain in place. No new pack is enabled, no private source is shared, and no live database is changed by this implementation. The October 12 frozen snapshot audit found zero active objects from either pack. This is a derivation and integration gap, not proof that source messages lack the evidence.

Next, qualify a small source adapter against independently reviewed messages: retain negative and third-party controls, resolve canonical authors/dates/purposes and validity, and compare extracted observations to labels before allowing them into readiness decisions. Evaluate purpose inference separately; fixing readiness cannot repair an incorrect inferred need. Keep the owner and granted-access tests separate when this capability later becomes available to other audiences.

## Reproduce

Run contract and integration tests:

```sh
python -m pytest -q tests/features/derivation/test_bench_readiness.py tests/features/derivation/test_bench_recalibration.py tests/features/derivation/test_social_bench.py tests/features/derivation/test_bench_work_engagement.py
python scripts/bench_readiness_eval.py --output /tmp/bench-readiness.json
```

The portable catalog is `tests/features/derivation/fixtures/bench_readiness_cases.json`, with stable IDs BR-001 onward. The evaluator emits individual results and a catalog digest. To additionally replay a frozen owner database, supply `--snapshot`, `--snapshot-sha256` and a timezone-aware `--as-of`. Use an offline immutable copy; the runner opens read-only, disables migrations only within its evaluation process, verifies the snapshot before and after, and emits aggregate counts without names, messages or dataset identifiers. No model is invoked. Do not pass a changing live database.
