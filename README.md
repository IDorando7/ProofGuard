# ProofGuard / Web3 AI Security Network

## Week 5 Day 1 - Protocol Specification v0

Week 5 Day 1 documents the protocol actors, specifies the finding/submission
lifecycle, defines conceptual message types, and establishes the future
on-chain/off-chain boundary. No smart contracts or protocol code were
implemented yet.

See the [Protocol v0 specification index](protocol/specs/README.md).

## Week 5 Day 2 - Node Identity and Registry v0

ProofGuard now has persistent, centralized off-chain identities for agent,
validator, and hybrid nodes. Registration assigns each node a UUID and stores
one human-readable record under
`apps/audit-api/data/protocol/nodes/<node_id>/node.json`. Nodes declare
categories from the existing vulnerability category enum and can be `active`,
`inactive`, `suspended`, or `banned`; banned is terminal through the normal
status API.

New nodes start with neutral reputation `0.5` and zeroed statistics. Those
values are reserved for later protocol services and cannot be changed through
the public metadata endpoint. Public keys are optional, unique opaque identity
strings: ownership and cryptographic validity are not verified. Private keys,
mnemonics, seed phrases, and other secret fields are never accepted.

This registry does not provide wallets, signatures, stake, Sybil resistance,
blockchain identity, permissionless admission, or decentralization.

### Register an agent

```bash
curl -X POST http://localhost:8000/nodes \
  -H "Content-Type: application/json" \
  -d '{
    "node_type": "agent",
    "display_name": "access-control-agent-01",
    "operator_id": "operator_local_01",
    "supported_categories": ["access_control"],
    "description": "Local access-control analysis agent."
  }'
```

### List active agent nodes

```bash
curl "http://localhost:8000/nodes?node_type=agent&status=active"
```

### Suspend a node

```bash
curl -X POST http://localhost:8000/nodes/<node_id>/status \
  -H "Content-Type: application/json" \
  -d '{
    "status": "suspended",
    "reason": "Administrative review."
  }'
```

## Week 5 Day 3 - Finding Submission Protocol v0

A submission is now a separate off-chain protocol record linking an existing
audit project, finding, and registered node. Every submission receives a UUID
and a deterministic SHA-256 `finding_hash` derived from normalized finding
content. Only active agent and hybrid nodes may submit, and the node must list
the finding category among its supported categories.

For one project, the same node cannot submit the same canonical content twice;
different nodes may independently submit the same hash. This submission-level
spam check is separate from Week 4 semantic deduplication, where the validator
decides whether findings describe the same underlying vulnerability.

New records begin with status `submitted` and reward status `pending`.
Submission creation does not run reproduction or validation, update reputation
or node statistics, score contributions, or calculate rewards. The hash proves
content consistency only: it does not prove correctness and is not written
on-chain. No signatures or blockchain writes exist.

### Submit a finding

```bash
curl -X POST http://localhost:8000/projects/<project_id>/submissions \
  -H "Content-Type: application/json" \
  -d '{
    "finding_id": "<finding_id>",
    "node_id": "<node_id>",
    "agent_name": "access_control_agent",
    "agent_version": "0.1.0"
  }'
```

### List project submissions

```bash
curl http://localhost:8000/projects/<project_id>/submissions
```

### Read a submission

```bash
curl http://localhost:8000/submissions/<submission_id>
```

### List submissions from a node

```bash
curl http://localhost:8000/nodes/<node_id>/submissions
```

## Week 5 Day 4 - Contribution Scoring Engine v0

ProofGuard now stores one deterministic, explainable contribution score for
each evaluated submission under
`apps/audit-api/data/protocol/contributions/<submission_id>/contribution.json`.
Scoring reads the separate finding, validation decision, and reproduction
result and assigns points for validity (30 maximum), normalized severity (25),
reproducibility (25), uniqueness (15), and evidence quality (10), followed by
explicit non-positive penalties. The raw positive maximum is 105 and the final
score is clamped to 0–100.

Only accepted, reproduced, non-duplicate, in-scope submissions in a compatible
protocol state are eligible for future reward calculation. Duplicate,
out-of-scope, unsafe, rejected, and unsupported findings are ineligible and
receive a final score of zero. Pending scores are diagnostic and capped at 25;
insufficient-evidence scores are ineligible and capped at 20.

The engine is rule-based and does not use AI or an LLM. It cannot change a
finding, validation decision, reproduction result, submission, node reputation,
or node statistics. It does not calculate or distribute a reward, and it does
not create an on-chain commitment.

### Calculate contribution score

```bash
curl -X POST \
  http://localhost:8000/submissions/<submission_id>/contribution/calculate
```

### Read contribution score

```bash
curl \
  http://localhost:8000/submissions/<submission_id>/contribution
```

### List project contribution scores

```bash
curl \
  http://localhost:8000/projects/<project_id>/contributions
```

### List reward-eligible scores

```bash
curl \
  "http://localhost:8000/projects/<project_id>/contributions?eligible_for_reward=true"
```

## Week 5 Day 5 - Reputation Engine v0

Every node has a global reputation between `0.0` and `1.0`; newly registered
nodes begin at `0.5`. Reputation changes only when a submission has finalized
validation and a stored contribution score. Accepted, reproduced, valuable
findings increase reputation, while duplicate, rejected, out-of-scope, and
insufficiently evidenced findings receive small deterministic penalties.
Unsafe submissions receive the larger `-0.08` reputation penalty. Unsupported
submissions are counted as finalized but do not change reputation, and
needs-review or otherwise unfinished inputs remain pending.

Each finalized submission creates at most one immutable `ReputationEvent`
under
`apps/audit-api/data/protocol/reputation/events/<submission_id>/event.json`.
The event references the node, submission, finding, validation, reproduction,
contribution score, and vulnerability category. Processing uses a SHA-256
source fingerprint and a prepared/applied recovery protocol, so repeated or
interrupted processing cannot apply reputation or statistics twice.

