import pytest

from app.services.command_policy import build_safe_forge_command, validate_allowed_command


def test_forge_build_is_allowed():
    assert validate_allowed_command(["forge", "build"]) == (True, None)


def test_forge_test_is_allowed():
    assert validate_allowed_command(["forge", "test"]) == (True, None)


def test_forge_test_match_test_is_allowed():
    assert validate_allowed_command(["forge", "test", "--match-test", "testName"]) == (True, None)


def test_forge_script_is_rejected():
    allowed, reason = validate_allowed_command(["forge", "script"])

    assert allowed is False
    assert reason is not None


def test_shell_interpreter_is_rejected():
    allowed, reason = validate_allowed_command(["bash", "-c", "echo hi"])

    assert allowed is False
    assert reason is not None


def test_network_tool_is_rejected():
    allowed, reason = validate_allowed_command(["curl", "https://example.com"])

    assert allowed is False
    assert reason is not None


def test_bad_match_test_name_is_rejected():
    allowed, reason = validate_allowed_command(["forge", "test", "--match-test", "testBad;rm"])

    assert allowed is False
    assert reason is not None


def test_command_containing_path_traversal_is_rejected():
    allowed, reason = validate_allowed_command(["forge", "test", "--match-test", "../evil"])

    assert allowed is False
    assert reason is not None


@pytest.mark.parametrize("metacharacter", [";", "|", "&", "$", "`", ">", "<", "*", "?", "~"])
def test_command_containing_shell_metacharacters_is_rejected(metacharacter):
    allowed, reason = validate_allowed_command(["forge", "test", "--match-test", f"test{metacharacter}Name"])

    assert allowed is False
    assert reason is not None


def test_build_safe_forge_command_keeps_forge_test_as_allowlisted():
    assert build_safe_forge_command(["forge", "test"]) == ["forge", "test"]
    assert build_safe_forge_command(["forge", "test", "--match-test", "testName"]) == [
        "forge",
        "test",
        "--match-test",
        "testName",
    ]


def test_build_safe_forge_command_keeps_forge_build_as_allowlisted():
    assert build_safe_forge_command(["forge", "build"]) == ["forge", "build"]
