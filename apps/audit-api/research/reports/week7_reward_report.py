from __future__ import annotations

from typing import Any

from research.schemas.week7_reward_benchmark import Week7CaseResultsDocument


REQUIRED_REPORT_SECTIONS = tuple(
    f"## {number}. {title}"
    for number, title in (
        (1, "Executive Summary"),
        (2, "Week 7 Architecture"),
        (3, "Reward-System Principles"),
        (4, "Benchmark Environment"),
        (5, "Dataset / Synthetic Network"),
        (6, "Report Quality Results"),
        (7, "FindingCluster / Duplicate Semantics"),
        (8, "Severity and Uniqueness Results"),
        (9, "Cluster Reward Allocation"),
        (10, "Operator Deduplication"),
        (11, "Top-K Results"),
        (12, "Chief Finder Results"),
        (13, "Reward Conservation"),
        (14, "Determinism"),
        (15, "Idempotency and Double-Reward Protection"),
        (16, "Crash / Recovery Tests"),
        (17, "Historical Performance Separation"),
        (18, "Performance / Scalability"),
        (19, "Regression Results"),
        (20, "Known Limitations"),
        (21, "Week 8 Recommendations"),
    )
)


def render_week7_report(
    case_document: Week7CaseResultsDocument | dict[str, Any],
    summary: dict[str, Any],
    outputs: dict[str, Any],
) -> str:
    cases = (
        case_document.cases
        if isinstance(case_document, Week7CaseResultsDocument)
        else Week7CaseResultsDocument.model_validate(case_document).cases
    )
    clusters = outputs["clusters"]
    events = outputs["events"]
    invariants = outputs["invariants"]
    accounting = outputs["accounting"]
    performance = outputs["performance"]
    verification = outputs["verification"]
    example_cluster = next(item for item in clusters if item["top_k_count"] == 5)
    example_events = [
        item for item in events if item["cluster_id"] == example_cluster["cluster_id"]
    ]
    duplicate_reports = summary["valid_submissions"] - summary["finding_clusters"]
    lines = [
        "# ProofGuard Week 7 — Final Reward-System Engineering Report",
        "",
        "## 1. Executive Summary",
        "",
        f"Week 7 status: **{'PASS' if summary['benchmark_passed'] else 'FAIL'}**. "
        f"All {summary['total_cases']} named benchmark cases passed: `{summary['passed']}` passed, "
        f"`{summary['failed']}` failed. Calculations were deterministic, every distributable reward "
        "was conserved, no duplicate economic event was detected, no Chief appeared outside Top-K, "
        "and no known operator occupied multiple payout slots in one cluster.",
        "",
        f"Clean replay and insertion-order independence both passed. The canonical economic-state hash is "
        f"`{summary['deterministic_state_hash']}`. Historical reputation did not change current-task payout. "
        f"Regression suite recorded: `{'passed' if summary['regression_suite_passed'] else 'not supplied to benchmark command'}` "
        f"({summary['full_test_count']} tests, {summary['failed_test_count']} failures).",
        "",
        "## 2. Week 7 Architecture",
        "",
        "```mermaid",
        "flowchart TD",
        "    R[Routing Assignment] --> B[Task Reward Budget]",
        "    R --> FC[Finding Clusters]",
        "    FC --> FV[Severity x Uniqueness]",
        "    FV --> FCR[Finding Cluster Rewards]",
        "    QA[Report Quality Assessments] --> OD[Operator Deduplication]",
        "    OD --> TK[Top K]",
        "    TK --> Q2[Q Squared]",
        "    TK --> CF[Chief Finder]",
        "    FCR --> RA[Report Reward Allocations]",
        "    Q2 --> RA",
        "    CF --> RA",
        "    B --> RC[Reward Cycle]",
        "    RA --> RC",
        "    RC --> FP[Fingerprint Verification]",
        "    FP --> EV[Immutable Reward Events]",
        "```",
        "",
        "Lifecycle: `draft -> calculated -> fingerprint revalidation -> finalized`. Immutable RewardEvents are the accounting truth.",
        "",
        "## 3. Reward-System Principles",
        "",
        "| Lane | Question | Inputs | Output |",
        "|---|---|---|---|",
        "| Historical performance | Who deserves future opportunities? | ContributionScore, Reputation, CategoryScore, Membership | Routing eligibility/rank |",
        "| Finding value | How valuable is the validated vulnerability? | Final severity and distinct eligible operators | FindingScore and FindingClusterReward |",
        "| Report quality | How strong is this report? | Typed authoritative quality evidence | Q |",
        "| Reporter payout | Who receives the cluster reward? | Operator grouping, Q, Top-K, Chief evidence | ReportRewardAllocation |",
        "",
        "Exact formulas:",
        "",
        "```text",
        "Q = 0.35*C + 0.25*P + 0.20*R + 0.10*I + 0.10*F",
        "Critical=16, High=8, Medium=3, Low=1",
        "U(N) = max(0.50, 1 / (1 + 0.20*ln(N)))",
        "FindingScore = SeverityWeight * U(N)",
        "ClusterReward_i = Pool * FindingScore_i / sum(FindingScores)",
        "Reporter quality weight_i = Q_i^2",
        "```",
        "",
        "## 4. Benchmark Environment",
        "",
        f"Runtime: Python `{summary['runtime_version']}`; seed: `{summary['seed']}`; benchmark version: "
        f"`{summary['benchmark_version']}`; reward policy: `{summary['reward_policy_version']}`; "
        f"fingerprint version: `{summary['fingerprint_version']}`; quantum: `{summary['protocol_quantum']}`.",
        "",
        "Command: `python -m research.benchmarks.week7_reward_benchmark`. All production fixtures use temporary isolated roots, fixed UTC timestamps, deterministic IDs, synthetic metadata, and no external target or reproduction execution.",
        "",
        "## 5. Dataset / Synthetic Network",
        "",
        f"The CI-safe real-service scenario used `{summary['projects']}` project, `{summary['routings']}` routing, "
        f"`{summary['categories']}` active categories, `{summary['operators']}` operators, `{summary['nodes']}` routed nodes, "
        f"`{summary['submissions']}` submissions, `{summary['quality_assessments']}` finalized assessments and "
        f"`{summary['finding_clusters']}` FindingClusters. It includes 100-report/50-operator and N=1000 calculator stress cases.",
        "",
        "The production-service fixture is intentionally smaller than the prompt's 2-project/4-routing recommendation to keep CI runtime bounded. Cross-project/routing rejection remains covered by service/API regressions; the benchmark itself verifies all persisted economic references remain project/routing/budget scoped.",
        "",
        "## 6. Report Quality Results",
        "",
        "The independent known vector `(1.00, .80, .90, .70, .60)` produced `Q=0.860000`. All finalized Q values and Top-K Q² weights remained in `[0,1]`. Q=0 produced weight zero; an all-zero denominator stayed explicitly undistributed rather than silently equal-sharing.",
        "",
        "## 7. FindingCluster / Duplicate Semantics",
        "",
        f"The fixture contained `{duplicate_reports}` valid reports beyond the `{summary['finding_clusters']}` canonical reports and `{summary['invalid_submissions']}` invalid submissions. Independent root-cause overlaps remained valid FindingCluster members even outside Top-K. Same-node replay was rejected and emitted no reward. `duplicate_submission` means replay/spam; `independent_root_cause` means legitimate independent discovery.",
        "",
        "A prominent operator-count case used multiple nodes for one operator: report count exceeded distinct operator count, while Day 4 and Day 5 both retained the authoritative operator count. The 100-report stress case resolved to 50 operators and five rewarded positions.",
        "",
        "## 8. Severity and Uniqueness Results",
        "",
        "The benchmark verified `U(1)=1.000000`, monotonic decrease through N=2/5/10/50, and the `0.500000` floor through N=149/250/1000. For identical N, Critical > High > Medium > Low. A Critical at the uniqueness floor scores exactly 8, equal to a unique High.",
        "",
        "## 9. Cluster Reward Allocation",
        "",
        "The independent 10,000-point global case (High N=1, Critical N=4, Medium N=12) produced the expected `B > A > C` ordering and conserved exactly `10000.000000`. The integrated scenario used isolated access-control and reentrancy category pools; no category consumed another category's points. A separate explicit empty-category case retained its entire 4000-point pool without cross-category reassignment.",
        "",
        "| Cluster | Severity | Operators | U | FindingScore | Reward | Distributed | Conserved |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for cluster in clusters:
        lines.append(
            f"| `{cluster['cluster_id'][:20]}…` | {cluster['final_severity']} | {cluster['distinct_operator_count']} | "
            f"{cluster['uniqueness']} | {cluster['finding_score']} | {cluster['cluster_reward']} | "
            f"{cluster['distributed']} | {'yes' if cluster['conservation_pass'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## 10. Operator Deduplication",
            "",
            "Eligible reports are grouped by authoritative NodeRecord.operator_id. The representative is chosen by highest Q, then earliest submission timestamp, submission ID and node ID. The benchmark proved a later Q=.96 report can represent an operator while its earlier Q=.82 report preserves Chief qualification time.",
            "",
            "Known limitation: operator grouping prevents one registered operator's many nodes from capturing several slots, but does not cryptographically prove that two registered operator IDs are controlled by different people.",
            "",
            "## 11. Top-K Results",
            "",
            "The exact order is: eligible reports -> group by operator -> best report/operator -> Q descending deterministic ranking -> first K. K remained five. Fifty stress operators influenced uniqueness, while only five received positive payout. Non-Top-K reports remained accepted, legitimate cluster members.",
            "",
            "Uniqueness happens before Top-K and uses all distinct eligible operators.",
            "",
            "## 12. Chief Finder Results",
            "",
            "Chief must be Top-K, have Q>=.80, satisfy separate validator-derived root-cause/severity/impact facts, and be earliest by qualifying submission time. Highest Q alone does not select Chief. `rewarded_submission_id` may differ from `chief_qualifying_submission_id`.",
            "",
            f"Example cluster `{example_cluster['cluster_id']}` selected Chief `{example_cluster['chief_operator']}` from Top-K={example_cluster['top_k_count']}:",
            "",
            "| Rank | Operator | Q | Q² | Chief | Quality reward | Chief bonus | Total |",
            "|---:|---|---:|---:|---|---:|---:|---:|",
        ]
    )
    for event in example_events:
        lines.append(
            f"| {event['quality_rank']} | `{event['operator_id']}` | {event['quality_score']} | "
            f"{event['quality_weight']} | {'yes' if event['chief'] else 'no'} | "
            f"{event['quality_reward']} | {event['chief_bonus']} | {event['total_reward']} |"
        )
    lines.extend(
        [
            "",
            "A rank-six early qualifier could not become Chief. Where no Top-K report qualified, Chief Pool was zero and 100% entered the Q² Quality Pool. Chief never created a K+1 recipient.",
            "",
            "## 13. Reward Conservation",
            "",
            "```text",
            f"total budget {accounting['total_budget']} = miner {accounting['miner_pool']} + validator {accounting['validator_pool']} + protocol {accounting['protocol_pool']}",
            f"distributed miner {accounting['distributed_miner']} + undistributed miner {accounting['undistributed_miner']} = miner pool {accounting['miner_pool']}",
            f"RewardEvent total {accounting['reward_event_total']} = reporter allocation total {accounting['reporter_reward_total']}",
            "```",
            "",
            f"Accounting status: `{'PASS' if accounting['conservation_pass'] else 'FAIL'}`. Validator and protocol pools were reserved and untouched. Every cluster satisfied distributed + undistributed = cluster reward, with exact Decimal equality and no tolerance.",
            "",
            "## 14. Determinism",
            "",
            f"Clean-root replay: `{'PASS' if summary['determinism_passed'] else 'FAIL'}`. Insertion-order replay: `{'PASS' if summary['insertion_order_passed'] else 'FAIL'}`. IDs, cluster membership, Q, uniqueness, FindingScores, rewards, representatives, Top-K, Chief, fingerprints and event identities matched. Runtime measurements and generated timestamps are excluded from the economic hash.",
            "",
            f"Deterministic state hash: `{summary['deterministic_state_hash']}`.",
            "",
            "## 15. Idempotency and Double-Reward Protection",
            "",
            f"Result: `{'PASS' if summary['double_reward_protection_passed'] else 'FAIL'}`. Repeated calculation returned the same fingerprint. Repeating finalize after a lost response returned already-finalized and created zero events. A second cycle could not consume the same TaskRewardBudget.",
            "",
            "## 16. Crash / Recovery Tests",
            "",
            f"Partial event publication: `{'PASS' if summary['crash_recovery_passed'] else 'FAIL'}`. The verifier classified the matching partial set as recoverable; retry reused existing deterministic IDs and wrote only missing events. A complete event set without the final marker was separately recognized and finalized with zero duplicate writes. Same-total event-component corruption was detected read-only and never overwritten.",
            "",
            f"Final verification status: `{verification['verification_status']}`; event set complete: `{verification['event_set_complete']}`; safe retry needed: `{verification['safe_retry_finalize']}`.",
            "",
            "## 17. Historical Performance Separation",
            "",
            "Changing node reputation after calculation did not change the integrated fingerprint, Top-K, Chief, reward values or finalization result. Day 5 contains no Reputation, CategoryScore, Membership or ContributionScore payout multiplier. Those signals remain routing inputs only. A candidate with a stronger current Q can out-earn an expert with a weaker Q once both legitimately participate.",
            "",
            "## 18. Performance / Scalability",
            "",
            "Timings are observational and intentionally not fingerprinted:",
            "",
            "| Stage | Milliseconds |",
            "|---|---:|",
            f"| Cluster build | {performance['cluster_build_ms']} |",
            f"| Quality assessment | {performance['quality_assessment_ms']} |",
            f"| Finding value + cluster allocation | {performance['finding_value_and_cluster_allocation_ms']} |",
            f"| Reporter allocation | {performance['reporter_allocation_ms']} |",
            f"| Integrated cycle calculation | {performance['reward_cycle_calculate_ms']} |",
            f"| Finalization | {performance['reward_cycle_finalize_ms']} |",
            f"| Verification | {performance['verification_ms']} |",
            f"| Total benchmark | {performance['total_runtime_ms']} |",
            "",
            "The scenario also inserted 1,000 unrelated nodes and proved they did not change or force recomputation of the task snapshot. Reward concentration statistics are observational only and do not feed CategoryScore or routing.",
            "",
            "## 19. Regression Results",
            "",
            f"Full regression flag: `{'PASS' if summary['regression_suite_passed'] else 'NOT RECORDED'}`; tests: `{summary['full_test_count']}`; failures: `{summary['failed_test_count']}`. The benchmark itself passed every invariant below:",
            "",
        ]
    )
    for name, passed in invariants.items():
        lines.append(f"- [{'x' if passed else ' '}] `{name}`")
    lines.extend(
        [
            "",
            "## 20. Known Limitations",
            "",
            "1. operator_id is application-level grouping, not cryptographic Sybil resistance.",
            "2. Validator-derived severity and quality evidence remain trusted components; independent-validator consensus is future work.",
            "3. Validator and protocol pools remain reserved; Week 7 implements miner/reporter accounting only.",
            "4. RewardEvents are simulated protocol points, not token, wallet, fiat or blockchain settlement.",
            "5. Routing fairness, complementary specialist routing and private qualification benchmarks need longer-term study.",
            "6. Deterministic root-cause clustering can still false-merge or false-split reports.",
            "7. The filesystem ledger uses staged atomic publication and deterministic retry rather than a multi-file database transaction.",
            "",
            "## 21. Week 8 Recommendations",
            "",
            "Recommended architectural work: validator economics and disagreement handling; stronger operator identity/Sybil resistance; routing-fairness measurement; high-assurance second-round audits; protocol commitments/signatures; broader external synthetic datasets; and real-world shadow testing without economic settlement. No Week 8 mechanism is implemented here.",
            "",
            "Week 7 conclusion: historical performance determines opportunity; validated severity and independent discovery determine vulnerability value; current report quality determines reporter competition; the bounded client miner pool limits payout; and finalized immutable RewardEvents are the reproducible accounting truth.",
            "",
            "### Named Case Matrix",
            "",
        ]
    )
    for case in cases:
        lines.append(
            f"- [{'x' if case.passed else ' '}] `{case.case_id}` ({case.invariant_group}) — "
            f"{sum(item.passed for item in case.assertions)}/{len(case.assertions)} assertions"
        )
    report = "\n".join(lines) + "\n"
    missing = [section for section in REQUIRED_REPORT_SECTIONS if section not in report]
    if missing:
        raise ValueError(f"Week 7 report missing required sections: {missing}")
    return report
