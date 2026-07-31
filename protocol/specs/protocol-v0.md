# ProofGuard Protocol v0

Status: design draft. Compatibility target: the existing Week 1–4 centralized
MVP.

## 1. Purpose

ProofGuard coordinates specialized security agents and validators to discover,
reproduce, validate, rank, and eventually reward Web3 vulnerability findings.
Its core rule is:

> Do not reward text. Reward reproducible and validated security value.

Protocol v0 is currently an off-chain simulation design built on top of the
existing centralized MVP. The MVP already supports project intake, candidate
findings, sandboxed PoC reproduction, deterministic validation, final audit
reports, a centralized off-chain Node Registry v0, Finding Submission Protocol
v0, Contribution Scoring Engine v0, and global Reputation Engine v0. Signed
protocol messages, category-specific reputation, rewards, financial penalties,
stake, consensus, cryptographic identity, and blockchain integration described
here remain future functionality unless explicitly identified as current.

The conceptual messages are defined in [message-types.md](message-types.md), the
submission lifecycle in [state-machine.md](state-machine.md), and the data
placement rules in
[onchain-offchain-boundary.md](onchain-offchain-boundary.md).

## 2. Current MVP versus future protocol

| Area | Current MVP | Future protocol |
| --- | --- | --- |
| Coordination | Central FastAPI backend | Distributed coordinator or protocol |
| Agents | Local software modules | Independently operated agent nodes |
| Validators | Deterministic centralized pipeline | Independent validator nodes |
| Storage | Local files and SQLite | Off-chain storage plus on-chain commitments |
| Identity | Off-chain UUID node records with optional opaque public keys | Cryptographically verified or wallet-backed node identities |
| Contribution scoring | Deterministic off-chain `contribution_v0` score records | Governed scoring plus optional public commitments |
| Reputation | Deterministic global off-chain score and event history | Optional public checkpoints |
| Category scoring | Source-fingerprinted off-chain scores derived independently from each per-category performance aggregate | Governed scoring and optional public commitments |
| Category specialization | Deterministic off-chain subnet registry records, one per vulnerability category | Automated membership, routing, independent operators, and optional public commitments |
| Rewards | Deterministic off-chain `protocol_points` simulator | Contribution-based protocol rewards |
| Stake | Not implemented | Optional validator/agent stake |
| Consensus | Single validator pipeline | Multi-validator decision aggregation |

The future column is a migration target, not a statement about deployed
capability.

## 3. Protocol actors

### 3.1 Audit Client

Responsibilities:

- submits a repository or other authorized project source;
- provides `scope.yaml`;
- defines assets at risk and attack categories;
- receives reports; and
- does not directly validate findings.

Trust assumptions:

- The client must have authorization to audit the submitted code.
- Scope information may be incomplete or incorrect.
- Client-provided code is treated as untrusted.

The current MVP implements repository/ZIP intake, scope parsing, workspaces, and
report retrieval. A protocol-level client identity is future work.

### 3.2 Agent Node

Responsibilities:

- analyzes only authorized projects;
- specializes in one or more vulnerability categories;
- submits candidate findings;
- may provide a PoC or reproduction artifact;
- does not decide whether its own finding is valid; and
- does not assign its own final reward.

The current off-chain Node Registry v0 stores:

- `node_id`;
- operator identity;
- an optional opaque public key string;
- supported categories; and
- software/agent version.

Public-key ownership is not verified. The current access-control and reentrancy
agents remain local software modules, not independently operated or
cryptographically identified nodes.

### 3.3 Validator Node

Responsibilities:

- checks project scope;
- checks reproduction evidence;
- detects duplicates;
- normalizes severity;
- evaluates evidence quality;
- produces a validation decision; and
- does not modify the original finding silently.

The current Validator Pipeline v0 is centralized and deterministic. It reads the
stored finding and reproduction result and writes a separate
`ValidationDecision`. Future validator nodes will independently reproduce and
evaluate submissions. How their decisions are aggregated remains open.

### 3.4 Protocol Coordinator

Current MVP responsibilities:

