import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.schemas.finding import Finding
from app.schemas.node import NodeCreate, NodeStatusChangeRequest
from app.schemas.submission import SubmissionCreate, SubmissionStatusUpdate
from app.services.node_registry_service import (
    NodeInactiveError,
    UnsupportedNodeCategoryError,
    change_node_status,
    create_node,
    load_node,
)
from app.services.submission_service import (
    DuplicateSubmissionError,
    FindingNotFoundForSubmissionError,
    InvalidSubmissionIdentifierError,
    InvalidSubmissionStatusTransitionError,
    NodeCannotSubmitError,
    SubmissionProjectMismatchError,
    attach_reproduction_reference,
    attach_validation_reference,
    build_canonical_finding_payload,
    canonical_json_bytes,
    compute_finding_hash,
    create_submission,
    get_submission_dir,
    list_submissions,
    load_submission,
    normalize_submission_path,
    normalize_submission_text,
    update_submission_status,
)


def _finding(project_id="project-1", finding_id="finding-1", **updates):
    values = {
        "finding_id": finding_id,
        "project_id": project_id,
        "title": "Missing access control on treasury setter",
        "category": "access_control",
        "severity": "High",
        "confidence": 0.8,
        "contracts": ["src/Treasury.sol"],
        "functions": ["setTreasury"],
        "root_cause": "Missing onlyOwner check",
        "attack_path": "Attacker calls setTreasury",
        "impact": "Unauthorized treasury modification",
        "recommended_fix": "Add onlyOwner authorization.",
        "agent_name": "access_control_agent",
    }
    values.update(updates)
    return Finding(**values)


def _write_findings(workspace, findings):
    findings_dir = workspace / "findings"
    findings_dir.mkdir(parents=True, exist_ok=True)
    (findings_dir / "findings.json").write_text(
        json.dumps([finding.model_dump(mode="json") for finding in findings], indent=2),
        encoding="utf-8",
    )


def _node_payload(name="agent-one", **updates):
    values = {
        "node_type": "agent",
        "display_name": name,
        "operator_id": f"operator-{name}",
        "supported_categories": ["access_control"],
    }
    values.update(updates)
    return NodeCreate(**values)


def _submission_payload(node_id, project_id="project-1", finding_id="finding-1", **updates):
    values = {
        "project_id": project_id,
        "finding_id": finding_id,
        "node_id": node_id,
        "agent_version": "0.1.0",
        "metadata": {"runtime": "local-mvp"},
    }
    values.update(updates)
    return SubmissionCreate(**values)


def _setup_submission(tmp_path, node_type="agent"):
    root = tmp_path / "protocol"
    workspace = tmp_path / "audit" / "project-1"
    finding = _finding()
    _write_findings(workspace, [finding])
    node = create_node(root, _node_payload(node_type=node_type))
    submission = create_submission(root, workspace, _submission_payload(node.node_id))
    return root, workspace, finding, node, submission


def test_text_and_path_normalizers_are_deterministic():
    assert normalize_submission_text(None) == ""
    assert normalize_submission_text(" A\r\n  B\tC ") == "a b c"
    assert normalize_submission_path(" ./src\\core//Vault.sol ") == "src/core/Vault.sol"


def test_canonical_payload_contains_only_hash_fields():
    payload = build_canonical_finding_payload("project-1", _finding())
    assert set(payload) == {
        "project_id",
        "category",
        "contracts",
        "functions",
        "root_cause",
        "attack_path",
        "impact",
    }


def test_canonical_json_is_sorted_compact_utf8():
    assert canonical_json_bytes({"z": "✓", "a": 1}) == '{"a":1,"z":"✓"}'.encode()


def test_hash_is_invariant_to_nonsemantic_representation_changes():
    baseline = {
        "finding_id": "finding-1",
        "category": "Access Control",
        "contracts": ["./src\\Vault.sol", "src/Token.sol", "src/Token.sol"],
        "functions": ["withdraw", " deposit "],
        "root_cause": " Missing   OWNER check ",
        "attack_path": "Caller\r\n invokes function",
        "impact": " Funds   move ",
        "agent_name": "agent-a",
        "confidence": 0.1,
        "status": "candidate",
    }
    equivalent = {
        "impact": "funds move",
        "attack_path": "caller invokes function",
        "root_cause": "missing owner check",
        "functions": ["deposit", "withdraw"],
        "contracts": ["src/Token.sol", "src/Vault.sol"],
        "category": "access_control",
        "finding_id": "different-id",
        "agent_name": "agent-b",
        "confidence": 0.99,
        "status": "rejected",
    }
    assert compute_finding_hash("project-1", baseline) == compute_finding_hash("project-1", equivalent)


