# ProofGuard E2E Demo Runbook

This runbook is designed for a presentation/demo, not for adding new protocol features.

## Goal

Show one complete audit from:

1. server start;
2. node registration;
3. target ingestion;
4. agent routing;
5. findings/submissions;
6. root-cause clustering;
7. validator committee selection;
8. independent reproduction;
9. validator attestations;
10. consensus/dispute/escalation;
11. miner rewards;
12. validator rewards;
13. agent + validator skill evolution;
14. a second routing decision that uses the evolved history.

The audience should leave with one idea:

> ProofGuard is already an end-to-end audit protocol, not just a collection of independent AI agents.

---

# A. Before the presentation

Do NOT run the first ever E2E attempt live in front of your managers.

First make one deterministic "gold run", save its artifacts, then repeat the same flow live.

Keep:
- JSON responses;
- the generated HTML report;
- terminal logs;
- final benchmark summary;
- screenshots.

If AI model calls are stochastic, avoid making 30 live model calls a hard dependency for the presentation. Use the same backend flow with a deterministic/mock model provider if the repository already has one, or pre-run the expensive analysis and demonstrate the deterministic protocol stages from persisted submissions.

---

# B. Start from clean state

Use your repository's real commands. Typical FastAPI examples are:

```bash
cd /path/to/proofguard

# activate env if used
source .venv/bin/activate

# make sure Docker is alive because reproduction needs the sandbox
docker info

# optional: run tests first
pytest -q

# start API
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

If your app entrypoint differs, use the real one.

For a clean demo, use a fresh test/demo storage directory or database. Do NOT delete production/dev data accidentally.

Recommended:
- a dedicated `demo.db`; or
- dedicated `PROOFGUARD_DATA_DIR`; or
- your existing test-mode storage option.

---

# C. First command to run

From this bundle:

```bash
python e2e_demo.py \
  --config demo_scenario.template.json \
  --list-routes
