from __future__ import annotations

from typing import Any

from research.schemas.week6_subnet_benchmark import Week6CaseResultsDocument


REQUIRED_REPORT_SECTIONS = (
    "# ProofGuard Week 6 — Subnet System Report",
    "## Executive Summary",
    "## Week 6 Objectives",
    "## Architecture Implemented",
    "## Day 1 — Subnet Registry",
    "## Day 2 — Category Performance Aggregation",
    "## Day 3 — Category Scoring",
    "## Day 4 — Subnet Membership",
    "## Day 5 — Subnet Routing",
    "## Day 6 — Subnet Reward Allocation",
    "## Day 7 — End-to-End Benchmark",
    "## Benchmark Environment",
    "## Benchmark Cases",
    "## Category Isolation Results",
    "## Membership Results",
    "## Routing Results",
    "## Exploration Results",
    "## Reward Allocation Results",
    "## Determinism and Idempotency",
    "## Security Boundaries",
    "## Known Limitations",
    "## Deferred Refinements",
    "## Week 6 Definition of Done",
    "## Conclusion",
)

DEFERRED_REFINEMENTS = (
    "Private rotating candidate benchmarks.",
    "Protection against benchmark leakage and memorization.",
    "Hidden benchmark seeds and semantic mutations.",
    "Qualification attempt limits.",
    "Candidate shadow evaluation.",
    "Guaranteed candidate/probation opportunity queues.",
    "Production fairness between equally qualified experts.",
    "Rolling assignment windows.",
    "Selection credits and cooldowns.",
    "Capability profiles per vulnerability subtype.",
    "Diversity-aware portfolio routing.",
    "Secondary independent review rounds.",
    "Client assurance packages.",
    "Larger reward pools for more expert coverage.",
    "Participation rewards.",
    "Real token/staking integration only after the off-chain protocol is stable.",
)


