# Week 3 Report - Sandboxed PoC Reproduction Layer

## 1. Goal

Week 3 turns candidate findings into testable reproduction attempts. The focus is safe PoC execution: tests are never run directly on the host, and execution must pass through Safety Preflight, CommandPolicy, and SandboxedCommandRunner.

## 2. What was implemented

### Reproduction schema and storage
ReproductionResult schemas and file-backed storage link reproduction attempts to findings.

### Foundry project detection
Foundry detector identifies repositories with `foundry.toml` and `src/` before reproduction is attempted.

### Tooling diagnostics
Tooling diagnostics report Docker, sandbox image, and host Foundry availability without executing tests.

### Safety Preflight
Safety Preflight validates test names, command shapes, foundry.toml settings, and PoC files.

### SandboxedCommandRunner
SandboxedCommandRunner executes allowlisted Foundry commands only inside Docker.

### foundry-sandbox Docker image
The `foundry-sandbox:latest` image provides Foundry tools, non-root execution, and cached compiler support.

### PoC upload and reproduction API
API endpoints store PoCs, run safety checks, invoke the sandbox, and persist ReproductionResult records.

### Synthetic reproduction benchmark
A four-case synthetic benchmark validates reproduced, rejected_unsafe, and unsupported paths.

## 3. Architecture

```text
Candidate Finding
|
v
PoC File
|
v
Safety Preflight
|
v
SandboxedCommandRunner
|
v
ReproductionResult
|
v
Metrics + Report
```

## 4. Security controls

- No `forge test` on host
- No `shell=True`
- Allowlisted commands only
- Docker network disabled
- No secrets mounted
- No `docker.sock` mounted
- Non-root user
- Timeout/resource limits
- FFI rejected
- Dangerous filesystem permissions rejected

## 5. Benchmark results

| Case | Expected | Actual | Passed | Notes |
| --- | --- | --- | --- | --- |
| access_control_poc_001 | reproduced | reproduced | yes | safe access-control PoC |
| reentrancy_poc_001 | reproduced | reproduced | yes | safe reentrancy PoC |
| unsafe_poc_001 | rejected_unsafe | rejected_unsafe | yes | FFI blocked by preflight |
| unsupported_non_foundry_001 | unsupported | unsupported | yes | missing foundry.toml |

## 6. Aggregate metrics

- Total cases: 4
- Passed cases: 4
- Failed cases: 0
- Reproduction attempts: 2
- Reproduced count: 2
- Rejected unsafe count: 1
- Unsupported count: 1
- Timeout count: 0
- Sandbox error count: 0
- Accuracy: 1.00

## 7. Current limitations

- Benchmark is synthetic
- Mock mode does not prove real Foundry execution
- Real mode requires Docker image
- PoCs are manual
- No AI PoC generation yet
- No validator AI yet

## 8. Next steps

- Validator layer v0
- Deduplication
- Severity normalization
- Report generation
- Reproduction quality scoring