Node statistics are updated exactly once with the finalized outcome. The
category is recorded for future aggregation, but the Week 5 engine does not
calculate category-specific reputation scores. Week 6 Day 1 adds a separate
off-chain subnet registry without changing reputation behavior. This engine
does not calculate rewards, transfer tokens, slash stake, or write a reputation
checkpoint on-chain.

### Process reputation

```bash
curl -X POST \
  http://localhost:8000/submissions/<submission_id>/reputation/process
```

### Read current node reputation

```bash
curl \
  http://localhost:8000/nodes/<node_id>/reputation
```

### Read reputation history

```bash
curl \
  http://localhost:8000/nodes/<node_id>/reputation/history
```

### Filter history by category

```bash
curl \
  "http://localhost:8000/nodes/<node_id>/reputation/history?category=access_control"
```

## Week 5 Day 6 - Reward and Penalty Simulator v0

ProofGuard now simulates deterministic project rewards in `protocol_points`.
These points have no monetary value and are not tokens. Only accepted,
reproduced, unique, in-scope submissions with a positive reward-eligible
contribution score can receive points. Duplicate, out-of-scope, rejected,
unsafe, unsupported, pending, needs-review, already rewarded, and already
penalized submissions receive zero.

The reward weight is the contribution score multiplied by a moderate global
reputation multiplier and an explicit category multiplier. Category
multipliers are fixed at `1.0` in v0; category scarcity and subnet economics
remain future work. Eligible weights split a fixed project reward pool
proportionally. Amounts are rounded to six decimal places, with any rounding
remainder assigned deterministically to the largest weight (then lowest
submission ID).

Reward cycles progress through `draft`, `calculated`, and `finalized` states.
Calculation records transparent inputs, eligibility reasons, formula
components, and a SHA-256 source fingerprint. Finalization reloads and verifies
those inputs, creates at most one immutable `RewardEvent` per eligible
submission, and is idempotent. Finalized cycles cannot be recalculated or
mutated.

Unsafe finalized submissions receive no reward and may create one idempotent
`PenaltyEvent` with 25 internal `protocol_penalty_points`, zero simulated stake
loss, no on-chain execution, and required human review. Ordinary duplicates,
rejections, out-of-scope findings, unsupported inputs, and insufficient
evidence do not automatically create penalty events. Reputation events, reward
denial, penalty points, and any future stake slashing remain separate concerns.

Reward events are stored under
`apps/audit-api/data/protocol/rewards/events/<submission_id>/reward_event.json`.
Penalty events are stored under
`apps/audit-api/data/protocol/penalties/events/<submission_id>/penalty_event.json`.

```text
protocol_points != token
protocol_penalty_points != stake slashing
```

### Create reward cycle

```bash
curl -X POST \
  http://localhost:8000/projects/<project_id>/reward-cycles \
  -H "Content-Type: application/json" \
  -d '{
    "reward_pool": 1000,
    "description": "Protocol reward simulation."
  }'
```

### Calculate reward cycle

```bash
curl -X POST \
  http://localhost:8000/reward-cycles/<cycle_id>/calculate
```

### Finalize reward cycle

```bash
curl -X POST \
  http://localhost:8000/reward-cycles/<cycle_id>/finalize
```

### Read node rewards

```bash
curl \
  http://localhost:8000/nodes/<node_id>/rewards
```

### Process unsafe penalty

```bash
curl -X POST \
  http://localhost:8000/submissions/<submission_id>/penalty/process
```

## Week 5 - Protocol and Incentive Layer v0

Week 5 completes the centralized off-chain Protocol v0 simulation:

- Protocol Specification v0;
- Node Identity and Registry v0;
- Finding Submission Protocol v0 with deterministic finding hashes;
- Contribution Scoring Engine v0;
- immutable ReputationEvents and global node reputation;
- deterministic Reward Cycles and RewardEvents;
- reviewable unsafe PenaltyEvents; and
- a seven-case synthetic end-to-end protocol benchmark.

Run the benchmark from `apps/audit-api`:

```bash
python research/evals/eval_protocol.py
```

Generated outputs:

```text
research/results/protocol_case_results.json
research/results/protocol_leaderboard.json
research/results/week_5_report.md
research/results/week_5_summary.json
```

Every case is copied into an isolated temporary audit workspace with a separate
temporary protocol-data root. Source fixtures and production protocol data are
not modified. ReproductionResult and ValidationDecision inputs are static: the
benchmark does not execute PoCs, invoke the reproduction runner or validator
pipeline, call Docker or forge, launch external subprocesses, or access an
external API.

Rewards remain simulated `protocol_points`, not tokens or money. Internal
`protocol_penalty_points` are not stake slashing. No wallet, blockchain
operation, real transfer, staking, or financial slash exists. Category-specific
reputation, subnet routing, and subnet reward pools are planned for Week 6.

## Week 6 Day 1 - Subnet Schema and Registry v0

ProofGuard now has an off-chain registry of logical vulnerability-category
subnets. A subnet is a protocol-level specialization record for exactly one
value from the existing `FindingCategory` enum; it is not a blockchain subnet,
validator consensus group, staking pool, reward pool, or node network.

Each category may have at most one registry subnet. Its stable, path-safe ID is
derived from the normalized category:

```text
subnet_<category>
```

For example, `Access Control` and `access-control` both resolve to
`subnet_access_control`. Configuration includes the minimum future category
score, minimum finalized submissions, maximum active nodes, and exploration
ratio. These values are stored policy metadata only: Day 1 does not calculate
category scores, membership, rankings, assignments, or rewards.

Subnet status is `active`, `inactive`, `suspended`, or `archived`. Active,
inactive, and suspended subnets may move among those states or become archived.
Archiving is terminal through the normal API, and archived records are retained.

Bootstrap iterates the existing `FindingCategory` enum, creates only missing
subnets, and returns every subnet in deterministic category order. Repeated
bootstrap calls preserve custom configuration and never reactivate inactive,
suspended, or archived records.

The `SubnetMemberRecord` schema and internal persistence foundation also exist
for later Week 6 work. There is no public member-create or member-update route,
and no automatic membership decision is made. Category scoring, membership
automation, routing and exploration assignments, and subnet rewards come later
in Week 6.

