from pathlib import Path

from app.schemas.safety import SafetyCheckStatus, SafetyIssueSeverity
from app.services.safety_preflight_service import (
    inspect_foundry_toml,
    inspect_poc_file,
    run_safety_preflight,
    validate_command,
    validate_test_name,
)


def _repo(tmp_path: Path, foundry_toml: str | None = "ffi = false\n") -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    if foundry_toml is not None:
        (repo / "foundry.toml").write_text(foundry_toml, encoding="utf-8")
    return repo


def _write_poc(repo: Path, content: str, path: str = "test/PoC.t.sol") -> str:
    poc_path = repo / path
    poc_path.parent.mkdir(parents=True, exist_ok=True)
    poc_path.write_text(content, encoding="utf-8")
    return path


def _codes(issues):
    return [issue.code for issue in issues]


def test_valid_test_name_passes():
    assert validate_test_name("testUnauthorizedSetTreasury") == []


def test_invalid_test_name_with_semicolon_fails():
    issues = validate_test_name("testHack; rm -rf /")

    assert issues[0].code == "INVALID_TEST_NAME"
    assert issues[0].severity == SafetyIssueSeverity.CRITICAL


def test_invalid_test_name_with_path_traversal_fails():
    assert validate_test_name("../../evil")[0].code == "INVALID_TEST_NAME"


def test_invalid_test_name_with_spaces_fails():
    assert validate_test_name("test name")[0].code == "INVALID_TEST_NAME"


def test_invalid_test_name_with_shell_substitution_fails():
    assert validate_test_name("test$(curl bad.site)")[0].code == "INVALID_TEST_NAME"


def test_forge_build_command_passes():
    assert validate_command(["forge", "build"]) == []


def test_forge_test_command_passes():
    assert validate_command(["forge", "test"]) == []


def test_forge_test_match_test_command_passes():
    assert validate_command(["forge", "test", "--match-test", "testName"]) == []


def test_forge_script_command_fails():
    assert validate_command(["forge", "script"])[0].code == "COMMAND_NOT_ALLOWED"


def test_shell_interpreter_command_fails():
    assert validate_command(["bash", "-c", "echo hi"])[0].code == "COMMAND_NOT_ALLOWED"


def test_network_tool_command_fails():
    assert validate_command(["curl", "https://example.com"])[0].code == "COMMAND_NOT_ALLOWED"


def test_command_with_invalid_test_name_fails():
    issues = validate_command(["forge", "test", "--match-test", "test;rm"])

    assert issues[0].code == "INVALID_TEST_NAME"


def test_shell_string_command_fails():
    assert validate_command("forge test")[0].code == "COMMAND_NOT_ALLOWED"


def test_missing_foundry_toml_returns_warning_not_critical(tmp_path: Path):
    repo = _repo(tmp_path, foundry_toml=None)

    issues = inspect_foundry_toml(repo)

    assert issues[0].code == "FOUNDRY_TOML_MISSING"
    assert issues[0].severity == SafetyIssueSeverity.WARNING


def test_ffi_true_returns_critical(tmp_path: Path):
    repo = _repo(tmp_path, foundry_toml="ffi = true\n")

    issues = inspect_foundry_toml(repo)

    assert "FFI_ENABLED" in _codes(issues)
    assert any(issue.severity == SafetyIssueSeverity.CRITICAL for issue in issues)


def test_ffi_false_passes(tmp_path: Path):
    repo = _repo(tmp_path, foundry_toml="ffi=false\n")

    assert inspect_foundry_toml(repo) == []


def test_dangerous_fs_permissions_with_root_path_fails(tmp_path: Path):
    repo = _repo(tmp_path, foundry_toml='fs_permissions = [{ access = "read", path = "/" }]\n')

    issues = inspect_foundry_toml(repo)

    assert "DANGEROUS_FS_PERMISSIONS" in _codes(issues)


def test_dangerous_fs_permissions_with_read_write_fails(tmp_path: Path):
    repo = _repo(tmp_path, foundry_toml='fs_permissions = [{ access = "read-write", path = "." }]\n')

    issues = inspect_foundry_toml(repo)

    assert "DANGEROUS_FS_PERMISSIONS" in _codes(issues)


def test_absent_or_empty_fs_permissions_passes(tmp_path: Path):
    assert inspect_foundry_toml(_repo(tmp_path, foundry_toml="ffi = false\n")) == []
    assert inspect_foundry_toml(_repo(tmp_path / "empty", foundry_toml="fs_permissions = []\n")) == []


