# ProofGuard On-Chain / Off-Chain Boundary v0

Status: design draft. No blockchain integration or smart contracts are
implemented.

## 1. Principle

A future blockchain may be used for commitments, accountability, stake,
reputation checkpoints, and rewards. It is not a confidential compute or code
execution environment for ProofGuard.

Blockchain is not used to execute:

- AI models;
- repository analysis;
- Docker;
- Foundry;
- PoCs;
- full validation reasoning; or
- complete audit reports.

All code execution remains off-chain. Untrusted repositories and PoCs must pass
authorization and safety controls and execute only in the configured sandbox.
Validators validate findings using off-chain evidence; a blockchain can at most
record commitments to their decisions. Contribution scoring measures value
after validation and cannot override an invalid validation status.

See [Protocol v0](protocol-v0.md) for actor responsibilities,
[message-types.md](message-types.md) for commitment-bearing messages, and
[state-machine.md](state-machine.md) for finalization rules.

## 2. Off-chain data

The following remains off-chain:

- full repository source code;
- private GitHub credentials;
- `scope.yaml` contents when sensitive;
- complete candidate findings;
- root cause descriptions;
- attack paths;
- impact details;
- recommended fixes;
- PoC source code;
- Foundry tests;
- stdout and stderr;
- sandbox logs;
- validator reasoning;
- report content;
- private client metadata; and
- unpublished vulnerability details.

This data is private, large, expensive to publish, or executable. Public storage
would conflict with coordinated disclosure, create avoidable security risk, and
make deletion or access revocation impossible. Off-chain placement is necessary
but not sufficient: future systems still need encryption, least-privilege access,
retention controls, audit logs, and secure deletion policies.

## 3. Potential future on-chain data

Only compact commitments and public accountability data are candidates for a
future chain:

- node public key or wallet identity;
- node type;
- stake balance;
- project or audit commitment hash;
- scope hash;
- finding submission hash;
- PoC commitment hash;
- validation decision hash;
- contribution score commitment;
- category reputation checkpoint;
- reward amount;
- reward claim status;
- slash event commitment;
- timestamps or block numbers; and
- protocol version.

“Potential” is deliberate. Public metadata can itself leak relationships or
timing, so each field requires a privacy and cost review before implementation.
An on-chain hash proves only that someone committed to specific bytes; it does
not prove that an agent found a real vulnerability or that a validator reproduced
it honestly.

## 4. Data classification table

| Data | Location | Public? | Reason |
| --- | --- | --- | --- |
| Repository source | Off-chain | Private or authorized | Sensitive and too large |
| Full finding | Off-chain | Restricted | Unpublished vulnerability |
| Finding hash | Future on-chain | Public | Integrity commitment |
| PoC source | Off-chain | Restricted | Executable exploit evidence |
| PoC hash | Future on-chain | Public | Commitment without disclosure |
| Validation reasoning | Off-chain | Restricted | May reveal exploit details |
| Validation status commitment | Future on-chain | Public | Accountability |
| Reputation checkpoint | Future on-chain | Public | Node accountability |
| Reward amount | Future on-chain | Public | Transparent economics |
| Private keys | Never stored by protocol | Never | Critical secret |

Wallet or signing software may use a private key locally, but ProofGuard storage,
messages, logs, repositories, and contracts must never contain that key.

## 5. Commitment model

```text
canonical payload
   |
   v
SHA-256
   |
   v
commitment hash
```

- Canonical serialization must be deterministic across all implementations.
- The committed domain should include the protocol version and payload type so
  the same bytes cannot be interpreted in another context.
- A hash proves consistency with revealed data, not correctness, validity,
  ownership, timing, or truthful analysis.
- A random salt may be required to prevent dictionary guessing of small or
  predictable private payloads, such as a short validation status.
- The salt must remain available to authorized parties that later reveal and
  verify the commitment.
- The exact canonicalization, domain separator, salting, and commitment format
  are not implemented on Day 1.

Illustrative only:

```text
SHA-256("proofguard:0.1:finding_submission:" || canonical_payload || salt)
```

The example is not an adopted wire format and must not be treated as one.

## 6. Confidential disclosure lifecycle

A future confidential flow is:

1. The client proves or attests authorization for the audit.
2. An assigned agent receives only the authorized code and scope.
3. The complete finding remains encrypted or access-controlled off-chain.
4. An assigned validator receives only the disclosure required to reproduce and
   evaluate that finding.
5. A future on-chain transaction receives a salted commitment, not the full
   finding, PoC, logs, or reasoning.
6. The complete report is disclosed to the authorized client through an
   authenticated off-chain channel.
