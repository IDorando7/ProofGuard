# ProofGuard Week 6 — Subnet System Report

## Executive Summary

Week 6 created vulnerability-specialized off-chain subnets. Node performance and scores are category-specific, membership is derived rather than self-selected, routing separates authoritative production work from shadow exploration, and simulated rewards stay inside their category pools. The Day 7 end-to-end benchmark validated the complete skeleton with overall status **PASS**.

## Week 6 Objectives

- Isolate historical performance and scoring by vulnerability category.
- Derive safe membership tiers from production policy.
- Route production and exploration assignments deterministically.
- Allocate simulated protocol points within isolated category pools.
- Verify deterministic replay, conservation, and idempotent finalization.

## Architecture Implemented

```text
Node Registry
     |
     v
Category Performance
     |
     v
Category Score
     |
     v
Subnet Membership
     |
     v
Project Router
     |
     v
Subnet Reward Allocation
```

## Day 1 — Subnet Registry

The benchmark bootstrapped the production registry twice, confirmed one deterministic subnet per supported category, and verified that custom access-control exploration configuration survived the second bootstrap.

## Day 2 — Category Performance Aggregation

Applied Week 5 ReputationEvents were rebuilt through the production aggregator. Counters, source IDs, and fingerprints remained isolated by node and category, and an unchanged rebuild did not double count history.

## Day 3 — Category Scoring

The production scoring engine generated bounded, explainable scores from Day 2 records. Low-sample histories remained confidence-shrunk, while strong histories qualified for strong or expert policy decisions.

## Day 4 — Subnet Membership

The membership manager derived candidate, probation, active, expert, and suspended states at a fixed UTC evaluation time. Recent unsafe behavior overrode score and hysteresis. No report rank was written back to member records.

## Day 5 — Subnet Routing

The router used project scope categories, exact member/score snapshots, and deterministic exploration fairness. Active and expert nodes received production assignments; candidate and probation nodes were shadow-only.

## Day 6 — Subnet Reward Allocation

A 10,000.000000 simulated protocol-point pool was split 3:2 between access control and reentrancy. Eligibility and multipliers came directly from the production Day 6 service, and finalization created immutable idempotent events.

## Day 7 — End-to-End Benchmark

The benchmark executed 7 cases and 82 assertions. 82 assertions passed and 0 failed.

## Benchmark Environment

- Synthetic nodes, projects, findings, decisions, and protocol events only.
- Separate temporary protocol-data and audit-workspace roots for each replay.
- Fixed UTC clock and deterministic synthetic identifiers.
- Real Week 5 and Week 6 service methods; no copied scoring, routing, or reward formula.
- Generated reports contain report-safe IDs and aggregate values, not fixture paths.

## Benchmark Cases

### best_access_control_selected

**Objective:** Verify that the strongest qualified access-control nodes receive ranked production assignments.

**Setup:** Active access-control subnet with expert, active, and developing members.

**Relevant metrics:** `expert_score=1.0, production_assignments=3, shadow_assignments=2`

**Assertions:** 8/8 passed.

**Result:** PASS

### weak_reentrancy_not_selected

**Objective:** Verify that weak category history cannot become a ranked production fallback.

**Setup:** One expert and weak developing reentrancy histories.

**Relevant metrics:** `ranked_selected=1, requested=5, weak_score=0.238999`

**Assertions:** 7/7 passed.

**Result:** PASS

### new_node_exploration

**Objective:** Verify candidate and probation exploration without production authority or promotion.

**Setup:** Exploration ratio 0.40 with both candidate and probation members.

**Relevant metrics:** `exploration_assignments=2, first_explorer=node_probation_explorer`

**Assertions:** 10/10 passed.

**Result:** PASS

### unsafe_node_suspended

**Objective:** Verify that a recent unsafe event overrides history, score, and routing eligibility.

**Setup:** Strong accepted history plus one applied unsafe event at the fixed evaluation time.

**Relevant metrics:** `category_score=0.854545, unsafe_events=1`

**Assertions:** 11/11 passed.

**Result:** PASS

### category_score_isolation

**Objective:** Verify that one node can be strong in access control and weak in reentrancy.

**Setup:** One node with six accepted access-control outcomes and mostly invalid reentrancy outcomes.

**Relevant metrics:** `access_events=6, access_score=0.788, reentrancy_events=6, reentrancy_score=0.475999`

**Assertions:** 10/10 passed.

**Result:** PASS

### multi_category_routing

**Objective:** Verify independent access-control and reentrancy routing for one project.

**Setup:** One scope manifest declaring access control and reentrancy.

**Relevant metrics:** `production=4, shadow=3, total_assignments=7`

**Assertions:** 12/12 passed.

**Result:** PASS

### subnet_reward_distribution

**Objective:** Verify eligibility, multipliers, proportional allocation, exact conservation, and immutable finalization.

**Setup:** Finalized multi-category route and 10,000.000000 simulated protocol-point pool.