- creates audit jobs;
- routes projects to local agents;
- stores submissions;
- triggers reproduction;
- calls validators;
- stores decisions; and
- can invoke contribution scoring and reputation processing explicitly;
  automatic orchestration remains future work.

These bullets define the centralized coordinator role across the MVP service
modules; they do not imply a distributed node router or an already automated
end-to-end protocol. Scoring and reputation are explicit API/service operations
and are not automatically invoked today.

The coordinator is trusted in Protocol v0. In particular, it controls job
ordering and local storage and is assumed to call the configured components
honestly.

The future goal is to reduce coordinator trust through signed messages,
independent validators, commitments, replay protection, and deterministic
protocol rules. Protocol v0 does not yet achieve that goal.

#### 3.4.1 Off-chain category subnet registry

Implementation note (Week 6 Day 1): category specialization now has an
off-chain registry foundation. Every registered subnet maps to exactly one
existing vulnerability category and uses the deterministic identifier
`subnet_<normalized_category>`. Versioned configuration and administrative
status are stored under the protocol data root. The subnet membership status
enum and member-record schema are defined, but the registry itself calculates
no performance or score, makes no membership decision, creates no ranking, and
routes no project. These records do not represent a blockchain subnet,
validator consensus group, staking pool, or reward pool.

Implementation note (Week 6 Day 2): applied ReputationEvents are aggregated by
node and vulnerability category. The aggregator validates their persisted
submission, contribution, validation, reproduction, and finalized reward
references, then rebuilds deterministic raw counts and contribution statistics
with a canonical SHA-256 source fingerprint. This aggregate supplies inputs for
the future Category Scoring Engine v0; it does not calculate a category score
or change global reputation, subnet membership, rankings, routing, or rewards.

#### 3.4.2 Off-chain category scoring

Implementation note (Week 6 Day 3): Category Scoring Engine v0 converts exactly
one `CategoryPerformanceRecord` for one node and vulnerability category into a
separate score between `0.0` and `1.0`. It does not read raw finding text and
does not use global reputation as an input. Categories remain isolated.

The fixed positive weights are precision `0.30`, reproduction rate `0.20`,
uniqueness `0.15`, accepted-only contribution quality `0.20`, accepted-score
consistency `0.10`, and experience confidence `0.05`. Duplicate,
out-of-scope, insufficient-evidence, rejected, and unsafe outcome rates produce
explicit penalties; unsupported outcomes have no separate penalty in v0.
Unsafe has the largest multiplier and a `0.10` minimum penalty when any unsafe
submission exists. Total penalties are capped at `0.60`.

Experience confidence is `min(finalized submissions / 10, 1)`. The final score
uses neutral-baseline shrinkage:

```text
category score =
    0.50
  + experience confidence
    * (clamp(positive score - penalties, 0, 1) - 0.50)
```

Accepted-score consistency uses the accepted-only score range. Compatible
older aggregates without accepted-only bounds use neutral consistency `0.50`
instead of overall bounds that may include invalid zero-score submissions.

Every `category_score_v0` record stores components, weighted values, penalties,
raw values, confidence, deterministic explanation text, and a descriptive band:
`insufficient_data`, `weak`, `developing`, `strong`, or `expert`. The expert
band does not grant expert membership. A canonical SHA-256 fingerprint binds
the score to the complete performance counts and contribution statistics,
performance fingerprint, fixed weights, neutral baseline, and formula
versions. Rebuilding identical sources is idempotent and does not rewrite the
record.

Category scoring remains centralized and off-chain. It creates no subnet
member, ranking, routing decision, reward, token, stake, consensus result, or
blockchain write.

### 3.5 Reward Engine

Reward Simulator v0 is implemented off-chain. Contribution Scoring Engine v0
reads finalized evidence and stores a deterministic eligibility result
separately. The simulator's responsibilities are to:

- read finalized validation decisions;
- read contribution scores and reward eligibility;
- distribute simulated `protocol_points` from a fixed project pool; and
- never change a validation result to make a finding eligible.

Critical invariant:

> Validation decides whether a finding is valid. Contribution scoring decides
> how valuable an already evaluated contribution is.

