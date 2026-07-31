from pathlib import Path


APP_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_DIR = APP_ROOT / "docker" / "foundry-sandbox"


def test_foundry_sandbox_files_exist():
    assert (SANDBOX_DIR / "Dockerfile").is_file()
    assert (SANDBOX_DIR / "README.md").is_file()
    assert (SANDBOX_DIR / ".dockerignore").is_file()


def test_dockerfile_creates_and_uses_sandbox_user():
    dockerfile = (SANDBOX_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "useradd --uid 1000" in dockerfile
    assert "sandbox" in dockerfile
    assert "USER sandbox" in dockerfile


def test_dockerfile_sets_workspace_workdir():
    dockerfile = (SANDBOX_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "WORKDIR /workspace/repo" in dockerfile


def test_dockerfile_installs_foundry_tools_to_path():
    dockerfile = (SANDBOX_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "/usr/local/bin/forge" in dockerfile
    assert "/usr/local/bin/cast" in dockerfile
    assert "/usr/local/bin/anvil" in dockerfile


def test_dockerfile_prewarms_solidity_compiler_cache():
    dockerfile = (SANDBOX_DIR / "Dockerfile").read_text(encoding="utf-8")

    assert "solc_version = \"0.8.20\"" in dockerfile
    assert "forge build" in dockerfile


def test_dockerfile_does_not_hardcode_secret_environment_names():
    dockerfile = (SANDBOX_DIR / "Dockerfile").read_text(encoding="utf-8")
    forbidden_names = [
        "PRIVATE_KEY",
        "MNEMONIC",
        "RPC_URL",
        "OPENAI_API_KEY",
        "ETHERSCAN_API_KEY",
        "DATABASE_URL",
        "JWT_SECRET",
    ]

    for name in forbidden_names:
        assert name not in dockerfile


def test_dockerignore_excludes_common_secret_and_runtime_paths():
    dockerignore = (SANDBOX_DIR / ".dockerignore").read_text(encoding="utf-8")

    for pattern in [
        ".env",
        "*.pem",
        "*.key",
        "id_rsa",
        "id_ed25519",
        ".ssh",
        "node_modules",
        ".git",
        "**pycache**",
        ".pytest_cache",
        "data/",
        "reports/",
        "findings/",
        "reproductions/",
    ]:
        assert pattern in dockerignore
