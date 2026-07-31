# Week 4 Report - Validator Layer v0

Generated: 2026-07-10T11:40:43.247915+00:00

## 1. Goal

Week 4 converts reproduced findings into validation decisions. The system now decides whether a finding is accepted, duplicate, out-of-scope, insufficient evidence, unsafe, unsupported, or needs review. No blockchain or reward logic is implemented yet.

## 2. What was implemented

- ValidationDecision schema and storage
- Scope Validator
- Deduplication Engine v0
- Severity Normalizer v0
- Validator Pipeline v0
- Validation API
- Final Audit Report v0
- Synthetic validation benchmark

## 3. Architecture

```text
Candidate Finding
|
v
ReproductionResult
|
v
Validator Pipeline
|
+--> Scope Validator
+--> Deduplication Engine
+--> Severity Normalizer
+--> Evidence Check
|
v
ValidationDecision
|
v
Final Audit Report
```

## 4. Validator decision rules

- Accepted requires in_scope + not duplicate + reproduced.
- Out-of-scope is rejected before acceptance.
- Duplicate is not counted as a new accepted finding.
- Failed reproduction maps to insufficient_evidence.
- rejected_unsafe maps to unsafe_poc.
- unsupported maps to unsupported.
- Missing reproduction maps to needs_review.

## 5. Benchmark results

| Case | Expected | Actual | Passed | Notes |
| --- | --- | --- | --- | --- |
| accepted_reproduced_001 | accepted | accepted | yes | reproduced and in scope |
| duplicate_finding_001 | one accepted, one duplicate | one accepted, one duplicate | yes | duplicate detected |
| out_of_scope_001 | out_of_scope | out_of_scope | yes | contract outside scope |
| insufficient_evidence_001 | insufficient_evidence | insufficient_evidence | yes | reproduction failed |
| unsafe_poc_001 | unsafe_poc | unsafe_poc | yes | PoC rejected by safety checks |
| unsupported_001 | unsupported | unsupported | yes | unsupported reproduction layer |
| needs_review_no_reproduction_001 | needs_review | needs_review | yes | missing reproduction |

## 6. Aggregate metrics

- Total cases: 7
- Passed cases: 7
- Failed cases: 0
- Total findings: 8
- Matched findings: 8
- Mismatched findings: 0
- Accepted count: 2
- Duplicate count: 1
- Out-of-scope count: 1
- Insufficient evidence count: 1
- Unsafe PoC count: 1
- Unsupported count: 1
- Needs review count: 1
- Accuracy: 1.00

## 7. Final audit report capability

- Week 4 can generate `final_report.md`.
- Week 4 can generate `final_report.json`.
- The report groups findings by validation status.
- The report includes severity and validation breakdowns.

## 8. Current limitations

- Synthetic benchmark only.
- Deterministic heuristic validator.
- No human review workflow yet.
- No decentralized validators yet.
- No token/staking/reward logic yet.
- No real dataset integration yet.

## 9. Next steps

- Week 5 can focus on protocol/reward model specification.
- Node identity.
- Reputation.
- Validator scoring.
- Reward rules.
- Slashing rules.
- Real dataset expansion.