@pytest.mark.parametrize(
    "change",
    [
        {"root_cause": "a different root cause"},
        {"contracts": ["src/Different.sol"]},
    ],
)
def test_content_changes_change_hash(change):
    original = _finding().model_dump(mode="json")
    changed = {**original, **change}
    assert compute_finding_hash("project-1", original) != compute_finding_hash("project-1", changed)


def test_project_id_changes_hash_but_identity_fields_do_not():
    original = _finding().model_dump(mode="json")
    changed_identity = {**original, "finding_id": "different", "agent_name": "different", "confidence": 0.1}
    assert compute_finding_hash("project-1", original) == compute_finding_hash("project-1", changed_identity)
    assert compute_finding_hash("project-1", original) != compute_finding_hash("project-2", original)


def test_create_submission_persists_service_defaults_and_derived_fields(tmp_path):
    root, _, _, node, submission = _setup_submission(tmp_path)
    path = root / "submissions" / submission.submission_id / "submission.json"
    assert path.is_file()
    assert submission.status.value == "submitted"
    assert submission.reward_status.value == "pending"
    assert submission.project_id == "project-1"
    assert submission.finding_id == "finding-1"
    assert submission.node_id == node.node_id
    assert submission.node_type == "agent"
    assert submission.category == "access_control"
    assert len(submission.finding_hash) == 64
    assert load_submission(root, submission.submission_id) == submission
    assert load_submission(root, "00000000-0000-0000-0000-000000000000") is None
    stored = path.read_text(encoding="utf-8")
    assert "\n  \"submission_id\"" in stored
    assert "/home/" not in stored
    assert "poc_content" not in stored
    assert "root_cause" not in stored


def test_agent_name_falls_back_to_finding(tmp_path):
    root, workspace, _, node, _ = _setup_submission(tmp_path)
    # The first setup submission consumes finding-1, so use equivalent content with a new node.
    other_node = create_node(root, _node_payload("agent-two"))
    created = create_submission(root, workspace, _submission_payload(other_node.node_id, agent_name=None))
    assert created.agent_name == "access_control_agent"


def test_list_submissions_filters_and_is_deterministic(tmp_path):
    root = tmp_path / "protocol"
    workspace = tmp_path / "project-1"
    findings = [_finding(), _finding(finding_id="finding-2", category="reentrancy", root_cause="External call before state update")]
    _write_findings(workspace, findings)
    first_node = create_node(root, _node_payload())
    second_node = create_node(root, _node_payload("hybrid-two", node_type="hybrid", supported_categories=["reentrancy"]))
    first = create_submission(root, workspace, _submission_payload(first_node.node_id))
    second = create_submission(root, workspace, _submission_payload(second_node.node_id, finding_id="finding-2"))
    second = update_submission_status(
        root,
        second.submission_id,
        SubmissionStatusUpdate(status="validation_pending", reason="Ready for validation."),
    )
    assert [item.submission_id for item in list_submissions(root)] == [first.submission_id, second.submission_id]
    assert list_submissions(root, project_id="missing") == []
    assert [item.submission_id for item in list_submissions(root, node_id=first_node.node_id)] == [first.submission_id]
    assert [item.submission_id for item in list_submissions(root, status="validation_pending")] == [second.submission_id]
    assert [item.submission_id for item in list_submissions(root, category="Reentrancy")] == [second.submission_id]
    assert len(list_submissions(root, reward_status="pending")) == 2


def test_create_rejects_missing_finding_node_and_project_mismatch(tmp_path):
    root = tmp_path / "protocol"
    workspace = tmp_path / "workspace"
    node = create_node(root, _node_payload())
    _write_findings(workspace, [_finding(project_id="other-project")])
    with pytest.raises(SubmissionProjectMismatchError):
        create_submission(root, workspace, _submission_payload(node.node_id))
    with pytest.raises(FindingNotFoundForSubmissionError):
        create_submission(root, workspace, _submission_payload(node.node_id, finding_id="missing"))
    _write_findings(workspace, [_finding()])
    with pytest.raises(Exception, match="Node not found"):
        create_submission(root, workspace, _submission_payload("00000000-0000-0000-0000-000000000000"))


