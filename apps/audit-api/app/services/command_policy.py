import re
from typing import Any


VALID_TEST_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
SHELL_METACHARACTERS = (";", "|", "&", "$", "`", ">", "<", "*", "?", "~")
FORBIDDEN_COMMANDS = {
    "bash",
    "sh",
    "zsh",
    "fish",
    "curl",
    "wget",
    "nc",
    "netcat",
    "npm",
    "pnpm",
    "yarn",
    "pip",
    "cargo",
    "rm",
    "mv",
    "chmod",
    "chown",
}


def validate_allowed_command(command: Any) -> tuple[bool, str | None]:
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        return False, "Command must be a list of strings."
    if not command:
        return False, "Command cannot be empty."

    for part in command:
        if any(character in part for character in SHELL_METACHARACTERS):
            return False, "Command contains shell metacharacters."
        if ".." in part:
            return False, "Command contains path traversal."

    executable = command[0].lower()
    if executable in FORBIDDEN_COMMANDS:
        return False, f"Command {command[0]} is not allowed."
    if executable != "forge":
        return False, "Only forge commands are allowed."

    if command == ["forge", "build"] or command == ["forge", "test"]:
        return True, None

    if len(command) == 4 and command[:3] == ["forge", "test", "--match-test"]:
        if VALID_TEST_NAME_PATTERN.fullmatch(command[3]):
            return True, None
        return False, "Invalid --match-test value."

    if len(command) >= 2 and command[1] == "script":
        return False, "forge script is not allowed."

    return False, "Command is not in the Week 3 allowlist."


def build_safe_forge_command(command: list[str]) -> list[str]:
    allowed, reason = validate_allowed_command(command)
    if not allowed:
        raise ValueError(reason or "Command is not allowed.")

    return list(command)