Within `apps/audit-api`, protocol-level storage is:

```text
data/protocol/subnets/<subnet_id>/subnet.json
data/protocol/subnets/<subnet_id>/members/<node_id>.json
```

No blockchain operation, token, wallet, staking, financial slashing, or
validator consensus is part of Subnet Registry v0.

### Bootstrap default subnets

```bash
curl -X POST \
  http://localhost:8000/subnets/bootstrap
```

### Create one subnet

```bash
curl -X POST \
  http://localhost:8000/subnets \
  -H "Content-Type: application/json" \
  -d '{
    "category": "access_control",
    "minimum_category_score": 0.6,
    "minimum_finalized_submissions": 5,
    "maximum_active_nodes": 20,
    "exploration_ratio": 0.2
  }'
```

### List active subnets

```bash
curl \
  "http://localhost:8000/subnets?status=active"
```

### Read access-control subnet

```bash
curl \
  http://localhost:8000/subnets/by-category/access_control
```

### Suspend subnet

```bash
curl -X POST \
  http://localhost:8000/subnets/subnet_access_control/status \
  -H "Content-Type: application/json" \
  -d '{
    "status": "suspended",
    "reason": "Administrative review."
  }'
```

## Week 6 Day 2 - Per-Category Performance Aggregator v0

Node history is now reconstructed independently for each vulnerability
category. Every persisted record belongs to exactly one `node_id + category`
pair and is built from applied `ReputationEvent` records. Prepared events,
pending work, and submissions without a finalized event are not included.
Global node reputation remains unchanged and continues to be the canonical
global score.

The aggregator verifies each event against its `SubmissionRecord`,
`ContributionScoreRecord`, `ValidationDecision`, and `ReproductionResult`, plus
any finalized `RewardEvent`. Conflicting identifiers, categories, statuses, or
source fingerprints fail the rebuild instead of being silently counted.

Each record stores:

- finalized, accepted unique, rejected, duplicate, out-of-scope,
  insufficient-evidence, unsafe, and unsupported counts;
- finalized reproduction attempts and successful reproductions;
- reward-eligible and actually rewarded submission counts;
- total, average, minimum, and maximum contribution scores;
- accepted-only contribution totals, averages, minimums, and maximums;
- deterministic source event/submission IDs and a canonical SHA-256 source
  fingerprint; and
- first and last category activity timestamps.

Rebuilds always start from finalized source events rather than incrementing a
previous aggregate. A submission is counted at most once. When the canonical
source fingerprint is unchanged, the stored record and timestamps are
preserved without rewriting the file. An explicit rebuild for a valid
node/category with no applied events persists a zero record; node-wide and
global rebuilds discover only categories that have applied historical events.

Records are stored under:

```text
data/protocol/category-performance/<node_id>/<category>.json
```

Category isolation is strict: an access-control event cannot affect a
reentrancy record. Historical events remain aggregatable even if a node later
becomes inactive or banned, or removes that category from its current support
declaration. Current support controls future participation, not historical
facts.

Day 2 stores raw historical performance only. It does not calculate a category
score, change subnet membership, create rankings, route projects, or calculate
subnet rewards.

### Rebuild one category

```bash
curl -X POST \
  http://localhost:8000/nodes/<node_id>/category-performance/access_control/rebuild
```

### Rebuild all categories for a node

```bash
curl -X POST \
  http://localhost:8000/nodes/<node_id>/category-performance/rebuild
```

### Rebuild all nodes

```bash
curl -X POST \
  http://localhost:8000/category-performance/rebuild
```

### Read one record

```bash
curl \
  http://localhost:8000/nodes/<node_id>/category-performance/access_control
```

### List category performance for a subnet

```bash
curl \
  http://localhost:8000/subnets/subnet_access_control/category-performance
```

## Week 6 Day 3 - Category Scoring Engine v0

ProofGuard now transforms each raw `CategoryPerformanceRecord` into a separate,
versioned category score between `0.0` and `1.0`. A score belongs to exactly one
`node_id + vulnerability category`; access-control history cannot influence a
reentrancy score. Category score is separate from the node's global reputation
and is rebuilt only from the Day 2 aggregate, not from raw findings or report
prose.

The positive score has six explicit components:

- precision: accepted unique submissions divided by finalized submissions;
- reproduction rate: successful finalized reproductions divided by finalized
  reproduction attempts;
- uniqueness: accepted unique submissions divided by accepted unique plus
  duplicate submissions;
- contribution quality: accepted-only average contribution score divided by
  `100`;
- consistency: `1 - accepted score range / 100`, using accepted-only minimum
  and maximum scores; and
- experience confidence: `min(finalized submissions / 10, 1)`.

New Day 2 aggregates include accepted-only minimum and maximum contribution
scores. Older compatible aggregates that predate those fields use a
conservative consistency value of `0.50` when they contain more than one
accepted submission. Overall minimum and maximum values are not substituted
because invalid outcomes may have contributed zero scores.

The fixed v0 positive formula is:

```text
positive score =
    0.30 * precision
  + 0.20 * reproduction rate
  + 0.15 * uniqueness
  + 0.20 * contribution quality
  + 0.10 * consistency
  + 0.05 * experience confidence
```

Rate-based penalties cover duplicates (maximum `0.10`), rejected findings
(`0.12`), out-of-scope findings (`0.15`), insufficient evidence (`0.10`), and
unsafe submissions (`0.30`). Unsupported submissions have no separate v0
penalty because they already reduce precision. The unsafe penalty has a
`0.10` floor whenever any unsafe submission exists, making it the strongest
category penalty. Total penalties are capped at `0.60`.

```text
raw after penalties =
    clamp(positive score - total penalties, 0, 1)
```

Low sample sizes are pulled toward the neutral baseline `0.50`:

```text
final category score =
    0.50
  + experience confidence
    * (raw after penalties - 0.50)
```

Consequently, one excellent finalized submission has only `0.10` experience
confidence and cannot immediately produce an expert score band. Bands are
`insufficient_data` below `0.30` confidence, then `weak` below `0.40`,
`developing` below `0.60`, `strong` below `0.80`, and `expert` from `0.80`.
The expert band is descriptive only and does not grant expert subnet
membership.

