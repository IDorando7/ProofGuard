import json
import os
import re
import tempfile
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from app.schemas.contribution import (
    ContributionComponentName,
    ContributionComponents,
    ContributionEligibilityStatus,
    ContributionScoreRecord,
    ContributionSignal,
)
from app.schemas.submission import SubmissionRewardStatus, SubmissionStatus
from app.services.finding_service import load_project_finding
from app.services.reproduction_service import load_reproduction_result
from app.services.severity_service import normalize_severity_value
from app.services.submission_service import load_submission
from app.services.validation_service import load_validation_decision


CONTRIBUTION_FILENAME = "contribution.json"
SCORING_VERSION = "contribution_v0"
SAFE_CONTRIBUTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

FINAL_INELIGIBLE_VALIDATION_STATUSES = {
    "duplicate",
    "out_of_scope",
    "rejected",
    "insufficient_evidence",
    "unsafe",
    "unsafe_poc",
    "unsupported",
}
FORCED_ZERO_VALIDATION_STATUSES = {
    "duplicate",
    "out_of_scope",
    "rejected",
    "unsafe",
    "unsafe_poc",
    "unsupported",
}
PENDING_REPRODUCTION_STATUSES = {
    "not_attempted",
    "generated",
    "running",
    "timeout",
    "sandbox_error",
    "error",
}
FINAL_FAILED_REPRODUCTION_STATUSES = {"failed", "rejected_unsafe", "unsupported"}
COMPATIBLE_ACCEPTED_SUBMISSION_STATUSES = {
    SubmissionStatus.ACCEPTED.value,
    SubmissionStatus.REWARD_PENDING.value,
}
PLACEHOLDER_TEXT = {"unknown", "n/a", "none", "todo", "placeholder", "not applicable"}


class ContributionServiceError(ValueError):
    """Base error for contribution-scoring domain failures."""


class ContributionScoreNotFoundError(ContributionServiceError):
    pass


class ContributionInputMismatchError(ContributionServiceError):
    pass


class ContributionFindingNotFoundError(ContributionServiceError):
    pass


class ContributionSubmissionNotFoundError(ContributionServiceError):
    pass


class InvalidContributionIdentifierError(ContributionServiceError):
    pass


class ContributionCalculationError(ContributionServiceError):
    pass


class ContributionStorageError(ContributionServiceError):
    pass


def get_contributions_root(protocol_data_root: Path) -> Path:
    return protocol_data_root / "contributions"


def get_contribution_dir(protocol_data_root: Path, submission_id: str) -> Path:
    _validate_contribution_id(submission_id)
    contributions_root = get_contributions_root(protocol_data_root)
    contribution_dir = contributions_root / submission_id
    try:
        contribution_dir.resolve().relative_to(contributions_root.resolve())
    except ValueError as exc:
        raise InvalidContributionIdentifierError("Invalid contribution identifier") from exc
    return contribution_dir


def extract_validation_status(validation: Any | None) -> str | None:
    return _normalize_status(_value(validation, "status"))


def extract_reproduction_status(reproduction: Any | None) -> str | None:
    return _normalize_status(_value(reproduction, "status"))


def extract_validation_evidence(validation: Any | None) -> Any | None:
    return _value(validation, "evidence")


def is_valid_independent_duplicate(validation_or_evidence: Any | None) -> bool:
    evidence = _value(validation_or_evidence, "evidence")
    if evidence is None:
        evidence = validation_or_evidence
    duplicate_kind = _normalize_status(_value(evidence, "duplicate_kind"))
    return (
        duplicate_kind == "independent_root_cause"
        and _value(evidence, "is_valid_duplicate") is True
        and _value(evidence, "is_duplicate") is True
        and bool(
            _value(evidence, "canonical_finding_id")
            or _value(evidence, "duplicate_of")
        )
    )


def extract_normalized_severity(finding: Any, validation: Any | None) -> str:
    evidence = extract_validation_evidence(validation)
    candidate = _value(evidence, "normalized_severity")
    if candidate is None:
        candidate = _value(validation, "normalized_severity")
    if candidate is None:
        candidate = _value(finding, "severity")
    if isinstance(candidate, Enum):
        candidate = candidate.value
    return normalize_severity_value(candidate).value


