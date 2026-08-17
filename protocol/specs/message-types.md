# ProofGuard Protocol Message Types

Status: design draft. These are specification-level messages only. Protocol v0
does not implement Pydantic schemas, API routes, network transport, signatures,
or message persistence for them.

The messages coordinate the actors in [Protocol v0](protocol-v0.md). Their
statuses must follow the mappings in [the state machine](state-machine.md), and
sensitive fields follow the
[on-chain/off-chain boundary](onchain-offchain-boundary.md).

## 1. Message envelope

Every future message uses this conceptual common envelope:

```json
{
  "protocol_version": "0.1",
  "message_id": "uuid",
  "message_type": "finding_submission",
  "created_at": "ISO-8601 UTC timestamp",
  "actor_id": "node-or-coordinator-id",
  "project_id": "project-id",
  "correlation_id": "optional-parent-message-id",
  "payload_hash": "sha256-of-canonical-payload",
  "payload": {}
}
```

| Field | Meaning |
| --- | --- |
| `protocol_version` | Message contract version. Protocol v0 uses conceptual version `0.1`. |
| `message_id` | Globally unique message identifier used for idempotency and future replay protection. |
| `message_type` | Stable discriminator naming the payload type, such as `finding_submission`. |
| `created_at` | Actor-provided ISO-8601 timestamp in UTC. It is metadata, not proof of ordering. |
| `actor_id` | ID of the node or coordinator that created the message. It is not cryptographically authenticated in v0. |
| `project_id` | Existing or future project identifier to which the message belongs. |
| `correlation_id` | Optional ID of the task, request, or message that caused this message. |
| `payload_hash` | Lowercase SHA-256 digest of the future canonical payload bytes. |
| `payload` | Type-specific content. Sensitive content is stored and disclosed off-chain only. |

Signatures are future work. `payload_hash` protects integrity only when compared
to the same canonical payload; it does not prove that a claim is true, that an
actor created it, or that a PoC works. Canonical JSON rules will be defined
before implementation. Sensitive payloads and full vulnerability details remain
off-chain even if their hashes are later committed publicly.

Examples below use deterministic placeholder IDs and timestamps. Hash strings
are illustrative labels, not hashes of real vulnerability data.

## 2. ProjectRegistration

`ProjectRegistration` records the authorized audit input and its scope
commitment. The full repository and sensitive scope content are not embedded in
the message.

Conceptual fields:

- `project_id`
- `project_name`
- `repository_commit`
- `scope_hash`
- `requested_categories`
- `created_by`
- `created_at`

```json
{
  "project_id": "project-0001",
  "project_name": "ExampleVault",
  "repository_commit": "0123456789abcdef0123456789abcdef01234567",
  "scope_hash": "sha256:scope-example-0001",
  "requested_categories": ["access_control", "reentrancy"],
  "created_by": "coordinator-0001",
  "created_at": "2026-07-13T12:00:00Z"
}
```

## 3. AuditTaskCreated

`AuditTaskCreated` assigns a category-scoped unit of analysis. Assignment is
centralized in Protocol v0; deterministic distributed assignment is future
work.

Conceptual fields:

- `task_id`
- `project_id`
- `category`
- `assigned_node_ids`
- `deadline`
- `scope_hash`
- `task_status`

```json
{
  "task_id": "task-0001",
  "project_id": "project-0001",
  "category": "reentrancy",
  "assigned_node_ids": ["agent-node-0001"],
  "deadline": "2026-07-14T12:00:00Z",
  "scope_hash": "sha256:scope-example-0001",
  "task_status": "task_created"
}
```

`task_status` is a protocol orchestration value, not a new current application
enum.

## 4. FindingSubmission

`FindingSubmission` associates an agent, task, and candidate finding while
keeping the full finding off-chain.

Conceptual fields:

- `submission_id`
- `project_id`
- `node_id`
- `finding_id`
- `finding_hash`
- `category`
- `agent_name`
- `agent_version`
- `submitted_at`
- `optional_poc_commitment`
- `status`