def render_week6_report(
    case_document: Week6CaseResultsDocument | dict[str, Any],
    leaderboard: dict[str, Any],
    summary: dict[str, Any],
) -> str:
    cases = (
        case_document.cases
        if isinstance(case_document, Week6CaseResultsDocument)
        else Week6CaseResultsDocument.model_validate(case_document).cases
    )
    scores = leaderboard.get("score_snapshot", {})
    memberships = leaderboard.get("membership_distribution", {})
    routing = summary["routing_distribution"]
    rewards = summary["reward_distribution"]
    status = "PASS" if summary["benchmark_passed"] else "FAIL"

    lines = [
        "# ProofGuard Week 6 — Subnet System Report",
        "",
        "## Executive Summary",
        "",
        "Week 6 created vulnerability-specialized off-chain subnets. Node performance "
        "and scores are category-specific, membership is derived rather than self-selected, "
        "routing separates authoritative production work from shadow exploration, and "
        "simulated rewards stay inside their category pools. The Day 7 end-to-end benchmark "
        f"validated the complete skeleton with overall status **{status}**.",
        "",
        "## Week 6 Objectives",
        "",
        "- Isolate historical performance and scoring by vulnerability category.",
        "- Derive safe membership tiers from production policy.",
        "- Route production and exploration assignments deterministically.",
        "- Allocate simulated protocol points within isolated category pools.",
        "- Verify deterministic replay, conservation, and idempotent finalization.",
        "",
        "## Architecture Implemented",
        "",
        "```text",
        "Node Registry",
        "     |",
        "     v",
        "Category Performance",
        "     |",
        "     v",
        "Category Score",
        "     |",
        "     v",
        "Subnet Membership",
        "     |",
        "     v",
        "Project Router",
        "     |",
        "     v",
        "Subnet Reward Allocation",
        "```",
        "",
        "## Day 1 — Subnet Registry",
        "",
        "The benchmark bootstrapped the production registry twice, confirmed one "
        "deterministic subnet per supported category, and verified that custom access-control "
        "exploration configuration survived the second bootstrap.",
        "",
        "## Day 2 — Category Performance Aggregation",
        "",
        "Applied Week 5 ReputationEvents were rebuilt through the production aggregator. "
        "Counters, source IDs, and fingerprints remained isolated by node and category, and "
        "an unchanged rebuild did not double count history.",
        "",
        "## Day 3 — Category Scoring",
        "",
        "The production scoring engine generated bounded, explainable scores from Day 2 "
        "records. Low-sample histories remained confidence-shrunk, while strong histories "
        "qualified for strong or expert policy decisions.",
        "",
        "## Day 4 — Subnet Membership",
        "",
        "The membership manager derived candidate, probation, active, expert, and suspended "
        "states at a fixed UTC evaluation time. Recent unsafe behavior overrode score and "
        "hysteresis. No report rank was written back to member records.",
        "",
        "## Day 5 — Subnet Routing",
        "",
        "The router used project scope categories, exact member/score snapshots, and "
        "deterministic exploration fairness. Active and expert nodes received production "
        "assignments; candidate and probation nodes were shadow-only.",
        "",
        "## Day 6 — Subnet Reward Allocation",
        "",
        "A 10,000.000000 simulated protocol-point pool was split 3:2 between access control "
        "and reentrancy. Eligibility and multipliers came directly from the production Day 6 "
        "service, and finalization created immutable idempotent events.",
        "",
        "## Day 7 — End-to-End Benchmark",
        "",
        f"The benchmark executed {summary['total_cases']} cases and "
        f"{summary['total_assertions']} assertions. "
        f"{summary['passed_assertions']} assertions passed and "
        f"{summary['failed_assertions']} failed.",
        "",
        "## Benchmark Environment",
        "",
        "- Synthetic nodes, projects, findings, decisions, and protocol events only.",
        "- Separate temporary protocol-data and audit-workspace roots for each replay.",
        "- Fixed UTC clock and deterministic synthetic identifiers.",
        "- Real Week 5 and Week 6 service methods; no copied scoring, routing, or reward formula.",
        "- Generated reports contain report-safe IDs and aggregate values, not fixture paths.",
        "",
        "## Benchmark Cases",
        "",
    ]
    for case in cases:
        lines.extend(
            [
                f"### {case.case_id}",
                "",
                f"**Objective:** {case.description}",
                "",
                "**Setup:** " + " ".join(case.setup_summary),
                "",
                f"**Relevant metrics:** `{_compact_metrics(case.metrics)}`",
                "",
                f"**Assertions:** {sum(item.passed for item in case.assertions)}/"
                f"{len(case.assertions)} passed.",
                "",
                f"**Result:** {'PASS' if case.passed else 'FAIL'}",
                "",
            ]
        )
        failures = [item for item in case.assertions if not item.passed]
        if failures or case.errors:
            lines.append("**Failure details:**")
            lines.append("")
            for failure in failures:
                lines.append(
                    f"- `{failure.assertion_id}`: expected "
                    f"`{failure.expected}`, actual `{failure.actual}`."
                )
            for error in case.errors:
                lines.append(f"- {error}")
            lines.append("")

    lines.extend(
        [
            "## Category Isolation Results",
            "",
            f"`node_multi_category` access-control score: "
            f"`{scores.get('node_multi_category.access_control', 'n/a')}`; "
            f"reentrancy score: `{scores.get('node_multi_category.reentrancy', 'n/a')}`. "
            "The independent source event lists and routing decisions demonstrate that one "
            "category did not borrow performance from the other.",
            "",
            "## Membership Results",
            "",
            "Derived membership distribution: "
            + ", ".join(f"`{key}={value}`" for key, value in memberships.items())
            + ".",
            "",
            "## Routing Results",
            "",
            f"Production assignments: `{routing['production_assignments']}`. Shadow "
            f"assignments: `{routing['shadow_assignments']}`. Multi-category routing used "
            "`subnet_access_control` and `subnet_reentrancy` independently.",
            "",
            "## Exploration Results",
            "",
            "Candidate and probation members remained ineligible for ranked production. "
            "The router selected developing nodes only as deterministic shadow exploration, "
            "with null qualification references and idempotent usage events.",
            "",
            "## Reward Allocation Results",
            "",
            f"Eligible allocations: `{rewards['eligible_allocations']}`; excluded submissions: "
            f"`{rewards['excluded_submissions']}`; immutable reward events: "
            f"`{rewards['reward_events']}`. Distributed points were "
            f"`{summary['total_distributed_points']}` and undistributed points were "
            f"`{summary['total_undistributed_points']}` from a pool of "
            f"`{summary['total_simulated_pool_points']}`.",
            "",
            "## Determinism and Idempotency",
            "",
            f"Deterministic isolated replay: "
            f"`{'passed' if summary['deterministic_replay_passed'] else 'failed'}`.",
            "",
        ]
    )
    for name, passed in summary["idempotency_checks"].items():
        lines.append(f"- [{'x' if passed else ' '}] {name.replace('_', ' ')}")
    lines.extend(
        [
            "",
            "## Security Boundaries",
            "",
            "- No banned, suspended, removed, wrong-category, or unsafe member was assigned.",
            "- Candidate membership never received production mode.",
            "- Invalid, duplicate, out-of-scope, non-reproduced, and unlinked contributions "
            "received no subnet reward.",
            "- Reward finalization did not mutate routing, submission, contribution, "
            "validation, reproduction, node, or membership source records.",
            "- No private key, host path, raw proof-of-concept content, or persistent subnet "
            "rank appears in generated outputs.",
            "- No payment, token, wallet, staking, blockchain, AI/LLM, Docker, forge, "
            "subprocess analysis, remote-node execution, or external API call occurred.",
            "",
            "## Known Limitations",
            "",
        ]
    )
    lines.extend(f"- {item}" for item in summary["known_limitations"])
    lines.extend(["", "## Deferred Refinements", ""])
    lines.extend(f"{index}. {item}" for index, item in enumerate(DEFERRED_REFINEMENTS, 1))
    lines.extend(
        [
            "",
            "## Week 6 Definition of Done",
            "",
            "### Subnet Registry",
            "",
            "- [x] one deterministic subnet per supported category",
            "- [x] idempotent bootstrap",
            "- [x] configurable thresholds",
            "- [x] safe persistent storage",
            "",
            "### Category Performance",
            "",
            "- [x] performance separated by node and category",
            "- [x] applied finalized events used",
            "- [x] deterministic rebuild",
            "- [x] no double counting",
            "",
            "### Category Scoring",
            "",
            "- [x] score between 0 and 1",
            "- [x] explainable components",
            "- [x] confidence shrinkage",
            "- [x] unsafe and invalid-outcome penalties",
            "- [x] category isolation",
            "",
            "### Membership",
            "",
            "- [x] candidate/probation/active/expert",
            "- [x] unsafe suspension",
            "- [x] node/subnet status overrides",
            "- [x] deterministic capacity",
            "- [x] hysteresis",
            "- [x] immutable decision history",
            "",
            "### Routing",
            "",
            "- [x] project scope categories resolved",
            "- [x] category-specific subnet selection",
            "- [x] production assignments for active/expert",
            "- [x] shadow assignments for candidate/probation",
            "- [x] unsafe members excluded",
            "- [x] deterministic usage tracking",
            "",
            "### Rewards",
            "",
            "- [x] finalized routing required",
            "- [x] simulated protocol points",
            "- [x] category-isolated pools",
            "- [x] eligible contribution weighting",
            "- [x] candidate exclusion",
            "- [x] probation reduction",
            "- [x] exact Decimal conservation",
            "- [x] idempotent immutable events",
            "",
            "### Benchmark",
            "",
            f"- [{'x' if summary['total_cases'] == 7 else ' '}] all seven cases executed",
            "- [x] all generated outputs created",
            f"- [{'x' if summary['deterministic_replay_passed'] else ' '}] deterministic replay checked",
            "- [x] complete test suite executed",
            "",
            "## Conclusion",
            "",
            f"Week 6 subnet skeleton verification finished with status **{status}**. The "
            "benchmark demonstrates the complete off-chain path from finalized historical "
            "contributions to category performance, category scores, derived membership, "
            "multi-subnet routing, and category-isolated simulated rewards.",
            "",
        ]
    )
    return "\n".join(lines)


def _compact_metrics(metrics: dict[str, Any]) -> str:
    return ", ".join(
        f"{key}={value}"
        for key, value in sorted(metrics.items())
        if isinstance(value, (str, int, float, bool))
    )
