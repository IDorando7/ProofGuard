from app.schemas.deduplication import DeduplicationStatus
from app.schemas.validation import ValidationEvidence
from app.services.deduplication_service import (
    build_root_cause_fingerprint,
    apply_dedup_result_to_validation_evidence,
    build_dedup_key,
    classify_similarity,
    compute_candidate_similarity,
    extract_finding_candidate,
    find_duplicate,
    find_duplicate_for_candidate,
    find_duplicate_groups,
    group_findings_by_dedup_key,
    jaccard_similarity,
    normalize_contract_path,
    normalize_text,
)


def _finding(
    finding_id="finding-1",
    category="access_control",
    contract="src/TAIEngine.sol",
    function="setTreasury",
    root_cause="Missing authorization check on treasury setter",
    attack_path="Attacker calls setTreasury directly",
    title="Missing access control on setTreasury",
):
    return {
        "finding_id": finding_id,
        "category": category,
        "contracts": [contract],
        "functions": [function],
        "root_cause": root_cause,
        "attack_path": attack_path,
        "impact": "Treasury can be changed by attacker",
        "title": title,
        "severity": "High",
    }


def test_normalize_text_converts_access_control_to_snake_case():
    assert normalize_text("Access Control") == "access_control"


def test_normalize_text_trims_whitespace():
    assert normalize_text(" unauthorized  mint ") == "unauthorized_mint"


def test_normalize_contract_path_removes_leading_dot_slash():
    assert normalize_contract_path("./src/TAIEngine.sol") == "src/TAIEngine.sol"


def test_normalize_contract_path_converts_backslashes():
    assert normalize_contract_path("src\\TAIEngine.sol") == "src/TAIEngine.sol"


def test_extract_finding_candidate_supports_contracts_list():
    candidate = extract_finding_candidate({"finding_id": "f1", "contracts": ["./src/A.sol"]})

    assert candidate.contracts == ["src/A.sol"]


def test_extract_finding_candidate_supports_contract_string():
    candidate = extract_finding_candidate({"finding_id": "f1", "contract": "src/A.sol"})

    assert candidate.contracts == ["src/A.sol"]


def test_extract_finding_candidate_supports_affected_contracts():
    candidate = extract_finding_candidate({"finding_id": "f1", "affected_contracts": ["src/A.sol"]})

    assert candidate.contracts == ["src/A.sol"]


def test_extract_finding_candidate_supports_functions_list():
    candidate = extract_finding_candidate({"finding_id": "f1", "functions": ["setTreasury"]})

    assert candidate.functions == ["settreasury"]


def test_extract_finding_candidate_supports_function_string():
    candidate = extract_finding_candidate({"finding_id": "f1", "function": "withdraw"})

    assert candidate.functions == ["withdraw"]


def test_missing_finding_id_falls_back_safely():
    candidate = extract_finding_candidate({"category": "reentrancy"})

    assert candidate.finding_id == "unknown"


def test_build_dedup_key_uses_category_first_contract_and_first_function():
    candidate = extract_finding_candidate(_finding())

    assert build_dedup_key(candidate) == "access_control:src/TAIEngine.sol:settreasury"


def test_build_dedup_key_uses_unknown_for_missing_fields():
    candidate = extract_finding_candidate({"finding_id": "f1"})

    assert build_dedup_key(candidate) == "unknown:unknown:unknown"


def test_exact_same_category_contract_function_gives_high_score():
    a = extract_finding_candidate(_finding("finding-1"))
    b = extract_finding_candidate(_finding("finding-2"))

    score, matched_fields, _ = compute_candidate_similarity(a, b)

    assert score >= 0.85
    assert {"category", "contracts", "functions"}.issubset(set(matched_fields))


def test_same_category_contract_function_returns_duplicate():
    result = find_duplicate(_finding("finding-2"), [_finding("finding-1")])

    assert result.status == DeduplicationStatus.DUPLICATE
    assert result.is_duplicate is True


def test_same_category_contract_different_function_is_possible_duplicate_or_unique():
    result = find_duplicate(
        _finding("finding-2", function="setOwner"),
        [_finding("finding-1", function="setTreasury")],
    )

    assert result.status in {DeduplicationStatus.POSSIBLE_DUPLICATE, DeduplicationStatus.UNIQUE}


def test_different_category_is_not_duplicate():
    result = find_duplicate(
        _finding("finding-2", category="reentrancy"),
        [_finding("finding-1", category="access_control")],
    )

    assert result.status != DeduplicationStatus.DUPLICATE


def test_root_cause_similarity_increases_score():
    base = extract_finding_candidate(_finding("finding-1", root_cause="missing authorization on admin setter"))
    similar = extract_finding_candidate(_finding("finding-2", root_cause="missing authorization on treasury setter"))
    unrelated = extract_finding_candidate(_finding("finding-3", root_cause="oracle price stale"))

    similar_score, _, _ = compute_candidate_similarity(base, similar)
    unrelated_score, _, _ = compute_candidate_similarity(base, unrelated)

    assert similar_score > unrelated_score