```json
{
  "submission_id": "submission-0001",
  "project_id": "project-0001",
  "node_id": "agent-node-0001",
  "finding_id": "finding-0001",
  "finding_hash": "sha256:finding-example-0001",
  "category": "reentrancy",
  "agent_name": "reentrancy_agent",
  "agent_version": "example-1.0.0",
  "submitted_at": "2026-07-13T12:05:00Z",
  "optional_poc_commitment": "sha256:poc-example-0001",
  "status": "submitted"
}
```

`finding_hash` identifies normalized finding content. It was conceptual on Day
1 and is implemented off-chain as described below. `status` is the protocol
submission state; the current finding remains `candidate` or `unverified`
according to the existing `FindingStatus` enum.

Implementation note (Week 5 Day 3): the centralized MVP now persists an
off-chain `SubmissionRecord`. Its `finding_hash` is SHA-256 over compact,
key-sorted UTF-8 JSON containing normalized project ID, category, contracts,
functions, root cause, attack path, and impact. The signed conceptual message
envelope is not implemented, and no hash is written on-chain. Repeated canonical
content from the same node and project is a submission-level duplicate; this is
separate from validator-determined semantic finding deduplication.

## 5. ReproductionResultSubmitted

`ReproductionResultSubmitted` commits to an off-chain reproduction attempt.

Conceptual fields:

- `reproduction_id`
- `submission_id`
- `finding_id`
- `executor_id`
- `status`
- `poc_hash`
- `output_hash`
- `duration_ms`
- `sandbox_profile`
- `created_at`

```json
{
  "reproduction_id": "reproduction-0001",
  "submission_id": "submission-0001",
  "finding_id": "finding-0001",
  "executor_id": "coordinator-sandbox-0001",
  "status": "reproduced",
  "poc_hash": "sha256:poc-example-0001",
  "output_hash": "sha256:output-example-0001",
  "duration_ms": 1842,
  "sandbox_profile": "foundry-sandbox-v0",
  "created_at": "2026-07-13T12:07:00Z"
}
```

The `status` field reuses the existing `ReproductionStatus` values:
`not_attempted`, `generated`, `running`, `reproduced`, `failed`, `error`,
`timeout`, `unsupported`, `rejected_unsafe`, and `sandbox_error`. The full PoC,
stdout, stderr, and sandbox logs remain off-chain. A status of `reproduced` does
not itself mean the finding is accepted.

## 6. ValidationDecisionSubmitted

`ValidationDecisionSubmitted` records a validator's separate evaluation of a
submission. In the current MVP the central Validator Pipeline v0 produces one
stored decision; independent validator messages are future work.

Conceptual fields:

- `validation_id`
- `submission_id`
- `finding_id`
- `validator_id`
- `status`
- `confidence`
- `normalized_severity`
- `duplicate_of`
- `evidence_hash`
- `created_at`

```json
{
  "validation_id": "validation-0001",
  "submission_id": "submission-0001",
  "finding_id": "finding-0001",
  "validator_id": "validator-0001",
  "status": "accepted",
  "confidence": 0.95,
  "normalized_severity": "High",
  "duplicate_of": null,
  "evidence_hash": "sha256:evidence-example-0001",
  "created_at": "2026-07-13T12:10:00Z"
}
```

`status` reuses the existing `ValidationStatus` values: `accepted`, `rejected`,
`duplicate`, `needs_review`, `out_of_scope`, `insufficient_evidence`,
`unsafe_poc`, and `unsupported`. Severity reuses the exact title-cased values
`Critical`, `High`, `Medium`, `Low`, and `Informational`. The decision commitment
does not reveal private vulnerability details and does not prove the validator
was honest.

Week 7 Day 2 keeps validation truth separate from root-cause relation. New
accepted independent matches additionally persist
`duplicate_kind=independent_root_cause`, `is_valid_duplicate=true`, and
`canonical_finding_id` (equal to the legacy-compatible `duplicate_of`
reference). Same-node submission spam is rejected before validation and cannot
set these fields. Historical `duplicate` decisions remain readable.

