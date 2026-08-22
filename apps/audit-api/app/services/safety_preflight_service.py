import re
from pathlib import Path
from typing import Any

from app.schemas.reproduction import ReproductionStatus
from app.schemas.safety import (
    SafetyCheckStatus,
    SafetyIssue,
    SafetyIssueSeverity,
    SafetyPreflightResult,
)


VALID_TEST_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
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
UNSAFE_CHEATCODES = {
    "vm.ffi": "UNSAFE_CHEATCODE",
    "vm.writeFile": "UNSAFE_CHEATCODE",
    "vm.writeLine": "UNSAFE_CHEATCODE",
    "vm.removeFile": "UNSAFE_CHEATCODE",
    "vm.removeDir": "UNSAFE_CHEATCODE",
    "vm.createDir": "UNSAFE_CHEATCODE",
    "vm.copyFile": "UNSAFE_CHEATCODE",
    "vm.readDir": "UNSAFE_CHEATCODE",
    "vm.env": "UNSAFE_CHEATCODE",
    "vm.envString": "UNSAFE_CHEATCODE",
    "vm.envUint": "UNSAFE_CHEATCODE",
    "vm.envAddress": "UNSAFE_CHEATCODE",
    "vm.envBytes": "UNSAFE_CHEATCODE",
    "vm.projectRoot": "UNSAFE_CHEATCODE",
}
SUSPICIOUS_FILE_READ_TOKENS = (
    "/",
    "~",
    "../",
    ".env",
    ".ssh",
    "id_rsa",
    "private",
    "secret",
    "mnemonic",
)
SUSPICIOUS_SECRET_TOKENS = (
    ".env",
    "PRIVATE_KEY",
    "MNEMONIC",
    ".ssh",
    "id_rsa",
    "api_key",
    "secret",
    "/home/",
    "/root/",
    "$HOME",
)
MAX_POC_BYTES = 262_144


def validate_test_name(test_name: str | None) -> list[SafetyIssue]:
    if test_name is None or test_name == "":
        return []
    if VALID_TEST_NAME_PATTERN.fullmatch(test_name):
        return []
    return [
        SafetyIssue(
            code="INVALID_TEST_NAME",
            severity=SafetyIssueSeverity.CRITICAL,
            message="Test name must contain only letters, numbers, and underscores.",
        )
    ]


def validate_command(command: Any) -> list[SafetyIssue]:
    if command is None:
        return []
    if not isinstance(command, list) or not all(isinstance(part, str) for part in command):
        return [_command_not_allowed("Command must be a list of strings.")]
    if not command:
        return [_command_not_allowed("Command cannot be empty.")]

    executable = command[0].lower()
    if executable in FORBIDDEN_COMMANDS:
        return [_command_not_allowed(f"Command {command[0]} is not allowed for reproduction preflight.")]
    if executable != "forge":
        return [_command_not_allowed("Only limited forge build/test commands are allowed.")]

    if command == ["forge", "build"] or command == ["forge", "test"]:
        return []

    if len(command) == 4 and command[:3] == ["forge", "test", "--match-test"]:
        test_name_issues = validate_test_name(command[3])
        if test_name_issues:
            return test_name_issues
        return []

    return [_command_not_allowed("Command is not in the Week 3 Day 3 allowlist.")]


def inspect_foundry_toml(repo_path: Path) -> list[SafetyIssue]:
    foundry_toml = repo_path / "foundry.toml"
    if not foundry_toml.is_file():
        return [
            SafetyIssue(
                code="FOUNDRY_TOML_MISSING",
                severity=SafetyIssueSeverity.WARNING,
                message="foundry.toml is missing; Foundry reproduction may be unsupported.",
                file_path="foundry.toml",
            )
        ]

    text = foundry_toml.read_text(encoding="utf-8", errors="replace")
    issues: list[SafetyIssue] = []
    for line_number, line in _iter_significant_lines(text):
        if re.search(r"\bffi\s*=\s*(true|[\"']true[\"'])($|\s|#)", line, flags=re.IGNORECASE):
            issues.append(
                SafetyIssue(
                    code="FFI_ENABLED",
                    severity=SafetyIssueSeverity.CRITICAL,
                    message="foundry.toml enables FFI, which is unsafe for reproduction preflight.",
                    file_path="foundry.toml",
                    line_number=line_number,
                )
            )
        if _line_has_dangerous_fs_permissions(line):
            issues.append(
                SafetyIssue(
                    code="DANGEROUS_FS_PERMISSIONS",
                    severity=SafetyIssueSeverity.CRITICAL,
                    message="foundry.toml declares filesystem permissions that are unsafe for reproduction preflight.",
                    file_path="foundry.toml",
                    line_number=line_number,
                )
            )

    return issues


