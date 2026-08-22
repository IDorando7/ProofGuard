from datetime import datetime, timezone

import pytest

from app.schemas.node import NodeStatusChangeRequest
from app.schemas.poc import ReproductionRunRequest
from app.schemas.reproduction import ReproductionResult, ReproductionStatus
from app.schemas.sandbox import SandboxCommandResult, SandboxRunStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestationRequest,
    ValidatorAssignmentRole,
    ValidatorReproductionMode,
    ValidatorReproductionRequest,
)
from app.schemas.validation import ValidationStatus
from app.schemas.validator_committee import ValidationAssuranceMode
from app.schemas.validator_reproduction import (
    IndependentReproductionArtifact,
    ValidatorReproductionExecutionRequest,
)
from app.services.node_registry_service import change_node_status
from app.services.reproduction_service import (
    execute_reproduction_safely,
    load_attributed_reproduction_result,
    load_reproduction_result,
    save_reproduction_result,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolEligibilityError,
    ValidatorProtocolRelationshipError,
)
from app.services.validator_committee_service import (
    finalize_validator_committee,
)
from app.services.validator_reproduction_execution_service import (
    build_environment_fingerprint,
    build_source_snapshot,
    execute_validator_reproduction,
    get_committee_reproduction_readiness,
)
from app.services.validator_reproduction_service import (
    create_validator_reproduction_record,
    list_validator_reproduction_records,
)
from app.services.validation_attestation_service import (
    create_validation_attestation,
    list_validation_attestations,
)
from tests.test_validator_committee_service import NOW, _context, _plan


def _finalized_context(tmp_path, *, high_assurance=False, validator_count=6):
    root, workspace, routing, clusters, _ = _context(
        tmp_path, validator_count=validator_count
    )
    committee = _plan(
        root,
        routing,
        clusters[0],
        assurance_mode=(
            ValidationAssuranceMode.HIGH_ASSURANCE
            if high_assurance
            else ValidationAssuranceMode.STANDARD
        ),
    )
    committee = finalize_validator_committee(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=committee.validator_committee_id,
    ).committee
    _install_safe_poc(workspace, committee.finding_cluster_id, clusters[0])
    return root, workspace, routing, clusters[0], committee


def _install_safe_poc(workspace, _cluster_id, cluster):
    (workspace / "repo" / "src").mkdir(parents=True, exist_ok=True)
    (workspace / "repo" / "test").mkdir(parents=True, exist_ok=True)
    (workspace / "repo" / "foundry.toml").write_text(
        "[profile.default]\nffi = false\nfs_permissions = []\n",
        encoding="utf-8",
    )
    (workspace / "repo" / "src" / "Vault.sol").write_text(
        "contract Vault {}\n", encoding="utf-8"
    )
    (workspace / "repo" / "test" / "Claim.t.sol").write_text(
        "contract ClaimTest { function testClaim() public {} }\n",
        encoding="utf-8",
    )
    result = load_reproduction_result(workspace, cluster.canonical_finding_id)
    assert result is not None
    save_reproduction_result(
        result.model_copy(
            update={"poc_file": "test/Claim.t.sol", "test_name": "testClaim"}
        ),
        workspace,
    )


def _fake_executor(status_by_node, monkeypatch):
    from app.services import validator_reproduction_execution_service as service

    current_node = {"value": None}

    def fake_execute(**kwargs):
        now = datetime.now(timezone.utc)
        status = status_by_node[current_node["value"]]
        return ReproductionResult(
            reproduction_id=kwargs["reproduction_id"],
            project_id=kwargs["project_id"],
            finding_id=kwargs["finding_id"],
            status=status,
            poc_file=kwargs["request"].poc_file,
            test_name=kwargs["request"].test_name,
            command=["forge", "test", "--match-test", kwargs["request"].test_name],
            stdout="validator output",
            stderr="",
            duration_ms=10,
            error_message=None,
            safety_notes=[],
            created_at=now,
            updated_at=now,
        )

    monkeypatch.setattr(service, "execute_reproduction_safely", fake_execute)
    return current_node


