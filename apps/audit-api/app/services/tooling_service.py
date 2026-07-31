import subprocess
from pathlib import Path

from app.schemas.tooling import ProjectToolingReport, SandboxToolingInfo, ToolAvailability
from app.services.foundry_service import detect_foundry_project


TOOL_TIMEOUT_SECONDS = 5


def check_host_tool_available(tool_name: str, args: list[str]) -> ToolAvailability:
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        return ToolAvailability(
            tool_name=tool_name,
            available=False,
            version=None,
            error_message="Tool check args must be a list of strings.",
        )

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        return ToolAvailability(
            tool_name=tool_name,
            available=False,
            version=None,
            error_message=f"{tool_name} was not found: {exc}",
        )
    except subprocess.TimeoutExpired:
        return ToolAvailability(
            tool_name=tool_name,
            available=False,
            version=None,
            error_message=f"{tool_name} check timed out.",
        )
    except OSError as exc:
        return ToolAvailability(
            tool_name=tool_name,
            available=False,
            version=None,
            error_message=f"{tool_name} check failed: {exc}",
        )

    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return ToolAvailability(
            tool_name=tool_name,
            available=False,
            version=None,
            error_message=output or f"{tool_name} exited with code {result.returncode}.",
        )

    return ToolAvailability(
        tool_name=tool_name,
        available=True,
        version=output.splitlines()[0] if output else None,
        error_message=None,
    )


def check_docker_available() -> ToolAvailability:
    return check_host_tool_available("docker", ["docker", "--version"])


def check_sandbox_image_available(image_name: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image_name],
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False

    return result.returncode == 0


def build_sandbox_tooling_info(
    sandbox_image: str = "foundry-sandbox:latest",
) -> SandboxToolingInfo:
    docker = check_docker_available()
    host_forge = check_host_tool_available("forge", ["forge", "--version"])
    sandbox_image_available = check_sandbox_image_available(sandbox_image) if docker.available else False
    warnings: list[str] = []
    notes: list[str] = [
        "Host forge detection is diagnostic only; Week 3 reproduction must use a sandbox.",
        "foundry_available_in_sandbox is not checked until the sandbox runner exists.",
    ]

    if host_forge.available and not sandbox_image_available:
        warnings.append("Host forge detected, but Week 3 reproduction must run in sandbox, not on host.")
    if not docker.available:
        warnings.append("Docker is required for future sandboxed reproduction.")
    if docker.available and not sandbox_image_available:
        warnings.append(f"Sandbox image {sandbox_image} is not available locally.")

    return SandboxToolingInfo(
        sandbox_required=True,
        sandbox_image=sandbox_image,
        sandbox_available=docker.available and sandbox_image_available,
        docker_available=docker.available,
        foundry_available_in_sandbox=False,
        host_foundry_available=host_forge.available,
        warnings=warnings,
        notes=notes,
    )


def build_project_tooling_report(
    project_id: str,
    repo_path: Path,
    sandbox_image: str = "foundry-sandbox:latest",
) -> ProjectToolingReport:
    foundry = detect_foundry_project(repo_path)
    sandbox = build_sandbox_tooling_info(sandbox_image)

    if foundry.is_foundry_project and sandbox.sandbox_available:
        status = "ready_for_reproduction_setup"
    elif foundry.is_foundry_project:
        status = "sandbox_missing"
    elif foundry.has_foundry_toml or foundry.has_src_dir:
        status = "foundry_project_incomplete"
    else:
        status = "unsupported"

    return ProjectToolingReport(
        project_id=project_id,
        foundry=foundry,
        sandbox=sandbox,
        status=status,
    )