def extract_finding_quality_fields(finding: Any) -> dict[str, Any]:
    return {
        "title": _value(finding, "title"),
        "category": _value(finding, "category"),
        "contracts": _as_list(_first_value(finding, "contracts", "contract", "affected_contracts")),
        "functions": _as_list(_first_value(finding, "functions", "function", "affected_functions")),
        "root_cause": _value(finding, "root_cause"),
        "attack_path": _value(finding, "attack_path"),
        "impact": _value(finding, "impact"),
        "recommended_fix": _value(finding, "recommended_fix"),
        "reproduction_steps": _value(finding, "reproduction_steps"),
        "poc_file": _value(finding, "poc_file"),
    }


def calculate_validity_component(
    validation_status: str | None,
) -> tuple[float, list[ContributionSignal]]:
    status = _normalize_status(validation_status)
    if status == "accepted":
        return 30.0, [_signal("VALIDATION_ACCEPTED", "validity", 30, "ValidationDecision accepted the finding.", "validation")]
    if status == "needs_review":
        return 5.0, [_signal("VALIDATION_NEEDS_REVIEW", "validity", 5, "Validation requires further review.", "validation")]
    if status is None:
        return 0.0, [_signal("VALIDATION_MISSING", "validity", 0, "No ValidationDecision is available.", "validation")]

    codes = {
        "duplicate": ("VALIDATION_DUPLICATE", "ValidationDecision marked the finding as duplicate."),
        "out_of_scope": ("VALIDATION_OUT_OF_SCOPE", "ValidationDecision marked the finding as out of scope."),
        "insufficient_evidence": ("VALIDATION_INSUFFICIENT_EVIDENCE", "ValidationDecision found insufficient evidence."),
        "unsafe": ("VALIDATION_UNSAFE", "ValidationDecision marked the submission as unsafe."),
        "unsafe_poc": ("VALIDATION_UNSAFE", "ValidationDecision marked the PoC as unsafe."),
        "unsupported": ("VALIDATION_UNSUPPORTED", "ValidationDecision marked the finding as unsupported."),
        "rejected": ("VALIDATION_REJECTED", "ValidationDecision rejected the finding."),
    }
    code, message = codes.get(status, ("VALIDATION_NOT_ACCEPTED", "ValidationDecision did not accept the finding."))
    return 0.0, [_signal(code, "validity", 0, message, "validation")]


def calculate_severity_component(
    normalized_severity: str,
) -> tuple[float, list[ContributionSignal]]:
    if isinstance(normalized_severity, Enum):
        normalized_severity = normalized_severity.value
    severity = normalize_severity_value(normalized_severity).value
    points = {
        "Critical": 25.0,
        "High": 20.0,
        "Medium": 12.0,
        "Low": 5.0,
        "Informational": 1.0,
    }[severity]
    return points, [
        _signal(
            f"SEVERITY_{severity.upper()}",
            "severity",
            points,
            f"The normalized severity is {severity}.",
            "validation",
        )
    ]


def calculate_reproducibility_component(
    reproduction_status: str | None,
) -> tuple[float, list[ContributionSignal]]:
    status = _normalize_status(reproduction_status)
    if status == "reproduced":
        return 25.0, [_signal("REPRODUCTION_CONFIRMED", "reproducibility", 25, "ReproductionResult confirmed the finding.", "reproduction")]
    if status == "timeout":
        return 5.0, [_signal("REPRODUCTION_TIMEOUT", "reproducibility", 5, "Reproduction timed out and is not conclusive.", "reproduction")]
    if status is None:
        return 0.0, [_signal("NO_REPRODUCTION_RESULT", "reproducibility", 0, "No ReproductionResult is available.", "reproduction")]

    codes = {
        "failed": ("REPRODUCTION_FAILED", "Reproduction did not confirm the finding."),
        "rejected_unsafe": ("REPRODUCTION_REJECTED_UNSAFE", "Reproduction was rejected as unsafe and was not executed."),
        "unsupported": ("REPRODUCTION_UNSUPPORTED", "The reproduction environment does not support this attempt."),
        "sandbox_error": ("REPRODUCTION_SANDBOX_ERROR", "The reproduction sandbox reported an error."),
    }
    code, message = codes.get(
        status,
        ("REPRODUCTION_INCOMPLETE", "Reproduction is incomplete."),
    )
    return 0.0, [_signal(code, "reproducibility", 0, message, "reproduction")]