```

The runner downloads:

```text
http://127.0.0.1:8000/openapi.json
```

and prints every real route.

It also saves:

```text
demo_artifacts/openapi.json
```

This is the file needed to map the template scenario to your exact API.

If `/health` does not exist, temporarily disable the health step in the JSON or replace it with the real endpoint.

---

# D. Node population for the gold demo

Do not manually create 50 nodes.

For the presentation, a good network size is:

## Agents

Create 12 agent-capable nodes.

Suggested distribution:

- 4 access-control specialists;
- 4 reentrancy specialists;
- 2 mixed/generalists;
- 2 intentionally weaker/new nodes.

Use several operators, e.g.:

```text
agent-op-01
agent-op-02
...
agent-op-12
```

Do not reuse one operator for all nodes because you want operator diversity to be visible.

## Validators

Create 12-16 validation-capable operators.

Suggested:

- 10 validator-only nodes;
- 4 hybrid nodes;
- 1-2 new/shadow-capable validators.

Give them declared categories matching the demo target.

Important screenshot:
the node registry should visibly show:
- node ID;
- operator ID;
- node type;
- supported categories;
- status;
- initial historical scores/membership if exposed.

---

# E. Demo codebase

The bundle contains:

```text
proofguard_demo_target/
```

Use this target for the first presentation.

It is deliberately small and synthetic, so:
- analysis is fast;
- findings are easy to explain;
- two vulnerability categories are known in advance;
- you do not depend on a large external repository;
- no live external target is involved.

Expected categories:

```text
access_control
reentrancy
```

---

# F. Recommended E2E scenario

## Stage 1 — Create audit/project

Create the project using the normal ProofGuard intake API.

Input:
- demo target path/zip/repository;
- `scope.yaml`.

CHECKPOINT 1 — Screenshot

Show:
- project ID;
- source type;
- source revision/fingerprint if exposed;
- parsed categories;
- scope.

Presentation message:

> "This is the client entering one codebase. From here everything else is protocol-driven."

---

## Stage 2 — Agent routing

Create/finalize the routing plan.

CHECKPOINT 2 — Screenshot

Show:
- selected nodes by category;
- ranked vs exploration/shadow if exposed;
- category score/membership;
- unique node/operator IDs.

Presentation message:

> "The backend does not send every task to every node. Historical specialization determines opportunity, while exploration lets new nodes build history."

---

## Stage 3 — Run agent analysis

Use the real execution path in the repo.

You ideally want several independent submissions per actual root cause.

Target result for the demo:
- at least one access-control cluster;
- at least one reentrancy cluster;
- 2-5 reports for each;
- one duplicate/spam-style repeated submission if you can inject this safely through the API;
- several nodes that find nothing or produce invalid evidence.

CHECKPOINT 3 — Screenshot

Show the submissions endpoint/table.

You want visible:
- node/operator;
- category;
- finding hash;
- accepted/rejected/pending state;
- same root cause coming from several independent operators.

Presentation message:

> "Agents do not vote on truth. They independently propose candidate findings."

---

## Stage 4 — Finding clustering

Run/rebuild the root-cause clustering service.

CHECKPOINT 4 — Screenshot

Show:
- cluster ID;
- canonical finding;
- category;
- member submission IDs;
- distinct reporting operators;
- duplicate/spam distinction.

Presentation message:

> "Ten reports do not automatically mean ten vulnerabilities. ProofGuard pays root causes, not report volume."

---

## Stage 5 — Validator committee

Create/finalize a STANDARD committee for one cluster.

Expected:

```text
5 authoritative validators
+ optional shadow validator
```

CHECKPOINT 5 — Screenshot

Show:
- all five validator node IDs;
- five distinct operator IDs;
- reporter operators excluded;
- shadow role if present.

This is one of the most important screenshots.

Presentation message:

> "A reporter cannot validate its own finding, and running multiple nodes does not create multiple committee seats."

---

## Stage 6 — Independent reproduction

Run the validator reproduction stage.

For the presentation, use your existing Week 3 sandbox path.

CHECKPOINT 6 — Screenshot

Show:
- five independent ValidatorReproductionRecords;
- validator/operator;
- same cluster;
- result status;
- distinct reproduction IDs;
- sandbox/preflight status;
- artifact/source fingerprint if exposed.

Presentation message:

> "The validator does not just read the report and vote. Each authoritative validator independently attempts to reproduce the claim in the isolated sandbox."

---

## Stage 7 — Build one intentionally disputed case

This is the most educational part of the demo.

Make one synthetic validation case result in:

```text
3 ACCEPT
2 REJECT
```

Do this through whatever test/fixture/API path already exists. Do NOT change production formulas.

CHECKPOINT 7 — Screenshot

Show the five attestations before consensus.

Then calculate consensus.

Expected:

```text
DISPUTED
```

CHECKPOINT 8 — Screenshot

Show:
- N=5;
- quorum=4;
- supermajority=4;
- ACCEPT=3;
- REJECT=2;
- final = DISPUTED.

Presentation message:

> "ProofGuard does not treat a 51% majority as security truth."

This screenshot is extremely strong for the presentation.

---

## Stage 8 — Escalation

Trigger the one allowed escalation round.

Expected:

```text
+4 new validator operators
```

Total:

```text
N = 9
```

Make the new validators resolve the case.

A good demo is:

Round 1:

```text
3 ACCEPT
2 REJECT
```

Round 2:

```text
4 REJECT
```

Cumulative:

```text
3 ACCEPT
6 REJECT
```

Final:

```text
REJECTED
```

CHECKPOINT 9 — Screenshot

Show:
- Round 1 operators;
- Round 2 four NEW operators;
- cumulative N=9;
- threshold=6;
- final result REJECTED.

Presentation message:

> "The two original validators that were in the minority were ultimately correct. This is why we do not punish disagreement."

---

## Stage 9 — Validator quality/performance

Run Day 5 performance evaluation.

CHECKPOINT 10 — Screenshot

Choose:
- one original minority validator;
- one original majority validator.

Show:
- ValidationQualityAssessment;
- validity correctness;
- reproduction/root-cause/severity/impact components;
- VQ;
- ValidatorCategoryScore before/after if possible;
- membership before/after if meaningful.

Presentation message:

> "Validators are scored against final truth, not against the crowd."

---

## Stage 10 — Miner reward cycle

Calculate/finalize the Week 7 miner reward stream.

CHECKPOINT 11 — Screenshot

Show:
- TaskRewardBudget;
- miner_pool;
- clusters;
- severity × uniqueness;
- top-K;
- Q²;
- Chief Finder if one exists;
- distributed/undistributed;
- RewardEvents.

Do not spend the entire presentation on formulas. One slide is enough.

---

## Stage 11 — Validator reward cycle

Calculate/finalize the Week 8 validator stream.

CHECKPOINT 12 — Screenshot

Show:
- validator_pool;
- authoritative work units;
- base work-unit budget;
- 30% completion;
- 70% quality;
- VQ²;
- distributed;
- undistributed;
- RewardEvents.

Highlight the original minority-correct validator if possible.

Presentation message:

> "A correct minority validator is economically valuable; agreement with the local majority is not a reward multiplier."

---

## Stage 12 — Pool conservation

Use verify/accounting endpoint.

CHECKPOINT 13 — Screenshot

You want something equivalent to:

```text
miner_pool: ...
validator_pool: ...