@pytest.mark.parametrize("status", ["inactive", "suspended", "banned"])
def test_non_active_node_cannot_submit(tmp_path, status):
    root = tmp_path / "protocol"
    workspace = tmp_path / "workspace"
    _write_findings(workspace, [_finding()])
    node = create_node(root, _node_payload())
    change_node_status(root, node.node_id, NodeStatusChangeRequest(status=status, reason="Review."))
    with pytest.raises(NodeInactiveError):
        create_submission(root, workspace, _submission_payload(node.node_id))


def test_validator_rejected_and_agent_hybrid_accepted(tmp_path):
    root = tmp_path / "protocol"
    workspace = tmp_path / "workspace"
    _write_findings(workspace, [_finding()])
    validator = create_node(root, _node_payload("validator-one", node_type="validator"))
    with pytest.raises(NodeCannotSubmitError):
        create_submission(root, workspace, _submission_payload(validator.node_id))
    agent = create_node(root, _node_payload("agent-two"))
    hybrid = create_node(root, _node_payload("hybrid-three", node_type="hybrid"))
    assert create_submission(root, workspace, _submission_payload(agent.node_id)).node_type == "agent"
    assert create_submission(root, workspace, _submission_payload(hybrid.node_id)).node_type == "hybrid"


def test_unsupported_category_is_rejected(tmp_path):
    root = tmp_path / "protocol"
    workspace = tmp_path / "workspace"
    _write_findings(workspace, [_finding(category="reentrancy")])
    node = create_node(root, _node_payload())
    with pytest.raises(UnsupportedNodeCategoryError):
        create_submission(root, workspace, _submission_payload(node.node_id))


def test_duplicate_rule_allows_different_content_node_and_project(tmp_path):
    root = tmp_path / "protocol"
    workspace = tmp_path / "project-1"
    _write_findings(
        workspace,
        [_finding(), _finding(finding_id="finding-2"), _finding(finding_id="finding-3", impact="Different security impact")],
    )
    first_node = create_node(root, _node_payload())
    second_node = create_node(root, _node_payload("agent-two"))
    create_submission(root, workspace, _submission_payload(first_node.node_id))
    with pytest.raises(DuplicateSubmissionError):
        create_submission(root, workspace, _submission_payload(first_node.node_id, finding_id="finding-2"))
    assert create_submission(root, workspace, _submission_payload(first_node.node_id, finding_id="finding-3"))
    assert create_submission(root, workspace, _submission_payload(second_node.node_id, finding_id="finding-2"))

    other_workspace = tmp_path / "project-2"
    _write_findings(other_workspace, [_finding(project_id="project-2")])
    assert create_submission(
        root,
        other_workspace,
        _submission_payload(first_node.node_id, project_id="project-2"),
    )


def test_create_does_not_mutate_node_statistics_reputation_or_finding(tmp_path):
    root = tmp_path / "protocol"
    workspace = tmp_path / "workspace"
    finding = _finding(poc_file="test/PoC.t.sol", reproduction_steps=["run exploit"])
    before_finding = deepcopy(finding.model_dump())
    _write_findings(workspace, [finding])
    node = create_node(root, _node_payload())
    before_node = load_node(root, node.node_id)
    create_submission(root, workspace, _submission_payload(node.node_id))
    assert load_node(root, node.node_id) == before_node
    assert finding.model_dump() == before_finding


@pytest.mark.parametrize("unsafe_id", ["../escape", "a/b", "a\\b", "/absolute", ".."])
def test_unsafe_submission_ids_cannot_escape_root(tmp_path, unsafe_id):
    with pytest.raises(InvalidSubmissionIdentifierError):
        get_submission_dir(tmp_path, unsafe_id)
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize(
    ("path", "statuses"),
    [
        ("reproduction", ["reproduction_pending", "validation_pending"]),
        ("accepted", ["validation_pending", "accepted", "reward_pending", "rewarded"]),
        ("duplicate", ["validation_pending", "duplicate"]),
        ("out_of_scope", ["validation_pending", "out_of_scope"]),
        ("insufficient_evidence", ["validation_pending", "insufficient_evidence"]),
        ("needs_review", ["validation_pending", "needs_review"]),
        ("unsafe", ["validation_pending", "unsafe", "penalized"]),
        ("unsupported", ["validation_pending", "unsupported"]),
        ("rejected", ["validation_pending", "rejected"]),
    ],
)
def test_valid_status_transition_paths(tmp_path, path, statuses):
    root, _, _, _, submission = _setup_submission(tmp_path)
    current = submission
    for status in statuses:
        reward_status = None
        if status == "rewarded":
            reward_status = "rewarded"
        elif status == "penalized":
            reward_status = "penalized"
        current = update_submission_status(
            root,
            current.submission_id,
            SubmissionStatusUpdate(
                status=status,
                reason=f"Transitioned through {path}.",
                reward_status=reward_status,
            ),
        )
    assert current.status.value == statuses[-1]
    assert current.status_reason == f"Transitioned through {path}."