Every record stores raw components, fixed weights, weighted contributions,
individual penalties, raw and confidence-adjusted scores, source fingerprints,
and deterministic human-readable explanations. Its canonical SHA-256 source
fingerprint covers the exact Day 2 aggregate identity, performance fingerprint,
counts, contribution statistics, scoring version, weights, baseline, and
formula versions. Unchanged rebuilds return `unchanged` without rewriting the
file.

Records are stored off-chain under:

```text
data/protocol/category-scores/<node_id>/<category>.json
```

Day 3 does not create or update subnet members, promote or demote nodes, persist
rankings, route projects, or calculate subnet rewards. Day 4 may consume these
canonical score records when implementing membership rules.

### Rebuild one category score

```bash
curl -X POST \
  "http://localhost:8000/nodes/<node_id>/category-scores/access_control/rebuild"
```

Pass `?rebuild_performance=true` only when the Day 2 aggregate should also be
explicitly rebuilt.

### Rebuild all scores for one node

```bash
curl -X POST \
  "http://localhost:8000/nodes/<node_id>/category-scores/rebuild"
```

### Rebuild all category scores

```bash
curl -X POST \
  "http://localhost:8000/category-scores/rebuild"
```

### List strong access-control nodes

```bash
curl \
  "http://localhost:8000/category-scores?category=access_control&minimum_score=0.6"
```

### List scores for subnet

```bash
curl \
  "http://localhost:8000/subnets/subnet_access_control/category-scores"
```

## Week 6 Day 4 - Subnet Membership Manager v0

ProofGuard now calculates off-chain membership independently for each
vulnerability-category subnet. Nodes cannot select their own tier. Version
`subnet_membership_v0` uses the persistent `NodeRecord`, `SubnetRecord`,
matching `CategoryPerformanceRecord`, matching `CategoryScoreRecord`, recent
applied reputation events, and the previous member record. Global reputation
is not a membership threshold.

Membership statuses are:

- `candidate`: fewer than 3 finalized submissions, or a category score below
  the probation threshold;
- `probation`: at least 3 finalized submissions and an acceptable initial
  score, but one or more active requirements are missing;
- `active`: the subnet score and finalized-history thresholds are met, at
  least 2 findings are accepted and unique, and no recent unsafe event exists;
- `expert`: score at least `max(0.80, subnet minimum)`, at least 10 finalized
  submissions, at least 6 accepted unique findings, and no historical or
  recent unsafe outcome;
- `suspended`: participation is blocked by node/subnet state, a recent unsafe
  event, or an administrative lock; and
- `removed`: the category is no longer supported, the subnet is archived, or
  an administrator removes the member. Normal refresh does not re-enroll a
  removed member.

The probation score threshold is `min(0.40, subnet minimum)`. Active promotion
uses `SubnetRecord.minimum_category_score` and
`minimum_finalized_submissions`. Existing active members use demotion
thresholds 0.10 below the active score threshold, one submission below the
history threshold (minimum 1), and at least one accepted unique finding.
Existing experts are preserved at a score 0.10 below the expert promotion
threshold (never below the active threshold), at least 8 finalized
submissions, and at least 5 accepted unique findings. Safety, bans, inactive
state, archival, unsupported categories, and administrative locks always take
priority over hysteresis.

An applied `unsafe_submission` reputation event in the exact node/category
during the inclusive 30-day UTC lookback suspends participation. Older unsafe
history does not automatically suspend a node, but a nonzero historical unsafe
count prevents expert membership.

`maximum_active_nodes` caps `active + expert`. Expert recommendations are
considered first, followed by category score, experience confidence, accepted
unique count, finalized count, and node ID. Overflow recommendations become
probation with `active_capacity_reached`. This order is a transient capacity
decision and is never stored as rank. Evaluating one node performs the same
complete subnet refresh.

Every logical decision has a canonical SHA-256 source fingerprint. It covers
the policy version, policy thresholds, relevant node/subnet state, exact
performance and score IDs/fingerprints and snapshots, recent unsafe event
IDs/fingerprints, policy-relevant prior state, and capacity context. Evaluation
time, explanations, display metadata, rank, assignment counters, and routing
state are excluded. An unchanged refresh neither rewrites the member nor
creates another decision event.

A supported node with neither a performance nor score record receives a
candidate membership with a clearly marked virtual zero snapshot. This does
not persist a fabricated Day 2 or Day 3 source. If exactly one dependency is
missing or the sources disagree, refresh reports a source error. Passing
`rebuild_dependencies=true` explicitly rebuilds Day 2 and Day 3 first.

Current records and immutable decisions are stored at:

```text
data/protocol/subnets/<subnet_id>/members/<node_id>.json

data/protocol/subnets/<subnet_id>/
  membership-events/<node_id>/<decision_id>.json
```

Day 4 does not persist rank, select exploration assignments, route projects,
or calculate subnet rewards.

### Refresh subnet membership

```bash
curl -X POST \
  "http://localhost:8000/subnets/subnet_access_control/members/refresh"
```

### Refresh and rebuild dependencies

```bash
curl -X POST \
  "http://localhost:8000/subnets/subnet_access_control/members/refresh?rebuild_dependencies=true"
```

### Evaluate one node

```bash
curl -X POST \
  "http://localhost:8000/subnets/subnet_access_control/members/<node_id>/evaluate"
```

### Read membership history

```bash
curl \
  "http://localhost:8000/subnets/subnet_access_control/members/<node_id>/history"
```

### Administratively suspend member

```bash
curl -X POST \
  "http://localhost:8000/subnets/subnet_access_control/members/<node_id>/suspend" \
  -H "Content-Type: application/json" \
  -d '{
    "reason": "Manual security review."
  }'
```

## Week 6 Day 5 - Subnet Router v0

ProofGuard now calculates deterministic off-chain assignment plans for the
vulnerability categories declared in a project's parsed `scope.yaml`. Each
category is routed independently through its matching subnet; a score or
membership from one category is never reused for another.