A high contribution score cannot override `rejected`, `duplicate`,
`out_of_scope`, `insufficient_evidence`, `unsafe_poc`, `unsupported`, or
`needs_review` validation status.

The simulator creates source-fingerprinted reward cycles and immutable
RewardEvents. Category multipliers are fixed at `1.0`; the separate off-chain
Subnet Registry v0 does not change reward calculation. No token, stake,
financial slashing, blockchain operation, subnet reward, or subnet routing
exists.

### 3.6 Future Smart Contracts

Potential future responsibilities include:

- node identity commitments;
- stake accounting;
- finding commitment hashes;
- validation commitments;
- reputation checkpoints;
- reward allocation; and
- slash event recording.

Smart contracts will not execute AI analysis, Solidity PoCs, Docker, Foundry,
or complete vulnerability reports. All analysis and code execution remains
off-chain; untrusted code execution must remain sandboxed.

## 4. End-to-end protocol flow

```text
Audit Client
|
v
Project + scope.yaml
|
v
Protocol Coordinator
|
v
Specialized Agent Nodes
|
v
Finding Submissions
|
v
PoC Reproduction Layer
|
v
Validator Nodes / Validator Pipeline
|
v
Validation Decisions
|
v
Contribution Scoring
|
+--> Reputation Update
|
+--> Reward Eligibility
|
+--> Penalty or Slash Event
|
v
Final Audit Report + Protocol Metrics
```

1. **Client authorization and registration.** The Audit Client supplies an
   authorized project source and `scope.yaml`. The current coordinator creates
   a local project and workspace. A signed `ProjectRegistration` is future.
2. **Task creation.** The coordinator derives one or more category-specific
   audit tasks from the scope. Current local agents may be invoked directly;
   future tasks will identify assigned node IDs and deadlines.
3. **Agent analysis.** Specialized agents inspect only the authorized project
   and produce candidate findings. Agent confidence is evidence metadata, not a
   validation result.
4. **Finding submission.** The coordinator stores the complete finding
   off-chain and now creates a separate off-chain `SubmissionRecord` with a
   deterministic SHA-256 hash of normalized finding content. Signed submission
   messages and public/on-chain commitments remain future work.
5. **PoC reproduction.** If evidence is provided or requested, Safety Preflight
   evaluates it first. Unsafe PoCs are rejected and never executed. Permitted
   Foundry commands execute off-chain in the Docker sandbox and produce a
   separate `ReproductionResult`.
6. **Validation.** The current deterministic Validator Pipeline checks scope,
   deduplication, reproduction evidence, and normalized severity. Future
   validator nodes independently evaluate the same committed submission.
7. **Decision finalization.** A validation decision is stored separately from
   the agent finding. A future aggregation rule will determine when multiple
   validator decisions are final.
8. **Contribution scoring.** The implemented off-chain scorer reads validation
   and reproduction data and stores a separate, versioned value record. It
   cannot rewrite validation status or calculate a reward amount.
9. **Reputation, reward, and penalty evaluation.** The implemented global
   reputation engine applies versioned, explainable deltas once per finalized
   submission. The off-chain simulator separately allocates protocol points and
   records unsafe PenaltyEvents. Category-specific reputation, tokens, staking,
   and financial slashing remain future work.
10. **Report and metrics.** The current report service produces authorized
    off-chain reports. Future protocol metrics may expose non-sensitive counts,
    commitments, scores, or checkpoints.

## 5. Finding lifecycle overview

The protocol-level phases are:

1. `project_registered`
2. `task_created`
3. `finding_submitted`
4. `reproduction_pending`
5. `reproduction_completed`
6. `validation_pending`
7. `validation_finalized`
8. `scoring_completed`
9. `reputation_updated`
10. `rewarded`, `no_reward`, or `penalized`

These phases describe orchestration around a submission. They map to, but do
not replace, the existing `FindingStatus`, `ReproductionStatus`, and
`ValidationStatus` schemas. For example, `reproduction_completed` can contain
the exact result `reproduced`, `failed`, `rejected_unsafe`, `unsupported`,
`timeout`, `sandbox_error`, or `error`; it does not imply successful
reproduction. Exact mappings and allowed transitions are in
[state-machine.md](state-machine.md).