def test_missing_poc_file_returns_high_issue(tmp_path: Path):
    repo = _repo(tmp_path)

    issues = inspect_poc_file(repo, "test/Missing.t.sol")

    assert issues[0].code == "POC_FILE_MISSING"
    assert issues[0].severity == SafetyIssueSeverity.HIGH


def test_absolute_poc_file_path_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)

    issues = inspect_poc_file(repo, "/tmp/PoC.t.sol")

    assert issues[0].code == "POC_FILE_OUTSIDE_REPO"
    assert issues[0].file_path is None


def test_path_traversal_poc_file_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)

    issues = inspect_poc_file(repo, "../PoC.t.sol")

    assert issues[0].code == "POC_FILE_OUTSIDE_REPO"


def test_poc_containing_vm_ffi_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    poc_file = _write_poc(repo, "contract PoC { function test() public { vm.ffi(new string[](0)); } }\n")

    issues = inspect_poc_file(repo, poc_file)

    assert "UNSAFE_CHEATCODE" in _codes(issues)


def test_poc_containing_vm_write_file_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    poc_file = _write_poc(repo, 'contract PoC { function test() public { vm.writeFile(".env", "x"); } }\n')

    issues = inspect_poc_file(repo, poc_file)

    assert "UNSAFE_CHEATCODE" in _codes(issues)


def test_poc_reading_env_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    poc_file = _write_poc(repo, 'contract PoC { function test() public { vm.readFile(".env"); } }\n')

    issues = inspect_poc_file(repo, poc_file)

    assert "SUSPICIOUS_FILE_READ" in _codes(issues)


def test_poc_reading_test_fixture_returns_warning(tmp_path: Path):
    repo = _repo(tmp_path)
    poc_file = _write_poc(
        repo,
        'contract PoC { function test() public { vm.readFile("test/fixtures/input.json"); } }\n',
    )

    issues = inspect_poc_file(repo, poc_file)

    assert issues[0].code == "LOCAL_FIXTURE_READ"
    assert issues[0].severity == SafetyIssueSeverity.WARNING


def test_poc_containing_private_key_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    poc_file = _write_poc(repo, 'string constant PRIVATE_KEY = "secret";\n')

    issues = inspect_poc_file(repo, poc_file)

    assert "SUSPICIOUS_SECRET_REFERENCE" in _codes(issues)
    assert any(issue.severity == SafetyIssueSeverity.HIGH for issue in issues)


def test_safe_repo_poc_and_valid_command_returns_passed(tmp_path: Path):
    repo = _repo(tmp_path)
    poc_file = _write_poc(repo, "contract PoC { function testSafe() public {} }\n")

    result = run_safety_preflight(
        repo_path=repo,
        poc_file=poc_file,
        test_name="testSafe",
        command=["forge", "test", "--match-test", "testSafe"],
    )

    assert result.passed is True
    assert result.status == SafetyCheckStatus.PASSED


def test_unsafe_poc_returns_failed(tmp_path: Path):
    repo = _repo(tmp_path)
    poc_file = _write_poc(repo, "contract PoC { function testUnsafe() public { vm.ffi(new string[](0)); } }\n")

    result = run_safety_preflight(repo_path=repo, poc_file=poc_file)

    assert result.passed is False
    assert result.status == SafetyCheckStatus.FAILED


def test_only_warning_issues_returns_warning_status(tmp_path: Path):
    repo = _repo(tmp_path, foundry_toml=None)

    result = run_safety_preflight(repo_path=repo)

    assert result.passed is True
    assert result.status == SafetyCheckStatus.WARNING


def test_result_contains_standard_notes(tmp_path: Path):
    repo = _repo(tmp_path)

    result = run_safety_preflight(repo_path=repo)

    assert "Safety preflight does not execute code." in result.notes
    assert "Passing preflight does not guarantee safety; sandbox execution is still required." in result.notes


def test_repository_symlink_is_rejected_before_execution(tmp_path: Path):
    repo = _repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("not part of the repository", encoding="utf-8")
    (repo / "linked.txt").symlink_to(outside)

    result = run_safety_preflight(repo_path=repo)

    assert result.passed is False
    assert "UNSAFE_SYMLINK" in _codes(result.issues)


def test_rpc_fork_request_is_rejected(tmp_path: Path):
    repo = _repo(tmp_path)
    poc_file = _write_poc(
        repo,
        'contract PoC { function testClaim() public { vm.createFork("https://example.invalid"); } }\n',
    )

    result = run_safety_preflight(repo_path=repo, poc_file=poc_file)

    assert result.passed is False
    assert "NETWORK_OR_RPC_ACCESS" in _codes(result.issues)
