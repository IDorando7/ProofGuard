# ProofGuard Protocol Specifications

These documents describe ProofGuard Protocol v0: a design for evolving the
existing centralized audit pipeline into a network of specialized agent nodes,
validator nodes, and an off-chain coordinator. They are architecture and
specification documents, not a production blockchain implementation.

## Documents

| Document | Purpose | Status |
| --- | --- | --- |
| [protocol-v0.md](protocol-v0.md) | Actors, responsibilities, trust model and complete flow | Draft v0 |
| [message-types.md](message-types.md) | Conceptual protocol messages and payloads | Draft v0 |
| [state-machine.md](state-machine.md) | Finding/submission lifecycle and allowed transitions | Draft v0 |
| [onchain-offchain-boundary.md](onchain-offchain-boundary.md) | Data placement and future blockchain boundary | Draft v0 |
| [reward-model.md](reward-model.md) | Week 7 reward architecture from task budget through deterministic client-task RewardEvents | Day 6 implemented |
| [validator-attestation-v1.md](validator-attestation-v1.md) | Week 8 authorized validator assignments, reproduction provenance, and structured immutable attestations | Day 1 implemented |
| [validator-committee-v1.md](validator-committee-v1.md) | Week 8 deterministic, conflict-free, operator-diverse validator committee planning and finalization | Day 2 implemented |
| [validator-reproduction-v1.md](validator-reproduction-v1.md) | Week 8 independent, sandboxed, validator-attributed reproduction execution and committee readiness | Day 3 implemented |
| [validator-consensus-v1.md](validator-consensus-v1.md) | Week 8 deterministic supermajority consensus, disputes, validation rounds, and bounded independent escalation | Day 4 implemented |
| [validator-performance-v1.md](validator-performance-v1.md) | Week 8 resolved-truth quality assessments, immutable validator history, category scores, neutral shrinkage, and validator-only membership | Day 5 implemented |
| [validator-reward-v1.md](validator-reward-v1.md) | Week 8 validator-pool work units, 30/70 completion-quality accounting, VQ², immutable events, and pool-specific consumption | Day 6 implemented |
| [week8-validator-benchmark.md](week8-validator-benchmark.md) | Week 8 adversarial real-service benchmark, deterministic replay, accounting verification, and final engineering report | Day 7 implemented |

## Principles

- Reward verifiable security value, not generated text.
- Full vulnerability details remain off-chain.
- Accepted findings require technical evidence.
- Validation status and contribution value are separate concepts.
- Protocol rules are tested off-chain before smart-contract implementation.
- The current coordinator is centralized and trusted in the MVP.
- Decentralization comes only after the validation and incentive rules are
  stable.

## Versioning

- Current version: Protocol v0
- Status: design draft
- Compatibility target: existing Week 1–4 MVP
- No on-chain deployment exists yet

Protocol v0 uses conceptual message-envelope version `0.1`. That wire-version
label does not imply that a network transport, API, or schema has been
implemented.

## Implemented off-chain components

Week 5 Day 2 implements Node Registry v0 in the centralized FastAPI MVP. It
persists agent, validator, and hybrid metadata off-chain, including optional
opaque public keys. Public-key ownership and cryptographic validity are not
verified; signatures and on-chain identity remain future work.

Week 5 Day 3 implements Finding Submission Protocol v0 as separate off-chain
records linking nodes, projects, and findings. Records include a deterministic
canonical SHA-256 finding hash and a guarded internal lifecycle, but no signed
message envelope or on-chain commitment.

Week 5 Day 4 implements Contribution Scoring Engine v0 as separate off-chain
records. Version `contribution_v0` deterministically scores validation,
severity, reproduction, uniqueness, evidence quality, and penalties without
changing validation or calculating a reward amount. No score commitment is
written on-chain.

Week 5 Day 5 implements Reputation Engine v0 as idempotent off-chain events.
The current node score is global, while every event records its vulnerability
category for future per-category aggregation. Per-category reputation remains
future work; the separate Week 6 Day 1 subnet registry does not change global
reputation behavior. No reward is calculated here and no blockchain reputation
checkpoint or registry exists.

Week 5 Day 6 implements Reward Simulator v0 and Penalty Event v0 as centralized
off-chain services. Reward cycles allocate simulated `protocol_points` from a
fixed project pool using contribution score and a moderate global-reputation
multiplier. Category multipliers are explicitly recorded but fixed at `1.0`.
Unsafe submissions may receive a separate reviewable `PenaltyEvent`; its
protocol penalty points are an internal severity indicator, not a financial
slash. Protocol points have no monetary value, no token contract exists, no
staking or financial slashing exists, and subnet economics remain future work.