def calculate_uniqueness_component(
    validation_status: str | None,
    validation_evidence: Any | None,
) -> tuple[float, list[ContributionSignal]]:
    status = _normalize_status(validation_status)
    if status is None:
        return 0.0, [_signal("UNIQUENESS_NOT_VALIDATED", "uniqueness", 0, "Uniqueness has not been validated.", "validation")]

    is_duplicate = _value(validation_evidence, "is_duplicate")
    if is_valid_independent_duplicate(validation_evidence):
        return 0.0, [
            _signal(
                "INDEPENDENT_ROOT_CAUSE_DUPLICATE",
                "uniqueness",
                0,
                "The valid report independently confirms an existing root cause.",
                "validation",
            )
        ]
    if status == "duplicate" or is_duplicate is True:
        return 0.0, [_signal("CONFIRMED_DUPLICATE", "uniqueness", 0, "Validation evidence confirms a duplicate finding.", "validation")]
    if status == "accepted" and is_duplicate is False:
        return 15.0, [_signal("CONFIRMED_UNIQUE", "uniqueness", 15, "Validation evidence confirms the finding is not a duplicate.", "validation")]

    notes = str(_value(validation_evidence, "notes") or "").lower().replace("_", "-")
    if "possible duplicate" in notes or "possible-duplicate" in notes:
        message = "Validation evidence identifies a possible duplicate."
    else:
        message = "Uniqueness is not conclusively established."
    return 5.0, [_signal("UNIQUENESS_UNCERTAIN", "uniqueness", 5, message, "validation")]


def calculate_quality_component(
    finding: Any,
    reproduction: Any | None,
) -> tuple[float, list[ContributionSignal]]:
    del reproduction  # PoC evidence is scored by reproducibility, not writing quality.
    fields = extract_finding_quality_fields(finding)
    points = 0.0
    signals: list[ContributionSignal] = []
    quality_fields = (
        ("root_cause", "QUALITY_ROOT_CAUSE", "The finding explains a meaningful root cause."),
        ("attack_path", "QUALITY_ATTACK_PATH", "The finding explains a meaningful attack path."),
        ("impact", "QUALITY_IMPACT", "The finding explains a meaningful impact."),
        ("recommended_fix", "QUALITY_RECOMMENDED_FIX", "The finding includes a meaningful recommended fix."),
    )
    for field, code, message in quality_fields:
        if _is_meaningful_text(fields[field]):
            points += 2
            signals.append(_signal(code, "quality", 2, message, "finding"))

    has_contract = any(_has_text(value) for value in fields["contracts"])
    has_function = any(_has_text(value) for value in fields["functions"])
    if has_contract and has_function:
        points += 2
        signals.append(_signal("QUALITY_LOCATION", "quality", 2, "The finding identifies at least one contract and function.", "finding"))
    return min(points, 10.0), signals


