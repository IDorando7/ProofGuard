# Proofguard Audit API

Week 1 backend skeleton for a centralized Web3 AI Security Audit Network MVP.
Week 2 Day 1 adds the strict Finding schema, BaseAgent interface, MockAgent, and JSON finding storage foundation for future audit agents.

This service creates audit projects from either an uploaded Solidity/Foundry repository zip or a GitHub repository URL. It parses a strict scope manifest, creates an internal audit workspace, stores project metadata in SQLite, and exposes a simple local preparation job/status flow.

## Week 1 Implements

- `GET /health`
- `POST /projects` with multipart project creation
- Strict YAML scope manifest parsing with Pydantic
- Safe zip extraction with zip-slip protection
- Minimal `git clone` support for GitHub repository URLs
- Audit workspace creation under `data/audits/<project_id>/`
- SQLite metadata storage
- Project metadata, scope, and status read endpoints
- Local background `prepare` job that verifies repo and scope inputs
- Pytest coverage for health, scope parsing, project creation, and unsafe zips
- Week 2 Day 1 Finding schema and BaseAgent foundation with focused tests

## Not Implemented Yet

- Real AI audit agents
- Vulnerability detection
- Blockchain interaction
- Token rewards
- Staking, slashing, decentralized validators, or validator consensus
- Report generation beyond placeholder folders

## Setup

```bash
cd apps/audit-api
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

## Run

```bash
cd apps/audit-api
source .venv/bin/activate
uvicorn app.main:app --reload
```

The API will be available at `http://127.0.0.1:8000`.

## API Documentation

After the API starts, the generated documentation is available at:

- Swagger UI: `http://127.0.0.1:8000/docs`
- ReDoc: `http://127.0.0.1:8000/redoc`
- OpenAPI JSON: `http://127.0.0.1:8000/openapi.json`

Swagger groups the endpoints by domain and documents every operation, request
parameter, request body, response model, summary, and description. Use **Try it
out** in Swagger UI to send requests to the local API.

The executive dashboard in `../../frontend` uses the read-only `GET /projects`
endpoint to discover existing audit projects. Browser access from the Vite dev
server is allowed for `http://localhost:5173` and `http://127.0.0.1:5173` by
default; configure a comma-separated list with `AUDIT_API_CORS_ORIGINS`.

For the local management Gold Demo only, start the API with
`PROOFGUARD_DEMO_MODE=1`. This conditionally exposes `POST /demo/bootstrap`,
which registers the seeded specialist nodes in the API process's existing local
agent registry and creates deterministic historical protocol inputs before
rebuilding category performance, scores, and memberships. The route is absent
when demo mode is disabled.

## Test

```bash
cd apps/audit-api
source .venv/bin/activate
pytest
```

## Week 2.5 Research Evals

Week 2.5 adds a local benchmark system under `research/` for measuring whether the access control and reentrancy agents improve over time. It uses synthetic Solidity cases, expected findings, deterministic eval scripts, a JSON leaderboard, and a simple experiment log.

Run the access control benchmark:

```bash
python research/evals/eval_access_control.py
```

Run the reentrancy benchmark:

```bash
python research/evals/eval_reentrancy.py
```

Read the current leaderboard:

```bash
cat research/results/leaderboard.json
```

Track a simple experiment:

```bash
python research/autoresearch/run_experiment.py --agent access_control --description "test new setter keywords"
```

This layer does not fine-tune models, run Foundry, execute exploits, call blockchains, issue token rewards, perform staking/slashing, or run decentralized validator nodes.

## Week 3 Day 1 Reproduction Foundation

Week 3 Day 1 adds `ReproductionResult` schemas and a storage service for linking reproduction status to findings. Reproduction records are saved under:

```text
data/audits/<project_id>/reproductions/<finding_id>/
  reproduction.json
  stdout.txt
  stderr.txt
```

The schema defines statuses such as `not_attempted`, `generated`, `running`, `reproduced`, `failed`, `timeout`, `rejected_unsafe`, and `sandbox_error`. No tests are executed yet: Docker sandboxing, Foundry runners, safety preflight, and PoC execution come later in Week 3.

## Week 3 Day 2 Tooling Detection