Router v0 distinguishes two assignment types:

- ranked assignments select active or expert members and always use
  `production` mode;
- exploration assignments select candidate or probation members and always use
  non-authoritative `shadow` mode.

The subnet's `exploration_ratio` reserves shadow slots. When exploration is
enabled and at least two nodes are requested:

```text
exploration target =
  min(nodes_per_category - 1, ceil(nodes_per_category * exploration_ratio))

ranked target =
  nodes_per_category - exploration target
```

One requested node always produces one ranked target and zero exploration
targets. A zero exploration ratio reserves no shadow slots.

Ranked candidates require an active node, active subnet, active/expert
membership, exact node/subnet/category identity, a current category score, and
a score-source fingerprint matching the Day 4 membership snapshot. They are
ordered by expert before active, category score, experience confidence,
accepted unique findings, finalized submissions, and node ID.

Candidate and probation exploration members remain shadow-only. Exploration
fairness uses finalized routing usage: fewer prior exploration assignments
first, never assigned before previously assigned, oldest assignment next,
probation before candidate on a complete fairness tie, then score, confidence,
and node ID. This is deterministic and does not claim to solve starvation,
Sybil behavior, or operator diversity in a large network.

Suspended, removed, administratively locked, globally inactive, globally
suspended, banned, wrong-category, and stale-source nodes are never used as an
unsafe fallback. Missing subnets, inactive subnets, and insufficient candidates
produce explicit partial results when `allow_partial=true`. With
`allow_partial=false`, no incomplete route is stored.

Routes begin as `calculated`. Finalization reloads and fingerprints the current
scope, subnet, member, score, and finalized usage inputs. A stale plan is
rejected. Successful finalization creates one exclusive `RoutingUsageEvent` per
assignment and is idempotent. It does not execute nodes, create submissions,
collect results, or include shadow output in a report.

Routing usage events are the authoritative fairness history. Day 5 deliberately
does not synchronize `SubnetMemberRecord.exploration_assignments` or
`last_assigned_at`, avoiding circular writes into Day 4 membership ownership.

Records are stored at:

```text
data/protocol/routing/projects/<project_id>/<routing_id>/routing.json

data/protocol/routing/usage/<node_id>/<assignment_id>.json
```

Private rotating qualification benchmarks, candidate queues, attempt limits,
benchmark leakage controls, assignment execution, leases, retries,
operator/model/geographic diversity, and workload limits remain future work.
No benchmark answer or fixture is stored in a routing record.

### Calculate project routing

```bash
curl -X POST \
  "http://localhost:8000/projects/<project_id>/routing/calculate" \
  -H "Content-Type: application/json" \
  -d '{
    "categories": null,
    "nodes_per_category": 4,
    "include_exploration": true,
    "allow_partial": true
  }'
```

### Finalize route

```bash
curl -X POST \
  "http://localhost:8000/projects/<project_id>/routing/<routing_id>/finalize"
```

### Read latest route

```bash
curl \
  "http://localhost:8000/projects/<project_id>/routing/latest"
```

### Read node usage

```bash
curl \
  "http://localhost:8000/nodes/<node_id>/routing-usage"
```

## Week 6 Day 6 - Subnet Reward Allocation v0

ProofGuard now allocates a caller-supplied simulated `protocol_points` pool for
one finalized routing record. Protocol points are off-chain accounting units:
they have no monetary value, are not tokens, and do not trigger a payment,
wallet operation, or blockchain transaction.

The pool is split independently between every routed category. Omitted
category weights produce an equal split; explicit positive weights are
normalized proportionally and must cover exactly the routed category set.
Six-decimal `Decimal` arithmetic and deterministic largest-remainder rounding
make category pools sum exactly to the project pool. A category with no
eligible contribution keeps its full pool as `undistributed_points`; it is
never moved into another category.

A submission must link to the finalized routing ID and one exact assignment.
It must be accepted, in scope, unique, successfully reproduced, backed by a
positive reward-eligible `ContributionScoreRecord`, and produced by a node
that is still globally active. Active/expert production assignments are
eligible. Probation shadow assignments are eligible with a reduced multiplier.
Candidate shadow assignments are ineligible in v0. Selection by the router
alone never guarantees a reward.

Multipliers use the immutable assignment snapshot, not current membership or
current category score:

```text
membership:
  probation = 0.90
  active    = 1.00
  expert    = 1.10
  candidate/suspended/removed = ineligible

category score:
  [0.00, 0.40) = 0.80
  [0.40, 0.60) = 0.95
  [0.60, 0.80) = 1.05
  [0.80, 1.00] = 1.15
```

```text
raw_weight =
    contribution_score
  * category_multiplier
  * membership_multiplier

reward =
    category_pool
  * raw_weight
  / total_category_weight
```

Cycles move from `draft` to `calculated` to terminal `finalized`. Calculation
stores allocations and deterministic exclusions but creates no reward events.
Finalization reloads submissions, contribution scores, validation decisions,
reproduction results, node status, routing, and prior rewards; a changed source
fingerprint blocks stale finalization. Each eligible allocation then creates
one exclusive, deterministic `SubnetRewardEvent`. Repeated calculation and
finalization are idempotent, and a submission rewarded by a finalized Week 5
reward event or another Day 6 cycle cannot receive the same contribution
reward again.

Legacy Week 5 submissions remain readable with null routing linkage. They are
not eligible for Day 6 subnet rewards; existing records are not rewritten.

Records are stored at:

```text
data/protocol/subnet-rewards/
  projects/<project_id>/<reward_cycle_id>/cycle.json

data/protocol/subnet-rewards/
  events/submissions/<submission_id>/<reward_event_id>.json
```

Participation rewards, client pricing, assurance packages, validator or
treasury payments, dispute handling, wallets, tokens, and on-chain settlement
remain deferred. Rewarding multiple unique submissions from one node depends
on the existing validation and semantic-deduplication pipeline preventing one
issue from being split into multiple accepted findings.

### Create reward cycle

