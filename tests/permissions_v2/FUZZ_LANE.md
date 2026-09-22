# The permissions v2 fuzz lane

Confidence program C5 (`audits/2026-09-14-permissions/CONFIDENCE_PROGRAM_PLAN.md` in the
control-plane monorepo), built 22 September 2026. Property-based tests over the hard
layer: the three-valued evaluator, the raw-message and fact decisions, the label-free
floors, admission, the three doors and the canonical encoding. Everything generated is
invented; nothing opens the owner's home, a socket, a model or a real database.

## Running it

Hypothesis must be importable (`pip install -e .[dev]` declares it). The lane skips
itself when it is not.

```
TOPOS_ENV_FILE=<scratch> TOPOS_UDS_PATH=<scratch> TOPOS_INGESTION_BASE_PATH=<scratch> TOPOS_DATABASE_PATH=<scratch> \
  python -m pytest tests/permissions_v2 -m fuzz -q -p no:cacheprovider
TOPOS_FUZZ_PROFILE=deep python -m pytest tests/permissions_v2 -m fuzz -q -p no:cacheprovider   # the recorded run
```

`lane` (the default, deterministic, 80 examples per pure property and 6 per door property)
is what every permissions lane run pays; `deep` (random seeds, 500 and 24) is the run the
phase-0 report records. Profiles live in `conftest.py`; no example database is written.

## Files and the invariants each states

| File | Invariants |
|---|---|
| `fuzz_support.py` | strategies: predicates, attribute maps, p2a policies of all three capabilities, reviewed evidence, the polarity rule for label narrowing, every policy narrowing |
| `test_fuzz_evaluator.py` | K1 Kleene tables vs an independent reference; K2 making an attribute unknown never flips a definite result; K3 the review vocabulary never reaches the evaluator as Unknown (on p2a the floors are the unknown gate); N1 every policy narrowing is monotone, singly and in chains; N2 adding a deny-side label never widens, with the exact polarity rule and the witness that a permit-side label can; S1 a decision is a pure function with a closed shape; S2 another subject rule or an incomplete classification set is refused; S3 only a raw permit rule covering every leaf can permit, a deny fires only on a leaf it selects |
| `test_fuzz_encoding.py` | C1 canonical JSON is a bijection on its grammar; C2 everything outside it is refused; C3 one encoding per policy document across all eight grammars, the undeclared budget's single encoding, the registry's closed dispatch; C4 opaque ids |
| `test_fuzz_admission.py` | A1 any single-field change to a signed envelope is refused and the original admits; A2 payload and request context are bound; A3 admission is one-shot in either order and a refusal burns the id once; A4 lifetime bounded by issue, expiry, validity and TTL |
| `test_fuzz_transports.py` | U1 whatever the adapter raises, one refusal frame; U2 any malformed relayed message, the same frame before the runtime is touched; U3 the three doors' frames differ only in the message type |
| `test_fuzz_fact_decisions.py` | T1 unknown time never permits; T2 every fact-policy narrowing is monotone; T3 stale authority dominates; T4 the decision equals the frozen 81c1e9c oracle on every generated v1 document |
| `test_fuzz_floors.py` | F1 every sequence of owner-side restriction events narrows P(g) on a generated corpus; F2 every failure of the floors is a PolicyError; F3 a review that leaves anything unknown withholds; F4 each event removes the fact it targets (non-vacuity) |
| `test_fuzz_discovery.py` | D1-D3 on generated corpora, every record any search returns is content the p2a-v3 locator door releases for that fact, no canary or hidden row appears, and the answer is bounded and closed |

## What the lane pinned that the design's wording did not say

- **"Adding a label never widens" holds exactly for the values a policy names on the deny
  side positively or the permit side negatively** (`fuzz_support.safe_to_add`). Every
  private value of a work-only preset is such a value. A value a permit atom names
  positively can widen -- a spurious `work` label on a hobby message permits it under cell C
  (`test_N2a_witness_...`). That is why D20 bounds sensitive *misses* and not spurious
  sensitive labels.
- **On p2a, Unknown is unreachable at the decision.** The review vocabulary always yields
  four string lists, so the evaluator's Unknown branches in `source_message_decision` are
  dead until the label layer emits unresolved domains; the unknown gate today is the floor
  (`_eligible`). Where Unknown is live -- the fact path's time -- T1 proves it withholds.
- **The pure raw-message decision does not itself refuse a duplicated classification entry**
  (`test_S2a_finding_...`): labels are keyed by identity, so a duplicate collapses. The floor
  refuses it before any decision is computed, so no door can reach it. Reported as C5-1.
- **A fact tombstone stales every review sharing its predicate prefix** (`test_F4_...
  [fact_tombstone]`): the prefix is part of each closure's protection identity. Narrowing,
  and an availability cost the design already records (F7).

## The mutation battery

`scripts/permissions_v2_mutants.py` applies named semantic mutants across the enforcement
path in place, runs the fuzz lane first and then the existing suites, and restores the
files. `--check` proves every patch applies exactly once; `--full-lane` re-runs survivors
against the whole permissions lane. A run needs a green base: pass the base's known reds as
`--deselect`. Results and the named survivors are in the phase-0 report.