def inspect_poc_file(repo_path: Path, poc_file: str | None) -> list[SafetyIssue]:
    if poc_file is None:
        return []

    resolved = _resolve_repo_file(repo_path, poc_file)
    if resolved is None:
        return [
            SafetyIssue(
                code="POC_FILE_OUTSIDE_REPO",
                severity=SafetyIssueSeverity.CRITICAL,
                message="PoC file must be a relative path inside the repository.",
                file_path=poc_file if not Path(poc_file).is_absolute() else None,
            )
        ]

    relative_path = _relative_path(repo_path, resolved)
    if not resolved.is_file():
        return [
            SafetyIssue(
                code="POC_FILE_MISSING",
                severity=SafetyIssueSeverity.HIGH,
                message="PoC file does not exist.",
                file_path=relative_path,
            )
        ]

    if resolved.stat().st_size > MAX_POC_BYTES:
        return [
            SafetyIssue(
                code="POC_ARTIFACT_TOO_LARGE",
                severity=SafetyIssueSeverity.HIGH,
                message="PoC artifact exceeds the reproduction size limit.",
                file_path=relative_path,
            )
        ]

    text = resolved.read_text(encoding="utf-8", errors="replace")
    issues: list[SafetyIssue] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        issues.extend(_inspect_poc_line(line, relative_path, line_number))
    return issues


