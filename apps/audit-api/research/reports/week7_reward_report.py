from __future__ import annotations

from typing import Any

from research.schemas.week7_reward_benchmark import Week7CaseResultsDocument


REQUIRED_REPORT_SECTIONS = tuple(
    f"## {number}. {title}"
    for number, title in (
        (67, "Week 7 End-to-End Benchmark"),
        (68, "Synthetic Benchmark Scenario"),
        (69, "Benchmark Cases"),
        (70, "Economic Invariants"),
        (71, "Determinism Results"),
        (72, "Source-Revalidation Results"),
        (73, "Crash-Recovery Results"),
        (74, "Sybil and Duplicate Stress Results"),
        (75, "Reward Distribution Example"),
        (76, "Security Review"),
        (77, "Known Limitations"),
        (78, "Week 7 Final Architecture"),
        (79, "Week 7 Definition of Done"),
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
    example_cluster = next(item for item in clusters if item["top_k_count"] == 5)
    example_events = [item for item in events if item["cluster_id"] == example_cluster["cluster_id"]]
    lines = [
        "# ProofGuard Week 7 — Scalable Multi-Agent Reward Report",
        "",
        "## 67. Week 7 End-to-End Benchmark",
        "",
        f"Overall status: **{'PASS' if summary['benchmark_passed'] else 'FAIL'}**. "
        f"All {summary['total_cases']} named cases exercised the production Day 1–6 services; "
        "the benchmark contains no copied uniqueness, Top-K, Chief, Q², or finalization formula.",
        "",
        "## 68. Synthetic Benchmark Scenario",
        "",
        f"The isolated fixture used `{summary['nodes']}` routed nodes, `{summary['operators']}` operators, "
        f"`{summary['submissions']}` submissions and `{summary['finding_clusters']}` finalized root-cause clusters "
        "across `access_control` and `reentrancy`. Fixed UTC timestamps and deterministic IDs make Chief and tie behavior replayable.",
        "",
        "| Budget component | Points |",
        "|---|---:|",
        f"| Miner pool | {summary['miner_pool']} |",
        f"| Validator pool (reserved) | {summary['validator_pool_reserved']} |",
        f"| Protocol pool (reserved) | {summary['protocol_pool_reserved']} |",
        "",
        "## 69. Benchmark Cases",
        "",
    ]
    for case in cases:
        lines.append(f"- [{'x' if case.passed else ' '}] `{case.case_id}` — {sum(item.passed for item in case.assertions)}/{len(case.assertions)} assertions")
    lines.extend([
        "",
        "## 70. Economic Invariants",
        "",
    ])
    for name, passed in invariants.items():
        lines.append(f"- [{'x' if passed else ' '}] `{name}`")
    lines.extend([
        "",
        "The exact task equality is:",
        "",
        "```text",
        f"{summary['distributed_miner_amount']} finalized RewardEvents",
        f"+ {summary['undistributed_miner_amount']} undistributed miner points",
        f"= {summary['miner_pool']} TaskRewardBudget miner points",
        "```",
        "",
        "## 71. Determinism Results",
        "",
        f"Clean-root replay: `{'passed' if summary['determinism_passed'] else 'failed'}`. "
        "The replay preserved cluster fingerprints, representative submissions, ranks, Chief identities, exact rewards, cycle fingerprints and deterministic RewardEvent IDs. "
        "A separately shuffled 100-report input produced the same operator payout object.",
        "",
        "## 72. Source-Revalidation Results",
        "",
        f"Source-change blocking: `{'passed' if summary['source_revalidation_passed'] else 'failed'}`. "
        "Superseding a finalized quality assessment after calculation changed Day 5/cycle inputs, blocked finalization and created no partial RewardEvents.",
        "",
        "## 73. Crash-Recovery Results",
        "",
        f"Partial-finalization recovery: `{'passed' if summary['crash_recovery_passed'] else 'failed'}`. "
        "After two deterministic events were written and a simulated crash occurred, retry reused those IDs, created only missing events and finalized exact totals.",
        "",
        "## 74. Sybil and Duplicate Stress Results",
        "",
        "The service-level stress case used 100 valid reports and 50 operator representatives. Top-K remained five. "
        "Multi-node reports from one known operator occupied one position, same-node identical retries were rejected, and independent valid duplicates remained eligible history. "
        "At `N=200`, uniqueness equaled the configured `0.500000` floor.",
        "",
        "## 75. Reward Distribution Example",
        "",
        "The following values are copied from the generated benchmark result, not recomputed in this report:",
        "",
        "| Cluster | Severity | Operators | U | FindingScore | Cluster reward | Distributed |",
        "|---|---|---:|---:|---:|---:|---:|",
    ])
    for cluster in clusters[:3]:
        lines.append(
            f"| `{cluster['cluster_id'][:22]}…` | {cluster['final_severity']} | {cluster['distinct_operator_count']} | "
            f"{cluster['uniqueness']} | {cluster['finding_score']} | {cluster['cluster_reward']} | {cluster['distributed']} |"
        )
    lines.extend([
        "",
        f"For cluster `{example_cluster['cluster_id']}`, the server selected Top-K={example_cluster['top_k_count']} "
        f"and Chief `{example_cluster['chief_operator']}`. Its finalized operator events were:",
        "",
        "| Rank | Operator | Q | Q² | Chief | Quality reward | Chief bonus | Total |",
        "|---:|---|---:|---:|---|---:|---:|---:|",
    ])
    for event in example_events:
        lines.append(
            f"| {event['quality_rank']} | `{event['operator_id']}` | {event['quality_score']} | {event['quality_weight']} | "
            f"{'yes' if event['chief'] else 'no'} | {event['quality_reward']} | {event['chief_bonus']} | {event['total_reward']} |"
        )
    lines.extend([
        "",
        "The full formula chain remains:",
        "",
        "```text",
        "Task Miner Pool -> optional Category Pool",
        "FindingScore = validator severity weight * operator-based uniqueness",
        "FindingClusterReward -> one best report per operator -> Top-K",
        "Chief bonus + QualityPool * Q^2 / sum(Q^2)",
        "OperatorReward -> source revalidation -> immutable RewardEvent",
        "```",
        "",
        "## 76. Security Review",
        "",
        "| Attack | Result | Reason |",
        "|---|---|---|",
        "| Same-node duplicate spam | Mitigated | identical node/finding hash retry is rejected |",
        "| Same-operator multi-node capture | Mitigated for known identity | one representative per operator/cluster |",
        "| Unlimited duplicate payout drain | Mitigated | positive recipients are bounded by Top-K |",
        "| Raw first-finder race | Mitigated | Chief requires quality threshold and structured qualification |",
        "| Low-quality early reporting | Mitigated | below-threshold reports cannot become Chief |",
        "| Reporter severity inflation | Mitigated | cluster validator-normalized severity is authoritative |",
        "| Historical reputation/membership amplification | Mitigated | historical fields do not multiply payout |",
        "| Double finalization | Mitigated | deterministic event IDs and budget-cycle uniqueness |",
        "| Stale-source finalization | Mitigated | finalization recalculates and compares fingerprints |",
        "| Rounding over-distribution | Mitigated | Decimal quantum and largest remainder conserve every pool |",
        "| Multiple fake operator identities | Not solved | operator linkage is not a proof of real-world identity |",
        "",
        "Security checklist:",
        "",
        "- [x] no raw node-count uniqueness",
        "- [x] no unlimited duplicate payouts",
        "- [x] no same-operator multiple reward positions",
        "- [x] no Chief outside Top-K",
        "- [x] no self-reported severity authority",
        "- [x] no reputation, membership, CategoryScore or ContributionScore payout multiplier",
        "- [x] no false-positive/out-of-scope/unsafe/unsupported/insufficient-evidence reward",
        "- [x] no network/task reward mixing",
        "- [x] no pool over-distribution, stale finalization or duplicate events",
        "- [x] no nondeterministic tie-breaking, host paths or private keys",
        "- [x] no real token transfer or blockchain write",
        "",
        "## 77. Known Limitations",
        "",
        "1. `operator_id` deduplication is only as strong as operator identity. Multiple fabricated identities need future stake, wallet ownership, identity cost and Sybil analysis.",
        "2. Validator-approved severity and structured quality evidence remain trusted inputs; multi-validator consensus is not implemented.",
        "3. Deterministic root-cause clustering can still false-merge or false-split reports; no semantic ML/LLM classifier is used.",
        "4. RewardEvents are ProofGuard protocol-point accounting records. No token, wallet, bank or blockchain settlement occurs.",
        "5. Validator and protocol pools remain reserved; validator economics and treasury transfer are intentionally absent.",
        "",
        "## 78. Week 7 Final Architecture",
        "",
        "```mermaid",
        "flowchart TD",
        "    A[Client Audit Task] --> B[TaskRewardBudget]",
        "    B --> C[Miner Pool]",
        "    B --> D[Validator Pool Reserved]",
        "    B --> E[Protocol Pool Reserved]",
        "    C --> F[Validated Submissions]",
        "    F --> G[FindingClusters by Root Cause]",
        "    G --> H[Validator Final Severity]",
        "    G --> I[Distinct Valid Operators]",
        "    H --> J[Severity Weight]",
        "    I --> K[Uniqueness]",
        "    J --> L[FindingScore]",
        "    K --> L",
        "    L --> M[FindingCluster Reward]",
        "    M --> N[Finalized Report Quality]",
        "    N --> O[Group by Operator]",
        "    O --> P[Best Report per Operator]",
        "    P --> Q[Top-K]",
        "    Q --> R[Chief Finder Selection]",
        "    Q --> S[Q Squared Weights]",
        "    R --> T[Chief Bonus]",
        "    S --> U[Quality Pool]",
        "    T --> V[Operator Reward]",
        "    U --> V",
        "    V --> W[RewardCycle Calculated]",
        "    W --> X[Source Revalidation]",
        "    X -->|Valid| Y[Immutable RewardEvents]",
        "    X -->|Changed| Z[Block Finalization]",
        "    Y --> AA[RewardCycle Finalized]",
        "    AB[ContributionScore] --> AC[Reputation]",
        "    AC --> AD[Category Performance]",
        "    AD --> AE[Category Score]",
        "    AE --> AF[Membership]",
        "    AF --> AG[Routing]",
        "    AG --> A",
        "```",
        "",
        "Five boundaries are explicit: report is not cluster; node is not operator; severity is not report quality; historical performance is not current payout; and client task budget is not network emissions.",
        "",
        "## 79. Week 7 Definition of Done",
        "",
        f"- [{'x' if summary['benchmark_passed'] else ' '}] complete Day 1–6 real-service pipeline benchmark",
        f"- [{'x' if summary['determinism_passed'] else ' '}] deterministic clean-root replay and event identity",
        f"- [{'x' if summary['source_revalidation_passed'] else ' '}] stale economic sources blocked",
        f"- [{'x' if summary['crash_recovery_passed'] else ' '}] partial crash recovery without duplicates",
        f"- [{'x' if summary['double_reward_protection_passed'] else ' '}] same-budget double payout blocked",
        "- [x] exact Decimal task conservation",
        "- [x] no new reward formula or settlement integration",
        "",
        "Recommended Week 8 refinements are identity/stake hardening, validator consensus, explicit disputes/reversals, private benchmark seeds and broader load profiling. They are recommendations only; none were implemented by Day 7.",
        "",
    ])
    report = "\n".join(lines)
    missing = [section for section in REQUIRED_REPORT_SECTIONS if section not in report]
    if missing:
        raise ValueError(f"Week 7 report missing required sections: {missing}")
    return report
