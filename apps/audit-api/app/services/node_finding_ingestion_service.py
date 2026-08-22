from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.schemas.agent_execution import AgentExecutionRecord
from app.schemas.finding import Finding, FindingCreate, FindingStatus
from app.schemas.submission import SubmissionCreate, SubmissionRecord
from app.services import submission_service
from app.services.finding_service import save_finding_exclusively
from app.utils.protocol_serialization import protocol_fingerprint


class FindingIngestionError(ValueError):
    pass


@dataclass(frozen=True)
class IngestedCandidate:
    finding: Finding
    submission: SubmissionRecord
    finding_created: bool
    submission_created: bool
    candidate_fingerprint: str


@dataclass(frozen=True)
class FindingIngestionResult:
    items: tuple[IngestedCandidate, ...]

    @property
    def finding_ids(self) -> list[str]:
        return sorted({item.finding.finding_id for item in self.items})

    @property
    def submission_ids(self) -> list[str]:
        return sorted({item.submission.submission_id for item in self.items})


class NodeFindingIngestionService:
    """Shared local/future-remote boundary into production Finding + Submission."""

    def __init__(self, protocol_data_root: Path, project_workspace: Path) -> None:
        self.protocol_data_root = protocol_data_root
        self.project_workspace = project_workspace

    def ingest(
        self,
        execution: AgentExecutionRecord,
        candidates: list[FindingCreate],
    ) -> FindingIngestionResult:
        ingested: list[IngestedCandidate] = []
        seen: set[str] = set()
        for candidate in candidates:
            validated = FindingCreate.model_validate(candidate)
            if validated.category != execution.category:
                raise FindingIngestionError(
                    "Finding candidate category does not match its execution"
                )
            if validated.status != FindingStatus.CANDIDATE:
                raise FindingIngestionError(
                    "Agent outputs must remain candidate findings"
                )
            candidate_fingerprint = protocol_fingerprint(
                {
                    "candidate_version": "agent_candidate_v1",
                    "finding": validated.model_dump(),
                }
            )
            if candidate_fingerprint in seen:
                continue
            seen.add(candidate_fingerprint)
            finding_id = _finding_id(
                execution.agent_execution_id, candidate_fingerprint
            )
            proposed = Finding(
                finding_id=finding_id,
                project_id=execution.project_id,
                **validated.model_dump(),
            )
            finding_hash = submission_service.compute_finding_hash(
                execution.project_id, proposed
            )
            duplicate = submission_service.find_submission_by_node_and_hash(
                self.protocol_data_root,
                execution.project_id,
                execution.node_id,
                finding_hash,
            )
            if duplicate is not None:
                from app.services.finding_service import load_project_finding

                existing_finding = load_project_finding(
                    self.project_workspace, duplicate.finding_id
                )
                if existing_finding is None:
                    raise FindingIngestionError(
                        "Existing duplicate submission references a missing finding"
                    )
                ingested.append(
                    IngestedCandidate(
                        finding=existing_finding,
                        submission=duplicate,
                        finding_created=False,
                        submission_created=False,
                        candidate_fingerprint=candidate_fingerprint,
                    )
                )
                continue

            finding, finding_created = save_finding_exclusively(
                self.project_workspace, proposed
            )
            submission = submission_service.create_submission(
                self.protocol_data_root,
                self.project_workspace,
                SubmissionCreate(
                    project_id=execution.project_id,
                    finding_id=finding.finding_id,
                    node_id=execution.node_id,
                    agent_name=execution.agent_type,
                    agent_version=execution.agent_version,
                    routing_id=execution.routing_id,
                    routing_assignment_id=execution.routing_assignment_id,
                    metadata={
                        "audit_run_id": execution.audit_run_id,
                        "agent_execution_id": execution.agent_execution_id,
                        "candidate_fingerprint": candidate_fingerprint,
                    },
                ),
            )
            ingested.append(
                IngestedCandidate(
                    finding=finding,
                    submission=submission,
                    finding_created=finding_created,
                    submission_created=True,
                    candidate_fingerprint=candidate_fingerprint,
                )
            )
        return FindingIngestionResult(tuple(ingested))


def _finding_id(agent_execution_id: str, candidate_fingerprint: str) -> str:
    digest = hashlib.sha256(
        f"finding_ingestion_v1\0{agent_execution_id}\0{candidate_fingerprint}".encode(
            "utf-8"
        )
    ).hexdigest()
    return f"finding_{digest[:40]}"
