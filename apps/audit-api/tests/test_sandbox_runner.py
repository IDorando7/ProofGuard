import subprocess
from pathlib import Path

from app.schemas.sandbox import SandboxRunStatus
from app.services.command_policy import build_safe_forge_command
from app.services.sandbox_runner import build_docker_command, is_docker_command_safe, run_in_sandbox


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo


def _docker_command(repo: Path) -> list[str]:
    return build_docker_command(
        repo_path=repo,
        safe_command=build_safe_forge_command(["forge", "test", "--match-test", "testName"]),
    )


def test_build_docker_command_returns_list_not_shell_string(tmp_path: Path):
    command = _docker_command(_repo(tmp_path))

    assert isinstance(command, list)
    assert all(isinstance(part, str) for part in command)


def test_docker_command_contains_network_none(tmp_path: Path):
    command = _docker_command(_repo(tmp_path))

    assert "--network" in command
    assert "none" in command


def test_docker_command_contains_rm(tmp_path: Path):
    assert "--rm" in _docker_command(_repo(tmp_path))


def test_docker_command_contains_resource_limits(tmp_path: Path):
    command = _docker_command(_repo(tmp_path))

    assert "--memory" in command
    assert "1g" in command
    assert "--cpus" in command
    assert "1" in command
    assert "--pids-limit" in command
    assert "256" in command


def test_docker_command_mounts_only_repo_path_to_workspace_repo(tmp_path: Path):
    repo = _repo(tmp_path)
    command = _docker_command(repo)
    mounts = [command[index + 1] for index, part in enumerate(command) if part == "-v"]

    assert mounts == [f"{repo.resolve()}:/workspace/repo:rw"]


def test_docker_command_does_not_contain_docker_sock(tmp_path: Path):
    command_text = "\n".join(_docker_command(_repo(tmp_path)))

    assert "docker.sock" not in command_text
    assert "/var/run/docker.sock" not in command_text


def test_docker_command_does_not_contain_home_or_root_mounts(tmp_path: Path):
    command_text = "\n".join(_docker_command(_repo(tmp_path)))

    assert "$HOME" not in command_text
    assert "~" not in command_text
    assert "/home/" not in command_text
    assert "/root/" not in command_text


def test_docker_command_appends_safe_forge_command(tmp_path: Path):
    command = _docker_command(_repo(tmp_path))

    assert command[-4:] == ["forge", "test", "--match-test", "testName"]
    assert is_docker_command_safe(command) is True


def test_run_in_sandbox_returns_completed_when_subprocess_exits_zero(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)

    def fake_run(args, **kwargs):
        assert args[0:2] == ["docker", "run"]
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_in_sandbox(repo, ["forge", "test"])

    assert result.status == SandboxRunStatus.COMPLETED
    assert result.exit_code == 0
    assert result.stdout == "ok"


def test_run_in_sandbox_returns_failed_when_subprocess_exits_nonzero(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="failure")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_in_sandbox(repo, ["forge", "test"])

    assert result.status == SandboxRunStatus.FAILED
    assert result.exit_code == 1
    assert result.stderr == "failure"


def test_run_in_sandbox_returns_timeout_on_timeout_expired(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)

    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"], output="partial")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_in_sandbox(repo, ["forge", "test"])

    assert result.status == SandboxRunStatus.TIMEOUT
    assert result.timed_out is True


def test_run_in_sandbox_returns_sandbox_error_on_os_error(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)

    def fake_run(args, **kwargs):
        raise OSError("docker missing")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_in_sandbox(repo, ["forge", "test"])

    assert result.status == SandboxRunStatus.SANDBOX_ERROR
    assert result.error_message is not None


def test_run_in_sandbox_returns_rejected_for_non_allowlisted_command(tmp_path: Path, monkeypatch):
    repo = _repo(tmp_path)

    def fake_run(args, **kwargs):
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_in_sandbox(repo, ["forge", "script"])

    assert result.status == SandboxRunStatus.REJECTED
    assert result.error_message is not None


def test_run_in_sandbox_rejects_missing_repo_path_cleanly(tmp_path: Path, monkeypatch):
    missing_repo = tmp_path / "missing"

    def fake_run(args, **kwargs):
        raise AssertionError("subprocess should not be called")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = run_in_sandbox(missing_repo, ["forge", "test"])

    assert result.status == SandboxRunStatus.REJECTED
    assert result.error_message == "repo_path does not exist or is not a directory."
