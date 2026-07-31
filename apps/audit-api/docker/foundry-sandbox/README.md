# Foundry Sandbox Image

This image provides the Foundry CLI tools used by the backend `SandboxedCommandRunner`.
It is intended for isolated execution of allowlisted Foundry commands against one mounted audit repository.

The image includes:

- `forge`
- `cast`
- `anvil`
- a pre-warmed Solidity `0.8.20` compiler cache for network-disabled benchmark runs
- a non-root `sandbox` user with UID `1000`
- default working directory `/workspace/repo`

## Build

```bash
docker build -t foundry-sandbox:latest docker/foundry-sandbox
```

## Test

Test that `forge` exists:

```bash
docker run --rm foundry-sandbox:latest forge --version
```

Test that `forge --version` works without runtime network access:

```bash
docker run --rm --network none foundry-sandbox:latest forge --version
```

For projects using Solidity versions not pre-warmed into the image, Foundry may try to download a compiler. Real reproduction should either use a pre-warmed compiler version or rebuild this image with the required compiler cached.

## Future Runner Usage

The backend runner will mount only the audit repository to `/workspace/repo`:

```bash
docker run --rm \
  --network none \
  --cpus 1 \
  --memory 1g \
  --pids-limit 256 \
  --tmpfs /tmp:rw,noexec,nosuid,size=256m \
  -v /absolute/path/to/audit/repo:/workspace/repo:rw \
  -w /workspace/repo \
  --user 1000:1000 \
  foundry-sandbox:latest \
  forge test --match-test testName
```

## Security Notes

- Run with `--network none` so PoCs cannot access external services.
- Run as non-root to limit container privileges.
- Do not pass secrets into the container.
- Do not mount home directories, SSH keys, cloud credentials, or `docker.sock`.
- The backend must still enforce Safety Preflight and CommandPolicy before using this image.
- Safety Preflight rejects `ffi = true` and unsafe `vm.ffi` usage before execution.
- This image alone is not a complete security boundary; it is one layer in the reproduction pipeline.