miner_pool_consumed_once: PASS
validator_pool_consumed_once: PASS

validator_distributed + validator_undistributed == validator_pool

duplicate_reward_events: 0
```

This screenshot supports the "auditable protocol" claim.

---

# G. Show node evolution with a second audit

This is important because otherwise the historical scoring work is invisible.

Run a second synthetic audit after the first one.

You do NOT need another huge codebase.

You can:
- reset project/task only;
- use another copy/variant of the same target;
- preserve node historical state.

Before second routing, record:

```text
Agent CategoryScore
Validator CategoryScore
Membership
```

Then create the second routing/committee.

CHECKPOINT 14 — Screenshot

Compare:

```text
AUDIT 1
access_control selected:
A, B, C, D

AUDIT 2
access_control selected:
A, C, F, G
```

with corresponding historical scores/fairness.

For validators show:

```text
new shadow validator
  -> good resolved validations
  -> probation/active
  -> now eligible for authoritative committee
```

if you have enough synthetic history.

Presentation message:

> "The network learns who is good at which role. Discovery skill and validation skill evolve independently."

---

# H. What NOT to demo live

Avoid depending on:
- dozens of paid LLM calls;
- a large public GitHub repository;
- live RPC endpoints;
- real smart-contract money;
- flaky external services;
- first-time Docker setup;
- migrations you have never tested.

The presentation should showcase your protocol architecture, not your Wi-Fi connection.

---

# I. How to use the runner

Once the template paths are mapped to your real API:

```bash
cp demo_scenario.template.json demo_scenario.json
```

Edit the real endpoint paths and JSON schemas.

Then:

```bash
python e2e_demo.py --config demo_scenario.json
```

At every step with:

```json
"checkpoint": "..."
```

the runner pauses.

Take the screenshot, then press ENTER.

All responses are saved under:

```text
demo_artifacts/
```

and a presentation-friendly report is generated at:

```text
demo_artifacts/demo_report.html
```

Open that in your browser:

```bash
xdg-open demo_artifacts/demo_report.html
```

The HTML is often cleaner for screenshots than raw Swagger JSON.

For an unattended gold run:

```bash
python e2e_demo.py \
  --config demo_scenario.json \
  --no-pause
```

---

# J. Exact next action

1. Start ProofGuard.
2. Run:

```bash
python e2e_demo.py \
  --config demo_scenario.template.json \
  --list-routes
```

3. This creates:

```text
demo_artifacts/openapi.json
```

4. Send/upload that `openapi.json`.

With that API spec, the scenario JSON can be mapped 1:1 to the real endpoints and request schemas without spending Codex credits.