def _execute(root, workspace, routing, seat, *, mode="submitted_poc", artifact=None):
    return execute_validator_reproduction(
        root,
        workspace,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_assignment_id=seat.validator_assignment_id,
        request=ValidatorReproductionExecutionRequest(
            validator_node_id=seat.validator_node_id,
            reproduction_mode=mode,
        ),
        independent_artifact=artifact,
    )


def test_standard_creates_five_independent_attributed_results_and_shadow_is_extra(
    tmp_path, monkeypatch
):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    statuses = {
        seat.validator_node_id: ReproductionStatus.REPRODUCED
        for seat in committee.authoritative_seats + committee.shadow_seats
    }
    current = _fake_executor(statuses, monkeypatch)
    records = []
    for seat in committee.authoritative_seats + committee.shadow_seats:
        current["value"] = seat.validator_node_id
        records.append(_execute(root, workspace, routing, seat))

    authoritative = [
        item for item in records if item.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE
    ]
    assert len(authoritative) == 5
    assert len(records) == 6
    assert len({item.underlying_reproduction_result_id for item in records}) == 6
    assert len({item.validator_assignment_id for item in records}) == 6
    assert len({item.validator_operator_id for item in authoritative}) == 5
    assert {item.poc_artifact_fingerprint for item in records}.__len__() == 1
    assert all(item.validator_committee_id == committee.validator_committee_id for item in records)
    assert all(item.underlying_reproduction_result_storage_ref for item in records)


def test_high_assurance_creates_seven_independent_authoritative_results(
    tmp_path, monkeypatch
):
    root, workspace, routing, _, committee = _finalized_context(
        tmp_path, high_assurance=True, validator_count=8
    )
    statuses = {
        seat.validator_node_id: ReproductionStatus.FAILED
        for seat in committee.authoritative_seats
    }
    current = _fake_executor(statuses, monkeypatch)
    records = []
    for seat in committee.authoritative_seats:
        current["value"] = seat.validator_node_id
        records.append(_execute(root, workspace, routing, seat))
    assert len(records) == 7
    assert len({item.underlying_reproduction_result_id for item in records}) == 7
    assert all(item.reproduction_status == ReproductionStatus.FAILED for item in records)


def test_mixed_terminal_outcomes_are_ready_without_a_verdict_and_shadow_does_not_block(
    tmp_path, monkeypatch
):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    outcomes = [
        ReproductionStatus.REPRODUCED,
        ReproductionStatus.REPRODUCED,
        ReproductionStatus.REPRODUCED,
        ReproductionStatus.FAILED,
        ReproductionStatus.TIMEOUT,
    ]
    statuses = {
        seat.validator_node_id: outcome
        for seat, outcome in zip(committee.authoritative_seats, outcomes, strict=True)
    }
    current = _fake_executor(statuses, monkeypatch)
    for seat in committee.authoritative_seats:
        current["value"] = seat.validator_node_id
        _execute(root, workspace, routing, seat)
    readiness = get_committee_reproduction_readiness(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=committee.validator_committee_id,
    )
    assert readiness.status == "ready"
    assert readiness.ready_for_attestation_or_consensus_stage is True
    assert readiness.authoritative_terminal_count == 5
    assert readiness.shadow_terminal_count == 0
    assert readiness.terminal_status_counts == {
        ReproductionStatus.FAILED: 1,
        ReproductionStatus.REPRODUCED: 3,
        ReproductionStatus.TIMEOUT: 1,
    }
    dumped = readiness.model_dump()
    assert "finding_verdict" not in dumped
    assert "consensus_result" not in dumped


def test_readiness_progression_uses_only_authoritative_denominator(tmp_path, monkeypatch):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    statuses = {
        seat.validator_node_id: ReproductionStatus.REJECTED_UNSAFE
        for seat in committee.authoritative_seats
    }
    current = _fake_executor(statuses, monkeypatch)
    assert get_committee_reproduction_readiness(
        root,
        project_id="project-1",
        routing_id=routing.routing_id,
        validator_committee_id=committee.validator_committee_id,
    ).status == "pending"
    for index, seat in enumerate(committee.authoritative_seats, start=1):
        current["value"] = seat.validator_node_id
        _execute(root, workspace, routing, seat)
        readiness = get_committee_reproduction_readiness(
            root,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_committee_id=committee.validator_committee_id,
        )
        assert readiness.authoritative_terminal_count == index
        assert readiness.ready_for_attestation_or_consensus_stage is (index == 5)


