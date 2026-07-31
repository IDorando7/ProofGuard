from eval_common import (
    RESEARCH_ROOT,
    aggregate_agent_metrics,
    evaluate_agent_cases,
    print_summary,
    write_case_results,
    write_leaderboard,
    write_week_2_5_report,
)


def main() -> None:
    cases_dir = RESEARCH_ROOT / "datasets" / "reentrancy" / "cases"
    case_results = evaluate_agent_cases("reentrancy", cases_dir)
    results = {"reentrancy": aggregate_agent_metrics(case_results)}
    write_leaderboard(results)
    write_case_results({"reentrancy": case_results})
    write_week_2_5_report()
    print_summary("reentrancy", results)


if __name__ == "__main__":
    main()