Week 3 Day 2 adds Foundry project detection and sandbox/tooling diagnostics. The detector checks for `foundry.toml`, `src/`, optional `test/`, and whether `test/` can be created later without creating it now.

The tooling service can check whether Docker, the future sandbox image, and host `forge` are available. Host `forge` is diagnostic only: real reproduction must run in a future sandbox runner. Day 2 does not run `forge build`, `forge test`, Docker containers, PoCs, or untrusted Solidity tests.

## Week 3 Day 3 Safety Preflight

Week 3 Day 3 adds a conservative Safety Preflight service for future PoC/test execution. It validates test names, checks that requested commands are limited to safe `forge build`/`forge test` forms, inspects `foundry.toml`, and scans PoC files for obvious unsafe patterns.

The preflight rejects FFI, dangerous `fs_permissions`, suspicious file reads, secret references, unsafe Foundry cheatcodes, path traversal, and shell injection attempts. It does not execute code, run Docker, call `forge`, or mutate repository files. Passing preflight is not enough for reproduction: future Docker sandbox execution is still required.

## Week 3 Day 4 Sandboxed Command Runner

Week 3 Day 4 adds a service-layer `SandboxedCommandRunner` foundation. Allowed commands are limited to `forge build`, `forge test`, and `forge test --match-test <test_name>`. FFI is blocked by Safety Preflight before sandbox execution.

Commands are run only through `docker run` with network disabled, resource limits, a non-root user, tmpfs for `/tmp`, and a single repo mount at `/workspace/repo`. No arbitrary shell commands are allowed, host secrets are not mounted, and `shell=True` is not used. Unit tests mock Docker execution, so Docker is not required for tests. Actual local execution requires a `foundry-sandbox:latest` image.

## Week 3 Day 4.5 Foundry Sandbox Image

Week 3 Day 4.5 adds the `foundry-sandbox:latest` Docker image definition used by `SandboxedCommandRunner`.

Build it manually:

```bash
docker build -t foundry-sandbox:latest docker/foundry-sandbox
```

Or use the helper:

```bash
scripts/build_foundry_sandbox.sh
```

Test the image:

```bash
docker run --rm foundry-sandbox:latest forge --version
docker run --rm --network none foundry-sandbox:latest forge --version
```

The image alone is not enough security. Real PoC execution still has to go through Safety Preflight, CommandPolicy, and SandboxRunner. The runner must keep using `--network none`, avoid mounting host secrets or `docker.sock`, avoid arbitrary shell commands, and enforce the command allowlist.

## Week 3 Day 5 PoC Reproduction API

Week 3 Day 5 adds PoC upload/storage and reproduction API wiring. The flow is:

```text
PoC upload -> Safety Preflight -> SandboxedCommandRunner -> ReproductionResult
```

Unsafe PoCs are marked `rejected_unsafe` and are not executed. Safe PoCs run only through the Docker sandbox runner. There is still no AI PoC generation and no validator AI.

Upload a PoC:

```bash
curl -X POST http://localhost:8000/projects/<project_id>/findings/<finding_id>/poc \
  -H "Content-Type: application/json" \
  -d '{
    "poc_filename": "PoC_AccessControl.t.sol",
    "poc_content": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.20;\ncontract DummyTest {}"
  }'
```

Run reproduction:

```bash
curl -X POST http://localhost:8000/projects/<project_id>/findings/<finding_id>/reproduction/run \
  -H "Content-Type: application/json" \
  -d '{
    "poc_file": "test/PoC_AccessControl.t.sol",
    "test_name": "testUnauthorizedSetTreasury",
    "timeout_seconds": 60
  }'
```

Read reproduction:

```bash
curl http://localhost:8000/projects/<project_id>/findings/<finding_id>/reproduction
```

List reproductions:

```bash
curl http://localhost:8000/projects/<project_id>/reproductions
```

## Week 3 Day 6 Reproduction Benchmark

Week 3 Day 6 adds a synthetic reproduction benchmark under `research/datasets/reproduction/`. It includes:

- `access_control_poc_001`
- `reentrancy_poc_001`
- `unsafe_poc_001`
- `unsupported_non_foundry_001`

Run the safe mock evaluation, which does not require Docker:

```bash
python research/evals/eval_reproduction.py --mock-sandbox
```

