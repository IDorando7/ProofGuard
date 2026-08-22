# Week 8 Validator Network Benchmark

Status: Week 8 Day 7 implemented.

Day 7 adds verification and reporting, not new protocol policy. The benchmark
uses the real Week 8 committee, reproduction, attestation, consensus,
performance, membership and reward services against isolated temporary state.
It uses fixed seed `8008`, deterministic UUID/time sources, synthetic protocol
metadata and the existing safe Week 3 fixture path. It never contacts a live
target.

## Running the benchmark

From `apps/audit-api`:

```bash
python -m research.benchmarks.week8_validator_benchmark --mode ci
python -m research.benchmarks.week8_validator_benchmark --mode medium
python -m research.benchmarks.week8_validator_benchmark --mode full
```

Modes describe the deterministic calculator-stress envelope: CI uses an
8-cluster-equivalent vector load, medium 20 and full 40. The complete
real-service adversarial corpus runs in every mode so critical cases are never
sampled away; FULL also persists resolved flows until the corpus reaches 40
clusters and at least 120 Day 5 assessments. Its generated summary reports the
actual persisted entity counts.
The corpus currently uses one logical project identifier across many isolated
routing roots; cross-project authorization is covered by the full regression
suite. Critical corruption scenarios remain in separate persistence roots so
they cannot contaminate valid accounting.

The runner exits non-zero when any named invariant fails. It writes structured
case, committee, consensus, quality, performance, reward, accounting,
determinism and timing JSON plus `week_8_report.md` under
`research/results/week8/` by default.

## Verified protocol boundaries

- reporters and their sibling nodes cannot validate their FindingClusters;
- one registered operator occupies at most one committee seat;
- STANDARD uses five authoritative operators and HIGH_ASSURANCE uses seven;
- insufficient diversity fails closed and never downgrades assurance;
- reproduction results remain assignment-, node- and operator-attributed;
- SafetyPreflight cannot be bypassed by validator role;
- 3-vs-2 and 4-vs-3 are disputes, not simple-majority truth;
- escalation adds four new operators, retains earlier evidence and is bounded;
- shadow evidence never changes authoritative consensus;
- quality is evaluated against final cumulative truth, not local majority;
- validator history is node/category specific and separate from agent history;
- client validator payout uses equal authoritative work units, 30% completion,
  70% current VQ squared, and no severity, reputation, membership, historical
  score or consensus-agreement multiplier;
- distributed plus undistributed validator points equals validator_pool exactly;
- validator pool consumption is isolated from miner and protocol pools;
- retry and partial-write recovery preserve one immutable event identity; and
- a clean replay produces an identical canonical semantic state hash.

## Correct-minority case

Round 1 has three ACCEPT and two REJECT attestations. The result is DISPUTED.
Four new escalation operators attest REJECT, producing cumulative six REJECT
and three ACCEPT. The final outcome is REJECTED. The two original REJECT
validators receive correctness credit and can receive full VQ-dependent
quality reward. No majority/minority label enters quality or reward formulas.

## Economic and skill separation

| Lane | Input | Purpose |
| --- | --- | --- |
| Agent historical performance | Agent CategoryScore | Future discovery opportunity |
| Validator historical performance | ValidatorCategoryScore | Future validation opportunity |
| Current miner payout | Finding value and current report quality | Current reporter reward |
| Current validator payout | Authorized work and current VQ² | Current validator reward |

Historical validator skill is never a direct Day 6 payout multiplier. Shadow
work builds performance evidence but does not consume client validator_pool.

## Deterministic replay

The state hash includes committee/operator identities, reproduction
attribution, attestation-derived consensus, escalation, quality assessments,
validator category scores/memberships, reward allocations, RewardEvents and
semantic fingerprints. It excludes timestamps used only for reporting,
temporary paths and runtime measurements.

## Honest limitation

`operator_id` is application-level diversity, not cryptographic Sybil
resistance. Several nominal operators may share a hidden controller, and a
sufficient internally consistent supermajority can compromise any
supermajority protocol. Stronger identity, verifiable randomness, signatures,
challenges, complementary implementations and carefully researched economic
security are Week 9 candidates, not Day 7 features.
