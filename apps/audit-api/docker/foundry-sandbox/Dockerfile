FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L https://foundry.paradigm.xyz | bash \
    && /root/.foundry/bin/foundryup \
    && cp /root/.foundry/bin/forge /usr/local/bin/forge \
    && cp /root/.foundry/bin/cast /usr/local/bin/cast \
    && cp /root/.foundry/bin/anvil /usr/local/bin/anvil \
    && rm -rf /root/.foundry

RUN useradd --uid 1000 --create-home --shell /bin/bash sandbox \
    && mkdir -p /workspace/repo \
    && chown -R sandbox:sandbox /workspace

USER sandbox
RUN mkdir -p /tmp/foundry-warmup/src \
    && printf '[profile.default]\nsrc = "src"\nsolc_version = "0.8.20"\n' > /tmp/foundry-warmup/foundry.toml \
    && printf '// SPDX-License-Identifier: MIT\npragma solidity ^0.8.20;\ncontract Warmup {}\n' > /tmp/foundry-warmup/src/Warmup.sol \
    && cd /tmp/foundry-warmup \
    && forge build \
    && rm -rf /tmp/foundry-warmup

WORKDIR /workspace/repo

CMD ["forge", "--version"]