Run the real sandbox evaluation:

```bash
python research/evals/eval_reproduction.py --real-sandbox
```

Real sandbox mode requires the sandbox image:

```bash
docker build -t foundry-sandbox:latest docker/foundry-sandbox
```

The benchmark is synthetic and small. Mock sandbox mode verifies orchestration, expected statuses, Foundry detection, and Safety Preflight behavior, but it does not prove real Foundry execution. Real-world datasets and AI-generated PoCs come later.

## Week 3 Summary

Week 3 implemented the sandboxed PoC reproduction layer: reproduction result storage, Foundry detection, tooling diagnostics, Safety Preflight, CommandPolicy, SandboxedCommandRunner, the `foundry-sandbox` Docker image, PoC upload/reproduction API wiring, and the synthetic reproduction benchmark.

Run the benchmark in safe mock mode:

```bash
python research/evals/eval_reproduction.py --mock-sandbox
```

Run real sandbox mode:

```bash
python research/evals/eval_reproduction.py --real-sandbox
```

Real mode requires:

```bash
docker build -t foundry-sandbox:latest docker/foundry-sandbox
```

Results are written to:

```text
research/results/reproduction_case_results.json
research/results/reproduction_leaderboard.json
research/results/week_3_reproduction_report.md
research/results/week_3_report.md
research/results/week_3_summary.json
```

Real PoC execution must flow through Safety Preflight -> CommandPolicy -> SandboxedCommandRunner.

## Week 4 Day 1 Validator Foundation

Week 4 starts Validator Layer v0. Day 1 adds `ValidationDecision` schemas and file-backed storage for validator decisions under:

```text
data/audits/<project_id>/validations/<finding_id>/validation.json
```

Validation statuses are `accepted`, `rejected`, `duplicate`, `needs_review`, `out_of_scope`, `insufficient_evidence`, `unsafe_poc`, and `unsupported`.

Day 1 does not perform real validation logic yet. Scope validation, deduplication, severity normalization, and the validator pipeline come next.

## Week 4 Day 2 Scope Validator

Week 4 Day 2 adds a Scope Validator for checking whether a finding is allowed by `scope.yaml`. It compares finding contracts against `contracts_in_scope` and `contracts_out_of_scope`, rejects findings in excluded directories such as `tests/`, `scripts/`, `lib/`, `node_modules/`, `out/`, and `cache/`, and checks finding categories against `attack_categories`.

Scope validation returns `in_scope`, `out_of_scope`, `invalid_scope`, or `unknown`. It only checks declared scope constraints and does not validate exploit correctness. Deduplication, severity normalization, and the full validation pipeline come later.

## Week 4 Day 3 Deduplication Engine

Week 4 Day 3 adds Deduplication Engine v0. It compares findings using category, contract, function, root cause, attack path, and title. The exact v0 dedup key is:

```text
category:contract:function
```

Deduplication returns `unique`, `duplicate`, `possible_duplicate`, or `unknown`. It is deterministic and does not use AI/LLM calls. This does not prove semantic equivalence; it will be integrated into the full validator pipeline later.

## Week 4 Day 4 Severity Normalizer

Week 4 Day 4 adds Severity Normalizer v0. It adjusts agent-reported severity using deterministic rules based on vulnerability category, impact text, attack path text, `assets_at_risk` from `scope.yaml`, and reproduction status.

The normalizer can keep severity unchanged, upgrade it, or downgrade it. It detects signals such as fund theft, arbitrary minting, privilege escalation, reentrancy drains, denial of service, low-impact indicators, reproduced evidence, failed reproduction, unsafe PoCs, and high-value assets at risk.

Severity normalization does not use AI/LLM calls and does not replace human security review. It will be integrated into the full validator pipeline later.

## Week 4 Day 5 Validator Pipeline

Week 4 Day 5 adds Validator Pipeline v0 and the Validation API. The pipeline combines finding existence, scope validation, deduplication, reproduction status, and severity normalization to produce a stored `ValidationDecision`.

Validation statuses include `accepted`, `duplicate`, `out_of_scope`, `insufficient_evidence`, `unsafe_poc`, `unsupported`, and `needs_review`. A finding is accepted only when it is in scope, not a duplicate, and has reproduced evidence.