7. Public disclosure occurs only after remediation or the client and protocol
   participants reach the agreed disclosure timeline.

Encryption, identity-based access control, key distribution, revocation,
secure viewer design, and disclosure governance are future work. The current MVP
uses local workspace access controls and must not be described as providing a
complete confidential-disclosure protocol.

## 7. Threat model

| Threat | Current mitigation | Future mitigation |
| --- | --- | --- |
| Malicious agent submits a fabricated finding | Findings remain `candidate`/`unverified`; the central pipeline requires scope and reproduced evidence for `accepted`. | Signed submissions, category reputation, independent reproduction, multiple validators, challenge periods, and possible stake. |
| Malicious PoC attempts host escape | Safety Preflight, command allowlisting, Docker network isolation, resource limits, non-root execution, and no host secrets or Docker socket mounts. Unsafe PoCs receive reproduction status `rejected_unsafe` and are not executed. | Hardened executor attestation, sandbox-profile commitments, independent sandbox implementations, and incident review. |
| Validator lies about reproduction | The current validator is a trusted centralized deterministic pipeline reading stored reproduction results; it does not independently provide decentralized assurance. | Validator signatures, evidence commitments, multiple independent reproductions, randomized assignment, stake, challenges, and reputation. |
| Coordinator modifies off-chain content | Local records and separation of finding/reproduction/decision data improve traceability, but the coordinator remains trusted in v0. | Deterministic salted commitments, actor signatures, append-only logs, client-held receipts, and challengeable on-chain checkpoints. |
| Public hash leaks information through dictionary guessing | Nothing is published on-chain today. | High-entropy salts, domain separation, delayed commitments where necessary, and privacy review before publication. |
| Node creates Sybil identities | Nodes are local modules/internal IDs today; there is no permissionless node network. | Allowlisting first, identity or stake costs, rate limits, category reputation, and later Sybil-resistant admission policy. |
| Operator replays an old submission | Existing storage IDs and deduplication reduce some duplicates but are not a protocol replay defense. | Signed unique message IDs, nonces or sequences, expiry/round binding, and durable processed-message tracking. |
| Client submits an unauthorized repository | The specification requires client authorization; uploaded code is treated as untrusted. There is no complete authorization-verification system. | Client attestations, terms/audit records, access checks, allowlisted sources, and auditable project registration. |
| Validator and agent collude | Roles are conceptually separate, but the current centralized MVP has no collusion-resistant network. | Conflict-of-interest rules, independent assignment, multiple validators, commit/reveal where appropriate, challenges, stake, and category reputation. |
| Reward engine is manipulated | Reward Simulator v0 uses finalized-decision inputs, deterministic versioned rules, source fingerprints, a capped project pool, and idempotent off-chain events. It transfers no tokens and remains coordinator-trusted. | Public commitments, audits, multi-party governance, and dispute windows. |

Stake, multiple validators, challenge periods, consensus, and Sybil/collusion
resistance are future possibilities, not present protections. Human review
remains important for authorization, ambiguous evidence, unsafe artifacts, and
serious penalty proposals.

## 8. MVP boundary

Currently implemented:

- local project workspaces;
- centralized off-chain UUID node records with optional opaque public keys;
- separate off-chain finding submission records and deterministic finding hashes;
- separate off-chain deterministic contribution score records;
- separate off-chain global reputation events and current node scores;
- separate off-chain reward cycles, simulated protocol-point events, and unsafe penalty events;
- findings with candidate/unverified semantics;
- reproduction results;
- validation decisions; and
- final audit reports.

These components use a centralized FastAPI application, local files, SQLite,
Safety Preflight, a Docker/Foundry sandbox path, and a deterministic Validator
Pipeline v0.

Not currently implemented:

- cryptographic verification of public-key ownership;
- signatures;
- on-chain commitments;
- stake;
- token rewards;
- blockchain reputation;
- disputes; and
- consensus.

Also not implemented are category-specific reputation or reward economics,
financial slashing, validator subnets, or signed protocol message APIs. Node
Registry v0, Finding Submission Protocol v0, Contribution Scoring Engine v0,
Reputation Engine v0, Reward Simulator v0, and Penalty Event v0 are centralized
off-chain services, not on-chain or permissionless systems.

## 9. Future smart-contract modules

Conceptual modules only:

- `NodeRegistry`
- `StakeManager`
- `SubmissionCommitments`
- `ValidationCommitments`
- `ReputationRegistry`
- `RewardDistributor`
- `SlashingRegistry`
- `DisputeManager`

No Solidity contracts are written or deployed for Protocol v0 Day 1. Module
names do not settle interfaces, upgradeability, governance, chain selection, or
whether every module should ultimately exist on-chain.
