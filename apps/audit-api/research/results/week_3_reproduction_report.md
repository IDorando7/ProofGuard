# Week 3 Reproduction Benchmark Report

Generated: 2026-08-07T15:52:06.545371+00:00

Benchmark: week_3_reproduction_synthetic

## Aggregate Metrics

- Total cases: 4
- Passed cases: 4
- Failed cases: 0
- Reproduction attempts: 2
- Reproduced count: 2
- Reproduction failed count: 0
- Rejected unsafe count: 1
- Unsupported count: 1
- Timeout count: 0
- Sandbox error count: 0
- Accuracy: 1.00
- Mode: mock sandbox

## Per-Case Status

| Case | Expected | Actual | Passed | Foundry | Preflight | Execution Mode | Safety Issues |
| --- | --- | --- | --- | --- | --- | --- | ---: |
| access_control_poc_001 | reproduced | reproduced | True | True | True | mock_sandbox | 0 |
| reentrancy_poc_001 | reproduced | reproduced | True | True | True | mock_sandbox | 0 |
| unsafe_poc_001 | rejected_unsafe | rejected_unsafe | True | True | False | not_executed | 3 |
| unsupported_non_foundry_001 | unsupported | unsupported | True | False | None | not_executed | 0 |

## Notes
- This benchmark is synthetic.
- Real-world datasets are not used yet.
- Mock sandbox mode does not prove Foundry execution.
- Real sandbox mode requires `foundry-sandbox:latest`.
- Safety preflight does not replace sandboxing.