def test_duplicate_request_returns_one_job_result_and_one_final_record(tmp_path, monkeypatch):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    seat = committee.authoritative_seats[0]
    current = _fake_executor(
        {seat.validator_node_id: ReproductionStatus.REPRODUCED}, monkeypatch
    )
    current["value"] = seat.validator_node_id
    first = _execute(root, workspace, routing, seat)
    repeated = _execute(root, workspace, routing, seat)
    assert repeated == first
    records = list_validator_reproduction_records(root, "project-1", routing.routing_id)
    assert records == [first]
    stored = load_attributed_reproduction_result(
        workspace, first.underlying_reproduction_result_storage_ref
    )
    assert stored is not None
    assert stored.reproduction_id == first.underlying_reproduction_result_id


@pytest.mark.parametrize("status", ["inactive", "suspended", "banned"])
def test_current_node_status_is_revalidated_before_execution(tmp_path, status):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    seat = committee.authoritative_seats[0]
    change_node_status(
        root,
        seat.validator_node_id,
        NodeStatusChangeRequest(status=status, reason="Day 3 eligibility test."),
    )
    with pytest.raises(ValidatorProtocolEligibilityError):
        _execute(root, workspace, routing, seat)
    assert list_validator_reproduction_records(root, "project-1", routing.routing_id) == []


def test_wrong_validator_and_cross_project_identity_are_rejected(tmp_path):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    seat = committee.authoritative_seats[0]
    with pytest.raises(ValidatorProtocolRelationshipError):
        execute_validator_reproduction(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidatorReproductionExecutionRequest(
                validator_node_id="wrong-validator"
            ),
        )
    with pytest.raises(ValidatorProtocolRelationshipError):
        execute_validator_reproduction(
            root,
            workspace,
            project_id="other-project",
            routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidatorReproductionExecutionRequest(
                validator_node_id=seat.validator_node_id
            ),
        )


def test_committee_assignment_cannot_adopt_reporter_week3_result(tmp_path):
    root, workspace, routing, cluster, committee = _finalized_context(tmp_path)
    seat = committee.authoritative_seats[0]
    result = load_reproduction_result(workspace, cluster.canonical_finding_id)
    assert result is not None
    with pytest.raises(ValidatorProtocolConflictError, match="independently executed"):
        create_validator_reproduction_record(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidatorReproductionRequest(
                validator_node_id=seat.validator_node_id,
                reproduction_mode=ValidatorReproductionMode.SUBMITTED_POC,
                underlying_reproduction_result_id=result.reproduction_id,
            ),
        )


def test_independent_mode_requires_trusted_ingestion_but_uses_same_safe_executor(
    tmp_path, monkeypatch
):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    seat = committee.authoritative_seats[0]
    with pytest.raises(ValidatorProtocolConflictError, match="not public"):
        _execute(root, workspace, routing, seat, mode="independent_reproduction")

    current = _fake_executor(
        {seat.validator_node_id: ReproductionStatus.REPRODUCED}, monkeypatch
    )
    current["value"] = seat.validator_node_id
    record = _execute(
        root,
        workspace,
        routing,
        seat,
        mode="independent_reproduction",
        artifact=IndependentReproductionArtifact(
            artifact_id="trusted-independent-artifact",
            poc_file="test/Claim.t.sol",
            test_name="testClaim",
        ),
    )
    assert record.reproduction_mode == ValidatorReproductionMode.INDEPENDENT_REPRODUCTION


def test_unsafe_submitted_poc_is_terminal_without_sandbox_launch(tmp_path, monkeypatch):
    root, workspace, routing, cluster, committee = _finalized_context(tmp_path)
    (workspace / "repo" / "test" / "Claim.t.sol").write_text(
        "contract ClaimTest { function testClaim() public { vm.ffi(new string[](0)); } }",
        encoding="utf-8",
    )
    from app.services import sandbox_runner

    monkeypatch.setattr(
        sandbox_runner,
        "run_in_sandbox",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError("unsafe artifact must not launch sandbox")
        ),
    )
    record = _execute(root, workspace, routing, committee.authoritative_seats[0])
    assert record.reproduction_status == ReproductionStatus.REJECTED_UNSAFE
    assert cluster.status.value == "finalized"