def calculate_penalties(
    submission: Any,
    validation_status: str | None,
    reproduction_status: str | None,
    validation_evidence: Any | None,
) -> tuple[float, list[ContributionSignal]]:
    status = _normalize_status(validation_status)
    reproduction = _normalize_status(reproduction_status)
    penalties = 0.0
    signals: list[ContributionSignal] = []

    if status is None:
        penalties -= 20
        signals.append(_signal("PENALTY_VALIDATION_MISSING", "penalties", -20, "Validation is missing.", "validation"))

    unsafe = status in {"unsafe", "unsafe_poc"} or reproduction == "rejected_unsafe"
    if unsafe:
        penalties -= 25
        signals.append(_signal("PENALTY_UNSAFE", "penalties", -25, "The submission or PoC was marked unsafe.", "validation"))
    else:
        status_penalties = {
            "rejected": (-15, "PENALTY_REJECTED", "Validation rejected the finding."),
            "duplicate": (-10, "PENALTY_DUPLICATE", "Validation confirmed a duplicate finding."),
            "out_of_scope": (-20, "PENALTY_OUT_OF_SCOPE", "Validation found the finding out of scope."),
            "insufficient_evidence": (-10, "PENALTY_INSUFFICIENT_EVIDENCE", "Validation found insufficient evidence."),
        }
        if status in status_penalties and not is_valid_independent_duplicate(
            validation_evidence
        ):
            value, code, message = status_penalties[status]
            penalties += value
            signals.append(_signal(code, "penalties", value, message, "validation"))

    if status == "unsupported" or reproduction == "unsupported":
        penalties -= 5
        signals.append(_signal("PENALTY_UNSUPPORTED", "penalties", -5, "The finding or reproduction is unsupported.", "validation"))

    inconsistent = False
    if _value(submission, "validation_id") is not None and status is None:
        inconsistent = True
    if _value(submission, "reproduction_id") is not None and reproduction is None:
        inconsistent = True
    if _value(validation_evidence, "has_reproduction") is True and reproduction is None:
        inconsistent = True
    evidence_reproduction = _normalize_status(_value(validation_evidence, "reproduction_status"))
    if evidence_reproduction is not None and reproduction is not None and evidence_reproduction != reproduction:
        inconsistent = True
    if inconsistent:
        penalties -= 15
        signals.append(_signal("PENALTY_INCONSISTENT_REFERENCES", "penalties", -15, "Required protocol references are inconsistent.", "protocol"))

    return max(penalties, -100.0), signals


def determine_reward_eligibility(
    submission: Any,
    validation: Any | None,
    reproduction: Any | None,
) -> tuple[ContributionEligibilityStatus, bool, list[str]]:
    validation_status = extract_validation_status(validation)
    reproduction_status = extract_reproduction_status(reproduction)
    evidence = extract_validation_evidence(validation)
    submission_status = _normalize_status(_value(submission, "status"))
    reward_status = _normalize_status(_value(submission, "reward_status"))
    reasons: list[str] = []
    pending = False
    ineligible = False

    if validation is None:
        reasons.append("Validation decision is missing.")
        pending = True
    elif validation_status == "accepted":
        reasons.append("Validation status is accepted.")
    elif validation_status == "needs_review":
        reasons.append("Validation requires further review.")
        pending = True
    elif validation_status in FINAL_INELIGIBLE_VALIDATION_STATUSES:
        reasons.append(f"Validation status is {validation_status}.")
        ineligible = True
    else:
        reasons.append("Validation status is not finalized.")
        pending = True

    if reproduction is None:
        reasons.append("Reproduction result is missing.")
        pending = True
    elif reproduction_status == "reproduced":
        reasons.append("Reproduction status is reproduced.")
    elif reproduction_status in PENDING_REPRODUCTION_STATUSES:
        reasons.append(f"Reproduction status is {reproduction_status} and is not finalized.")
        pending = True
    elif reproduction_status in FINAL_FAILED_REPRODUCTION_STATUSES:
        reasons.append(f"Reproduction status is {reproduction_status} and does not establish eligibility.")
        ineligible = True
    else:
        reasons.append("Reproduction status does not establish eligibility.")
        pending = True

    is_duplicate = _value(evidence, "is_duplicate")
    independent_duplicate = is_valid_independent_duplicate(evidence)
    if independent_duplicate:
        reasons.append(
            "Finding is a valid independent report of an existing root cause; "
            "legacy reward_v0 does not reward it as a new vulnerability."
        )
        ineligible = True
    elif is_duplicate is True:
        reasons.append("Finding is marked duplicate.")
        ineligible = True
    elif validation is not None:
        reasons.append("Finding is not marked duplicate.")

    in_scope = _value(evidence, "in_scope")
    if in_scope is False:
        reasons.append("Finding is marked out of scope.")
        ineligible = True
    elif validation is not None and in_scope is True:
        reasons.append("Finding is in scope.")
    elif validation_status == "accepted":
        reasons.append("Finding scope is not confirmed by validation evidence.")
        pending = True

    if submission_status in {
        SubmissionStatus.DUPLICATE.value,
        SubmissionStatus.OUT_OF_SCOPE.value,
        SubmissionStatus.REJECTED.value,
        SubmissionStatus.INSUFFICIENT_EVIDENCE.value,
        SubmissionStatus.UNSAFE.value,
        SubmissionStatus.UNSUPPORTED.value,
        SubmissionStatus.PENALIZED.value,
        SubmissionStatus.REWARDED.value,
    }:
        reasons.append(f"Submission status {submission_status} is not reward eligible.")
        ineligible = True
    elif submission_status not in COMPATIBLE_ACCEPTED_SUBMISSION_STATUSES:
        reasons.append(f"Submission status {submission_status} is still awaiting accepted processing.")
        pending = True

    if reward_status in {SubmissionRewardStatus.REWARDED.value, SubmissionRewardStatus.PENALIZED.value}:
        reasons.append(f"Submission reward status is already {reward_status}.")
        ineligible = True

    if _value(submission, "validation_id") is not None and validation is None:
        reasons.append("The referenced validation decision is missing.")
        pending = True
    if _value(submission, "reproduction_id") is not None and reproduction is None:
        reasons.append("The referenced reproduction result is missing.")
        pending = True

    if ineligible:
        return ContributionEligibilityStatus.INELIGIBLE, False, reasons
    if pending:
        return ContributionEligibilityStatus.PENDING, False, reasons
    return ContributionEligibilityStatus.ELIGIBLE, True, reasons