```bash
curl -X POST \
  "http://localhost:8000/projects/<project_id>/subnet-reward-cycles" \
  -H "Content-Type: application/json" \
  -d '{
    "routing_id": "<routing_id>",
    "total_pool_points": "10000.000000",
    "category_weights": {
      "access_control": "3",
      "reentrancy": "2"
    }
  }'
```

### Calculate cycle

```bash
curl -X POST \
  "http://localhost:8000/projects/<project_id>/subnet-reward-cycles/<cycle_id>/calculate"
```

### Finalize cycle

```bash
curl -X POST \
  "http://localhost:8000/projects/<project_id>/subnet-reward-cycles/<cycle_id>/finalize"
```

### Read node rewards

```bash
curl \
  "http://localhost:8000/nodes/<node_id>/subnet-rewards"
```

## Week 6 Day 7 - End-to-End Subnet Benchmark

Week 6 now includes a deterministic end-to-end benchmark for the complete
off-chain subnet skeleton. It creates synthetic nodes, project scopes,
finalized protocol histories, routing-linked submissions, and simulated reward
inputs under temporary protocol-data and project-workspace roots. It then
exercises the real production services for:

- Subnet Registry bootstrap;
- Category Performance aggregation;
- Category Scoring;
- Subnet Membership;
- production and shadow Subnet Routing; and
- category-isolated Subnet Reward Allocation.

The seven required scenarios are:

```text
best_access_control_selected
weak_reentrancy_not_selected
new_node_exploration
unsafe_node_suspended
category_score_isolation
multi_category_routing
subnet_reward_distribution
```

Run from `apps/audit-api`:

```bash
python -m research.benchmarks.week6_subnet_benchmark
```

Following the existing research convention, outputs are written directly under
`research/results/`:

```text
subnet_case_results.json
subnet_leaderboard.json
week_6_summary.json
week_6_report.md
```

Every normal run executes the complete benchmark twice in independent
temporary roots and compares normalized logical outputs. The leaderboard is
benchmark reporting only. It does not persist a subnet rank.

No production project or protocol data is modified. The benchmark executes no
agent, PoC, Docker, forge, project-analysis subprocess, AI/LLM, remote service,
or external API. Protocol points are simulated only: no real payment, pricing,
token, wallet, staking, slashing, smart-contract, or blockchain action exists.

## Week 7 Day 1 - Reward Architecture Refactor Foundation

ProofGuard now represents client-funded task budgets separately from future
network/protocol incentives. The task identity is one finalized
`ProjectRoutingRecord`, allowing two audit executions of the same project to
have distinct budgets. One routing can have one idempotent `TaskRewardBudget`
stored under `data/protocol/task-rewards/budgets/routing/<routing_id>/budget.json`.

The budget uses exact six-decimal `protocol_points` and centralized configurable
shares for miner, validator, and protocol pools. The default development split
is 70/20/10. Week 6 largest-remainder allocation guarantees exact conservation.
Budget creation and finalization do not distribute rewards or create
RewardEvents.

Week 5 `reward_v0` and Week 6 `subnet_reward_v0` remain readable legacy task
allocation policies. Their historical reputation, category-score, and
membership multipliers are not part of new `task_reward_v1` semantics.
ContributionScore, Reputation, CategoryScore, Membership, and Routing remain
performance/history/opportunity systems.

See the [Week 7 reward system specification](protocol/specs/reward-model.md).

## Week 7 Day 2 - Root-Cause Clustering and Duplicate Semantics

ProofGuard now materializes one deterministic `FindingCluster` for each
accepted unique vulnerability inside one finalized routing task. Independent
reports remain individually attributable members, while
`distinct_operator_count` resolves and deduplicates `NodeRecord.operator_id`.
Cluster identity is project-, routing-, and category-scoped and reuses the Week
4 dedup canonical relation and normalized root-cause identity.

The validator now separates a valid independent root-cause match from a
same-node submission retry. A reproduced, in-scope independent match is
accepted with explicit `independent_root_cause` metadata and is not
automatically reputation-negative. The Week 5 same-node/project/finding-hash
guard remains unchanged: a repeated submission is rejected before persistence
and never becomes a cluster member.

Clusters are rebuilt from immutable findings, routed submissions, validation
decisions, reproduction results, and registry operator identity under
`data/protocol/finding-clusters/tasks/<routing_id>/`. Rebuilds are idempotent,
source-fingerprinted, and do not calculate a task reward, transfer tokens, run
an LLM classifier, or write to a blockchain.

## Week 7 Day 3 - Deterministic Report Quality Assessment

Every eligible validated `FindingCluster` member can now receive a separate,
versioned `ReportQualityAssessment`. Its five normalized Decimal components are
correctness (35%), reproduction-evidence quality (25%), root-cause quality
(20%), impact quality (10%), and remediation quality (10%). Persisted v1 records
retain the repository's established aliases `poc_quality` and `fix_quality` for
the latter two mapped concepts. ProofGuard calculates Q at six-decimal precision;
clients cannot submit the total score, node/operator identity, rank, or reward.

Deterministic rules consume existing immutable protocol records and persist
component sources, reason codes, and evidence references. Where the current
records cannot honestly establish a value—most notably fix quality—the record
remains a draft until structured validator input is supplied. Finalized records
are immutable and changed sources require explicit versioned supersession.

Assessments are stored under
`data/protocol/report-quality/tasks/<routing_id>/submissions/<submission_id>/`.
They neither replace Week 5 ContributionScore nor modify reputation,
CategoryScore, membership, routing, clusters, budgets, or rewards. No LLM,
Top-K, Chief Finder, Q² allocation, payment, token, or blockchain operation is
part of Day 3. See the [Week 7 reward system specification](protocol/specs/reward-model.md).

## Week 7 Day 4 - FindingCluster Economic Valuation