## 7. ContributionScoreCalculated

`ContributionScoreCalculated` is represented in the centralized MVP by the
off-chain `ContributionScoreRecord`. The implementation uses scoring version
`contribution_v0` and deterministic component rules. It calculates reward
eligibility but no reward or token amount, and it writes no on-chain score
commitment. A signed protocol message envelope remains future work.

Conceptual fields:

- `score_id`
- `submission_id`
- `node_id`
- `validation_id`
- `total_score`
- `component_scores`
- `eligible_for_reward`
- `scoring_version`
- `created_at`

```json
{
  "score_id": "score-0001",
  "submission_id": "submission-0001",
  "node_id": "agent-node-0001",
  "validation_id": "validation-0001",
  "total_score": 72,
  "component_scores": {
    "reproduction_evidence": 30,
    "severity_value": 25,
    "novelty": 17
  },
  "eligible_for_reward": true,
  "scoring_version": "draft-example-v0",
  "created_at": "2026-07-13T12:11:00Z"
}
```

The JSON above remains a conceptual message example; the implemented record
uses the component names `validity`, `severity`, `reproducibility`,
`uniqueness`, `quality`, and `penalties`. Scoring cannot alter the validation
decision. In particular, a high `total_score` cannot make an invalid or
non-final finding reward-eligible.

## 8. ReputationUpdated

`ReputationUpdated` is represented in the centralized MVP by the off-chain
`ReputationEvent`. The implementation uses version `reputation_v0`, references
exactly one source submission, and applies a submission event idempotently
through prepared and applied states. Every event stores the vulnerability
category for future category-specific scoring, while the current node score
remains global. No reward amount is calculated and no reputation checkpoint is
written on-chain.

Conceptual fields:

- `reputation_event_id`
- `node_id`
- `category`
- `previous_score`
- `delta`
- `new_score`
- `reason_code`
- `source_submission_id`
- `created_at`

```json
{
  "reputation_event_id": "reputation-event-0001",
  "node_id": "agent-node-0001",
  "category": "reentrancy",
  "previous_score": 50,
  "delta": 3,
  "new_score": 53,
  "reason_code": "VALIDATED_CONTRIBUTION",
  "source_submission_id": "submission-0001",
  "created_at": "2026-07-13T12:12:00Z"
}
```

The conceptual JSON above uses an older 0–100 illustration. The implemented
global reputation range is `0.0`–`1.0`, new nodes begin at `0.5`, and events
store deterministic delta components, signals, before/after statistics, and a
SHA-256 source fingerprint. Reputation decay and per-category reputation are
not implemented. Category performance remains separable in event history so
success in one vulnerability category need not imply competence in every
category later.

## 9. RewardCalculated

`RewardCalculated` now has an implemented off-chain representation as
`RewardEvent`, created only when a calculated reward cycle is finalized.

Conceptual fields:

- `reward_event_id`
- `node_id`
- `submission_id`
- `contribution_score`
- `reputation_multiplier`
- `reward_amount`
- `reward_unit`
- `executed_onchain`
- `created_at`

```json
{
  "reward_event_id": "reward-event-0001",
  "node_id": "agent-node-0001",
  "submission_id": "submission-0001",
  "contribution_score": 72,
  "reputation_multiplier": 1.0,
  "reward_amount": 72,
  "reward_unit": "protocol_points",
  "executed_onchain": false,
  "created_at": "2026-07-13T12:13:00Z"
}
```

For the implemented Protocol v0 simulator, `reward_unit` is always
`protocol_points`. Allocation uses the immutable contribution-score snapshot
and the node's global-reputation bracket; the category multiplier is fixed at
`1.0`. The implemented `RewardEvent` deliberately has no `executed_onchain`
field because no chain operation or transfer occurs. The conceptual field above
remains a future message-design illustration. Protocol points are simulation
accounting, not tokens, currency, or a promise of future monetary value.

