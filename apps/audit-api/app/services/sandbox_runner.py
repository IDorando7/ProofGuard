import subprocess
import threading
import time
from pathlib import Path

from app.schemas.sandbox import SandboxCommandResult, SandboxRunStatus
from app.services.command_policy import build_safe_forge_command, validate_allowed_command


MAX_CONCURRENT_SANDBOX_JOBS = 4
MAX_OUTPUT_BYTES = 1_048_576
_SANDBOX_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_SANDBOX_JOBS)


def build_docker_command(
    repo_path: Path,
    safe_command: list[str],
    sandbox_image: str = "foundry-sandbox:latest",
    timeout_seconds: int = 60,
    memory_limit: str = "1g",
    cpus: str = "1",
    pids_limit: int = 256,
) -> list[str]:
    resolved_repo_path = repo_path.resolve()
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        "none",
        "--cpus",
        cpus,
        "--memory",
        memory_limit,
        "--pids-limit",
        str(pids_limit),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=256m",
        "-v",
        f"{resolved_repo_path}:/workspace/repo:rw",
        "-w",
        "/workspace/repo",
        "--user",
        "1000:1000",
        sandbox_image,
        *safe_command,
    ]


def run_in_sandbox(
    repo_path: Path,
    command: list[str],
    sandbox_image: str = "foundry-sandbox:latest",
    timeout_seconds: int = 60,
    memory_limit: str = "1g",
    cpus: str = "1",
    pids_limit: int = 256,
) -> SandboxCommandResult:
    start = time.monotonic()

    if not repo_path.exists() or not repo_path.is_dir():
        return _result(
            status=SandboxRunStatus.REJECTED,
            command=command if isinstance(command, list) else [],
            start=start,
            error_message="repo_path does not exist or is not a directory.",
        )

    allowed, reason = validate_allowed_command(command)
    if not allowed:
        return _result(
            status=SandboxRunStatus.REJECTED,
            command=command if isinstance(command, list) else [],
            start=start,
            error_message=reason,
        )

    try:
        safe_command = build_safe_forge_command(command)
        docker_command = build_docker_command(
            repo_path=repo_path,
            safe_command=safe_command,
            sandbox_image=sandbox_image,
            timeout_seconds=timeout_seconds,
            memory_limit=memory_limit,
            cpus=cpus,
            pids_limit=pids_limit,
        )
        with _SANDBOX_SLOTS:
            completed = subprocess.run(
                docker_command,
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
    except subprocess.TimeoutExpired as exc:
        return _result(
            status=SandboxRunStatus.TIMEOUT,
            command=command,
            start=start,
            stdout=_bounded_output(exc.stdout if isinstance(exc.stdout, str) else None),
            stderr=_bounded_output(exc.stderr if isinstance(exc.stderr, str) else None),
            error_message="Sandbox command timed out.",
            timed_out=True,
        )
    except OSError as exc:
        del exc
        return _result(
            status=SandboxRunStatus.SANDBOX_ERROR,
            command=command,
            start=start,
            error_message="Sandbox command failed before execution.",
        )

    status = SandboxRunStatus.COMPLETED if completed.returncode == 0 else SandboxRunStatus.FAILED
    return _result(
        status=status,
        command=command,
        start=start,
        exit_code=completed.returncode,
        stdout=_bounded_output(completed.stdout),
        stderr=_bounded_output(completed.stderr),
        error_message=None if completed.returncode == 0 else "Sandbox command exited with a non-zero status.",
    )


def is_docker_command_safe(docker_command: list[str]) -> bool:
    command_text = "\n".join(docker_command)
    forbidden_fragments = (
        "/var/run/docker.sock",
        "docker.sock",
        "$HOME",
        "~",
        "/home/",
        "/root/",
    )
    required_fragments = (
        "--rm",
        "--network",
        "none",
        "--pids-limit",
        "--memory",
        "--cpus",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
    )
    return all(fragment in docker_command for fragment in required_fragments) and not any(
        fragment in command_text for fragment in forbidden_fragments
    )


def _result(
    status: SandboxRunStatus,
    command: list[str],
    start: float,
    exit_code: int | None = None,
    stdout: str | None = None,
    stderr: str | None = None,
    error_message: str | None = None,
    timed_out: bool = False,
) -> SandboxCommandResult:
    return SandboxCommandResult(
        status=status,
        command=command,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=max(0, int((time.monotonic() - start) * 1000)),
        error_message=error_message,
        timed_out=timed_out,
    )


def _bounded_output(value: str | None) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= MAX_OUTPUT_BYTES:
        return value
    suffix = "\n[ProofGuard output truncated]"
    budget = MAX_OUTPUT_BYTES - len(suffix.encode("utf-8"))
    return encoded[:budget].decode("utf-8", errors="ignore") + suffix