Week 7 Day 4 allocates the finalized client-funded miner pool to unique,
finalized FindingClusters. `finding_cluster_value_v1` uses validator-approved
severity weights (`16/8/3/1`, Informational `0`) and distinct-operator
uniqueness `max(0.50, 1 / (1 + 0.20 * ln(N)))`. FindingScore is severity weight
times the persisted six-decimal uniqueness. Existing Week 6 category pool and
largest-remainder accounting are reused; category-isolated allocation is the
default and global allocation is supported. Cluster operator attribution is
rechecked against authoritative NodeRegistry records before allocation.

The result is an immutable, audit-readable cluster allocation snapshot. It
does not read report Q, select operators, Top-K or Chief Finder, create a
RewardEvent, distribute a payout, or change reputation, CategoryScore,
membership or routing. Integrated reward-cycle finalization later re-runs this
snapshot and rejects stale sources. See the [Week 7 reward system specification](protocol/specs/reward-model.md).

## Week 7 Day 5 - Operator-Level Task Reward Preview

Week 7 Day 5 consumes fixed Day 4 cluster rewards and finalized Day 3 report
quality. It selects the best report per server-derived operator, ranks all
operator representatives, rewards at most the configured Top-K (default 5),
selects an early qualified Chief only from Top-K, and distributes the Quality
Pool with exact Decimal Q² weights and largest remainder.

Chief qualification is auditable and requires three separate authoritative
facts: valid canonical/independent cluster membership for root-cause
consistency, an accepted-final-severity reference with a structured consistency
reason, and a positive impact assessment with a validator-derived impact-valid
reason. One fact cannot stand in for another.

Candidate/shadow, probation, active and expert assignments can compete when
properly authorized; membership, reputation, CategoryScore, and
ContributionScore do not multiply current-task payout. The result is an
auditable calculated preview with exact per-cluster/task conservation. No
RewardEvent, final payout, token, payment, reputation update, or blockchain
operation is created. See the [Week 7 reward system specification](protocol/specs/reward-model.md).

## Week 7 Day 6 - Integrated Client-Task Reward Cycle

Day 6 connects the Week 7 budget, cluster valuation and operator allocation
snapshots to a `draft -> calculated -> finalized` client-task reward cycle.
Finalization re-runs the authoritative Day 4/Day 5 orchestration, rejects stale
sources, and creates deterministic immutable RewardEvents only for positive
operator allocations. Event creation is atomic and retry-safe; one finalized
TaskRewardBudget cannot be paid by a second Week 7 cycle.

Each cycle persists the complete immutable policy bundle and fingerprint
version used by its calculation. A read-only `.../task-reward-cycles/{id}/verify`
endpoint audits source/calculation fingerprints, nested conservation, event-set
completeness, duplicate economic identities, and safe-retry state without
repairing or mutating the ledger.

Economic history is readable by cycle, operator, representative node,
submission and FindingCluster. It remains separate from ContributionScore,
reputation, CategoryScore, membership and routing. Validator/protocol pools stay
reserved, and no wallet, token or blockchain operation occurs.

## Week 7 Day 7 - End-to-End Reward Benchmark

Day 7 runs the real Week 7 services from routing through immutable RewardEvents
inside temporary isolated roots. The CI-safe production fixture contains 64
nodes, 40 operators, eight root-cause clusters, two categories and 120 reports,
plus 100-report/50-operator, N=1000 and exact mathematical boundary cases. Its
33 named cases verify the known Q vector, exact Decimal conservation,
operator-based uniqueness, one operator/one position, Top-K and Chief bounds,
Q² distribution, global/category allocation, stale-source blocking, read-only
verification, partial/complete crash recovery, event-corruption detection,
double-reward protection, reload and deterministic replay.

Run it with:

```bash
cd apps/audit-api
python -m research.benchmarks.week7_reward_benchmark
```

Generated case, allocation, event, invariant, determinism and Markdown outputs
are stored under `research/results/week7/`, including canonical accounting,
cluster/operator, performance, deterministic-state-hash and 21-section final
report artifacts. The benchmark introduces no new
reward formula, validator economics, network emission, token transfer, wallet
payment or blockchain write. See the [Week 7 reward system specification](protocol/specs/reward-model.md).

## Week 8 Day 1 - Validator Attestation Protocol

Week 8 introduces authorized independent validator evidence for finalized
`FindingCluster` records. Active validator and hybrid nodes may use an internal
`ValidatorAssignment`; public nodes cannot self-assign. `NodeRecord` remains
authoritative for node/operator identity, and an operator is blocked from
validating any cluster containing that operator's report. Deterministic
assignment identity prevents several nodes of one operator from creating
several authoritative seats in one cluster.

Validator reproduction is an immutable attribution/provenance wrapper around a
final Week 3 `ReproductionResult`. It distinguishes `submitted_poc` from
`independent_reproduction`, preserves failed/timeout/unsafe/unsupported states,
and never bypasses Safety Preflight or the existing sandbox. The structured
`ValidationAttestation` separately records validity, root cause, server-derived
reproduction, severity, impact, reason codes, and authoritative evidence. A
failed reproduction does not automatically reject the finding.

Attestations are canonical, SHA-256 fingerprinted, atomically persisted,
immutable, and idempotent per assignment/version. They do not create consensus,
change cluster severity, update performance, recalculate Week 7 rewards, or pay
validators. See the
[Validator Attestation Protocol v1](protocol/specs/validator-attestation-v1.md).

Week 8 Day 2 adds protocol-owned validator committees around that evidence
flow. STANDARD plans require five independent authoritative operators and
HIGH_ASSURANCE plans require seven; one optional shadow operator is additional.
Selection filters NodeRegistry type/status/category, rebuilds reporter conflicts
from current operator identity, groups multiple nodes by operator, enforces
capacity, and ranks deterministic workload/history fairness. Agent
`CategoryScore` and global reputation are not validator-skill inputs. Planned
membership is fingerprinted and revalidated before immutable, idempotent
assignment/usage finalization. The public creation endpoint is STANDARD-only
and accepts no validator identities. Committee creation performs no PoC,
attestation, consensus, or validator payment. See the
[Validator Committee Selection v1](protocol/specs/validator-committee-v1.md).

## Week 8 Day 3 - Independent Validator Reproduction