## 10. PenaltyProposed

`PenaltyProposed` now has an implemented off-chain `PenaltyEvent` representation
for finalized unsafe submissions. It remains a reviewable record, not an
automatic financial slash.

Conceptual fields:

- `penalty_event_id`
- `node_id`
- `submission_id`
- `reason_code`
- `reputation_delta`
- `simulated_slash_points`
- `requires_human_review`
- `executed_onchain`
- `created_at`

```json
{
  "penalty_event_id": "penalty-event-0001",
  "node_id": "agent-node-0002",
  "submission_id": "submission-0002",
  "reason_code": "UNSAFE_ARTIFACT_REVIEW_REQUIRED",
  "reputation_delta": 0,
  "simulated_slash_points": 0,
  "requires_human_review": true,
  "executed_onchain": false,
  "created_at": "2026-07-13T12:14:00Z"
}
```

- Ordinary failed reproduction must not automatically cause financial
  slashing.
- Unsafe or demonstrably malicious behavior may justify stronger penalties
  after evidence-based review.
- Real slashing requires future dispute, appeal, finality, and consensus rules.
- Protocol v0 proposals use simulated points only and always set
  `executed_onchain` to `false`.
- Implemented unsafe events deny reward, assign 25 internal
  `protocol_penalty_points`, set simulated stake loss to zero, set
  `executed_onchain` to `false`, and require human review.
- Ordinary mistakes, duplicates, rejected or out-of-scope findings,
  insufficient evidence, unsupported inputs, and needs-review results do not
  automatically produce a `PenaltyEvent`.

## 11. Error and rejection messages

An error or rejection response should preserve the original `message_id`, use
it as `correlation_id`, and provide a stable reason code plus a non-sensitive
explanation. Conceptual reason codes are:

- `NODE_NOT_FOUND`
- `NODE_INACTIVE`
- `CATEGORY_NOT_SUPPORTED`
- `FINDING_NOT_FOUND`
- `DUPLICATE_SUBMISSION`
- `INVALID_SCOPE`
- `OUT_OF_SCOPE`
- `UNSAFE_POC`
- `REPRODUCTION_FAILED`
- `VALIDATION_PENDING`
- `NOT_REWARD_ELIGIBLE`
- `INTERNAL_PROTOCOL_ERROR`

```json
{
  "reason_code": "VALIDATION_PENDING",
  "message": "Reward calculation requires a finalized validation decision.",
  "retryable": true,
  "source_message_id": "message-0007"
}
```

These reason codes are protocol concepts, not additions to the current Python
enums. Error messages must not echo private finding or PoC content.

## 12. Message integrity and future signatures

- Canonical payload serialization must be deterministic across implementations,
  including field ordering, Unicode normalization, number encoding, and null
  handling.
- SHA-256 commitments bind a message to canonical bytes but do not establish
  correctness.
- Future actor signatures should cover the protocol version, message ID,
  message type, project ID, correlation ID, and payload hash to prevent field
  substitution.
- Timestamps do not prove ordering by themselves. Future ordering may rely on a
  coordinator sequence, validator round, or on-chain block reference.
- Replay protection requires unique message IDs plus defined nonce or sequence
  handling and durable processed-message tracking.
- Hash commitments to small or guessable private payloads may require salts.
- Full encrypted disclosure and key-management design are future work.

## 13. Off-chain routing messages

The following conceptual messages are represented today by centralized
off-chain routing records and usage events. They are not signed network
messages or on-chain transactions.

### RoutingCalculated

Records the project, resolved scope categories, routing request, category
results, assignment snapshots, shortages, and canonical source/request
fingerprints. Calculation does not execute an assigned node.

### RoutingAssignmentCreated

Represents one immutable assignment inside a routing plan. `ranked` maps to
`production` for active/expert members. `exploration` maps to non-authoritative
`shadow` for candidate/probation members. Router v0 always stores
`qualification_reference` as null.

