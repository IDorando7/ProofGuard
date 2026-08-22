import hashlib
import json
import re
from typing import Any

from app.schemas.deduplication import (
    DeduplicationCandidate,
    DeduplicationMatch,
    DeduplicationResult,
    DeduplicationStatus,
)
from app.schemas.validation import ValidationEvidence


NOTES = [
    "Deduplication v0 uses deterministic field similarity.",
    "This does not prove semantic equivalence.",
]


def normalize_text(value: str | None) -> str:
    if value is None:
        return ""
    normalized = value.lower().strip()
    normalized = re.sub(r"[^\w\s-]", "", normalized)
    normalized = normalized.replace("-", " ")
    normalized = re.sub(r"\s+", "_", normalized)
    normalized = re.sub(r"_+", "_", normalized)
    return normalized.strip("_")


def normalize_contract_path(path: str) -> str:
    normalized = path.strip().replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized)
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def extract_finding_candidate(finding: Any) -> DeduplicationCandidate:
    finding_id = _get_value(finding, "finding_id") or _get_value(finding, "id") or "unknown"
    contracts = _string_list_values(
        _get_value(finding, "contracts"),
        _get_value(finding, "contract"),
        _get_value(finding, "affected_contracts"),
    )
    functions = _string_list_values(
        _get_value(finding, "functions"),
        _get_value(finding, "function"),
        _get_value(finding, "affected_functions"),
    )
    category = _get_value(finding, "category")

    return DeduplicationCandidate(
        finding_id=str(finding_id),
        category=normalize_text(str(category)) if category is not None else None,
        contracts=_unique([normalize_contract_path(contract) for contract in contracts]),
        functions=_unique([normalize_text(function) for function in functions]),
        root_cause=_optional_str(_get_value(finding, "root_cause")),
        attack_path=_optional_str(_get_value(finding, "attack_path")),
        impact=_optional_str(_get_value(finding, "impact")),
        title=_optional_str(_get_value(finding, "title")),
        severity=_optional_str(_get_value(finding, "severity")),
    )


def build_dedup_key(candidate: DeduplicationCandidate) -> str:
    category = candidate.category or "unknown"
    contract = candidate.contracts[0] if candidate.contracts else "unknown"
    function = candidate.functions[0] if candidate.functions else "unknown"
    return f"{category}:{contract}:{function}"