**Relevant metrics:** `distributed_points=10000.000000, eligible_allocations=4, excluded_submissions=4, reward_events=4, undistributed_points=0.000000`

**Assertions:** 24/24 passed.

**Result:** PASS

## Category Isolation Results

`node_multi_category` access-control score: `0.788`; reentrancy score: `0.475999`. The independent source event lists and routing decisions demonstrate that one category did not borrow performance from the other.

## Membership Results

Derived membership distribution: `candidate=2`, `probation=3`, `active=2`, `expert=2`, `suspended=2`, `removed=1`.

## Routing Results

Production assignments: `7`. Shadow assignments: `5`. Multi-category routing used `subnet_access_control` and `subnet_reentrancy` independently.

## Exploration Results

Candidate and probation members remained ineligible for ranked production. The router selected developing nodes only as deterministic shadow exploration, with null qualification references and idempotent usage events.

## Reward Allocation Results

Eligible allocations: `4`; excluded submissions: `4`; immutable reward events: `4`. Distributed points were `10000.000000` and undistributed points were `0.000000` from a pool of `10000.000000`.

## Determinism and Idempotency

Deterministic isolated replay: `passed`.

- [x] category performance rebuild
- [x] category score rebuild
- [x] membership refresh access control
- [x] membership refresh reentrancy
- [x] reward calculation
- [x] reward cycle creation
- [x] reward finalization
- [x] reward source immutability
- [x] routing calculation benchmark access project
- [x] routing calculation benchmark multicategory project
- [x] routing finalization benchmark access project
- [x] routing finalization benchmark multicategory project
- [x] subnet bootstrap

## Security Boundaries

- No banned, suspended, removed, wrong-category, or unsafe member was assigned.
- Candidate membership never received production mode.
- Invalid, duplicate, out-of-scope, non-reproduced, and unlinked contributions received no subnet reward.
- Reward finalization did not mutate routing, submission, contribution, validation, reproduction, node, or membership source records.
- No private key, host path, raw proof-of-concept content, or persistent subnet rank appears in generated outputs.
- No payment, token, wallet, staking, blockchain, AI/LLM, Docker, forge, subprocess analysis, remote-node execution, or external API call occurred.

## Known Limitations

- The benchmark uses a small synthetic network and a centralized coordinator.
- Membership and routing policy thresholds are v0 policy choices, not economic guarantees.
- Shadow exploration does not yet evaluate candidate output or guarantee waiting-time fairness.
- No private rotating qualification benchmark or benchmark-leakage protection exists.
- No Sybil resistance, operator diversity, assignment lease, load balancing, or remote execution exists.
- Protocol points are simulated accounting units with no monetary value.

## Deferred Refinements

1. Private rotating candidate benchmarks.
2. Protection against benchmark leakage and memorization.
3. Hidden benchmark seeds and semantic mutations.
4. Qualification attempt limits.
5. Candidate shadow evaluation.
6. Guaranteed candidate/probation opportunity queues.
7. Production fairness between equally qualified experts.
8. Rolling assignment windows.
9. Selection credits and cooldowns.
10. Capability profiles per vulnerability subtype.
11. Diversity-aware portfolio routing.
12. Secondary independent review rounds.
13. Client assurance packages.
14. Larger reward pools for more expert coverage.
15. Participation rewards.
16. Real token/staking integration only after the off-chain protocol is stable.

## Week 6 Definition of Done

### Subnet Registry

- [x] one deterministic subnet per supported category
- [x] idempotent bootstrap
- [x] configurable thresholds
- [x] safe persistent storage

### Category Performance

- [x] performance separated by node and category
- [x] applied finalized events used
- [x] deterministic rebuild
- [x] no double counting

### Category Scoring

- [x] score between 0 and 1
- [x] explainable components
- [x] confidence shrinkage
- [x] unsafe and invalid-outcome penalties
- [x] category isolation

### Membership

- [x] candidate/probation/active/expert
- [x] unsafe suspension
- [x] node/subnet status overrides
- [x] deterministic capacity
- [x] hysteresis
- [x] immutable decision history

### Routing

- [x] project scope categories resolved
- [x] category-specific subnet selection
- [x] production assignments for active/expert
- [x] shadow assignments for candidate/probation
- [x] unsafe members excluded
- [x] deterministic usage tracking

### Rewards

- [x] finalized routing required
- [x] simulated protocol points
- [x] category-isolated pools
- [x] eligible contribution weighting
- [x] candidate exclusion
- [x] probation reduction
- [x] exact Decimal conservation
- [x] idempotent immutable events

### Benchmark

- [x] all seven cases executed
- [x] all generated outputs created
- [x] deterministic replay checked
- [x] complete test suite executed

## Conclusion

Week 6 subnet skeleton verification finished with status **PASS**. The benchmark demonstrates the complete off-chain path from finalized historical contributions to category performance, category scores, derived membership, multi-subnet routing, and category-isolated simulated rewards.