Validation API endpoints:

```text
POST /projects/{project_id}/findings/{finding_id}/validate
GET /projects/{project_id}/findings/{finding_id}/validation
GET /projects/{project_id}/validations
POST /projects/{project_id}/validate-all
```

The validator pipeline does not execute tests or PoCs. It only reads existing findings and reproduction results, then writes validation decisions under `data/audits/<project_id>/validations/<finding_id>/validation.json`. Final audit report generation comes next.

## Week 4 Day 6 Final Audit Report

Week 4 Day 6 adds Final Audit Report v0. Report generation reads stored findings, reproduction results, and validation decisions, then writes:

```text
data/audits/<project_id>/reports/final_report.md
data/audits/<project_id>/reports/final_report.json
```

The report includes project metadata, scope, an executive summary, severity breakdown, validation breakdown, accepted findings, rejected or insufficient-evidence findings, duplicates, out-of-scope findings, unsafe or unsupported findings, limitations, and notes.

Report generation API endpoints:

```text
POST /projects/{project_id}/reports/final
GET /projects/{project_id}/reports/final
GET /projects/{project_id}/reports/final/markdown
```

Report generation does not execute PoCs, run validations automatically, call Docker, call forge, or use AI/LLM report writing.

## Week 4 Summary

Week 4 completes Validator Layer v0. Validation decisions are generated from declared scope, deterministic deduplication, severity normalization, and reproduction evidence. Accepted findings must be in scope, not duplicates, and reproduced.

Week 4 also adds Final Audit Report v0 and a synthetic validation benchmark for checking the validator pipeline decisions.

Run the validation benchmark:

```bash
python research/evals/eval_validation.py
```

Generated benchmark files:

```text
research/results/validation_case_results.json
research/results/validation_leaderboard.json
research/results/week_4_report.md
research/results/week_4_summary.json
```

The validation benchmark does not call Docker, does not run PoCs, and does not call forge. It only evaluates stored findings and reproduction results in `research/datasets/validation`.

## Example scope.yaml

```yaml
project_name: MiniLendingProtocol
language: Solidity
framework: Foundry
chain: Ethereum
commit_hash: "local-dev"

contracts_in_scope:
  - src/Vault.sol
  - src/OracleRouter.sol
  - src/LendingPool.sol

contracts_out_of_scope:
  - test/
  - script/
  - mocks/

assets_at_risk:
  - user deposits
  - protocol treasury
  - collateral balances

attack_categories:
  - access_control
  - reentrancy
  - oracle_manipulation
  - accounting
  - upgradeability

known_issues:
  - "Owner can pause protocol intentionally"

forbidden_actions:
  - mainnet exploit
  - private key extraction
  - social engineering
```

## Example Requests

Health:

```bash
curl http://127.0.0.1:8000/health
```

Create a project from a zip:

```bash
curl -X POST http://127.0.0.1:8000/projects \
  -F "project_name=MiniLendingProtocol" \
  -F "scope_file=@scope.yaml" \
  -F "repo_zip=@repo.zip"
```

Create a project from GitHub:

```bash
curl -X POST http://127.0.0.1:8000/projects \
  -F "project_name=MiniLendingProtocol" \
  -F "scope_file=@scope.yaml" \
  -F "github_url=https://github.com/example/minilending.git"
```

Read project metadata:

```bash
curl http://127.0.0.1:8000/projects/<project_id>
```

Read parsed scope:

```bash
curl http://127.0.0.1:8000/projects/<project_id>/scope
```

Start preparation:

```bash
curl -X POST http://127.0.0.1:8000/projects/<project_id>/prepare
```

Read preparation status:

```bash
curl http://127.0.0.1:8000/projects/<project_id>/status
```

## Workspace Layout

Project creation writes:

```text
data/audits/<project_id>/
  repo/
  scope/
    scope.yaml
    parsed_scope.json
  findings/
  reports/
  logs/
  metadata.json
```

## Notes

- API responses use relative workspace paths where possible.
- Uploaded repositories are never executed.
- Zip extraction rejects absolute paths and `..` entries.
- GitHub support is intentionally minimal: the API accepts a URL and runs `git clone --depth 1`.