def test_submitted_can_move_directly_to_validation_pending_and_same_status_is_idempotent(tmp_path):
    root, _, _, _, submission = _setup_submission(tmp_path)
    changed = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(status="validation_pending", reason="No reproduction required."),
    )
    repeated = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(status="validation_pending", reason="Still pending."),
    )
    assert repeated.status == changed.status
    assert repeated.status_reason == "Still pending."
    assert repeated.status_updated_at >= changed.status_updated_at


@pytest.mark.parametrize("target", ["rewarded", "accepted"])
def test_invalid_submitted_transitions_are_rejected(tmp_path, target):
    root, _, _, _, submission = _setup_submission(tmp_path)
    with pytest.raises(InvalidSubmissionStatusTransitionError):
        update_submission_status(
            root,
            submission.submission_id,
            SubmissionStatusUpdate(status=target, reason="Invalid."),
        )


@pytest.mark.parametrize("terminal", ["duplicate", "out_of_scope", "unsafe"])
def test_invalid_terminal_to_rewarded_transitions_are_rejected(tmp_path, terminal):
    root, _, _, _, submission = _setup_submission(tmp_path)
    current = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(status="validation_pending", reason="Pending."),
    )
    current = update_submission_status(
        root,
        current.submission_id,
        SubmissionStatusUpdate(status=terminal, reason="Final validation result."),
    )
    with pytest.raises(InvalidSubmissionStatusTransitionError):
        update_submission_status(
            root,
            current.submission_id,
            SubmissionStatusUpdate(status="rewarded", reason="Invalid."),
        )


def test_rewarded_cannot_return_to_submitted(tmp_path):
    root, _, _, _, submission = _setup_submission(tmp_path)
    current = submission
    for status in ["validation_pending", "accepted", "reward_pending", "rewarded"]:
        current = update_submission_status(
            root,
            current.submission_id,
            SubmissionStatusUpdate(
                status=status,
                reason="Progress.",
                reward_status="rewarded" if status == "rewarded" else None,
            ),
        )
    with pytest.raises(InvalidSubmissionStatusTransitionError):
        update_submission_status(
            root,
            current.submission_id,
            SubmissionStatusUpdate(status="submitted", reason="Invalid rollback."),
        )


def test_references_attach_without_executing_services(tmp_path):
    root, _, _, _, submission = _setup_submission(tmp_path)
    pending_reproduction = update_submission_status(
        root,
        submission.submission_id,
        SubmissionStatusUpdate(status="reproduction_pending", reason="Awaiting reproduction."),
    )
    reproduced = attach_reproduction_reference(root, pending_reproduction.submission_id, "reproduction-1")
    validation_pending = update_submission_status(
        root,
        reproduced.submission_id,
        SubmissionStatusUpdate(status="validation_pending", reason="Awaiting validation."),
    )
    validated = attach_validation_reference(root, validation_pending.submission_id, "validation-1")
    assert validated.reproduction_id == "reproduction-1"
    assert validated.validation_id == "validation-1"


def test_reference_and_reward_status_constraints(tmp_path):
    root, _, _, _, submission = _setup_submission(tmp_path)
    with pytest.raises(InvalidSubmissionStatusTransitionError):
        update_submission_status(
            root,
            submission.submission_id,
            SubmissionStatusUpdate(
                status="reproduction_pending",
                reason="Too early for validation.",
                validation_id="validation-1",
            ),
        )
    with pytest.raises(InvalidSubmissionStatusTransitionError):
        update_submission_status(
            root,
            submission.submission_id,
            SubmissionStatusUpdate(
                status="reproduction_pending",
                reason="No reward.",
                reward_status="rewarded",
            ),
        )


def test_status_update_model_rejects_empty_reason():
    with pytest.raises(ValidationError):
        SubmissionStatusUpdate(status="submitted", reason="   ")