## 6. Trust assumptions

- Repositories and PoCs are untrusted.
- Agent output can be wrong, low quality, spam, or malicious.
- Validators can disagree, fail, or act dishonestly.
- The coordinator is trusted in Protocol v0.
- Full findings may contain sensitive, unpublished vulnerabilities.
- Reward calculations must use finalized validation data.
- All external code execution must remain off-chain and sandboxed.
- On-chain hashes prove a commitment to bytes, not correctness or truth.
- A high contribution or reputation score does not prove a finding is valid.
- A failed reproduction is not automatically malicious behavior.
- The client is responsible for authorization, but the protocol should still
  record and check that authorization where practical.

## 7. Protocol invariants

1. A finding cannot be accepted only because an agent reported high confidence.
2. Accepted status requires scope compliance and sufficient validation
   evidence.
3. A duplicate is not rewarded as a new vulnerability.
4. An out-of-scope finding receives no reward.
5. An unsafe PoC is never executed after rejection.
6. A scoring function cannot change `rejected` into `accepted`.
7. Reward eligibility requires a finalized validation decision.
8. Full vulnerability content is not placed on-chain.
9. Existing audit data is never silently mutated by a protocol message.
10. Every reward or penalty references an explainable submission and decision.
11. Validator and agent identities are separable, even if one operator may run
    both roles in a future deployment.
12. Simple mistakes and malicious behavior do not receive identical penalties.
13. A high contribution score cannot override an invalid or non-final
    validation status.
14. Smart contracts do not claim to validate PoCs; PoC execution and technical
    validation remain off-chain.

## 8. Security goals

- safe handling of untrusted repositories;
- reproducible findings;
- traceable submissions and decisions;
- deterministic commitments;
- resistance to duplicate spam;
- resistance to unsafe PoCs;
- explainable, versioned scoring;
- reputation isolation by vulnerability category; and
- eventual resistance to validator collusion and Sybil attacks.

Collusion resistance and Sybil resistance are future work. They may require
independent validator selection, stake, identity costs, category reputation,
challenge periods, and monitoring, none of which exists in the current MVP.

## 9. Non-goals for Protocol v0

- production blockchain deployment;
- permissionless public execution;
- a real token economy;
- real stake slashing;
- a final consensus algorithm;
- privacy-preserving vulnerability disclosure;
- fully autonomous AI validation;
- replacing professional human auditors; and
- storing vulnerability details publicly on-chain.

## 10. Migration path

### Stage 0: Current centralized MVP

```text
Repository -> agents -> reproduction -> validator -> report
```

This stage is implemented with a FastAPI coordinator, local agent modules,
SQLite and file storage, sandboxed reproduction, one deterministic Validator
Pipeline v0, final reports, and the centralized off-chain Node Registry v0.

### Stage 1: Off-chain protocol simulation

Node Registry v0, Finding Submission Protocol v0, Contribution Scoring Engine
v0, and global Reputation Engine v0 are implemented components of this stage.
Remaining future additions are:

- signed submission messages;
- category-specific reputation;
- reward and penalty simulation using non-monetary protocol points.

These remaining additions are future work. Protocol rules should be measured
and revised here before being encoded in contracts.

### Stage 2: Independent operators

Add:

- allowlisted external agent nodes;
- independent validator nodes;
- signed messages;
- deterministic task assignment; and
- a local sandbox requirement for every component that executes untrusted code.

### Stage 3: On-chain commitments

Add:

- an identity/public-key registry;
- stake;
- submission hashes;
- validation commitments;
- reward checkpoints; and
- slash-event commitments.

Only commitments and accounting data move on-chain. Analysis, PoCs, and reports
remain off-chain.

### Stage 4: Decentralized protocol

Add:

- validator consensus;
- decentralized category subnets and independent subnet operation;
- a challenge/dispute mechanism; and
- permissionless or semi-permissionless participation.

This stage depends on proven validation, incentive, privacy, and governance
rules. Its design is intentionally not finalized in Protocol v0.