Every assignment in a finalized validator committee now has one logical,
independently attributable reproduction job. STANDARD committees require five
authoritative terminal records and HIGH_ASSURANCE committees require seven;
an optional shadow record is persisted separately and never blocks authoritative
readiness. The server derives the canonical submitted PoC, audited source
snapshot, validator/operator/role attribution, environment fingerprint, and
terminal status.

Execution reuses the Week 3 SafetyPreflight and Docker/Foundry sandbox. It runs
against a temporary copy with networking disabled, a non-root user, dropped
capabilities, resource/time/output limits, and bounded global concurrency.
Committee readiness means only that all authoritative validators reached a
terminal reproduction state. It does not mean accepted, rejected, reproduced by
consensus, or rewarded. See
[Independent Validator Reproduction v1](protocol/specs/validator-reproduction-v1.md).

## Week 8 Day 4 - Validator Consensus and Bounded Escalation

Finalized authoritative attestations and validator-attributed reproduction
records now feed a deterministic five-component consensus engine. Quorum is
`N-1`; supermajority is `ceil(2N/3)` using integer arithmetic over the
authoritative target. STANDARD therefore requires four-of-five and
HIGH_ASSURANCE five-of-seven. A three-versus-two split is explicitly disputed,
shadow evidence never counts, and reputation or category scores never weight a
validator's conclusion.

A reproducible finding is confirmed only when accepted validity, reproduced
execution, confirmed root cause, exact normalized severity, and validated
impact each reach supermajority. Non-accepted validity outcomes can terminate
independently. Finalized disputed/no-quorum Round 1 snapshots open an idempotent
dispute. One explicit escalation may reuse the Day 2 selector to add four new
independent operators; cumulative consensus retains every legitimate Round 1
and Round 2 result. No further rounds, minority penalties, validator scoring,
reward changes, or new PoC execution are performed. See
[Validator Consensus v1](protocol/specs/validator-consensus-v1.md).

## Week 8 Day 5 - Validator Performance and Membership

Only a stable finalized validator-consensus outcome may now trigger
retrospective performance evaluation. Each authoritative or shadow validator's
attestation and independent reproduction are compared with the final cumulative
truth to create a per-task `ValidationQualityAssessment`, followed by one
immutable event and a deterministic category aggregate. A disputed or
no-quorum finding creates no correctness history. In particular, a validator
who was locally in the minority can receive full accuracy credit when a bounded
escalation later confirms that conclusion.

Historical `ValidatorCategoryScore` uses strict Decimal component rates,
neutral 0.50 defaults for missing history, and experience shrinkage toward
0.50 until ten resolved tasks. `ValidatorMembership` is a separate
candidate/probation/active/expert lifecycle with hysteresis. Neither object
reuses or mutates agent `CategoryScore`, agent subnet membership,
`ContributionScore`, or global reputation; hybrid nodes may hold different
agent and validator scores. Shadow work builds skill without influencing past
consensus. No validator reward or `validator_pool` allocation occurs. See
[Validator Performance v1](protocol/specs/validator-performance-v1.md).

## Week 8 Day 6 - Validator Reward Allocation

The client-funded validator pool is now an independent, single-consumption
reward stream. Every legitimate authoritative assignment reserves one equal
work unit. Completed work earns 30% of that unit; the remaining 70% is scaled
by the finalized current-task ValidationQualityScore squared. Shadow work,
severity, majority agreement, reputation, membership, agent skill, and
historical validator skill do not multiply payout. Incomplete and unearned
amounts remain undistributed, while miner and protocol pools remain isolated.

See [Validator Reward Allocation v1](protocol/specs/validator-reward-v1.md).

## Week 8 Day 7 - Adversarial Validator Network Benchmark

Day 7 runs the real Week 8 services through conflict-aware committee
selection, safe independently attributed reproduction, multidimensional
supermajority consensus, bounded escalation, final-truth quality assessment,
validator history and pool-isolated rewards. The fixed-seed benchmark provides
CI, medium and full corpus modes, uses isolated temporary persistence, verifies
crash/retry/corruption behavior and compares canonical semantic state across a
clean replay.

```bash
cd apps/audit-api
python -m research.benchmarks.week8_validator_benchmark --mode ci
```

The generated Week 8 report demonstrates the central anti-herding case: a
Round 1 3-ACCEPT/2-REJECT split remains disputed; four new REJECT validators
produce final cumulative REJECTED truth, so the original minority receives
correctness and quality credit. Validator payout has no severity, majority,
membership, reputation or historical-score multiplier. See the
[Week 8 validator benchmark specification](protocol/specs/week8-validator-benchmark.md).

## Integration Week, Day 1 - AuditRun Foundation

Integration Day 1 adds the durable `AuditRun` orchestration identity, a
centrally validated stage lifecycle, ordered structured `AuditEvent` timeline,
logical artifact references and the `AuditOrchestrator` delegation skeleton.
Public APIs create/read/list runs, start the skeleton at `PREPARING`, and page
events after a sequence cursor. No protocol business stage executes
automatically yet. See the [Integration Day 1 architecture report](apps/audit-api/research/results/integration_week1.md).

## Integration Week, Day 2 - Local Agent Execution

Prepared local-simulator audits now run the real Week 6 routing service through
`ROUTING`, execute selected AccessControl and Reentrancy assignments through a
generic `AgentExecutor`, and ingest candidate output into the production
`Finding` and `SubmissionRecord` services. One durable, deterministic
`AgentExecutionRecord` per assignment exposes node/operator/category, stable
runtime version, duration, Findings, Submissions and safe failures. Structured
AuditEvents preserve node-selection explanations and the complete execution /
ingestion timeline.

Simulator node/candidate setup is explicit and idempotent; starting an audit
does not create fake capacity or modify calibration. Orchestration stops after
`EXECUTING_AGENTS=COMPLETED` with reproduction still pending. No Docker,
Foundry, validation, calibration, clustering, reward or report stage runs on
Day 2. The architecture, persistence, APIs, retry semantics and Day 3 linkage
are documented in the [Integration Week report](apps/audit-api/research/results/integration_week1.md#integration-day-2).
