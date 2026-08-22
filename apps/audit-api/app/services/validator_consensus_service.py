from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from pydantic import ValidationError

from app.schemas.validation import ValidationStatus
from app.schemas.validator_attestation import (
    ImpactDecision,
    RootCauseDecision,
    ValidationAttestation,
    ValidatorAssignment,
    ValidatorAssignmentRole,
    ValidatorReproductionRecord,
)
from app.schemas.validator_committee import (
    ValidationAssuranceMode,
    ValidatorCommittee,
    ValidatorCommitteePolicyV1,
    ValidatorCommitteeStatus,
)
from app.schemas.validator_consensus import (
    VALIDATION_CONSENSUS_VERSION,
    VALIDATION_DISPUTE_VERSION,
    VALIDATION_ROUND_VERSION,
    ConsensusComponentResult,
    ConsensusComponentType,
    ConsensusUnresolvedReason,
    ConsensusValueEvidence,
    ConsensusVerificationResult,
    ValidationConsensus,
    ValidationConsensusLifecycle,
    ValidationConsensusOutcome,
    ValidationDispute,
    ValidationDisputeReason,
    ValidationDisputeStatus,
    ValidationEscalationPolicyV1,
    ValidationEscalationResult,
    ValidationRound,
    ValidationRoundStatus,
    ValidationRoundType,
    ValidatorConsensusPolicyV1,
)
from app.services.finding_cluster_service import (
    attach_final_validator_consensus,
    load_finding_cluster,
)
from app.services.node_registry_service import load_node
from app.services.validation_attestation_service import (
    build_attestation_source_payload,
    list_validation_attestations,
)
from app.services.validator_assignment_service import (
    ValidatorProtocolConflictError,
    ValidatorProtocolNotFoundError,
    ValidatorProtocolRelationshipError,
    ValidatorProtocolStorageError,
    _ensure_within,
    _validate_identifier,
    get_validator_protocol_root,
    load_validator_assignment,
)
from app.services.validator_committee_service import (
    ValidatorCommitteeInsufficientValidatorsError,
    finalize_validator_committee,
    list_validator_committees,
    load_validator_committee,
    plan_validator_committee,
)
from app.services.validator_reproduction_service import (
    list_validator_reproduction_records,
    validate_validator_reproduction_current,
)
from app.utils.protocol_serialization import atomic_create_json, atomic_write_json, protocol_fingerprint


class ValidatorConsensusError(ValidatorProtocolConflictError):
    pass


class ValidatorConsensusIntegrityError(ValidatorConsensusError):
    """Authoritative evidence is corrupt/inconsistent, not merely disputed."""


class ValidatorConsensusStaleError(ValidatorConsensusError):
    pass


@dataclass(frozen=True)
class _Evidence:
    rounds: list[ValidationRound]
    committees: list[ValidatorCommittee]
    assignments: list[ValidatorAssignment]
    attestations: list[ValidationAttestation]
    reproductions: list[ValidatorReproductionRecord]
    target_size: int


def quorum_required(target_size: int, policy: ValidatorConsensusPolicyV1 | None = None) -> int:
    if target_size <= 0:
        raise ValueError("Authoritative target size must be positive")
    policy = policy or ValidatorConsensusPolicyV1()
    return max(1, target_size - policy.quorum_missing_tolerance)


def supermajority_required(target_size: int, policy: ValidatorConsensusPolicyV1 | None = None) -> int:
    if target_size <= 0:
        raise ValueError("Authoritative target size must be positive")
    policy = policy or ValidatorConsensusPolicyV1()
    numerator = policy.supermajority_numerator * target_size
    return (numerator + policy.supermajority_denominator - 1) // policy.supermajority_denominator