def calculate_final_score(
    components: ContributionComponents,
    eligibility_status: ContributionEligibilityStatus,
    validation_status: str | None,
    reproduction_status: str | None = None,
) -> float:
    status = _normalize_status(validation_status)
    reproduction = _normalize_status(reproduction_status)
    score = max(0.0, min(100.0, components.raw_total))
    if eligibility_status == ContributionEligibilityStatus.PENDING:
        score = min(score, 25.0)
    if status in FORCED_ZERO_VALIDATION_STATUSES or reproduction in {"rejected_unsafe", "unsupported"}:
        score = 0.0
    elif status == "insufficient_evidence":
        score = min(score, 20.0)
    return round(score, 2)


def build_contribution_reason(
    total_score: float,
    eligibility_status: ContributionEligibilityStatus,
    validation_status: str | None,
    reproduction_status: str | None,
    normalized_severity: str,
    components: ContributionComponents,
) -> str:
    del components
    status = _normalize_status(validation_status)
    reproduction = _normalize_status(reproduction_status)
    score = _format_score(total_score)
    if eligibility_status == ContributionEligibilityStatus.ELIGIBLE:
        return (
            f"Eligible contribution with score {score}. The finding was accepted, reproduced, "
            f"confirmed unique, and normalized as {normalized_severity} severity."
        )
    if eligibility_status == ContributionEligibilityStatus.PENDING:
        if status is None:
            detail = "Validation has not been finalized."
        elif reproduction is None or reproduction in PENDING_REPRODUCTION_STATUSES:
            detail = "Reproduction has not been finalized."
        else:
            detail = "Protocol processing has not been finalized."
        return f"Pending contribution with diagnostic score {score}. {detail}"

    details = {
        "duplicate": "The finding was marked duplicate and cannot receive a reward.",
        "out_of_scope": "The finding was marked out of scope and cannot receive a reward.",
        "rejected": "The finding was rejected and cannot receive a reward.",
        "unsafe": "The submission was marked unsafe and cannot receive a reward.",
        "unsafe_poc": "The PoC was rejected as unsafe.",
        "unsupported": "The finding was marked unsupported and cannot receive a reward.",
        "insufficient_evidence": "The finding has insufficient evidence and cannot receive a reward.",
    }
    if status in details:
        detail = details[status]
    elif reproduction == "rejected_unsafe":
        detail = "The PoC was rejected as unsafe."
    elif reproduction in FINAL_FAILED_REPRODUCTION_STATUSES:
        detail = "Technical reproduction did not satisfy the reward requirements."
    else:
        detail = "A hard reward-eligibility requirement was not satisfied."
    return f"Ineligible contribution with score {score}. {detail}"


