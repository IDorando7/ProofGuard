# Finding clusters and validation truth

`FindingCluster` is an immutable grouping of routed candidate reports that appear
to describe the same deterministic root cause. Finalizing a cluster freezes its
membership, canonical identity, operator attribution, and source fingerprint so a
validator committee has a stable work unit. It does **not** prove that the claimed
vulnerability is real.

The previous implicit lifecycle required a Week 4 accepted/reproduced finding to
create a cluster. Week 8 then required that cluster before it could assign the
independent reproductions intended to establish network truth. That produced the
circular dependency `reproduction -> cluster -> validator reproduction`.

The corrected lifecycle is:

```text
candidate routed reports
  -> deterministic root-cause cluster
  -> frozen membership snapshot
  -> conflict-free validator committee
  -> independent sandbox reproductions and attestations
  -> quorum + supermajority consensus
  -> final cluster validation truth
  -> reward eligibility
```

Before a terminal consensus, the cluster uses
`validation_authority=pending_validator_consensus`, has no
`final_validation_status`, and has no `final_severity`. `claimed_severity` remains
available for presentation but is not economic truth. A finalized terminal
consensus is linked back by consensus ID and source fingerprint. Only `CONFIRMED`
maps to accepted miner-reward eligibility; rejected, out-of-scope, insufficient,
unsafe, unsupported, disputed, no-quorum, and pending states remain in audit
history but cannot enter the Week 7 miner pool.

Legacy Week 4 clusters with genuine accepted and reproduced evidence remain
supported under `validation_authority=legacy_backend`. Week 4 itself still returns
`NEEDS_REVIEW` when reproduction evidence is absent.

Current Week 5/6 agent history and per-report Week 7 quality records still require
their original Week 4 validation/reproduction sources. A Week 8 resolution is not
silently rewritten into those records: bulk quality rebuild skips such reports
until an explicit consensus-to-agent outcome adapter is approved. Cluster-level
miner eligibility and validator accounting remain independently gated by the
resolved consensus recorded on the cluster.