### RoutingFinalized

Marks a source-verified calculated plan immutable. A superseded plan cannot be
finalized, and finalization does not create findings, submissions, execution
leases, or report results.

### RoutingUsageRecorded

Records one finalized assignment usage exactly once. These off-chain events
drive exploration fairness and are not reward events. Replaying finalization
reuses the existing matching event rather than incrementing usage again.

## 14. Off-chain subnet reward messages

These are conceptual names for the currently persisted centralized objects.
They are not signed messages, payment instructions, token transfers, or
blockchain events.

### SubnetRewardCycleCreated

Records one draft cycle for a finalized routing, including its simulated
protocol-points pool, exact routed category weights, category pools, and stable
request fingerprint.

### SubnetRewardCalculated

Records deterministic contribution allocations and exclusions, routing-time
score/membership snapshots, exact Decimal totals, node summaries, and a source
fingerprint. Calculation creates no reward event.

### SubnetRewardExcluded

Explains why a routed submission was ineligible, such as candidate shadow
status, duplicate or out-of-scope validation, failed reproduction, missing or
ineligible contribution score, inactive node state, source mismatch, or an
existing contribution reward.

### SubnetRewardFinalized

Marks a source-revalidated calculated cycle immutable. Finalization does not
modify routing, membership, scoring, validation, reproduction, findings, or
submissions.

### SubnetRewardEventApplied

Records one simulated contribution allocation exactly once for a submission.
Its deterministic event ID binds the reward cycle and submission; it does not
represent a real payment or on-chain claim.

## 15. Off-chain FindingCluster valuation records

These Week 7 Day 4 objects are deterministic calculation snapshots, not signed
payment messages.

### FindingClusterRewardAllocation

Explains one finalized root cause using its validator-approved severity,
configured severity weight, distinct valid operator count, persisted
uniqueness, FindingScore, parent miner/category pool, and allocated protocol
points. It contains no report Q, operator rank, Top-K, Chief Finder, or
individual reward.

### TaskFindingRewardCalculated

Records one `finding_cluster_value_v1` allocation across a finalized
`TaskRewardBudget.miner_pool_points`. It conserves the miner pool between
distributed and undistributed cluster points, stores a canonical source
fingerprint, and creates no RewardEvent. Changed economic sources create a
superseding snapshot; identical inputs reuse the existing logical result.

## 16. Off-chain operator reward preview records

### OperatorClusterRewardAllocation

Explains one operator's representative report, finalized Q and Q², ordinal
quality rank, Top-K state, optional distinct Chief-qualifying report, Quality
Pool points, Chief points, and total. Exactly one allocation exists per
eligible operator per FindingCluster; reports not representing the operator
remain visible as exclusions.

### TaskOperatorRewardCalculated

Aggregates per-cluster `operator_cluster_payout_v1` previews and per-operator
task totals. Its canonical fingerprint binds the active Day 4 snapshot,
authorized report/assignment/operator identities, finalized Day 3 assessments,
Top-K and Chief policy, selections, Q² values, and exact conserved totals. It is
not `RewardEventApplied`, does not consume a budget, and is not a payment.

## 17. Week 7 Day 6 reward-cycle messages

### Week7TaskRewardCycle

Policy-versioned `client_task` record with `draft`, `calculated`, and
`finalized` states. It references the finalized TaskRewardBudget and current
Day 4/Day 5 calculations, commits their economic content through deterministic
fingerprints, and conserves miner distributed plus undistributed points.

### Week7TaskRewardEvent

Immutable positive economic event for one reward cycle, FindingCluster and
operator. It references the representative node/submission, Q and Q² weight,
quality rank, Chief evidence, Quality Pool amount, Chief bonus and total. Its
identifier is derived from cycle, cluster, operator, representative submission
and policy.

### Week7TaskRewardHistorySummary

Read-only economic summary by operator, representative node, submission or
FindingCluster. It is deliberately separate from ContributionScore,
ReputationEvent and CategoryScore messages.