Week 5 Day 7 adds a synthetic protocol benchmark covering node participation,
submission guards, contribution scoring, reputation, reward allocation,
unsafe penalties, and idempotency through the real off-chain services. Static
ReproductionResult and ValidationDecision fixtures are copied into isolated
temporary workspaces; no PoC, Docker, forge, external subprocess, production
protocol data, token, stake, or blockchain operation is involved.

Week 6 Day 1 implements **Subnet Registry v0 — off-chain**. Each registry
subnet corresponds to exactly one existing vulnerability category and uses the
deterministic ID `subnet_<normalized_category>`. The registry persists
configuration and administrative status, and defines read-only
`SubnetMemberRecord` persistence for later Week 6 services. Automated member
scoring, promotion or demotion, ranking, routing, exploration assignment, and
subnet rewards are not implemented. This registry is centralized off-chain
metadata, not a blockchain subnet or consensus group.

Week 6 Day 2 implements **Per-Category Performance Aggregator v0 — implemented
off-chain**. Applied ReputationEvents are grouped independently by node and
vulnerability category, verified against their persisted Week 5 sources, and
rebuilt into deterministic source-fingerprinted historical aggregates. Global
reputation remains the separate canonical global score. The aggregate provides
raw statistics only: no category score, subnet membership decision, ranking,
routing assignment, or subnet reward is produced.

Week 6 Day 3 implements **Category Scoring Engine v0 — implemented off-chain**.
Each score is derived only from one matching per-category performance
aggregate, remains separate from global reputation, and records its components,
penalties, experience confidence, score band, explanation, and canonical
source fingerprint. Confidence shrinkage pulls small samples toward neutral
`0.50`, so one successful submission cannot establish expertise. Score bands
are descriptive and do not automatically grant or change subnet membership.

Week 6 Day 4 implements **Subnet Membership Manager v0 — implemented
off-chain**. Membership is derived by the coordinator from node/subnet state,
category performance, category score, recent unsafe events, and policy-relevant
previous membership. Nodes cannot choose their own tier. Candidate, probation,
active, expert, suspended, and removed are performance/participation states;
expert is not a validator or consensus role. Hysteresis and deterministic
active-member capacity prevent oscillation and over-allocation. Membership
events are source-fingerprinted and immutable. No routing, exploration
selection, subnet reward allocation, or on-chain membership exists yet.

Week 6 Day 5 implements **Subnet Router v0 — implemented off-chain**. It reads
project scope categories and existing subnet membership/score records to
produce deterministic assignment plans. Active and expert members receive
ranked production assignments; candidate and probation members may receive
non-authoritative shadow exploration assignments. Finalization records
idempotent usage but does not execute nodes, collect results, alter membership,
or include shadow output in reports. Private rotating qualification benchmarks
and stronger candidate queues are not implemented.

Week 6 Day 6 implements **Subnet Reward Allocation v0 — implemented
off-chain**. One cycle uses one finalized routing record and divides an
explicit simulated protocol-points pool between isolated category pools.
Accepted, reproduced, unique, reward-eligible contributions receive
deterministic proportional allocations using routing-time membership and
category-score snapshots. Finalization creates immutable idempotent reward
events; it performs no payment or blockchain transfer. Participation rewards,
pricing, assurance packages, wallets, tokens, and settlement remain deferred.

Week 6 Day 7 completes the **Week 6 subnet skeleton — implemented and covered
by an end-to-end off-chain benchmark**. The benchmark uses temporary synthetic
records and the production services for:

- Subnet Registry;
- Category Performance;
- Category Scoring;
- Membership;
- Routing;
- Reward Allocation; and
- the Week 6 Benchmark and report.

It verifies category isolation, derived eligibility, unsafe suspension,
production/shadow routing, category-isolated simulated rewards, deterministic
replay, and idempotent finalization. It creates no permanent subnet rank and
performs no payment, token, wallet, staking, blockchain, AI/LLM, PoC, Docker,
forge, subprocess-analysis, or external-service action.

Week 7 Day 1 implements the **Reward Architecture Refactor Foundation —
off-chain**. A `RewardDomain` now separates `client_task` accounting from
future `network_protocol` incentives. One finalized routing may receive one
idempotent `TaskRewardBudget`, split exactly into miner, validator, and protocol
pools using centralized Decimal configuration and Week 6 largest-remainder
accounting. Budget creation creates no RewardEvent, payout, reputation change,
membership change, routing change, token action, or blockchain write. Week 5
and Week 6 v0 reward records and policies remain unchanged and readable.
