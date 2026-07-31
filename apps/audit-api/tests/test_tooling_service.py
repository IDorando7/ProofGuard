import subprocess
from pathlib import Path

from app.schemas.tooling import ToolAvailability
from app.services.tooling_service import (
    build_project_tooling_report,
    check_docker_available,
    check_host_tool_available,
    check_sandbox_image_available,
)


def test_check_host_tool_available_uses_list_args_and_handles_missing_command(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        raise FileNotFoundError("missing")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = check_host_tool_available("missing-tool", ["missing-tool", "--version"])

    assert result.available is False
    assert result.error_message is not None
    assert calls[0][0] == ["missing-tool", "--version"]
    assert calls[0][1].get("shell") is None


def test_check_docker_available_returns_tool_availability(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="Docker version 1.0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = check_docker_available()

    assert isinstance(result, ToolAvailability)
    assert result.tool_name == "docker"
    assert result.available is True


def test_sandbox_image_missing_returns_false(monkeypatch):
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="No such image")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert check_sandbox_image_available("foundry-sandbox:latest") is False


def test_build_project_tooling_report_returns_unsupported_for_non_foundry_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(
        "app.services.tooling_service.build_sandbox_tooling_info",
        lambda sandbox_image="foundry-sandbox:latest": _sandbox_info(sandbox_available=False),
    )

    report = build_project_tooling_report("project-1", repo)

    assert report.status == "unsupported"
    assert report.foundry.is_foundry_project is False


def test_build_project_tooling_report_returns_sandbox_missing_for_foundry_without_image(
    tmp_path: Path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foundry.toml").write_text("[profile.default]\n", encoding="utf-8")
    (repo / "src").mkdir()
    monkeypatch.setattr(
        "app.services.tooling_service.build_sandbox_tooling_info",
        lambda sandbox_image="foundry-sandbox:latest": _sandbox_info(sandbox_available=False),
    )

    report = build_project_tooling_report("project-1", repo)

    assert report.status == "sandbox_missing"
    assert report.foundry.is_foundry_project is True


def test_build_project_tooling_report_does_not_run_forge_test_or_untrusted_commands(
    tmp_path: Path,
    monkeypatch,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "foundry.toml").write_text("[profile.default]\n", encoding="utf-8")
    (repo / "src").mkdir()
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        if args == ["docker", "--version"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="Docker version 1.0\n", stderr="")
        if args == ["docker", "image", "inspect", "foundry-sandbox:latest"]:
            return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr="missing")
        if args == ["forge", "--version"]:
            return subprocess.CompletedProcess(args=args, returncode=0, stdout="forge 1.0\n", stderr="")
        raise AssertionError(f"unexpected command: {args}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    report = build_project_tooling_report("project-1", repo)

    assert report.status == "sandbox_missing"
    assert ["forge", "test"] not in calls
    assert ["forge", "build"] not in calls
    assert all("test" not in args for args in calls if args and args[0] == "forge")


def _sandbox_info(sandbox_available: bool):
    from app.schemas.tooling import SandboxToolingInfo

    return SandboxToolingInfo(
        sandbox_required=True,
        sandbox_image="foundry-sandbox:latest",
        sandbox_available=sandbox_available,
        docker_available=sandbox_available,
        foundry_available_in_sandbox=False,
        host_foundry_available=False,
        warnings=[],
        notes=[],
    )