def test_source_and_environment_fingerprints_are_deterministic_and_semantic(tmp_path):
    root, workspace, routing, _, _ = _finalized_context(tmp_path)
    first = build_source_snapshot(
        root, workspace, project_id="project-1", routing_id=routing.routing_id
    )
    repeated = build_source_snapshot(
        root, workspace, project_id="project-1", routing_id=routing.routing_id
    )
    assert repeated == first
    assert build_environment_fingerprint() == build_environment_fingerprint()
    (workspace / "repo" / "src" / "Vault.sol").write_text(
        "contract Vault { uint256 changed; }\n", encoding="utf-8"
    )
    changed = build_source_snapshot(
        root, workspace, project_id="project-1", routing_id=routing.routing_id
    )
    assert changed[0] == first[0]
    assert changed[1] != first[1]


def test_source_change_after_reproduction_blocks_stale_attestation(tmp_path, monkeypatch):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    seat = committee.authoritative_seats[0]
    current = _fake_executor(
        {seat.validator_node_id: ReproductionStatus.REPRODUCED}, monkeypatch
    )
    current["value"] = seat.validator_node_id
    record = _execute(root, workspace, routing, seat)
    (workspace / "repo" / "src" / "Vault.sol").write_text(
        "contract Vault { uint256 changed; }\n", encoding="utf-8"
    )
    with pytest.raises(ValidatorProtocolConflictError, match="source snapshot"):
        create_validation_attestation(
            root,
            workspace,
            project_id="project-1",
            routing_id=routing.routing_id,
            validator_assignment_id=seat.validator_assignment_id,
            request=ValidationAttestationRequest(
                validator_node_id=seat.validator_node_id,
                validator_reproduction_id=record.validator_reproduction_id,
                validity_decision=ValidationStatus.ACCEPTED,
                root_cause_decision=RootCauseDecision.CONFIRMED,
                normalized_severity="High",
                impact_decision=ImpactDecision.VALIDATED,
                reason_codes=["reproduction_confirmed"],
                evidence_references=[],
            ),
        )


def test_shared_week3_executor_runs_from_cleaned_temporary_copy(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / "repo" / "test").mkdir(parents=True)
    (workspace / "repo" / "foundry.toml").write_text(
        "ffi = false\n", encoding="utf-8"
    )
    (workspace / "repo" / "test" / "Claim.t.sol").write_text(
        "contract ClaimTest { function testClaim() public {} }\n", encoding="utf-8"
    )
    captured = {}
    from app.services import sandbox_runner

    def fake_sandbox(*, repo_path, command, timeout_seconds):
        captured["repo_path"] = repo_path
        assert repo_path != workspace / "repo"
        assert (repo_path / "test" / "Claim.t.sol").is_file()
        return SandboxCommandResult(
            status=SandboxRunStatus.COMPLETED,
            command=command,
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=1,
            error_message=None,
        )

    monkeypatch.setattr(sandbox_runner, "run_in_sandbox", fake_sandbox)
    result = execute_reproduction_safely(
        project_id="project-1",
        finding_id="finding-1",
        project_workspace=workspace,
        request=ReproductionRunRequest(
            poc_file="test/Claim.t.sol", test_name="testClaim"
        ),
    )
    assert result.status == ReproductionStatus.REPRODUCED
    assert not captured["repo_path"].exists()


def test_execution_has_no_consensus_reward_or_performance_side_effects(tmp_path, monkeypatch):
    root, workspace, routing, _, committee = _finalized_context(tmp_path)
    seat = committee.authoritative_seats[0]
    current = _fake_executor(
        {seat.validator_node_id: ReproductionStatus.FAILED}, monkeypatch
    )
    current["value"] = seat.validator_node_id
    _execute(root, workspace, routing, seat)
    assert list_validation_attestations(root, "project-1", routing.routing_id) == []
    assert not (root / "validator-consensus").exists()
    assert not (root / "validator-rewards").exists()
    assert not (root / "validator-performance").exists()