def inspect_repository_symlinks(repo_path: Path) -> list[SafetyIssue]:
    """Reject symlinks so the copied sandbox workspace cannot dereference host data."""
    if not repo_path.is_dir():
        return []
    for path in sorted(repo_path.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            return [
                SafetyIssue(
                    code="UNSAFE_SYMLINK",
                    severity=SafetyIssueSeverity.CRITICAL,
                    message="Repository symlinks are not allowed in reproduction input.",
                    file_path=_relative_path(repo_path, path),
                )
            ]
    return []


def run_safety_preflight(
    repo_path: Path,
    poc_file: str | None = None,
    test_name: str | None = None,
    command: list[str] | None = None,
) -> SafetyPreflightResult:
    issues: list[SafetyIssue] = []
    issues.extend(inspect_repository_symlinks(repo_path))
    issues.extend(validate_test_name(test_name))
    issues.extend(validate_command(command))
    issues.extend(inspect_foundry_toml(repo_path))
    issues.extend(inspect_poc_file(repo_path, poc_file))

    has_blocking_issue = any(
        issue.severity in {SafetyIssueSeverity.HIGH, SafetyIssueSeverity.CRITICAL}
        for issue in issues
    )
    if has_blocking_issue:
        status = SafetyCheckStatus.FAILED
    elif issues:
        status = SafetyCheckStatus.WARNING
    else:
        status = SafetyCheckStatus.PASSED

    return SafetyPreflightResult(
        passed=not has_blocking_issue,
        status=status,
        issues=issues,
        notes=[
            "Safety preflight does not execute code.",
            "Passing preflight does not guarantee safety; sandbox execution is still required.",
        ],
    )


def map_safety_result_to_reproduction_status(result: SafetyPreflightResult) -> ReproductionStatus:
    if not result.passed:
        return ReproductionStatus.REJECTED_UNSAFE
    return ReproductionStatus.NOT_ATTEMPTED


def _command_not_allowed(message: str) -> SafetyIssue:
    return SafetyIssue(
        code="COMMAND_NOT_ALLOWED",
        severity=SafetyIssueSeverity.CRITICAL,
        message=message,
    )


def _iter_significant_lines(text: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((line_number, stripped))
    return lines


def _line_has_dangerous_fs_permissions(line: str) -> bool:
    if "fs_permissions" not in line:
        return False

    line_without_comment = line.split("#", 1)[0]
    normalized = re.sub(r"\s+", "", line_without_comment.lower())
    if normalized == "fs_permissions=[]":
        return False

    dangerous_tokens = (
        "read-write",
        'path="/"',
        "path='/'",
        'path=".."',
        "path='..'",
        'path="~"',
        "path='~'",
        "..",
        "$home",
        "~",
    )
    return normalized != "fs_permissions=[]" or any(token in normalized for token in dangerous_tokens)


def _resolve_repo_file(repo_path: Path, file_path: str) -> Path | None:
    candidate = Path(file_path)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None

    try:
        repo_root = repo_path.resolve(strict=False)
        resolved = (repo_root / candidate).resolve(strict=False)
        resolved.relative_to(repo_root)
    except ValueError:
        return None

    return resolved


def _relative_path(repo_path: Path, file_path: Path) -> str:
    try:
        return file_path.resolve(strict=False).relative_to(repo_path.resolve(strict=False)).as_posix()
    except ValueError:
        return file_path.name


def _inspect_poc_line(line: str, file_path: str, line_number: int) -> list[SafetyIssue]:
    issues: list[SafetyIssue] = []
    lowered = line.lower()

    if re.search(r"\bffi\s*\(", line):
        issues.append(
            _poc_issue(
                "UNSAFE_CHEATCODE",
                SafetyIssueSeverity.CRITICAL,
                "PoC contains direct ffi usage.",
                file_path,
                line_number,
            )
        )

    for pattern in UNSAFE_CHEATCODES:
        if pattern.lower() in lowered:
            issues.append(
                _poc_issue(
                    "UNSAFE_CHEATCODE",
                    SafetyIssueSeverity.CRITICAL,
                    f"PoC contains unsafe Foundry cheatcode {pattern}.",
                    file_path,
                    line_number,
                )
            )

    read_file_match = re.search(r"vm\.readFile\s*\(\s*([\"'])(?P<path>[^\"']+)\1", line)
    if read_file_match:
        read_path = read_file_match.group("path")
        if _is_safe_fixture_read(read_path):
            issues.append(
                _poc_issue(
                    "LOCAL_FIXTURE_READ",
                    SafetyIssueSeverity.WARNING,
                    "PoC reads a local test fixture; sandbox execution is still required.",
                    file_path,
                    line_number,
                )
            )
        elif _is_suspicious_file_read(read_path):
            issues.append(
                _poc_issue(
                    "SUSPICIOUS_FILE_READ",
                    SafetyIssueSeverity.CRITICAL,
                    "PoC attempts to read a suspicious file path.",
                    file_path,
                    line_number,
                )
            )
        else:
            issues.append(
                _poc_issue(
                    "LOCAL_FILE_READ",
                    SafetyIssueSeverity.WARNING,
                    "PoC reads a file; sandbox execution is required.",
                    file_path,
                    line_number,
                )
            )

    for token in SUSPICIOUS_SECRET_TOKENS:
        if token.lower() in lowered:
            issues.append(
                _poc_issue(
                    "SUSPICIOUS_SECRET_REFERENCE",
                    SafetyIssueSeverity.HIGH,
                    f"PoC references suspicious secret-related token {token}.",
                    file_path,
                    line_number,
                )
            )

    network_patterns = (
        "vm.createfork",
        "vm.createselectfork",
        "vm.rpcurl",
        "http://",
        "https://",
    )
    if any(token in lowered for token in network_patterns):
        issues.append(
            _poc_issue(
                "NETWORK_OR_RPC_ACCESS",
                SafetyIssueSeverity.CRITICAL,
                "PoC requests network or RPC-backed execution, which is forbidden.",
                file_path,
                line_number,
            )
        )

    return issues


def _poc_issue(
    code: str,
    severity: SafetyIssueSeverity,
    message: str,
    file_path: str,
    line_number: int,
) -> SafetyIssue:
    return SafetyIssue(
        code=code,
        severity=severity,
        message=message,
        file_path=file_path,
        line_number=line_number,
    )


def _is_safe_fixture_read(read_path: str) -> bool:
    return read_path.startswith("test/fixtures/") and ".." not in Path(read_path).parts


def _is_suspicious_file_read(read_path: str) -> bool:
    lowered = read_path.lower()
    return any(token.lower() in lowered for token in SUSPICIOUS_FILE_READ_TOKENS)
