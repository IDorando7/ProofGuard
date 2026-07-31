from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


APP_ROOT = Path(__file__).resolve().parents[2]
RESEARCH_ROOT = APP_ROOT / "research"
LEADERBOARD_PATH = RESEARCH_ROOT / "results" / "leaderboard.json"
EXPERIMENT_LOG_PATH = RESEARCH_ROOT / "autoresearch" / "experiment_log.json"

EVAL_SCRIPTS = {
    "access_control": RESEARCH_ROOT / "evals" / "eval_access_control.py",
    "reentrancy": RESEARCH_ROOT / "evals" / "eval_reentrancy.py",
}


def main() -> None:
    args = _parse_args()
    old_score = _read_agent_score(args.agent)

    subprocess.run(
        [sys.executable, str(EVAL_SCRIPTS[args.agent])],
        cwd=APP_ROOT,
        check=True,
    )

    new_score = _read_agent_score(args.agent)
    entry = {
        "experiment_id": str(uuid.uuid4()),
        "agent": args.agent,
        "description": args.description,
        "old_score": old_score,
        "new_score": new_score,
        "accepted": new_score >= old_score,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if args.prompt_file:
        entry["prompt_file"] = args.prompt_file
    if args.rule_config:
        entry["rule_config"] = args.rule_config

    _append_experiment(entry)
    print(
        f"{args.agent} experiment: old_score={old_score}, "
        f"new_score={new_score}, accepted={entry['accepted']}"
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a deterministic Week 2.5 eval experiment.")
    parser.add_argument("--agent", choices=sorted(EVAL_SCRIPTS), required=True)
    parser.add_argument("--description", required=True)
    parser.add_argument("--prompt-file", help="Optional prompt file being tested.")
    parser.add_argument("--rule-config", help="Optional rule config file being tested.")
    return parser.parse_args()


def _read_agent_score(agent: str) -> int:
    leaderboard = _read_json(LEADERBOARD_PATH, default={})
    return int(leaderboard.get(agent, {}).get("score", 0))


def _append_experiment(entry: dict[str, Any]) -> None:
    EXPERIMENT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = _read_json(EXPERIMENT_LOG_PATH, default=[])
    log.append(entry)
    EXPERIMENT_LOG_PATH.write_text(json.dumps(log, indent=2), encoding="utf-8")


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()