## 11. Open design questions

- How many validators must agree before a decision is finalized?
- How is a validator selected, rotated, and excluded for conflicts of interest?
- How are conflicting reproduction results handled?
- Who can view an unpublished vulnerability, and how is access revoked?
- How long is the challenge window?
- How are new nodes given exploration opportunities without weakening quality?
- How is global and category reputation initialized and decayed?
- Which behaviors justify reputation loss versus financial slashing?
- How are rewards divided between agent and validator nodes?
- How are coordinator failures, censorship, or equivocation handled?
- What evidence is sufficient to distinguish an unsafe mistake from a
  deliberately malicious PoC?
- What canonical serialization and salt scheme prevents commitment ambiguity
  and guessing attacks?

## 12. Off-chain subnet membership policy v0

The implemented Subnet Membership Manager derives one membership independently
for each `node_id + subnet category`. It consumes the authoritative node and
subnet records, the matching per-category performance and score records,
recent applied unsafe reputation events, and policy-relevant previous member
state. Global reputation does not determine membership, and one category
cannot affect another.

The tiers are `candidate`, `probation`, `active`, and `expert`, with additional
`suspended` and terminal-under-normal-refresh `removed` states. Candidate
applies below 3 finalized submissions or below the probation score threshold
`min(0.40, subnet minimum score)`. Active requires the subnet minimum score and
finalized-history settings plus 2 accepted unique findings. Expert requires
score `max(0.80, subnet minimum score)`, 10 finalized submissions, 6 accepted
unique findings, and no historical unsafe submissions.

Active demotion uses a score threshold 0.10 below promotion, one fewer
finalized submission (minimum 1), and one accepted unique finding. Expert
demotion uses a score threshold 0.10 below promotion but never below the active
threshold, plus 8 finalized and 5 accepted unique findings. Hysteresis cannot
override node or subnet ineligibility, archival, category removal,
administrative locks, or recent unsafe behavior.

An applied unsafe-submission reputation event for the exact node/category at or
after the inclusive UTC time `evaluated_at - 30 days` suspends membership.
Older unsafe outcomes still prevent expert promotion through the category
performance snapshot.

The subnet's `maximum_active_nodes` caps active plus expert members. Capacity is
resolved deterministically: expert recommendations first, then category score,
experience confidence, accepted unique count, finalized count, and node ID.
Overflow becomes probation. The ordering is not persisted as rank.

Each logical evaluation stores an immutable membership decision before
atomically updating the current member. Its canonical JSON SHA-256 fingerprint
covers versioned policy constants, relevant node/subnet state, exact
performance and score source identities, recent unsafe event identities,
policy-relevant prior state, and capacity context. Evaluation timestamps,
explanation prose, display metadata, rank, routing, and assignment counters are
excluded. Identical inputs reuse the existing event and do not rewrite the
member.

This policy is centralized and off-chain. It does not route projects, allocate
exploration work, calculate subnet rewards, elect validators, or perform any
blockchain operation.

## 13. Off-chain subnet routing policy v0

Subnet Router v0 resolves project categories only from the parsed
`ScopeManifest.attack_categories` field. Explicit requested categories must be
a subset of that scope. Missing categories do not cause all registered subnets
to be selected, and source code is never scanned to infer routing intent.

Each category uses its exact active subnet and membership records. Ranked
production eligibility requires an active node, active subnet, active/expert
membership, matching node/subnet/category identity, and a current category
score whose source fingerprint matches the membership snapshot. Exploration
eligibility is limited to candidate/probation members and always produces a
shadow assignment. An insufficient-history candidate may use its Day 4 zero
snapshot when no score exists. Suspended, removed, administratively locked,
inactive, banned, wrong-category, and stale-source nodes are excluded without
fallback promotion.

Exploration slots use the subnet's registry ratio while preserving at least one
ranked slot. Ranked ordering is expert before active, then score, confidence,
accepted unique count, finalized count, and node ID. Exploration ordering uses
fewer finalized exploration assignments, never/oldest assignment time,
probation before candidate on a full fairness tie, score, confidence, and node
ID. No random selection or permanent subnet rank is used.

