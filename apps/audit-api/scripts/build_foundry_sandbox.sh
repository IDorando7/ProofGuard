#!/usr/bin/env bash
set -euo pipefail

docker build -t foundry-sandbox:latest docker/foundry-sandbox

echo "Built foundry-sandbox:latest"
echo "Test it with:"
echo "docker run --rm foundry-sandbox:latest forge --version"

