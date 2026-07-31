# Week 5 Report - Protocol and Incentive Layer v0

Generated: 2026-07-16T12:29:30.750198+00:00

## 1. Goal

Week 5 transforms the centralized audit pipeline into an off-chain protocol simulation. Nodes have identities, submissions link findings to nodes, finalized contribution value changes global reputation, eligible contributions receive simulated protocol points, and unsafe submissions can create separate penalty records. No blockchain or real token exists.

## 2. What was implemented

- Protocol Specification v0
- Node Identity and Registry v0
- Finding Submission Protocol v0 and deterministic finding hashes
- Contribution Scoring Engine v0
- Reputation Engine v0
- Reward Cycle Simulator and RewardEvents
- Unsafe PenaltyEvents
- Week 5 synthetic protocol benchmark

## 3. Protocol architecture

```text
Audit Project
    |
    v
Agent Node
    |
    v
Finding Submission
    |
    v
ReproductionResult
    |
    v
ValidationDecision
    |
    v
ContributionScore
    |
    v
ReputationEvent
    |
    +--------------------+
    |                    |
    v                    v
Reward Cycle       Penalty Event
    |
    v
RewardEvent
```

## 4. Protocol actors

The Audit Client supplies an authorized project and scope. Agent Nodes are implemented as off-chain UUID records and submit findings in declared categories. Validator Nodes remain conceptual; the current Validator Pipeline and static benchmark decisions are centralized. The Protocol Coordinator is the trusted FastAPI/service layer. The implemented Reward Engine is an off-chain deterministic protocol-points simulator.

## 5. Node identity and submission flow

Nodes receive UUIDs. Agent and hybrid nodes declare supported categories; inactive, suspended, and banned nodes cannot participate. A SubmissionRecord links node, project, finding, category, and canonical finding hash. The same node cannot submit the same canonical content twice in one project.

## 6. Contribution scoring

Contribution scores combine validity, severity, reproducibility, uniqueness, quality, and explicit penalties on a 0–100 scale. Accepted, reproduced, unique, in-scope inputs pass the hard reward gates; validation failures cannot be overridden by a high score.

```text
Validation decides correctness.
Contribution scoring decides value.
```

## 7. Reputation model

Reputation starts at 0.5, remains between 0.0 and 1.0, and changes through one immutable event per finalized submission. Idempotent processing prevents double application.

| Outcome | Implemented delta |
| --- | ---: |
| Accepted base | +0.03 |
| Contribution score >= 90 | +0.02 |
| Contribution score 75–89.999 | +0.01 |
| High severity | +0.01 |
| Critical severity | +0.02 |
| Duplicate | -0.01 |
| Out of scope | -0.02 |
| Insufficient evidence | -0.02 |
| Rejected | -0.03 |
| Unsafe | -0.08 |
| Unsupported | 0.00 |

## 8. Reward-cycle model

Reward cycles move through draft, calculated, and finalized. The only unit is `protocol_points`.

```text
raw weight = contribution score * reputation multiplier * category multiplier
category multiplier = 1.0 in Week 5
```

Eligible raw weights divide a fixed project pool proportionally. Source fingerprints protect finalization inputs and deterministic remainder correction preserves the pool total. protocol_points are not tokens.

## 9. Penalty model

Unsafe findings are denied reward. Their PenaltyEvent records 25 internal protocol penalty points, simulated stake loss 0, `executed_onchain=false`, and required human review. Ordinary mistakes do not automatically create penalty events.

## 10. Benchmark cases

| Case | Primary expectation | Actual result | Passed |
| --- | --- | --- | --- |
| accepted_high_value_001 | Positive reputation and reward | rewarded 1000.000000 protocol points | yes |
| accepted_medium_value_001 | Positive but smaller contribution | rewarded 1000.000000 protocol points | yes |
| duplicate_no_reward_001 | No reward, small reputation penalty | finalized with zero reward | yes |
| out_of_scope_no_reward_001 | No reward | finalized with zero reward | yes |
| unsafe_penalty_001 | No reward and PenaltyEvent | penalized, zero reward | yes |
| inactive_node_rejected_001 | Submission rejected | submission rejected | yes |
| reputation_growth_001 | Gradual idempotent growth | rewarded 1200.000000 protocol points | yes |

## 11. Aggregate metrics

- Total cases: 7
- Passed cases: 7
- Failed cases: 0
- Pass rate: 1.000000
- Total checks: 98
- Accuracy (passed checks / total checks): 1.000000
- Submissions created: 8
- Submissions rejected: 1
- Eligible contributions: 5
- Ineligible contributions: 3
- Reputation events: 8
- Reward cycles finalized: 6
- Reward events: 5
- Zero-reward submissions: 3
- Penalty events: 1
- Idempotency checks: 9/9
- Protocol points distributed: 3200.000000

## 12. High versus Medium reward comparison

Both contributions were calculated in one shared reward cycle with equal initial reputation.

| Metric | High | Medium |
| --- | ---: | ---: |
| Contribution score | 100.000000 | 82.000000 |
| Reputation multiplier | 1.000000 | 1.000000 |
| Raw weight | 100.000000 | 82.000000 |
| Allocation ratio | 0.549450549451 | 0.450549450549 |
| Reward amount | 549.450549 | 450.549451 |

Comparison passed: yes. The High contribution receives more points because its contribution score and raw weight are larger.

## 13. Idempotency verification

All 9 of 9 explicit checks passed. They cover repeated reputation processing, repeated reward calculation, repeated reward finalization, repeated unsafe penalty processing, and same-node duplicate submission prevention.

## 14. Security properties

- No PoCs were executed.
- No Docker or forge command was called.
- No external subprocess or external API was called.
- No private key, seed phrase, or wallet was used.
- No blockchain operation occurred.
- No real token transfer occurred.
- No stake was locked or reduced.

## 15. Current limitations

The coordinator is centralized, fixtures are synthetic and small, validators are not independent, and no consensus, dispute system, signature verification, or Sybil resistance exists. Reputation is global, category multiplier is fixed at 1.0, no category subnets exist, and protocol points have no monetary value.

## 16. Week 6 direction

Week 6 should add category specialization and subnets:

```text
project attack categories
    |
    v
Subnet Router
    |
    +--> Access Control Subnet
    +--> Reentrancy Subnet
    +--> Accounting Subnet
```

Planned work includes category statistics and scores, subnet membership and ranking, exploration slots, routing, category reward pools, and a subnet benchmark.

## 17. Conclusion

Week 5 proves a complete deterministic off-chain protocol flow. It does not prove a decentralized blockchain network, token economy, validator consensus, or financial slashing system.