def calculate_contribution_for_submission(
    protocol_data_root: Path,
    project_workspace: Path,
    submission_id: str,
) -> ContributionScoreRecord:
    try:
        submission = load_submission(protocol_data_root, submission_id)
    except ValueError as exc:
        if exc.__class__.__name__ == "InvalidSubmissionIdentifierError":
            raise InvalidContributionIdentifierError("Invalid contribution identifier") from exc
        raise ContributionCalculationError("Unable to load the submission record") from exc
    if submission is None:
        raise ContributionSubmissionNotFoundError("Submission not found")

    finding = load_project_finding(project_workspace, submission.finding_id)
    if finding is None:
        raise ContributionFindingNotFoundError("Finding not found for contribution calculation")
    _require_matching_reference(finding, "finding", submission.project_id, submission.finding_id)

    validation = load_validation_decision(project_workspace, submission.finding_id)
    reproduction = load_reproduction_result(project_workspace, submission.finding_id)
    if validation is not None:
        _require_matching_reference(validation, "validation", submission.project_id, submission.finding_id)
        if submission.validation_id is not None and submission.validation_id != validation.validation_id:
            raise ContributionInputMismatchError("Validation reference does not match the submission")
    if reproduction is not None:
        _require_matching_reference(reproduction, "reproduction", submission.project_id, submission.finding_id)
        if submission.reproduction_id is not None and submission.reproduction_id != reproduction.reproduction_id:
            raise ContributionInputMismatchError("Reproduction reference does not match the submission")

    validation_status = extract_validation_status(validation)
    reproduction_status = extract_reproduction_status(reproduction)
    evidence = extract_validation_evidence(validation)
    severity = extract_normalized_severity(finding, validation)

    validity, validity_signals = calculate_validity_component(validation_status)
    severity_points, severity_signals = calculate_severity_component(severity)
    reproducibility, reproduction_signals = calculate_reproducibility_component(reproduction_status)
    uniqueness, uniqueness_signals = calculate_uniqueness_component(validation_status, evidence)
    quality, quality_signals = calculate_quality_component(finding, reproduction)
    penalties, penalty_signals = calculate_penalties(
        submission,
        validation_status,
        reproduction_status,
        evidence,
    )
    raw_total = validity + severity_points + reproducibility + uniqueness + quality + penalties
    components = ContributionComponents(
        validity=validity,
        severity=severity_points,
        reproducibility=reproducibility,
        uniqueness=uniqueness,
        quality=quality,
        penalties=penalties,
        raw_total=raw_total,
    )
    eligibility_status, eligible_for_reward, eligibility_reasons = determine_reward_eligibility(
        submission,
        validation,
        reproduction,
    )
    total_score = calculate_final_score(
        components,
        eligibility_status,
        validation_status,
        reproduction_status,
    )
    reason = build_contribution_reason(
        total_score,
        eligibility_status,
        validation_status,
        reproduction_status,
        severity,
        components,
    )

    existing = load_contribution_score(protocol_data_root, submission.submission_id)
    now = _utc_now()
    record = ContributionScoreRecord(
        score_id=existing.score_id if existing is not None else str(uuid.uuid4()),
        scoring_version=SCORING_VERSION,
        submission_id=submission.submission_id,
        project_id=submission.project_id,
        finding_id=submission.finding_id,
        node_id=submission.node_id,
        validation_id=_value(validation, "validation_id"),
        reproduction_id=_value(reproduction, "reproduction_id"),
        total_score=total_score,
        eligibility_status=eligibility_status,
        eligible_for_reward=eligible_for_reward,
        eligibility_reasons=eligibility_reasons,
        components=components,
        signals=(
            validity_signals
            + severity_signals
            + reproduction_signals
            + uniqueness_signals
            + quality_signals
            + penalty_signals
        ),
        reason=reason,
        created_at=existing.created_at if existing is not None else now,
        updated_at=now,
    )
    _write_contribution(protocol_data_root, record)
    return record


def save_contribution_score(
    protocol_data_root: Path,
    record: ContributionScoreRecord,
) -> ContributionScoreRecord:
    existing = load_contribution_score(protocol_data_root, record.submission_id)
    saved = ContributionScoreRecord.model_validate(
        {
            **record.model_dump(),
            "score_id": existing.score_id if existing is not None else record.score_id,
            "created_at": existing.created_at if existing is not None else record.created_at,
            "updated_at": _utc_now(),
        }
    )
    _write_contribution(protocol_data_root, saved)
    return saved