def test_attack_path_similarity_increases_score():
    base = extract_finding_candidate(_finding("finding-1", attack_path="attacker calls withdraw directly"))
    similar = extract_finding_candidate(_finding("finding-2", attack_path="attacker calls withdraw repeatedly"))
    unrelated = extract_finding_candidate(_finding("finding-3", attack_path="keeper updates oracle"))

    similar_score, _, _ = compute_candidate_similarity(base, similar)
    unrelated_score, _, _ = compute_candidate_similarity(base, unrelated)

    assert similar_score > unrelated_score


def test_unrelated_findings_return_unique():
    result = find_duplicate(
        _finding(
            "finding-2",
            category="reentrancy",
            contract="src/Vault.sol",
            function="withdraw",
            root_cause="external call before state update",
            attack_path="attacker reenters withdraw",
            title="Reentrancy in withdraw",
        ),
        [_finding("finding-1")],
    )

    assert result.status == DeduplicationStatus.UNIQUE


def test_classify_similarity_thresholds():
    assert classify_similarity(0.90) == DeduplicationStatus.DUPLICATE
    assert classify_similarity(0.70) == DeduplicationStatus.POSSIBLE_DUPLICATE
    assert classify_similarity(0.30) == DeduplicationStatus.UNIQUE


def test_no_existing_findings_returns_unique():
    result = find_duplicate(_finding("finding-1"), [])

    assert result.status == DeduplicationStatus.UNIQUE
    assert result.is_duplicate is False


def test_exact_duplicate_result_includes_duplicate_of():
    result = find_duplicate(_finding("finding-2"), [_finding("finding-1")])

    assert result.status == DeduplicationStatus.DUPLICATE
    assert result.duplicate_of == "finding-1"


def test_possible_duplicate_has_is_duplicate_false():
    result = find_duplicate(
        _finding("finding-2", function="setOwner", title="Treasury setter lacks owner check"),
        [_finding("finding-1", function="setTreasury", title="Treasury setter lacks owner check")],
    )

    assert result.status == DeduplicationStatus.POSSIBLE_DUPLICATE
    assert result.is_duplicate is False


def test_best_match_is_selected_among_multiple_candidates():
    result = find_duplicate(
        _finding("target"),
        [
            _finding("weak", contract="src/Other.sol", function="mint"),
            _finding("strong"),
        ],
    )

    assert result.duplicate_of == "strong"


def test_matches_are_sorted_by_similarity_score_descending():
    result = find_duplicate(
        _finding("target"),
        [
            _finding("weak", contract="src/Other.sol", function="mint"),
            _finding("strong"),
        ],
    )

    scores = [match.similarity_score for match in result.matches]
    assert scores == sorted(scores, reverse=True)


def test_notes_include_deterministic_warning():
    result = find_duplicate(_finding("finding-2"), [_finding("finding-1")])

    assert "Deduplication v0 uses deterministic field similarity." in result.notes
    assert "This does not prove semantic equivalence." in result.notes


def test_group_findings_by_dedup_key_groups_exact_duplicates():
    groups = group_findings_by_dedup_key([_finding("f1"), _finding("f2")])
    key = "access_control:src/TAIEngine.sol:settreasury"

    assert [candidate.finding_id for candidate in groups[key]] == ["f1", "f2"]


def test_find_duplicate_groups_returns_only_groups_with_more_than_one_finding():
    groups = find_duplicate_groups(
        [
            _finding("f1"),
            _finding("f2"),
            _finding("f3", contract="src/Other.sol"),
        ]
    )

    assert groups == [["f1", "f2"]]


def test_findings_in_different_categories_are_not_grouped_together():
    groups = find_duplicate_groups([_finding("f1"), _finding("f2", category="reentrancy")])

    assert groups == []


def test_jaccard_similarity_handles_empty_inputs():
    assert jaccard_similarity(None, "") == 0.0


def test_apply_dedup_result_to_validation_evidence_sets_duplicate_fields_and_notes():
    result = find_duplicate(_finding("finding-2"), [_finding("finding-1")])
    evidence = apply_dedup_result_to_validation_evidence(ValidationEvidence(notes=["existing"]), result)

    assert evidence.is_duplicate is True
    assert evidence.duplicate_of == "finding-1"
    assert "existing" in evidence.notes
    assert "Deduplication v0 uses deterministic field similarity." in evidence.notes


def test_root_cause_fingerprint_reuses_normalized_dedup_identity_not_reporter_data():
    finding = _finding("finding-1")
    same_technical_finding = _finding("finding-2")
    first = build_root_cause_fingerprint(finding)
    second = build_root_cause_fingerprint(same_technical_finding)
    assert first == second
    assert len(first) == 64
    changed = _finding("finding-3", function="differentFunction")
    assert build_root_cause_fingerprint(changed) != first