Missing or inactive subnets and insufficient eligible nodes create explicit
shortages. Partial plans may be persisted only when requested. A routing source
fingerprint covers the project scope, request, relevant subnet configuration,
all considered member/node/score snapshots, administrative locks, and
finalized usage history. Descriptions, timestamps, host paths, findings, and
PoCs are excluded.

Routing records move from `calculated` to immutable `finalized`, or may be
`superseded` before finalization. Finalization reloads current sources and
rejects stale plans. It creates one exclusive usage event per assignment;
repeated finalization cannot double-count usage. Usage events remain the
fairness authority, so Router v0 does not write membership counters or tiers.

The router produces plans only. It does not communicate with nodes, execute
agents, collect results, create findings/submissions, promote members, route
shadow results into reports, or allocate rewards. It does not implement private
qualification benchmarks, candidate waiting guarantees, operator/geographic/
model diversity, Sybil resistance, concurrency limits, or workload balancing.

## 14. Off-chain subnet reward allocation policy v0

Subnet Reward Allocation v0 creates at most one reward cycle for one finalized
project routing record. Its caller supplies a simulated protocol-points pool;
no price, client payment, wallet, token, or on-chain transfer is involved.
Submission records may optionally reference a routing and assignment. Legacy
unlinked submissions remain valid for Week 5 but cannot receive a Day 6 subnet
reward.

The routed category set is fixed by the routing record. Missing category
weights mean equal weights. Explicit positive weights must cover exactly that
set and are normalized proportionally. Category amounts are rounded down to
`0.000001` protocol points, then residual units use largest fractional
remainder with category-name tie-breaking. Category pools always sum exactly
to the project pool and remain isolated; undistributed points never flow into
another category.

Eligible contributions require an exact routing link, finalized routing,
matching project/node/category assignment, accepted in-scope non-duplicate
validation, successful reproduction, a positive reward-eligible contribution
score, and a currently active node. Active/expert production assignments and
probation shadow assignments may receive contribution rewards. Candidate
shadow, suspended, and removed snapshots are ineligible.

Membership multipliers are `probation=0.90`, `active=1.00`, and `expert=1.10`.
Category-score multipliers are `0.80` below 0.40, `0.95` from 0.40 below 0.60,
`1.05` from 0.60 below 0.80, and `1.15` from 0.80 through 1.00. Both values
come from the immutable routing assignment snapshot rather than later
membership or score state.

```text
raw weight = contribution score
           * routing-time category multiplier
           * routing-time membership multiplier

reward = category pool * raw weight / category total raw weight
```

Per-submission rewards use Decimal arithmetic, round down to six places, and
distribute residual units by fractional remainder, raw weight, contribution
score, node ID, and submission ID. This preserves exact category and cycle
totals independently of input order.

Cycles transition `draft -> calculated -> finalized`. Calculation records
allocations, exclusions, source snapshots, node summaries, and a canonical
SHA-256 source fingerprint without creating events. Finalization reloads all
relevant sources and refuses stale inputs, then creates one deterministic
exclusive event per eligible submission. A finalized Week 5 contribution
reward or an applied Day 6 event from another cycle prevents double reward.
Finalized cycles and events are immutable.

Participation rewards are not present: a routed node with no accepted
contribution receives no protocol points. Pricing/assurance packages,
validator/treasury allocation, disputes, real settlement, and protection
against deliberately splitting one issue into multiple findings remain future
work. V0 relies on the existing semantic deduplication and validation pipeline
for the latter.

## 15. Week 6 end-to-end verification

The Week 6 end-to-end benchmark validates:

- category isolation;
- membership eligibility;
- unsafe suspension;
- production/shadow routing;
- category-isolated rewards;
- deterministic replay; and
- idempotent finalization.

The verification uses synthetic temporary protocol records and project
workspaces while calling the real Week 5 and Week 6 services. It does not
modify production data, persist leaderboard rank to membership, execute a
node or PoC, invoke Docker, forge, project-analysis subprocesses, AI/LLMs, or
external APIs, or perform a payment, token, wallet, staking, smart-contract, or
blockchain operation.
