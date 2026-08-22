from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.validator_attestation import ValidationAttestationRequest
from app.services.validation_attestation_service import build_attestation_source_payload
from app.utils.protocol_serialization import protocol_fingerprint


def _payload():
    return {
        "assignment_source_fingerprint": "a" * 64,
        "reproduction_source_fingerprint": "b" * 64,
        "validator_assignment_id": "assignment-1",
        "project_id": "project-1",
        "routing_id": "routing-1",
        "finding_cluster_id": "finding-cluster-1",
        "validator_node_id": "validator-node-1",
        "validator_operator_id": "validator-operator-1",
        "assignment_role": "authoritative",
        "validity_decision": "accepted",
        "root_cause_decision": "confirmed",
        "reproduction_decision": "reproduced",
        "normalized_severity": "High",
        "impact_decision": "validated",
        "reason_codes": ["root_cause_confirmed", "reproduction_confirmed"],
        "evidence_references": ["result-1", "cluster-1"],
        "validator_reproduction_id": "validator-reproduction-1",
    }


def _request(**updates):
    values = {
        "validator_node_id": "validator-node-1",
        "validator_reproduction_id": "validator-reproduction-1",
        "validity_decision": "accepted",
        "root_cause_decision": "confirmed",
        "normalized_severity": "High",
        "impact_decision": "validated",
        "reason_codes": ["reproduction_confirmed", "root_cause_confirmed"],
        "evidence_references": ["result-1", "cluster-1"],
    }
    values.update(updates)
    return ValidationAttestationRequest(**values)


def test_reason_and_evidence_order_are_canonical_and_deduplicated():
    first = _request()
    second = _request(
        reason_codes=[
            "root_cause_confirmed",
            "reproduction_confirmed",
            "root_cause_confirmed",
        ],
        evidence_references=["cluster-1", "result-1", "cluster-1"],
    )
    assert first.reason_codes == second.reason_codes
    assert first.evidence_references == second.evidence_references


@pytest.mark.parametrize(
    "field,value",
    [
        ("validity_decision", "duplicate"),
        ("validity_decision", "needs_review"),
        ("root_cause_decision", "yes"),
        ("impact_decision", "maybe"),
        ("normalized_severity", "Severe"),
        ("reason_codes", ["arbitrary_prose_code"]),
        ("evidence_references", ["../host-path"]),
    ],
)
def test_malformed_attestation_decisions_are_rejected(field, value):
    with pytest.raises(ValidationError):
        _request(**{field: value})


def test_accepted_requires_severity_and_rejected_forbids_severity():
    with pytest.raises(ValidationError):
        _request(normalized_severity=None)
    with pytest.raises(ValidationError):
        _request(validity_decision="rejected", normalized_severity="Critical")
    assert _request(
        validity_decision="rejected",
        normalized_severity=None,
        root_cause_decision="mismatch",
        impact_decision="rejected",
    ).normalized_severity is None


def test_attestation_fingerprint_is_order_independent_and_field_sensitive():
    first = build_attestation_source_payload(**_payload())
    reordered = _payload()
    reordered["reason_codes"] = list(reversed(reordered["reason_codes"]))
    reordered["evidence_references"] = list(reversed(reordered["evidence_references"]))
    assert protocol_fingerprint(first) == protocol_fingerprint(
        build_attestation_source_payload(**reordered)
    )

    for field, value in (
        ("validity_decision", "insufficient_evidence"),
        ("root_cause_decision", "mismatch"),
        ("reproduction_decision", "failed"),
        ("normalized_severity", "Medium"),
        ("impact_decision", "rejected"),
        ("validator_operator_id", "another-operator"),
        ("finding_cluster_id", "another-cluster"),
    ):
        changed = deepcopy(_payload())
        changed[field] = value
        assert protocol_fingerprint(first) != protocol_fingerprint(
            build_attestation_source_payload(**changed)
        )


def test_request_forbids_derived_identity_status_consensus_reward_and_private_key():
    for field, value in (
        ("validator_operator_id", "forged"),
        ("reproduction_decision", "reproduced"),
        ("consensus", "accepted"),
        ("cluster_final_severity", "Critical"),
        ("reward", "100"),
        ("private_key", "secret"),
    ):
        with pytest.raises(ValidationError):
            _request(**{field: value})