def aggregate_enum_counts(values: Iterable[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return dict(sorted(counts.items()))


def get_validation_round_path(protocol_data_root: Path, routing_id: str, round_id: str) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(round_id, "validation round")
    root = get_validator_protocol_root(protocol_data_root) / "rounds" / routing_id
    path = root / f"{round_id}.json"
    _ensure_within(path, root)
    return path


def get_validation_consensus_path(protocol_data_root: Path, routing_id: str, consensus_id: str) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(consensus_id, "validation consensus")
    root = get_validator_protocol_root(protocol_data_root) / "consensus" / routing_id
    path = root / f"{consensus_id}.json"
    _ensure_within(path, root)
    return path


def get_validation_dispute_path(protocol_data_root: Path, routing_id: str, dispute_id: str) -> Path:
    _validate_identifier(routing_id, "routing")
    _validate_identifier(dispute_id, "validation dispute")
    root = get_validator_protocol_root(protocol_data_root) / "disputes" / routing_id
    path = root / f"{dispute_id}.json"
    _ensure_within(path, root)
    return path


def build_validation_round_id(project_id: str, routing_id: str, cluster_id: str, round_number: int) -> str:
    return "validation_round_" + protocol_fingerprint({
        "round_version": VALIDATION_ROUND_VERSION,
        "project_id": project_id,
        "routing_id": routing_id,
        "finding_cluster_id": cluster_id,
        "round_number": round_number,
    })


def _round_payload(committee: ValidatorCommittee, *, parent_round_id: str | None, trigger_consensus_id: str | None) -> dict:
    return {
        "round_version": VALIDATION_ROUND_VERSION,
        "project_id": committee.project_id,
        "routing_id": committee.routing_id,
        "finding_cluster_id": committee.finding_cluster_id,
        "round_number": committee.validation_round,
        "round_type": "initial" if committee.validation_round == 1 else "escalation",
        "validator_committee_id": committee.validator_committee_id,
        "committee_source_fingerprint": committee.source_fingerprint,
        "parent_round_id": parent_round_id,
        "trigger_consensus_id": trigger_consensus_id,
    }


def _ensure_round(
    protocol_data_root: Path,
    committee: ValidatorCommittee,
    *,
    parent_round_id: str | None = None,
    trigger_consensus_id: str | None = None,
    status: ValidationRoundStatus = ValidationRoundStatus.EVIDENCE_READY,
) -> ValidationRound:
    round_id = build_validation_round_id(
        committee.project_id, committee.routing_id, committee.finding_cluster_id, committee.validation_round
    )
    path = get_validation_round_path(protocol_data_root, committee.routing_id, round_id)
    existing = load_validation_round(protocol_data_root, committee.project_id, committee.routing_id, round_id)
    payload = _round_payload(
        committee, parent_round_id=parent_round_id, trigger_consensus_id=trigger_consensus_id
    )
    expected_fingerprint = protocol_fingerprint(payload)
    if existing and existing.status == ValidationRoundStatus.FINALIZED:
        if existing.source_fingerprint != expected_fingerprint:
            raise ValidatorConsensusIntegrityError("Validation round source changed")
        return existing
    now = _utc_now()
    desired = ValidationRound(
        validation_round_id=round_id,
        project_id=committee.project_id,
        routing_id=committee.routing_id,
        finding_cluster_id=committee.finding_cluster_id,
        round_number=committee.validation_round,
        round_type=(ValidationRoundType.INITIAL if committee.validation_round == 1 else ValidationRoundType.ESCALATION),
        validator_committee_id=committee.validator_committee_id,
        parent_round_id=parent_round_id,
        trigger_consensus_id=trigger_consensus_id,
        status=status,
        source_fingerprint=expected_fingerprint,
        created_at=existing.created_at if existing else now,
        finalized_at=None,
    )
    if existing:
        if existing.source_fingerprint != desired.source_fingerprint:
            raise ValidatorConsensusIntegrityError("Validation round source changed")
        if existing.status == ValidationRoundStatus.FINALIZED:
            return existing
        atomic_write_json(path, desired, temporary_prefix=".validation-round-")
        return desired
    if not atomic_create_json(path, desired, temporary_prefix=".validation-round-create-"):
        raced = load_validation_round(protocol_data_root, committee.project_id, committee.routing_id, round_id)
        if raced is None or raced.source_fingerprint != desired.source_fingerprint:
            raise ValidatorConsensusIntegrityError("Conflicting validation round exists")
        return raced
    return desired


def load_validation_round(protocol_data_root: Path, project_id: str, routing_id: str, round_id: str) -> ValidationRound | None:
    path = get_validation_round_path(protocol_data_root, routing_id, round_id)
    if not path.exists():
        return None
    return _load_model(path, ValidationRound, project_id, routing_id, "validation round")


def _load_model(path: Path, model, project_id: str, routing_id: str, label: str):
    try:
        value = model.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ValidatorProtocolStorageError(f"Stored {label} is malformed") from exc
    if value.project_id != project_id or value.routing_id != routing_id:
        raise ValidatorProtocolRelationshipError(f"Stored {label} belongs to another project or routing")
    return value


def _collect_evidence(
    protocol_data_root: Path,
    project_workspace: Path,
    *,
    project_id: str,
    routing_id: str,
    finding_cluster_id: str,
    included_round_ids: list[str] | None = None,
    mutate_rounds: bool = True,
) -> _Evidence:
    cluster = load_finding_cluster(protocol_data_root, project_id, routing_id, finding_cluster_id)
    if cluster is None:
        raise ValidatorProtocolNotFoundError("Finding cluster not found")
    reporting_operators: set[str] = set()
    for member in cluster.members:
        reporter = load_node(protocol_data_root, member.node_id)
        if reporter is None:
            raise ValidatorConsensusIntegrityError(
                "Finding cluster reporter is missing from NodeRegistry"
            )
        reporting_operators.add(reporter.operator_id)
    committees = [
        item for item in list_validator_committees(
            protocol_data_root, project_id, routing_id, finding_cluster_id=finding_cluster_id
        ) if item.status == ValidatorCommitteeStatus.FINALIZED
    ]
    committees.sort(key=lambda item: (item.validation_round, item.validator_committee_id))
    if not committees or committees[0].validation_round != 1:
        raise ValidatorConsensusError("Consensus requires a finalized Round 1 committee")
    round_numbers = [item.validation_round for item in committees]
    if round_numbers != list(range(1, max(round_numbers) + 1)) or len(set(round_numbers)) != len(round_numbers):
        raise ValidatorConsensusIntegrityError("Validation committee rounds are not unique and contiguous")
    rounds: list[ValidationRound] = []
    previous_id = None
    for committee in committees:
        round_id = build_validation_round_id(project_id, routing_id, finding_cluster_id, committee.validation_round)
        stored = load_validation_round(protocol_data_root, project_id, routing_id, round_id)
        trigger = stored.trigger_consensus_id if stored else None
        if mutate_rounds:
            current = _ensure_round(
                protocol_data_root, committee, parent_round_id=previous_id,
                trigger_consensus_id=trigger, status=ValidationRoundStatus.EVIDENCE_READY,
            )
        else:
            if stored is None:
                raise ValidatorConsensusIntegrityError("Validation round is missing")
            expected = protocol_fingerprint(
                _round_payload(
                    committee,
                    parent_round_id=previous_id,
                    trigger_consensus_id=trigger,
                )
            )
            if stored.source_fingerprint != expected:
                raise ValidatorConsensusIntegrityError("Validation round source changed")
            current = stored
        rounds.append(current)
        previous_id = current.validation_round_id
    if included_round_ids is not None:
        expected = sorted(included_round_ids)
        selected = [round_ for round_ in rounds if round_.validation_round_id in set(expected)]
        if sorted(item.validation_round_id for item in selected) != expected:
            raise ValidatorConsensusStaleError("Consensus included round set changed")
        rounds = sorted(selected, key=lambda item: item.round_number)
        committees = [
            next(item for item in committees if item.validator_committee_id == round_.validator_committee_id)
            for round_ in rounds
        ]

    assignments: list[ValidatorAssignment] = []
    operators: set[str] = set()
    prior_round_operators: set[str] = set()
    for committee in committees:
        if len(committee.authoritative_seats) != committee.authoritative_target_size:
            raise ValidatorConsensusIntegrityError("Committee target does not match authoritative seats")
        if committee.validation_round > 1:
            new_operators = {
                seat.operator_id for seat in committee.authoritative_seats
            }
            if new_operators & prior_round_operators:
                raise ValidatorConsensusIntegrityError(
                    "Escalation reused a prior-round validator operator"
                )
            if not prior_round_operators.issubset(
                set(committee.excluded_operator_ids)
            ):
                raise ValidatorConsensusIntegrityError(
                    "Escalation committee omitted prior operators from exclusions"
                )
        for seat in committee.authoritative_seats:
            if seat.operator_id in operators:
                raise ValidatorConsensusIntegrityError("Authoritative operator identity is duplicated across included rounds")
            operators.add(seat.operator_id)
            if seat.operator_id in reporting_operators:
                raise ValidatorConsensusIntegrityError(
                    "Reporting operator cannot contribute authoritative consensus"
                )
            assignment = load_validator_assignment(protocol_data_root, project_id, routing_id, seat.validator_assignment_id)
            if assignment is None:
                raise ValidatorConsensusIntegrityError("Authoritative assignment is missing")
            if not (
                assignment.validator_committee_id == committee.validator_committee_id
                and assignment.finding_cluster_id == finding_cluster_id
                and assignment.validation_round == committee.validation_round
                and assignment.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE
                and assignment.validator_node_id == seat.validator_node_id
                and assignment.validator_operator_id == seat.operator_id
            ):
                raise ValidatorConsensusIntegrityError("Authoritative assignment attribution is inconsistent")
            node = load_node(protocol_data_root, assignment.validator_node_id)
            if node is None or node.operator_id != assignment.validator_operator_id:
                raise ValidatorConsensusIntegrityError("Current authoritative operator mapping is inconsistent")
            assignments.append(assignment)
        prior_round_operators.update(
            seat.operator_id
            for seat in committee.authoritative_seats + committee.shadow_seats
        )

    assignment_by_id = {item.validator_assignment_id: item for item in assignments}
    all_reproductions = list_validator_reproduction_records(
        protocol_data_root, project_id, routing_id, finding_cluster_id=finding_cluster_id
    )
    reproductions: list[ValidatorReproductionRecord] = []
    reproduction_by_assignment: dict[str, ValidatorReproductionRecord] = {}
    for record in all_reproductions:
        if record.validator_assignment_id not in assignment_by_id:
            continue
        if record.validator_assignment_id in reproduction_by_assignment:
            raise ValidatorConsensusIntegrityError("Duplicate authoritative reproduction records")
        assignment = assignment_by_id[record.validator_assignment_id]
        if not (
            record.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE
            and record.validator_committee_id == assignment.validator_committee_id
            and record.validator_operator_id == assignment.validator_operator_id
            and record.validator_node_id == assignment.validator_node_id
            and record.finding_cluster_id == finding_cluster_id
        ):
            raise ValidatorConsensusIntegrityError("Authoritative reproduction attribution is inconsistent")
        try:
            validate_validator_reproduction_current(
                project_workspace, record, assignment_source_fingerprint=assignment.source_fingerprint
            )
        except ValidatorProtocolConflictError as exc:
            raise ValidatorConsensusIntegrityError(str(exc)) from exc
        reproduction_by_assignment[record.validator_assignment_id] = record
        reproductions.append(record)

    all_attestations = list_validation_attestations(
        protocol_data_root, project_id, routing_id, finding_cluster_id=finding_cluster_id
    )
    attestations: list[ValidationAttestation] = []
    seen_assignments: set[str] = set()
    for attestation in all_attestations:
        if attestation.validator_assignment_id not in assignment_by_id:
            continue
        if attestation.validator_assignment_id in seen_assignments:
            raise ValidatorConsensusIntegrityError("Duplicate finalized authoritative attestations")
        assignment = assignment_by_id[attestation.validator_assignment_id]
        reproduction = reproduction_by_assignment.get(attestation.validator_assignment_id)
        if reproduction is None:
            raise ValidatorConsensusIntegrityError("Attestation lacks authoritative reproduction evidence")
        if not (
            attestation.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE
            and attestation.validator_operator_id == assignment.validator_operator_id
            and attestation.validator_node_id == assignment.validator_node_id
            and attestation.finding_cluster_id == finding_cluster_id
            and attestation.validator_reproduction_id == reproduction.validator_reproduction_id
            and attestation.reproduction_decision == reproduction.reproduction_status
        ):
            raise ValidatorConsensusIntegrityError("Authoritative attestation attribution is inconsistent")
        payload = build_attestation_source_payload(
            assignment_source_fingerprint=assignment.source_fingerprint,
            reproduction_source_fingerprint=reproduction.source_fingerprint,
            validator_assignment_id=assignment.validator_assignment_id,
            project_id=project_id,
            routing_id=routing_id,
            finding_cluster_id=finding_cluster_id,
            validator_node_id=attestation.validator_node_id,
            validator_operator_id=attestation.validator_operator_id,
            assignment_role=attestation.assignment_role.value,
            validity_decision=attestation.validity_decision.value,
            root_cause_decision=attestation.root_cause_decision.value,
            reproduction_decision=attestation.reproduction_decision.value,
            normalized_severity=(attestation.normalized_severity.value if attestation.normalized_severity else None),
            impact_decision=attestation.impact_decision.value,
            reason_codes=[item.value for item in attestation.reason_codes],
            evidence_references=attestation.evidence_references,
            validator_reproduction_id=attestation.validator_reproduction_id,
        )
        if protocol_fingerprint(payload) != attestation.source_fingerprint:
            raise ValidatorConsensusIntegrityError("Attestation fingerprint is inconsistent")
        seen_assignments.add(attestation.validator_assignment_id)
        attestations.append(attestation)
    return _Evidence(
        rounds=rounds,
        committees=committees,
        assignments=sorted(assignments, key=lambda item: (item.validation_round, item.seat_index or 0)),
        attestations=sorted(attestations, key=lambda item: item.validator_assignment_id),
        reproductions=sorted(reproductions, key=lambda item: item.validator_assignment_id),
        target_size=sum(item.authoritative_target_size for item in committees),
    )


def _component(
    component_type: ConsensusComponentType,
    rows: list[tuple[str, str, str, str | None]],
    target_size: int,
    policy: ValidatorConsensusPolicyV1,
) -> ConsensusComponentResult:
    quorum = quorum_required(target_size, policy)
    threshold = supermajority_required(target_size, policy)
    grouped: dict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
    for value, operator_id, attestation_id, reproduction_id in rows:
        grouped[value].append((operator_id, attestation_id, reproduction_id))
    winners = sorted(value for value, evidence in grouped.items() if len(evidence) >= threshold)
    if len(winners) > 1:
        raise ValidatorConsensusIntegrityError("Mutually exclusive component outcomes both reached threshold")
    winner = winners[0] if winners else None
    all_attestations = sorted({attestation_id for _, _, attestation_id, _ in rows if attestation_id})
    supporters = sorted({item[1] for item in grouped.get(winner, []) if item[1]}) if winner else []
    return ConsensusComponentResult(
        component_type=component_type,
        authoritative_target_size=target_size,
        valid_response_count=len(rows),
        quorum_required=quorum,
        supermajority_required=threshold,
        value_counts={value: len(items) for value, items in sorted(grouped.items())},
        value_evidence=[
            ConsensusValueEvidence(
                value=value,
                count=len(items),
                operator_ids=[item[0] for item in items],
                attestation_ids=[item[1] for item in items if item[1]],
                reproduction_record_ids=[item[2] for item in items if item[2]],
            )
            for value, items in sorted(grouped.items())
        ],
        consensus_reached=winner is not None,
        consensus_value=winner,
        supporting_attestation_ids=supporters,
        dissenting_attestation_ids=sorted(set(all_attestations) - set(supporters)),
        unresolved_reason=(None if winner else (ConsensusUnresolvedReason.NO_QUORUM if len(rows) < quorum else ConsensusUnresolvedReason.NO_SUPERMAJORITY)),
    )


def build_component_results(evidence: _Evidence, policy: ValidatorConsensusPolicyV1) -> dict[ConsensusComponentType, ConsensusComponentResult]:
    attestations = evidence.attestations
    reproduction_attestation = {item.validator_reproduction_id: item.attestation_id for item in attestations}
    return {
        ConsensusComponentType.VALIDITY: _component(ConsensusComponentType.VALIDITY, [(a.validity_decision.value, a.validator_operator_id, a.attestation_id, None) for a in attestations], evidence.target_size, policy),
        ConsensusComponentType.REPRODUCTION: _component(ConsensusComponentType.REPRODUCTION, [(r.reproduction_status.value, r.validator_operator_id, reproduction_attestation.get(r.validator_reproduction_id, ""), r.validator_reproduction_id) for r in evidence.reproductions], evidence.target_size, policy),
        ConsensusComponentType.ROOT_CAUSE: _component(ConsensusComponentType.ROOT_CAUSE, [(a.root_cause_decision.value, a.validator_operator_id, a.attestation_id, None) for a in attestations], evidence.target_size, policy),
        ConsensusComponentType.SEVERITY: _component(
            ConsensusComponentType.SEVERITY,
            [
                (a.normalized_severity.value, a.validator_operator_id, a.attestation_id, None)
                for a in attestations
                if a.validity_decision == ValidationStatus.ACCEPTED
                and a.normalized_severity is not None
            ],
            evidence.target_size,
            policy,
        ),
        ConsensusComponentType.IMPACT: _component(ConsensusComponentType.IMPACT, [(a.impact_decision.value, a.validator_operator_id, a.attestation_id, None) for a in attestations], evidence.target_size, policy),
    }


def derive_overall_outcome(
    components: dict[ConsensusComponentType, ConsensusComponentResult],
    *, target_size: int, attestation_count: int, policy: ValidatorConsensusPolicyV1,
) -> tuple[ValidationConsensusOutcome, list[ValidationDisputeReason], str | None]:
    if attestation_count < quorum_required(target_size, policy):
        return ValidationConsensusOutcome.NO_QUORUM, [ValidationDisputeReason.NO_QUORUM], None
    validity = components[ConsensusComponentType.VALIDITY]
    if not validity.consensus_reached:
        return ValidationConsensusOutcome.DISPUTED, [ValidationDisputeReason.VALIDITY_DISAGREEMENT], None
    terminal = {
        ValidationStatus.REJECTED.value: ValidationConsensusOutcome.REJECTED,
        ValidationStatus.OUT_OF_SCOPE.value: ValidationConsensusOutcome.OUT_OF_SCOPE,
        ValidationStatus.INSUFFICIENT_EVIDENCE.value: ValidationConsensusOutcome.INSUFFICIENT_EVIDENCE,
        ValidationStatus.UNSAFE_POC.value: ValidationConsensusOutcome.UNSAFE,
        ValidationStatus.UNSUPPORTED.value: ValidationConsensusOutcome.UNSUPPORTED,
    }
    if validity.consensus_value in terminal:
        return terminal[validity.consensus_value], [], None
    if validity.consensus_value != ValidationStatus.ACCEPTED.value:
        raise ValidatorConsensusIntegrityError("Unsupported terminal validity consensus")
    required = [
        (ConsensusComponentType.REPRODUCTION, "reproduced", ValidationDisputeReason.REPRODUCTION_DISAGREEMENT),
        (ConsensusComponentType.ROOT_CAUSE, RootCauseDecision.CONFIRMED.value, ValidationDisputeReason.ROOT_CAUSE_DISAGREEMENT),
        (ConsensusComponentType.IMPACT, ImpactDecision.VALIDATED.value, ValidationDisputeReason.IMPACT_DISAGREEMENT),
    ]
    reasons = [reason for component, value, reason in required if components[component].consensus_value != value]
    severity = components[ConsensusComponentType.SEVERITY]
    if not severity.consensus_reached:
        reasons.append(ValidationDisputeReason.SEVERITY_DISAGREEMENT)
    if reasons:
        return ValidationConsensusOutcome.DISPUTED, sorted(set(reasons), key=lambda item: item.value), None
    return ValidationConsensusOutcome.CONFIRMED, [], severity.consensus_value


def _source_payload(cluster, evidence: _Evidence, policy: ValidatorConsensusPolicyV1) -> dict:
    return {
        "consensus_protocol_version": VALIDATION_CONSENSUS_VERSION,
        "consensus_policy": policy,
        "project_id": cluster.project_id,
        "routing_id": cluster.routing_id,
        "finding_cluster_id": cluster.finding_cluster_id,
        "finding_cluster_source_fingerprint": cluster.source_fingerprint,
        "included_rounds": [{"id": r.validation_round_id, "number": r.round_number, "fingerprint": r.source_fingerprint} for r in evidence.rounds],
        "included_committees": [{"id": c.validator_committee_id, "target": c.authoritative_target_size, "fingerprint": c.source_fingerprint} for c in evidence.committees],
        "assignments": [{"id": a.validator_assignment_id, "round": a.validation_round, "node": a.validator_node_id, "operator": a.validator_operator_id, "role": a.assignment_role.value, "fingerprint": a.source_fingerprint} for a in evidence.assignments],
        "attestations": [{"id": a.attestation_id, "fingerprint": a.source_fingerprint, "validity": a.validity_decision.value, "root_cause": a.root_cause_decision.value, "reproduction": a.reproduction_decision.value, "severity": a.normalized_severity.value if a.normalized_severity else None, "impact": a.impact_decision.value} for a in evidence.attestations],
        "reproductions": [{"id": r.validator_reproduction_id, "fingerprint": r.source_fingerprint, "status": r.reproduction_status.value} for r in evidence.reproductions],
        "authoritative_target_size": evidence.target_size,
        "quorum_required": quorum_required(evidence.target_size, policy),
        "supermajority_required": supermajority_required(evidence.target_size, policy),
        "reproduction_requirement": "required",
    }


def calculate_validation_consensus(
    protocol_data_root: Path, project_workspace: Path, *, project_id: str, routing_id: str,
    finding_cluster_id: str, policy: ValidatorConsensusPolicyV1 | None = None,
    included_round_ids: list[str] | None = None,
    persist: bool = True,
) -> ValidationConsensus:
    policy = policy or ValidatorConsensusPolicyV1()
    cluster = load_finding_cluster(protocol_data_root, project_id, routing_id, finding_cluster_id)
    if cluster is None:
        raise ValidatorProtocolNotFoundError("Finding cluster not found")
    evidence = _collect_evidence(
        protocol_data_root, project_workspace, project_id=project_id,
        routing_id=routing_id, finding_cluster_id=finding_cluster_id,
        included_round_ids=included_round_ids, mutate_rounds=persist,
    )
    components = build_component_results(evidence, policy)
    outcome, reasons, severity = derive_overall_outcome(components, target_size=evidence.target_size, attestation_count=len(evidence.attestations), policy=policy)
    source_fingerprint = protocol_fingerprint(_source_payload(cluster, evidence, policy))
    calculation_payload = _calculation_payload(
        source_fingerprint=source_fingerprint,
        components=components,
        outcome=outcome,
        severity=severity,
        reasons=reasons,
    )
    consensus_id = "validation_consensus_" + protocol_fingerprint({
        "consensus_protocol_version": VALIDATION_CONSENSUS_VERSION,
        "finding_cluster_id": finding_cluster_id,
        "included_round_ids": sorted(item.validation_round_id for item in evidence.rounds),
        "policy_version": policy.policy_version,
        "source_fingerprint": source_fingerprint,
    })
    now = _utc_now()
    consensus = ValidationConsensus(
        validation_consensus_id=consensus_id,
        consensus_policy=policy,
        project_id=project_id,
        routing_id=routing_id,
        finding_cluster_id=finding_cluster_id,
        validation_round_number=max(item.round_number for item in evidence.rounds),
        included_round_ids=[item.validation_round_id for item in evidence.rounds],
        included_committee_ids=[item.validator_committee_id for item in evidence.committees],
        authoritative_target_size=evidence.target_size,
        valid_authoritative_attestation_count=len(evidence.attestations),
        quorum_required=quorum_required(evidence.target_size, policy),
        supermajority_required=supermajority_required(evidence.target_size, policy),
        lifecycle_status=ValidationConsensusLifecycle.CALCULATED,
        consensus_outcome=outcome,
        validity_result=components[ConsensusComponentType.VALIDITY],
        reproduction_result=components[ConsensusComponentType.REPRODUCTION],
        root_cause_result=components[ConsensusComponentType.ROOT_CAUSE],
        severity_result=components[ConsensusComponentType.SEVERITY],
        impact_result=components[ConsensusComponentType.IMPACT],
        final_normalized_severity=severity,
        dispute_reason_codes=reasons,
        source_fingerprint=source_fingerprint,
        calculation_fingerprint=protocol_fingerprint(calculation_payload),
        created_at=now,
        calculated_at=now,
    )
    if not persist:
        return consensus
    path = get_validation_consensus_path(protocol_data_root, routing_id, consensus_id)
    existing = load_validation_consensus(protocol_data_root, project_id, routing_id, consensus_id)
    if existing:
        return existing
    if not atomic_create_json(path, consensus, temporary_prefix=".validation-consensus-create-"):
        raced = load_validation_consensus(protocol_data_root, project_id, routing_id, consensus_id)
        if raced is None or raced.calculation_fingerprint != consensus.calculation_fingerprint:
            raise ValidatorConsensusError("Conflicting semantic consensus exists")
        return raced
    return consensus


def load_validation_consensus(protocol_data_root: Path, project_id: str, routing_id: str, consensus_id: str) -> ValidationConsensus | None:
    path = get_validation_consensus_path(protocol_data_root, routing_id, consensus_id)
    if not path.exists():
        return None
    return _load_model(path, ValidationConsensus, project_id, routing_id, "validation consensus")


def list_validation_consensus(protocol_data_root: Path, project_id: str, routing_id: str, *, finding_cluster_id: str | None = None) -> list[ValidationConsensus]:
    root = get_validator_protocol_root(protocol_data_root) / "consensus" / routing_id
    values = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        value = load_validation_consensus(protocol_data_root, project_id, routing_id, path.stem)
        if value and (finding_cluster_id is None or value.finding_cluster_id == finding_cluster_id):
            values.append(value)
    return sorted(values, key=lambda item: (item.finding_cluster_id, item.validation_round_number, item.calculated_at, item.validation_consensus_id))


def finalize_validation_consensus(
    protocol_data_root: Path, project_workspace: Path, *, project_id: str, routing_id: str,
    validation_consensus_id: str,
    policy: ValidatorConsensusPolicyV1 | None = None,
) -> ValidationConsensus:
    stored = load_validation_consensus(protocol_data_root, project_id, routing_id, validation_consensus_id)
    if stored is None:
        raise ValidatorProtocolNotFoundError("Validation consensus not found")
    current_policy = policy or ValidatorConsensusPolicyV1()
    if stored.consensus_policy != current_policy:
        raise ValidatorConsensusStaleError(
            "Consensus policy changed; calculate a new snapshot"
        )
    if stored.lifecycle_status == ValidationConsensusLifecycle.FINALIZED:
        _ensure_finalization_side_effects(protocol_data_root, stored)
        return stored
    current = calculate_validation_consensus(
        protocol_data_root, project_workspace, project_id=project_id, routing_id=routing_id,
        finding_cluster_id=stored.finding_cluster_id, policy=current_policy,
        included_round_ids=stored.included_round_ids, persist=False,
    )
    if current.source_fingerprint != stored.source_fingerprint or current.calculation_fingerprint != stored.calculation_fingerprint:
        raise ValidatorConsensusStaleError("Consensus evidence changed; calculate a new snapshot")
    now = _utc_now()
    finalized = stored.model_copy(update={"lifecycle_status": ValidationConsensusLifecycle.FINALIZED, "finalized_at": now})
    atomic_write_json(get_validation_consensus_path(protocol_data_root, routing_id, validation_consensus_id), finalized, temporary_prefix=".validation-consensus-finalize-")
    for round_id in finalized.included_round_ids:
        round_ = load_validation_round(protocol_data_root, project_id, routing_id, round_id)
        if round_ and round_.status != ValidationRoundStatus.FINALIZED:
            atomic_write_json(get_validation_round_path(protocol_data_root, routing_id, round_id), round_.model_copy(update={"status": ValidationRoundStatus.FINALIZED, "finalized_at": now}), temporary_prefix=".validation-round-finalize-")
    _ensure_finalization_side_effects(protocol_data_root, finalized)
    return finalized


def _ensure_finalization_side_effects(
    protocol_data_root: Path, consensus: ValidationConsensus
) -> None:
    unresolved = consensus.consensus_outcome in {
        ValidationConsensusOutcome.DISPUTED,
        ValidationConsensusOutcome.NO_QUORUM,
    }
    if consensus.validation_round_number == 1 and unresolved:
        _open_dispute(protocol_data_root, consensus)
    if consensus.validation_round_number > 1:
        _resolve_triggering_dispute(protocol_data_root, consensus)
    if not unresolved:
        attach_final_validator_consensus(
            protocol_data_root,
            project_id=consensus.project_id,
            routing_id=consensus.routing_id,
            finding_cluster_id=consensus.finding_cluster_id,
            validation_consensus_id=consensus.validation_consensus_id,
            consensus_outcome=consensus.consensus_outcome.value,
            consensus_severity=consensus.final_normalized_severity,
        )


def _open_dispute(protocol_data_root: Path, consensus: ValidationConsensus) -> ValidationDispute:
    dispute_id = "validation_dispute_" + protocol_fingerprint({"dispute_version": VALIDATION_DISPUTE_VERSION, "triggering_consensus_id": consensus.validation_consensus_id})
    escalation_policy = ValidationEscalationPolicyV1()
    payload = {"dispute_version": VALIDATION_DISPUTE_VERSION, "triggering_consensus_id": consensus.validation_consensus_id, "triggering_round_id": consensus.included_round_ids[-1], "reason_codes": [item.value for item in consensus.dispute_reason_codes], "escalation_policy": escalation_policy}
    now = _utc_now()
    dispute = ValidationDispute(
        validation_dispute_id=dispute_id, project_id=consensus.project_id, routing_id=consensus.routing_id,
        finding_cluster_id=consensus.finding_cluster_id, triggering_consensus_id=consensus.validation_consensus_id,
        triggering_round_id=consensus.included_round_ids[-1], reason_codes=consensus.dispute_reason_codes,
        escalation_policy=escalation_policy, status=ValidationDisputeStatus.OPEN,
        source_fingerprint=protocol_fingerprint(payload), created_at=now, updated_at=now,
    )
    path = get_validation_dispute_path(protocol_data_root, consensus.routing_id, dispute_id)
    if atomic_create_json(path, dispute, temporary_prefix=".validation-dispute-create-"):
        return dispute
    existing = load_validation_dispute(protocol_data_root, consensus.project_id, consensus.routing_id, dispute_id)
    if existing is None or existing.source_fingerprint != dispute.source_fingerprint:
        raise ValidatorConsensusError("Conflicting dispute record exists")
    return existing


def load_validation_dispute(protocol_data_root: Path, project_id: str, routing_id: str, dispute_id: str) -> ValidationDispute | None:
    path = get_validation_dispute_path(protocol_data_root, routing_id, dispute_id)
    if not path.exists():
        return None
    return _load_model(path, ValidationDispute, project_id, routing_id, "validation dispute")


def list_validation_disputes(protocol_data_root: Path, project_id: str, routing_id: str, *, finding_cluster_id: str | None = None) -> list[ValidationDispute]:
    root = get_validator_protocol_root(protocol_data_root) / "disputes" / routing_id
    values = []
    for path in sorted(root.glob("*.json")) if root.exists() else []:
        value = load_validation_dispute(protocol_data_root, project_id, routing_id, path.stem)
        if value and (finding_cluster_id is None or value.finding_cluster_id == finding_cluster_id):
            values.append(value)
    return sorted(values, key=lambda item: (item.created_at, item.validation_dispute_id))


def escalate_validation_dispute(
    protocol_data_root: Path, *, project_id: str, routing_id: str, validation_dispute_id: str,
    policy: ValidationEscalationPolicyV1 | None = None,
) -> ValidationEscalationResult:
    policy = policy or ValidationEscalationPolicyV1()
    dispute = load_validation_dispute(protocol_data_root, project_id, routing_id, validation_dispute_id)
    if dispute is None:
        raise ValidatorProtocolNotFoundError("Validation dispute not found")
    if dispute.escalation_policy != policy:
        raise ValidatorConsensusStaleError(
            "Escalation policy changed; dispute cannot be escalated"
        )
    if dispute.escalation_round_id and dispute.status in {
        ValidationDisputeStatus.ESCALATED,
        ValidationDisputeStatus.ESCALATION_PLANNED,
    }:
        round_ = load_validation_round(protocol_data_root, project_id, routing_id, dispute.escalation_round_id)
        return ValidationEscalationResult(dispute=dispute, validation_round=round_, validator_committee_id=dispute.escalation_committee_id)
    if dispute.escalation_round_id:
        raise ValidatorConsensusError(
            "Maximum escalation rounds already consumed"
        )
    if dispute.status != ValidationDisputeStatus.OPEN:
        raise ValidatorConsensusError("Dispute is not eligible for escalation")
    triggering = load_validation_consensus(protocol_data_root, project_id, routing_id, dispute.triggering_consensus_id)
    if triggering is None or triggering.lifecycle_status != ValidationConsensusLifecycle.FINALIZED:
        raise ValidatorConsensusError("Escalation requires finalized triggering consensus")
    if triggering.validation_round_number > policy.max_escalation_rounds:
        raise ValidatorConsensusError("Maximum escalation rounds already consumed")
    prior_committees = [load_validator_committee(protocol_data_root, project_id, routing_id, committee_id) for committee_id in triggering.included_committee_ids]
    if any(item is None for item in prior_committees):
        raise ValidatorConsensusIntegrityError("Prior committee is missing")
    excluded = sorted({seat.operator_id for committee in prior_committees if committee for seat in committee.authoritative_seats + committee.shadow_seats})
    target = policy.additional_authoritative_validators
    committee_policy = ValidatorCommitteePolicyV1(
        standard_authoritative_size=target, high_assurance_authoritative_size=target,
        standard_shadow_slots=policy.shadow_slots, high_assurance_shadow_slots=policy.shadow_slots,
    )
    try:
        committee = plan_validator_committee(
            protocol_data_root, project_id=project_id, routing_id=routing_id,
            finding_cluster_id=dispute.finding_cluster_id,
            assurance_mode=prior_committees[0].assurance_mode,
            validation_round=2, policy=committee_policy, excluded_operator_ids=excluded,
            escalation_authorized=True,
        )
        committee = finalize_validator_committee(
            protocol_data_root, project_id=project_id, routing_id=routing_id,
            validator_committee_id=committee.validator_committee_id, policy=committee_policy,
        ).committee
    except ValidatorCommitteeInsufficientValidatorsError:
        blocked = dispute.model_copy(update={"status": ValidationDisputeStatus.BLOCKED_INSUFFICIENT_VALIDATORS, "updated_at": _utc_now()})
        atomic_write_json(get_validation_dispute_path(protocol_data_root, routing_id, validation_dispute_id), blocked, temporary_prefix=".validation-dispute-blocked-")
        return ValidationEscalationResult(dispute=blocked)
    parent_round = load_validation_round(protocol_data_root, project_id, routing_id, dispute.triggering_round_id)
    if parent_round is None:
        raise ValidatorConsensusIntegrityError("Triggering validation round is missing")
    round_ = _ensure_round(protocol_data_root, committee, parent_round_id=parent_round.validation_round_id, trigger_consensus_id=triggering.validation_consensus_id, status=ValidationRoundStatus.OPEN)
    updated = dispute.model_copy(update={
        "status": ValidationDisputeStatus.ESCALATED,
        "escalation_round_id": round_.validation_round_id,
        "escalation_committee_id": committee.validator_committee_id,
        "updated_at": _utc_now(),
    })
    atomic_write_json(get_validation_dispute_path(protocol_data_root, routing_id, validation_dispute_id), updated, temporary_prefix=".validation-dispute-escalate-")
    return ValidationEscalationResult(dispute=updated, validation_round=round_, validator_committee_id=committee.validator_committee_id)


def _resolve_triggering_dispute(protocol_data_root: Path, consensus: ValidationConsensus) -> None:
    for dispute in list_validation_disputes(protocol_data_root, consensus.project_id, consensus.routing_id, finding_cluster_id=consensus.finding_cluster_id):
        if dispute.escalation_round_id not in consensus.included_round_ids:
            continue
        resolved = consensus.consensus_outcome not in {ValidationConsensusOutcome.DISPUTED, ValidationConsensusOutcome.NO_QUORUM}
        desired_status = (
            ValidationDisputeStatus.RESOLVED
            if resolved
            else ValidationDisputeStatus.UNRESOLVED
        )
        if (
            dispute.status == desired_status
            and dispute.resolution_consensus_id == consensus.validation_consensus_id
        ):
            continue
        updated = dispute.model_copy(update={
            "status": desired_status,
            "resolution_consensus_id": consensus.validation_consensus_id,
            "updated_at": _utc_now(),
            "resolved_at": _utc_now(),
        })
        atomic_write_json(get_validation_dispute_path(protocol_data_root, consensus.routing_id, dispute.validation_dispute_id), updated, temporary_prefix=".validation-dispute-resolve-")


def verify_validation_consensus(
    protocol_data_root: Path, project_workspace: Path, *, project_id: str, routing_id: str,
    validation_consensus_id: str,
) -> ConsensusVerificationResult:
    errors: list[str] = []
    checks = {"identity": False, "source_fingerprint": False, "calculation_fingerprint": False, "thresholds": False, "outcome": False, "operator_diversity": False, "shadow_excluded": False}
    stored = load_validation_consensus(protocol_data_root, project_id, routing_id, validation_consensus_id)
    if stored is None:
        raise ValidatorProtocolNotFoundError("Validation consensus not found")
    checks["identity"] = True
    try:
        evidence = _collect_evidence(
            protocol_data_root, project_workspace, project_id=project_id,
            routing_id=routing_id, finding_cluster_id=stored.finding_cluster_id,
            included_round_ids=stored.included_round_ids, mutate_rounds=False,
        )
        checks["operator_diversity"] = len({a.validator_operator_id for a in evidence.assignments}) == len(evidence.assignments)
        checks["shadow_excluded"] = all(a.assignment_role == ValidatorAssignmentRole.AUTHORITATIVE for a in evidence.assignments)
        current = calculate_validation_consensus(protocol_data_root, project_workspace, project_id=project_id, routing_id=routing_id, finding_cluster_id=stored.finding_cluster_id, policy=stored.consensus_policy, included_round_ids=stored.included_round_ids, persist=False)
        checks["source_fingerprint"] = current.source_fingerprint == stored.source_fingerprint
        stored_components = {
            ConsensusComponentType.VALIDITY: stored.validity_result,
            ConsensusComponentType.REPRODUCTION: stored.reproduction_result,
            ConsensusComponentType.ROOT_CAUSE: stored.root_cause_result,
            ConsensusComponentType.SEVERITY: stored.severity_result,
            ConsensusComponentType.IMPACT: stored.impact_result,
        }
        stored_calculation_fingerprint = protocol_fingerprint(
            _calculation_payload(
                source_fingerprint=stored.source_fingerprint,
                components=stored_components,
                outcome=stored.consensus_outcome,
                severity=(
                    stored.final_normalized_severity.value
                    if stored.final_normalized_severity
                    else None
                ),
                reasons=stored.dispute_reason_codes,
            )
        )
        checks["calculation_fingerprint"] = (
            current.calculation_fingerprint == stored.calculation_fingerprint
            and stored_calculation_fingerprint == stored.calculation_fingerprint
        )
        checks["thresholds"] = current.quorum_required == stored.quorum_required and current.supermajority_required == stored.supermajority_required
        checks["outcome"] = current.consensus_outcome == stored.consensus_outcome and current.final_normalized_severity == stored.final_normalized_severity
    except (ValidatorConsensusError, ValidatorProtocolRelationshipError, ValidatorProtocolStorageError) as exc:
        errors.append(str(exc))
    if not all(checks.values()) and not errors:
        errors.append("Stored consensus does not match deterministic authoritative evidence")
    return ConsensusVerificationResult(validation_consensus_id=validation_consensus_id, valid=all(checks.values()), checks=checks, errors=errors)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _calculation_payload(
    *,
    source_fingerprint: str,
    components: dict[ConsensusComponentType, ConsensusComponentResult],
    outcome: ValidationConsensusOutcome,
    severity: str | None,
    reasons: Iterable[ValidationDisputeReason],
) -> dict:
    return {
        "source_fingerprint": source_fingerprint,
        "components": {key.value: value for key, value in components.items()},
        "consensus_outcome": outcome.value,
        "final_normalized_severity": severity,
        "dispute_reason_codes": [item.value for item in reasons],
    }
