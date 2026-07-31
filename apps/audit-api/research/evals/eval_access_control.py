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
    cases_dir = RESEARCH_ROOT / "datasets" / "access_control" / "cases"
    case_results = evaluate_agent_cases("access_control", cases_dir)
    results = {"access_control": aggregate_agent_metrics(case_results)}
    write_leaderboard(results)
    write_case_results({"access_control": case_results})
    write_week_2_5_report()
    print_summary("access_control", results)


if __name__ == "__main__":
    main()