def build_root_cause_fingerprint(candidate_or_finding: Any) -> str:
    """Hash the normalized Week 4 dedup identity, never reporter identity.

    This function does not classify matches.  It gives a stable identity to the
    canonical candidate selected by the existing dedup relation.
    """
    candidate = (
        candidate_or_finding
        if isinstance(candidate_or_finding, DeduplicationCandidate)
        else extract_finding_candidate(candidate_or_finding)
    )
    payload = {
        "dedup_policy": "deterministic_field_similarity_v0",
        "dedup_key": build_dedup_key(candidate),
        "category": candidate.category or "unknown",
        "contracts": sorted(set(candidate.contracts)),
        "functions": sorted(set(candidate.functions)),
        "root_cause": normalize_text(candidate.root_cause),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def token_set(text: str | None) -> set[str]:
    normalized = normalize_text(text)
    if not normalized:
        return set()
    return {token for token in re.split(r"[_\s]+", normalized) if len(token) >= 3}


def jaccard_similarity(a: str | None, b: str | None) -> float:
    a_tokens = token_set(a)
    b_tokens = token_set(b)
    if not a_tokens and not b_tokens:
        return 0.0
    return len(a_tokens & b_tokens) / len(a_tokens | b_tokens)


def compute_candidate_similarity(
    a: DeduplicationCandidate,
    b: DeduplicationCandidate,
) -> tuple[float, list[str], str]:
    score = 0.0
    matched_fields: list[str] = []

    if a.category and b.category and a.category == b.category:
        score += 0.25
        matched_fields.append("category")
    if set(a.contracts) & set(b.contracts):
        score += 0.25
        matched_fields.append("contracts")
    if set(a.functions) & set(b.functions):
        score += 0.20
        matched_fields.append("functions")

    root_similarity = jaccard_similarity(a.root_cause, b.root_cause)
    if root_similarity > 0:
        score += 0.15 * root_similarity
        matched_fields.append("root_cause")

    attack_similarity = jaccard_similarity(a.attack_path, b.attack_path)
    if attack_similarity > 0:
        score += 0.10 * attack_similarity
        matched_fields.append("attack_path")

    title_similarity = jaccard_similarity(a.title, b.title)
    if title_similarity > 0:
        score += 0.05 * title_similarity
        matched_fields.append("title")

    score = min(1.0, round(score, 4))
    reason = (
        f"Similarity {score:.2f} based on matched fields: {', '.join(matched_fields)}"
        if matched_fields
        else "No meaningful field overlap detected."
    )
    return score, matched_fields, reason


def classify_similarity(score: float) -> DeduplicationStatus:
    if score >= 0.85:
        return DeduplicationStatus.DUPLICATE
    if score >= 0.60:
        return DeduplicationStatus.POSSIBLE_DUPLICATE
    return DeduplicationStatus.UNIQUE


def find_duplicate_for_candidate(
    candidate: DeduplicationCandidate,
    existing_candidates: list[DeduplicationCandidate],
) -> DeduplicationResult:
    dedup_key = build_dedup_key(candidate)
    if not existing_candidates:
        return DeduplicationResult(
            status=DeduplicationStatus.UNIQUE,
            is_duplicate=False,
            similarity_score=0.0,
            reason="No existing findings to compare.",
            dedup_key=dedup_key,
            notes=NOTES.copy(),
        )

    matches = []
    for existing in existing_candidates:
        score, matched_fields, reason = compute_candidate_similarity(candidate, existing)
        status = classify_similarity(score)
        if score > 0:
            matches.append(
                DeduplicationMatch(
                    finding_id=existing.finding_id,
                    similarity_score=score,
                    status=status,
                    reason=reason,
                    matched_fields=matched_fields,
                )
            )
    matches.sort(key=lambda match: match.similarity_score, reverse=True)

    if not matches:
        return DeduplicationResult(
            status=DeduplicationStatus.UNIQUE,
            is_duplicate=False,
            similarity_score=0.0,
            reason="No meaningful match found.",
            matches=[],
            dedup_key=dedup_key,
            notes=NOTES.copy(),
        )

    best = matches[0]
    if best.status == DeduplicationStatus.DUPLICATE:
        return DeduplicationResult(
            status=DeduplicationStatus.DUPLICATE,
            is_duplicate=True,
            duplicate_of=best.finding_id,
            similarity_score=best.similarity_score,
            reason=best.reason,
            matches=matches,
            dedup_key=dedup_key,
            notes=NOTES.copy(),
        )
    if best.status == DeduplicationStatus.POSSIBLE_DUPLICATE:
        return DeduplicationResult(
            status=DeduplicationStatus.POSSIBLE_DUPLICATE,
            is_duplicate=False,
            similarity_score=best.similarity_score,
            reason=best.reason,
            matches=matches,
            dedup_key=dedup_key,
            notes=NOTES.copy(),
        )
    return DeduplicationResult(
        status=DeduplicationStatus.UNIQUE,
        is_duplicate=False,
        similarity_score=best.similarity_score,
        reason=best.reason,
        matches=matches,
        dedup_key=dedup_key,
        notes=NOTES.copy(),
    )


def find_duplicate(
    finding: Any,
    existing_findings: list[Any],
) -> DeduplicationResult:
    candidate = extract_finding_candidate(finding)
    existing_candidates = [extract_finding_candidate(existing) for existing in existing_findings]
    return find_duplicate_for_candidate(candidate, existing_candidates)


def group_findings_by_dedup_key(
    findings: list[Any],
) -> dict[str, list[DeduplicationCandidate]]:
    groups: dict[str, list[DeduplicationCandidate]] = {}
    for finding in findings:
        candidate = extract_finding_candidate(finding)
        groups.setdefault(build_dedup_key(candidate), []).append(candidate)
    return groups


def find_duplicate_groups(
    findings: list[Any],
) -> list[list[str]]:
    groups = group_findings_by_dedup_key(findings)
    return [
        [candidate.finding_id for candidate in candidates]
        for candidates in groups.values()
        if len(candidates) > 1
    ]


def apply_dedup_result_to_validation_evidence(
    evidence: ValidationEvidence,
    dedup_result: DeduplicationResult,
) -> ValidationEvidence:
    return evidence.model_copy(
        update={
            "is_duplicate": dedup_result.is_duplicate,
            "duplicate_of": dedup_result.duplicate_of,
            "notes": [*evidence.notes, *dedup_result.notes],
        }
    )


def _get_value(source: Any, key: str) -> Any:
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def _string_list_values(*values: Any) -> list[str]:
    output: list[str] = []
    for value in values:
        if isinstance(value, str):
            output.append(value)
        elif isinstance(value, list):
            output.extend(item for item in value if isinstance(item, str))
    return output


def _optional_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _unique(values: list[str]) -> list[str]:
    seen = set()
    unique_values = []
    for value in values:
        if value not in seen:
            seen.add(value)
            unique_values.append(value)
    return unique_values