def load_contribution_score(
    protocol_data_root: Path,
    submission_id: str,
) -> ContributionScoreRecord | None:
    path = get_contribution_dir(protocol_data_root, submission_id) / CONTRIBUTION_FILENAME
    if not path.exists():
        return None
    if not path.is_file():
        raise ContributionStorageError("Stored contribution record is not a regular file")
    try:
        return ContributionScoreRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValidationError, json.JSONDecodeError) as exc:
        raise ContributionStorageError("Stored contribution record is malformed") from exc


def list_contribution_scores(
    protocol_data_root: Path,
    project_id: str | None = None,
    node_id: str | None = None,
    eligibility_status: ContributionEligibilityStatus | None = None,
    eligible_for_reward: bool | None = None,
) -> list[ContributionScoreRecord]:
    contributions_root = get_contributions_root(protocol_data_root)
    if not contributions_root.exists():
        return []

    records: list[ContributionScoreRecord] = []
    for path in sorted(contributions_root.glob(f"*/{CONTRIBUTION_FILENAME}")):
        record = load_contribution_score(protocol_data_root, path.parent.name)
        if record is None:
            continue
        if project_id is not None and record.project_id != project_id:
            continue
        if node_id is not None and record.node_id != node_id:
            continue
        if eligibility_status is not None and record.eligibility_status != eligibility_status:
            continue
        if eligible_for_reward is not None and record.eligible_for_reward != eligible_for_reward:
            continue
        records.append(record)
    return sorted(records, key=lambda item: (item.created_at, item.submission_id))


def list_node_contribution_scores(
    protocol_data_root: Path,
    node_id: str,
) -> list[ContributionScoreRecord]:
    return list_contribution_scores(protocol_data_root, node_id=node_id)


def list_project_contribution_scores(
    protocol_data_root: Path,
    project_id: str,
) -> list[ContributionScoreRecord]:
    return list_contribution_scores(protocol_data_root, project_id=project_id)


def _validate_contribution_id(submission_id: str) -> None:
    if not isinstance(submission_id, str) or not SAFE_CONTRIBUTION_ID.fullmatch(submission_id):
        raise InvalidContributionIdentifierError("Invalid contribution identifier")
    if submission_id in {".", ".."} or "/" in submission_id or "\\" in submission_id:
        raise InvalidContributionIdentifierError("Invalid contribution identifier")


def _write_contribution(protocol_data_root: Path, record: ContributionScoreRecord) -> None:
    contribution_dir = get_contribution_dir(protocol_data_root, record.submission_id)
    contribution_dir.mkdir(parents=True, exist_ok=True)
    output_path = contribution_dir / CONTRIBUTION_FILENAME
    payload = json.dumps(
        record.model_dump(mode="json"),
        indent=2,
        ensure_ascii=False,
        allow_nan=False,
    )
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=contribution_dir,
            prefix=".contribution-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_name = temporary.name
        os.replace(temporary_name, output_path)
    except OSError as exc:
        raise ContributionStorageError("Unable to save contribution record") from exc
    finally:
        if temporary_name is not None:
            Path(temporary_name).unlink(missing_ok=True)


def _require_matching_reference(
    record: Any,
    record_name: str,
    project_id: str,
    finding_id: str,
) -> None:
    if str(_value(record, "project_id") or "") != project_id:
        raise ContributionInputMismatchError(f"{record_name.capitalize()} project does not match the submission")
    record_finding_id = _first_value(record, "finding_id", "id")
    if str(record_finding_id or "") != finding_id:
        raise ContributionInputMismatchError(f"{record_name.capitalize()} finding does not match the submission")


def _value(source: Any | None, field: str) -> Any:
    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(field)
    return getattr(source, field, None)


def _first_value(source: Any | None, *fields: str) -> Any:
    for field in fields:
        value = _value(source, field)
        if value is not None:
            return value
    return None


def _normalize_status(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    return str(value).strip().lower()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _has_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _is_meaningful_text(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    cleaned = " ".join(value.strip().split())
    return len(cleaned) >= 10 and cleaned.lower() not in PLACEHOLDER_TEXT


def _signal(
    code: str,
    component: str,
    points: float,
    message: str,
    source: str | None,
) -> ContributionSignal:
    return ContributionSignal(
        code=code,
        component=ContributionComponentName(component),
        points=points,
        message=message,
        source=source,
    )


def _format_score(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}".rstrip("0").rstrip(".")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)
