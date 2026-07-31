# Week 2.5 Research Evaluation Report

Generated: 2026-07-06T09:10:05.551021+00:00

Findings produced by agents are candidate/unverified only. This benchmark does not validate findings or execute exploits.

## Leaderboard Summary
| Agent | Cases | TP | FP | FN | Precision | Recall | F1 | Score |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| access_control | 3 | 2 | 0 | 0 | 1.00 | 1.00 | 1.00 | 10 |
| reentrancy | 3 | 2 | 0 | 0 | 1.00 | 1.00 | 1.00 | 10 |

## Metrics Per Agent
### access_control

- Total cases: 3
- Positive cases: 2
- Negative cases: 1
- Expected findings: 2
- Actual findings: 2
- True positives: 2
- False positives: 0
- False negatives: 0
- Out-of-scope findings: 0
- Duplicate findings: 0
- Schema invalid findings: 0
- Precision: 1.00
- Recall: 1.00
- F1 score: 1.00
- Score: 10

### reentrancy

- Total cases: 3
- Positive cases: 2
- Negative cases: 1
- Expected findings: 2
- Actual findings: 2
- True positives: 2
- False positives: 0
- False negatives: 0
- Out-of-scope findings: 0
- Duplicate findings: 0
- Schema invalid findings: 0
- Precision: 1.00
- Recall: 1.00
- F1 score: 1.00
- Score: 10

## Per-Case Summary

### access_control

| Case | Positive | Expected | Actual | TP | FP | FN | Precision | Recall | F1 | Score |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| access_001 | True | 1 | 1 | 1 | 0 | 0 | 1.00 | 1.00 | 1.00 | 5 |
| access_002 | True | 1 | 1 | 1 | 0 | 0 | 1.00 | 1.00 | 1.00 | 5 |
| access_negative_001 | False | 0 | 0 | 0 | 0 | 0 | 0.00 | 1.00 | 0.00 | 0 |

### reentrancy

| Case | Positive | Expected | Actual | TP | FP | FN | Precision | Recall | F1 | Score |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| reentrancy_001 | True | 1 | 1 | 1 | 0 | 0 | 1.00 | 1.00 | 1.00 | 5 |
| reentrancy_002 | True | 1 | 1 | 1 | 0 | 0 | 1.00 | 1.00 | 1.00 | 5 |
| reentrancy_negative_001 | False | 0 | 0 | 0 | 0 | 0 | 0.00 | 1.00 | 0.00 | 0 |

## Best Performing Agent
- access_control with score 10 and F1 1.00

## Weakest Metric Per Agent
- access_control: precision (1.00)
- reentrancy: precision (1.00)

## Notes
- Findings are still candidate/unverified.
- No PoC validation is implemented yet.
- The benchmark is synthetic and small.

## Recommended Next Improvements
- Expand benchmark cases for modifiers, role-based access control, and inherited contracts.
- Improve Solidity parsing before relying on more complex project layouts.
- Track false positive and false negative categories per rule change.
- Keep findings as candidate/unverified until a validator or reproduction layer exists.
