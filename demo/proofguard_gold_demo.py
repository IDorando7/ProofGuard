#!/usr/bin/env python3
"""
ProofGuard Gold E2E Demo Driver
==============================

Generated from the uploaded ProofGuard OpenAPI 0.1.0 contract.

Purpose
-------
Drive the REAL FastAPI backend through a presentation-friendly E2E scenario,
while the React frontend is open in a browser.

The script:
- seeds a small demo node network;
- bootstraps/refreshes the relevant agent subnets;
- creates a project from scope.yaml + repo ZIP;
- prepares the project;
- creates/starts a local-simulator AuditRun;
- observes the legacy Week 4 NEEDS_REVIEW result without treating it as truth;
- rebuilds/finalizes FindingClusters;
- creates validator committees;
- runs validator reproduction through the existing Week 3 sandbox path;
- creates a deliberate 3-vs-2 dispute on the first cluster;
- escalates that dispute with four NEW validators;
- produces a 3 ACCEPT / 6 REJECT cumulative resolution;
- evaluates validator performance against the final resolution;
- rebuilds agent historical performance;
- creates the client TaskRewardBudget;
- attempts Week 7 miner rewards;
- attempts Week 8 validator rewards;
- generates the final report;
- saves every response and pauses at presentation checkpoints.

It does NOT bypass sandboxing, generate exploit payloads, or interact with live
targets. Use only with a synthetic/local audit target you are authorized to test.

Important
---------
Use a CLEAN DEMO DB / DATA DIRECTORY for every gold run. Node registration is
intentionally explicit so the frontend has a meaningful network to display.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def pretty(obj: Any) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=False)


def slug(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


@dataclass
class StepArtifact:
    index: int
    name: str
    method: str
    path: str
    status_code: int
    response: Any


class DemoAPI:
    def __init__(self, base_url: str, artifact_dir: Path):
        self.base_url = base_url.rstrip("/")
        self.artifact_dir = artifact_dir
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.session = requests.Session()
        self.step_no = 0
        self.steps: list[StepArtifact] = []

    def _save(self, artifact: StepArtifact) -> None:
        p = self.artifact_dir / f"{artifact.index:03d}_{slug(artifact.name)}.json"
        p.write_text(pretty({
            "index": artifact.index,
            "name": artifact.name,
            "method": artifact.method,
            "path": artifact.path,
            "status_code": artifact.status_code,
            "response": artifact.response,
        }), encoding="utf-8")

    def request(
        self,
        name: str,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        expected: Iterable[int] = (200, 201),
        timeout: int = 240,
    ) -> Any:
        self.step_no += 1
        url = self.base_url + path
        print(f"\n[{self.step_no:03d}] {name}")
        print(f"{method.upper()} {url}")

        r = self.session.request(
            method.upper(),
            url,
            json=json_body,
            data=data,
            files=files,
            params=params,
            timeout=timeout,
        )

        try:
            body = r.json()
        except Exception:
            body = {"_raw_text": r.text}

        artifact = StepArtifact(
            index=self.step_no,
            name=name,
            method=method.upper(),
            path=path,
            status_code=r.status_code,
            response=body,
        )
        self.steps.append(artifact)
        self._save(artifact)

        print(f"HTTP {r.status_code}")
        if r.status_code not in set(expected):
            print(pretty(body)[:6000])
            raise RuntimeError(
                f"{name} failed: expected HTTP {sorted(set(expected))}, "
                f"got {r.status_code}"
            )
        return body

    def get(self, name: str, path: str, **kwargs: Any) -> Any:
        return self.request(name, "GET", path, **kwargs)

    def post(self, name: str, path: str, **kwargs: Any) -> Any:
        return self.request(name, "POST", path, **kwargs)


class DemoContext:
    def __init__(self, artifact_dir: Path, frontend_url: str, pause: bool):
        self.artifact_dir = artifact_dir
        self.frontend_url = frontend_url.rstrip("/")
        self.pause = pause
        self.checkpoints: list[dict[str, Any]] = []

    def checkpoint(
        self,
        title: str,
        *,
        frontend_hint: str = "",
        message: str = "",
        important: bool = True,
    ) -> None:
        n = len(self.checkpoints) + 1
        url = self.frontend_url + frontend_hint if frontend_hint else self.frontend_url
        entry = {
            "checkpoint": n,
            "title": title,
            "frontend_url_hint": url,
            "message": message,
            "important_for_powerpoint": important,
        }
        self.checkpoints.append(entry)
        (self.artifact_dir / "presentation_checkpoints.json").write_text(
            pretty(self.checkpoints), encoding="utf-8"
        )

        print("\n" + "=" * 90)
        print(f"SCREENSHOT CHECKPOINT {n}: {title}")
        print(f"Frontend hint: {url}")
        if message:
            print(message)
        print("=" * 90)

        if self.pause:
            input("Open/refresh the frontend, take the screenshot, then press ENTER... ")


# ---------------------------------------------------------------------------
# Demo data
# ---------------------------------------------------------------------------

def agent_seed() -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    for i in range(1, 5):
        nodes.append({
            "display_name": f"Demo Access Agent {i:02d}",
            "node_type": "agent",
            "operator_id": f"demo-agent-access-op-{i:02d}",
            "supported_categories": ["access_control"],
            "description": "Synthetic demo discovery node",
        })

    for i in range(1, 5):
        nodes.append({
            "display_name": f"Demo Reentrancy Agent {i:02d}",
            "node_type": "agent",
            "operator_id": f"demo-agent-reentrancy-op-{i:02d}",
            "supported_categories": ["reentrancy"],
            "description": "Synthetic demo discovery node",
        })

    return nodes


def validator_seed() -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []

    # Enough independent operators for:
    #   Round 1: 5 authoritative + optional shadow
    #   Round 2: +4 new authoritative
    # plus spare capacity.
    for i in range(1, 13):
        nodes.append({
            "display_name": f"Demo Validator {i:02d}",
            "node_type": "validator",
            "operator_id": f"demo-validator-op-{i:02d}",
            "supported_categories": ["access_control", "reentrancy"],
            "description": "Synthetic demo validator operator",
        })

    for i in range(1, 3):
        nodes.append({
            "display_name": f"Demo Validation Hybrid {i:02d}",
            "node_type": "hybrid",
            "operator_id": f"demo-validator-hybrid-op-{i:02d}",
            "supported_categories": ["access_control", "reentrancy"],
            "description": "Synthetic demo hybrid used on validation side",
        })

    return nodes


# ---------------------------------------------------------------------------
# Core workflow helpers
# ---------------------------------------------------------------------------

def seed_nodes(api: DemoAPI, ctx: DemoContext) -> dict[str, list[dict[str, Any]]]:
    created_agents: list[dict[str, Any]] = []
    created_validators: list[dict[str, Any]] = []

    print("\nSeeding discovery nodes...")
    for payload in agent_seed():
        created_agents.append(
            api.post(
                f"register_{payload['operator_id']}",
                "/nodes",
                json_body=payload,
                expected=(201,),
            )
        )

    # Runtime registration is process-local, so the FastAPI process performs the
    # demo-only runtime + historical calibration bootstrap. Run it before adding
    # validator nodes so the discovery membership snapshot remains specialist-only.
    demo_bootstrap = api.post(
        "bootstrap_gold_demo_network",
        "/demo/bootstrap",
    )

    subnet_ids: dict[str, str] = {}
    for category in ("access_control", "reentrancy"):
        subnet = api.get(
            f"get_subnet_{category}",
            f"/subnets/by-category/{category}",
        )
        subnet_id = subnet["subnet_id"]
        subnet_ids[category] = subnet_id

    print("\nSeeding validation nodes...")
    for payload in validator_seed():
        created_validators.append(
            api.post(
                f"register_{payload['operator_id']}",
                "/nodes",
                json_body=payload,
                expected=(201,),
            )
        )

    nodes = api.get("list_all_nodes_after_seed", "/nodes")
    ctx.checkpoint(
        "Initial ProofGuard node network",
        frontend_hint="/nodes",
        message=(
            "Show node_id, operator_id, Agent/Validator/Hybrid role, supported "
            "categories and active status. This is the opening network-state screenshot."
        ),
    )

    return {
        "agents": created_agents,
        "demo_bootstrap": demo_bootstrap,
        "validators": created_validators,
        "subnet_ids": subnet_ids,
        "all_nodes": nodes,
    }


def create_project(
    api: DemoAPI,
    ctx: DemoContext,
    project_name: str,
    scope_file: Path,
    repo_zip: Path | None,
    github_url: str | None,
) -> dict[str, Any]:
    if not scope_file.exists():
        raise FileNotFoundError(scope_file)
    if repo_zip is None and not github_url:
        raise ValueError("Provide either --repo-zip or --github-url.")

    data: dict[str, Any] = {"project_name": project_name}
    file_handles = []

    try:
        scope_handle = scope_file.open("rb")
        file_handles.append(scope_handle)
        files: dict[str, Any] = {
            "scope_file": (scope_file.name, scope_handle, "application/yaml")
        }

        if repo_zip:
            if not repo_zip.exists():
                raise FileNotFoundError(repo_zip)
            repo_handle = repo_zip.open("rb")
            file_handles.append(repo_handle)
            files["repo_zip"] = (repo_zip.name, repo_handle, "application/zip")

        if github_url:
            data["github_url"] = github_url

        project = api.post(
            "create_demo_project",
            "/projects",
            data=data,
            files=files,
            expected=(200,),
            timeout=300,
        )
    finally:
        for fh in file_handles:
            fh.close()

    project_id = project["project_id"]

    api.post(
        "prepare_project",
        f"/projects/{project_id}/prepare",
        json_body=None,
        timeout=300,
    )

    # The OpenAPI intentionally exposes project preparation status as a free
    # string. Poll conservatively: fail on last_error; continue once status is
    # no longer a common transient value.
    transient = {"created", "creating", "preparing", "pending", "queued", "running"}
    deadline = time.time() + 180
    last_status = None
    while time.time() < deadline:
        state = api.get(
            "poll_project_status",
            f"/projects/{project_id}/status",
        )
        last_status = str(state.get("status", "")).strip()
        if state.get("last_error"):
            raise RuntimeError(f"Project preparation failed: {state['last_error']}")
        if last_status and last_status.lower() not in transient:
            break
        time.sleep(1)
    else:
        raise TimeoutError(
            f"Project did not leave preparation state in time. Last status={last_status!r}"
        )

    scope = api.get(
        "read_project_scope",
        f"/projects/{project_id}/scope",
    )

    ctx.checkpoint(
        "Audit target ingested and scope parsed",
        frontend_hint="/audits",
        message=(
            f"Project {project_id}. Show repository/source, scope, attack categories "
            "and current project status."
        ),
    )

    return {
        "project": project,
        "project_id": project_id,
        "scope": scope,
        "status": last_status,
    }


def run_agent_audit(api: DemoAPI, ctx: DemoContext, project_id: str) -> dict[str, Any]:
    run = api.post(
        "create_audit_run",
        f"/projects/{project_id}/audit-runs",
        json_body={"execution_mode": "local_simulator"},
        expected=(201,),
    )
    audit_run_id = run["audit_run_id"]

    # The API description explicitly says START may only leave an unprepared
    # project in PREPARING. Retry the same AuditRun start if routing_id is still
    # absent, while preserving the same run identity.
    started = None
    for attempt in range(1, 4):
        started = api.post(
            f"start_audit_run_attempt_{attempt}",
            f"/projects/{project_id}/audit-runs/{audit_run_id}/start",
            json_body={},
            timeout=600,
        )
        if started.get("routing_id"):
            break

        state = api.get(
            f"project_status_before_start_retry_{attempt}",
            f"/projects/{project_id}/status",
        )
        if state.get("last_error"):
            raise RuntimeError(state["last_error"])
        print(
            "AuditRun does not have routing_id yet. "
            "Waiting for project preparation and retrying START..."
        )
        time.sleep(2)

    if not started or not started.get("routing_id"):
        raise RuntimeError(
            "AuditRun did not reach routing/agent execution. "
            "Inspect the saved AuditRun and ProjectStatus artifacts."
        )

    routing_id = started["routing_id"]

    routing = api.get(
        "get_routing_plan",
        f"/projects/{project_id}/routing/{routing_id}",
    )
    ctx.checkpoint(
        "Agent routing plan",
        frontend_hint=f"/audits/{project_id}/{routing_id}",
        message=(
            "Show category routing, production/ranked versus exploration assignments, "
            "CategoryScore, membership and node/operator identity."
        ),
    )

    agent_execs = api.get(
        "list_agent_executions",
        f"/projects/{project_id}/audit-runs/{audit_run_id}/agent-executions",
    )
    submissions = api.get(
        "list_agent_submissions",
        f"/projects/{project_id}/submissions",
    )

    ctx.checkpoint(
        "Independent agent submissions",
        frontend_hint=f"/audits/{project_id}/{routing_id}",
        message=(
            "Show candidate findings by node/operator/category. Explain that agents "
            "propose claims; they do not establish protocol truth."
        ),
    )

    if not submissions:
        raise RuntimeError(
            "The local simulator produced zero submissions. "
            "Inspect routing and agent execution artifacts before continuing."
        )

    return {
        "audit_run_id": audit_run_id,
        "routing_id": routing_id,
        "routing": routing,
        "agent_executions": agent_execs,
        "submissions": submissions,
    }


def observe_legacy_validation(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
    submissions: list[dict[str, Any]],
) -> dict[str, Any]:
    """
    Show that Week 4 correctly remains unresolved before reproduction.

    FindingCluster construction no longer consumes this as accepted truth. Agent
    contribution, reputation, category performance, CategoryScore, and membership
    updates for the current audit are deliberately deferred because those legacy
    services still require genuine Week 4 ReproductionResult/ValidationDecision
    sources. The demo never fabricates those records.
    """
    validations = api.post(
        "observe_legacy_validate_all",
        f"/projects/{project_id}/validate-all",
        timeout=900,
    )
    finding_ids = {submission["finding_id"] for submission in submissions}
    current = [item for item in validations if item["finding_id"] in finding_ids]
    if len(current) != len(finding_ids) or any(
        item["status"] != "needs_review" for item in current
    ):
        raise RuntimeError(
            "Gold Demo expected Week 4 NEEDS_REVIEW before validator reproduction."
        )
    return {
        "validations": validations,
        "agent_history_update": "deferred_until_protocol_approved_consensus_adapter",
    }


def build_clusters(
    api: DemoAPI,
    ctx: DemoContext,
    project_id: str,
    routing_id: str,
) -> list[dict[str, Any]]:
    rebuilt = api.post(
        "rebuild_finding_clusters",
        f"/projects/{project_id}/routing/{routing_id}/finding-clusters/rebuild",
        json_body={},
    )
    api.post(
        "finalize_finding_clusters",
        f"/projects/{project_id}/routing/{routing_id}/finding-clusters/finalize",
        json_body={},
    )

    listed = api.get(
        "list_finding_clusters",
        f"/projects/{project_id}/routing/{routing_id}/finding-clusters",
    )
    clusters = listed["clusters"]

    ctx.checkpoint(
        "Candidate reports frozen into pre-consensus FindingClusters",
        frontend_hint=f"/audits/{project_id}/{routing_id}",
        message=(
            f"{len(clusters)} clusters available. Show canonical finding, report_count, "
            "distinct_operator_count, category, claimed severity, frozen membership, "
            "and pending validator-consensus truth."
        ),
    )

    if not clusters:
        raise RuntimeError(
            "No FindingClusters were produced. "
            "Inspect validation/submission artifacts."
        )

    return clusters


def prepare_validator_artifacts(
    api: DemoAPI,
    project_id: str,
    clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Ingest synthetic PoCs through the public API without executing Week 4."""
    fixtures = {
        "access_control": (
            "ProofGuardAccessControl.t.sol",
            "testUnauthorizedSetAdmin",
        ),
        "reentrancy": (
            "ProofGuardReentrancy.t.sol",
            "testReentrantWithdrawal",
        ),
    }
    fixture_root = Path(__file__).resolve().parent / "proofguard_demo_target" / "pocs"
    uploaded = []
    for cluster in clusters:
        category = cluster["category"]
        if category not in fixtures:
            raise RuntimeError(f"No synthetic validator PoC fixture for {category}")
        filename, test_name = fixtures[category]
        content = (fixture_root / filename).read_text(encoding="utf-8")
        uploaded.append(
            api.post(
                f"ingest_validator_artifact_{cluster['finding_cluster_id']}",
                f"/projects/{project_id}/findings/{cluster['canonical_finding_id']}/poc",
                json_body={
                    "poc_filename": filename,
                    "poc_content": content,
                    "test_name": test_name,
                },
            )
        )
    return uploaded


def create_committee(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
    cluster_id: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    committee = api.post(
        f"committee_create_{cluster_id}",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/finding-clusters/{cluster_id}/validator-committees"
        ),
        json_body={},
        expected=(201,),
    )
    cid = committee["validator_committee_id"]

    api.post(
        f"committee_finalize_{cluster_id}",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-committees/{cid}/finalize"
        ),
        json_body={},
    )

    assignments_response = api.get(
        f"committee_assignments_{cluster_id}",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-committees/{cid}/assignments"
        ),
    )
    return committee, assignments_response["assignments"]


def run_validator_assignment(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
    assignment: dict[str, Any],
    validity: str,
    severity: str | None,
) -> dict[str, Any]:
    aid = assignment["validator_assignment_id"]
    node_id = assignment["validator_node_id"]

    reproduction = api.post(
        f"validator_reproduction_{aid}",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-assignments/{aid}/reproduction"
        ),
        json_body={"validator_node_id": node_id},
        timeout=900,
    )

    if validity == "accepted":
        root = "confirmed"
        impact = "validated"
        normalized_severity = severity or "High"
    elif validity == "out_of_scope":
        root = "insufficient_evidence"
        impact = "insufficient_evidence"
        normalized_severity = None
    else:
        root = "mismatch"
        impact = "rejected"
        normalized_severity = None

    attestation = api.post(
        f"validator_attestation_{aid}_{validity}",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-assignments/{aid}/attestations"
        ),
        json_body={
            "validator_node_id": node_id,
            "validator_reproduction_id": reproduction["validator_reproduction_id"],
            "validity_decision": validity,
            "root_cause_decision": root,
            "normalized_severity": normalized_severity,
            "impact_decision": impact,
            "reason_codes": [],
            "evidence_references": [],
        },
    )
    return {
        "assignment": assignment,
        "reproduction": reproduction,
        "attestation": attestation,
    }


def calculate_and_finalize_consensus(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
    cluster_id: str,
    label: str,
) -> dict[str, Any]:
    consensus = api.post(
        f"{label}_consensus_calculate",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/finding-clusters/{cluster_id}/validation-consensus"
        ),
        json_body={},
        expected=(201,),
    )
    cid = consensus["validation_consensus_id"]

    finalized = api.post(
        f"{label}_consensus_finalize",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validation-consensus/{cid}/finalize"
        ),
        json_body={},
    )
    return finalized


def escalate_dispute(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
    cluster_id: str,
    dispute_id: str,
    decision: str,
    severity: str | None,
) -> dict[str, Any]:
    escalation = api.post(
        f"escalate_{cluster_id}",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validation-disputes/{dispute_id}/escalate"
        ),
        json_body={},
    )
    committee_id = escalation.get("validator_committee_id")
    if not committee_id:
        raise RuntimeError(
            f"Escalation for {cluster_id} did not create a committee: {escalation}"
        )

    api.post(
        f"escalation_committee_finalize_{cluster_id}",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-committees/{committee_id}/finalize"
        ),
        json_body={},
    )

    assignment_list = api.get(
        f"escalation_assignments_{cluster_id}",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-committees/{committee_id}/assignments"
        ),
    )["assignments"]

    authoritative = [
        a for a in assignment_list if a["assignment_role"] == "authoritative"
    ]
    shadow = [
        a for a in assignment_list if a["assignment_role"] == "shadow"
    ]

    if len(authoritative) != 4:
        raise RuntimeError(
            f"Expected 4 authoritative escalation validators, got {len(authoritative)}"
        )

    results = []
    for assignment in authoritative:
        results.append(
            run_validator_assignment(
                api,
                project_id,
                routing_id,
                assignment,
                decision,
                severity,
            )
        )

    # Shadow evidence is useful for Day 5 history but never authoritative.
    for assignment in shadow:
        results.append(
            run_validator_assignment(
                api,
                project_id,
                routing_id,
                assignment,
                decision,
                severity,
            )
        )

    return {
        "escalation": escalation,
        "assignments": assignment_list,
        "evidence": results,
    }


def validate_cluster_with_network(
    api: DemoAPI,
    ctx: DemoContext,
    project_id: str,
    routing_id: str,
    cluster: dict[str, Any],
    *,
    is_demo_dispute_cluster: bool,
) -> dict[str, Any]:
    cluster_id = cluster["finding_cluster_id"]
    severity = cluster.get("final_severity") or "High"

    committee, assignments = create_committee(
        api, project_id, routing_id, cluster_id
    )

    authoritative = [
        a for a in assignments if a["assignment_role"] == "authoritative"
    ]
    shadow = [
        a for a in assignments if a["assignment_role"] == "shadow"
    ]

    if len(authoritative) != 5:
        raise RuntimeError(
            f"STANDARD committee expected 5 authoritative assignments, "
            f"got {len(authoritative)} for {cluster_id}"
        )

    if is_demo_dispute_cluster:
        ctx.checkpoint(
            "Five independent validator operators selected",
            frontend_hint=f"/audits/{project_id}/{routing_id}",
            message=(
                "Show STANDARD committee: 5 authoritative seats, unique operator IDs, "
                "reporter conflicts excluded, and optional shadow validator."
            ),
        )

    evidence: list[dict[str, Any]] = []

    # First demo cluster: exactly 3 ACCEPT / 2 REJECT.
    # Other clusters: 4 ACCEPT / 1 REJECT to exercise the confirmed path when
    # independent reproduction also reaches the backend's required threshold.
    if is_demo_dispute_cluster:
        validity_vector = ["accepted", "accepted", "accepted", "rejected", "rejected"]
    else:
        validity_vector = ["accepted", "accepted", "accepted", "accepted", "rejected"]

    for assignment, validity in zip(authoritative, validity_vector):
        evidence.append(
            run_validator_assignment(
                api,
                project_id,
                routing_id,
                assignment,
                validity,
                severity,
            )
        )

    # Shadow validator receives evidence/performance but does not affect consensus.
    for assignment in shadow:
        evidence.append(
            run_validator_assignment(
                api,
                project_id,
                routing_id,
                assignment,
                "rejected" if is_demo_dispute_cluster else "accepted",
                severity,
            )
        )

    if is_demo_dispute_cluster:
        readiness = api.get(
            f"reproduction_readiness_{cluster_id}",
            (
                f"/projects/{project_id}/routing/{routing_id}"
                f"/validator-committees/{committee['validator_committee_id']}"
                f"/reproduction-readiness"
            ),
        )
        ctx.checkpoint(
            "Independent validator reproduction evidence",
            frontend_hint=f"/audits/{project_id}/{routing_id}",
            message=(
                "Show one independently attributable ValidatorReproductionRecord per "
                "authoritative validator. Highlight SafetyPreflight/sandbox-derived status."
            ),
        )

        api.get(
            f"attestations_before_consensus_{cluster_id}",
            (
                f"/projects/{project_id}/routing/{routing_id}"
                f"/finding-clusters/{cluster_id}/attestations"
            ),
        )
        ctx.checkpoint(
            "Three ACCEPT vs two REJECT attestations",
            frontend_hint=f"/audits/{project_id}/{routing_id}",
            message=(
                "Before consensus, show the 5 authoritative attestations. "
                "This prepares the most important management example."
            ),
        )

    first_consensus = calculate_and_finalize_consensus(
        api,
        project_id,
        routing_id,
        cluster_id,
        "round1",
    )

    result: dict[str, Any] = {
        "cluster": cluster,
        "committee": committee,
        "assignments": assignments,
        "round1_evidence": evidence,
        "round1_consensus": first_consensus,
        "final_consensus": first_consensus,
        "escalation": None,
    }

    if is_demo_dispute_cluster:
        if first_consensus["consensus_outcome"] != "disputed":
            raise RuntimeError(
                "The deliberate 3-vs-2 case did not produce DISPUTED. "
                f"Actual={first_consensus['consensus_outcome']}"
            )

        ctx.checkpoint(
            "3-vs-2 is DISPUTED — simple majority is not security truth",
            frontend_hint=f"/audits/{project_id}/{routing_id}",
            message=(
                "Show N=5, quorum=4, supermajority=4, ACCEPT=3, REJECT=2 and "
                "overall DISPUTED. This should be a PowerPoint screenshot."
            ),
        )

        disputes = api.get(
            f"list_disputes_{cluster_id}",
            (
                f"/projects/{project_id}/routing/{routing_id}"
                f"/finding-clusters/{cluster_id}/validation-disputes"
            ),
        )["disputes"]

        if not disputes:
            raise RuntimeError("DISPUTED consensus created no ValidationDispute.")

        dispute = disputes[-1]
        escalated = escalate_dispute(
            api,
            project_id,
            routing_id,
            cluster_id,
            dispute["validation_dispute_id"],
            decision="rejected",
            severity=None,
        )

        cumulative = calculate_and_finalize_consensus(
            api,
            project_id,
            routing_id,
            cluster_id,
            "cumulative",
        )
        result["escalation"] = escalated
        result["final_consensus"] = cumulative

        if cumulative["consensus_outcome"] != "rejected":
            raise RuntimeError(
                "Expected cumulative 3 ACCEPT / 6 REJECT to resolve REJECTED, "
                f"actual={cumulative['consensus_outcome']!r}"
            )

        ctx.checkpoint(
            "Escalation: 5 → 9 validators; original minority becomes correct",
            frontend_hint=f"/audits/{project_id}/{routing_id}",
            message=(
                "Show Round 1: 3 ACCEPT / 2 REJECT. Round 2: +4 NEW REJECT. "
                "Cumulative: 3 ACCEPT / 6 REJECT, N=9, threshold=6, final REJECTED. "
                "This should be a PowerPoint screenshot."
            ),
        )

    elif first_consensus["consensus_outcome"] in {"disputed", "no_quorum"}:
        # Keep non-demo clusters economically closed where possible by using the
        # one bounded escalation round. For these clusters we bias the synthetic
        # attestation fixture toward ACCEPTED; the actual reproduction evidence
        # remains server-derived.
        disputes = api.get(
            f"list_disputes_optional_{cluster_id}",
            (
                f"/projects/{project_id}/routing/{routing_id}"
                f"/finding-clusters/{cluster_id}/validation-disputes"
            ),
        )["disputes"]

        if disputes:
            escalated = escalate_dispute(
                api,
                project_id,
                routing_id,
                cluster_id,
                disputes[-1]["validation_dispute_id"],
                decision="accepted",
                severity=severity,
            )
            result["escalation"] = escalated
            result["final_consensus"] = calculate_and_finalize_consensus(
                api,
                project_id,
                routing_id,
                cluster_id,
                "cumulative_optional",
            )

    final_consensus = result["final_consensus"]
    if final_consensus.get("consensus_outcome") in {"disputed", "no_quorum"}:
        raise RuntimeError(
            f"Validator consensus remained unresolved for {cluster_id} after "
            "the available escalation round."
        )

    # Day 5 evaluation only applies once stable terminal validator truth exists.
    if final_consensus.get("consensus_outcome") in {
        "confirmed",
        "rejected",
        "out_of_scope",
        "insufficient_evidence",
        "unsafe",
        "unsupported",
    }:
        result["performance"] = api.post(
            f"validator_performance_{cluster_id}",
            (
                f"/projects/{project_id}/routing/{routing_id}"
                f"/validation-consensus/{final_consensus['validation_consensus_id']}"
                f"/validator-performance/evaluate"
            ),
            json_body={},
        )
    else:
        result["performance"] = None

    return result


def rebuild_report_quality(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
) -> Any:
    return api.post(
        "rebuild_report_quality_assessments",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/quality-assessments/rebuild"
        ),
        json_body={},
    )


def create_task_budget(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
    total_budget: str,
) -> dict[str, Any]:
    created = api.post(
        "create_task_reward_budget",
        f"/projects/{project_id}/task-reward-budgets",
        json_body={
            "routing_id": routing_id,
            "total_budget_points": total_budget,
            "pool_split": {
                "miner_share": "0.70",
                "validator_share": "0.20",
                "protocol_share": "0.10",
            },
            "description": "ProofGuard management Gold E2E demo budget",
        },
        expected=(201,),
    )
    budget = created["budget"]

    finalized = api.post(
        "finalize_task_reward_budget",
        (
            f"/projects/{project_id}/task-reward-budgets/"
            f"{budget['task_reward_budget_id']}/finalize"
        ),
        json_body={},
    )
    return finalized["budget"]


def run_miner_rewards(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
    budget_id: str,
) -> dict[str, Any]:
    created = api.post(
        "create_week7_miner_reward_cycle",
        f"/projects/{project_id}/routing/{routing_id}/task-reward-cycles",
        json_body={
            "task_reward_budget_id": budget_id,
            "allocation_scope": "global",
            "description": "ProofGuard Gold demo miner reward stream",
        },
        expected=(201,),
    )
    cycle_id = created["cycle"]["reward_cycle_id"]

    calculated = api.post(
        "calculate_week7_miner_reward_cycle",
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/calculate",
        json_body={},
    )
    finalized = api.post(
        "finalize_week7_miner_reward_cycle",
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/finalize",
        json_body={},
    )
    verification = api.get(
        "verify_week7_miner_reward_cycle",
        f"/projects/{project_id}/task-reward-cycles/{cycle_id}/verify",
    )
    return {
        "created": created,
        "calculated": calculated,
        "finalized": finalized,
        "verification": verification,
    }


def run_validator_rewards(
    api: DemoAPI,
    project_id: str,
    routing_id: str,
    budget_id: str,
) -> dict[str, Any]:
    created = api.post(
        "create_validator_reward_cycle",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-reward-cycles"
        ),
        json_body={
            "task_reward_budget_id": budget_id,
            "description": "ProofGuard Gold demo validator reward stream",
        },
        expected=(201,),
    )
    cycle_id = created["cycle"]["reward_cycle_id"]

    calculated = api.post(
        "calculate_validator_reward_cycle",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-reward-cycles/{cycle_id}/calculate"
        ),
        json_body={},
    )
    finalized = api.post(
        "finalize_validator_reward_cycle",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-reward-cycles/{cycle_id}/finalize"
        ),
        json_body={},
    )
    allocations = api.get(
        "validator_reward_allocations",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-reward-cycles/{cycle_id}/allocations"
        ),
    )
    events = api.get(
        "validator_reward_events",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-reward-cycles/{cycle_id}/events"
        ),
    )
    verification = api.get(
        "verify_validator_reward_cycle",
        (
            f"/projects/{project_id}/routing/{routing_id}"
            f"/validator-reward-cycles/{cycle_id}/verify"
        ),
    )
    return {
        "created": created,
        "calculated": calculated,
        "finalized": finalized,
        "allocations": allocations,
        "events": events,
        "verification": verification,
    }


def generate_final_report(api: DemoAPI, project_id: str) -> dict[str, Any] | None:
    try:
        generated = api.post(
            "generate_final_report",
            f"/projects/{project_id}/reports/final",
            json_body=None,
        )
        # Also fetch the API-visible final report state / markdown if present.
        report = api.get(
            "get_final_report",
            f"/projects/{project_id}/reports/final",
        )
        try:
            markdown = api.get(
                "get_final_report_markdown",
                f"/projects/{project_id}/reports/final/markdown",
            )
        except Exception:
            markdown = None
        return {
            "generation": generated,
            "report": report,
            "markdown": markdown,
        }
    except RuntimeError as exc:
        print(f"WARNING: final report generation failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive ProofGuard through a management-ready Gold E2E demo."
    )
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="FastAPI base URL",
    )
    parser.add_argument(
        "--frontend-url",
        default="http://localhost:5173",
        help="Frontend base URL used only for screenshot hints",
    )
    parser.add_argument(
        "--scope-file",
        type=Path,
        required=True,
        help="scope.yaml accepted by your current ProofGuard backend",
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--repo-zip",
        type=Path,
        help="ZIP source uploaded through POST /projects",
    )
    source.add_argument(
        "--github-url",
        help="GitHub URL accepted by POST /projects",
    )
    parser.add_argument(
        "--project-name",
        default="ProofGuard Management Gold Demo",
    )
    parser.add_argument(
        "--budget",
        default="10000",
        help="Generic protocol points; default 10000",
    )
    parser.add_argument(
        "--artifact-dir",
        type=Path,
        default=Path("demo_artifacts/gold_run"),
    )
    parser.add_argument(
        "--no-pause",
        action="store_true",
        help="Run without presentation screenshot pauses",
    )
    parser.add_argument(
        "--skip-node-seed",
        action="store_true",
        help="Use existing clean demo nodes instead of registering the seed network",
    )

    args = parser.parse_args()

    artifact_dir = args.artifact_dir.resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    api = DemoAPI(args.base_url, artifact_dir)
    ctx = DemoContext(
        artifact_dir=artifact_dir,
        frontend_url=args.frontend_url,
        pause=not args.no_pause,
    )

    summary: dict[str, Any] = {
        "api_base_url": args.base_url,
        "frontend_url": args.frontend_url,
        "project_name": args.project_name,
        "budget": args.budget,
        "artifacts": str(artifact_dir),
    }

    try:
        health = api.get("health", "/health")
        summary["health"] = health

        if not args.skip_node_seed:
            summary["seed"] = seed_nodes(api, ctx)
        else:
            summary["seed"] = {"skipped": True}
            api.get("existing_nodes", "/nodes")
            ctx.checkpoint(
                "Existing ProofGuard node network",
                frontend_hint="/nodes",
                message="Verify that enough independent validator operators exist.",
            )

        project_state = create_project(
            api,
            ctx,
            args.project_name,
            args.scope_file.resolve(),
            args.repo_zip.resolve() if args.repo_zip else None,
            args.github_url,
        )
        summary["project"] = project_state
        project_id = project_state["project_id"]

        run_state = run_agent_audit(api, ctx, project_id)
        summary["audit_run"] = run_state
        routing_id = run_state["routing_id"]

        calibration = observe_legacy_validation(
            api,
            project_id,
            routing_id,
            run_state["submissions"],
        )
        summary["agent_calibration"] = calibration

        clusters = build_clusters(api, ctx, project_id, routing_id)
        summary["clusters"] = [
            {
                "finding_cluster_id": c["finding_cluster_id"],
                "category": c["category"],
                "claimed_severity": c["claimed_severity"],
                "final_severity": c["final_severity"],
                "validation_authority": c["validation_authority"],
                "final_validation_status": c["final_validation_status"],
                "report_count": c["report_count"],
                "distinct_operator_count": c["distinct_operator_count"],
            }
            for c in clusters
        ]
        summary["validator_artifacts"] = prepare_validator_artifacts(
            api,
            project_id,
            clusters,
        )

        validation_results = []
        for idx, cluster in enumerate(clusters):
            validation_results.append(
                validate_cluster_with_network(
                    api,
                    ctx,
                    project_id,
                    routing_id,
                    cluster,
                    is_demo_dispute_cluster=(idx == 0),
                )
            )

        summary["validator_network"] = [
            {
                "cluster_id": x["cluster"]["finding_cluster_id"],
                "round1_outcome": x["round1_consensus"]["consensus_outcome"],
                "final_outcome": x["final_consensus"]["consensus_outcome"],
                "escalated": x["escalation"] is not None,
                "final_consensus_id": x["final_consensus"]["validation_consensus_id"],
            }
            for x in validation_results
        ]

        ctx.checkpoint(
            "Validator quality and historical role-specific evolution",
            frontend_hint="/nodes",
            message=(
                "Compare a correct original-minority validator against an original "
                "majority validator. Show ValidationQualityAssessment, "
                "ValidatorCategoryScore and ValidatorMembership separately from agent skill."
            ),
        )

        quality_rebuild = rebuild_report_quality(api, project_id, routing_id)
        summary["report_quality_rebuild"] = quality_rebuild

        budget = create_task_budget(
            api,
            project_id,
            routing_id,
            args.budget,
        )
        summary["task_reward_budget"] = budget

        miner_rewards = run_miner_rewards(
            api,
            project_id,
            routing_id,
            budget["task_reward_budget_id"],
        )
        summary["miner_rewards"] = miner_rewards

        ctx.checkpoint(
            "Week 7 miner reward stream",
            frontend_hint="/rewards",
            message=(
                "Show TaskRewardBudget miner pool, cluster allocation, report quality, "
                "operator rewards and immutable events if eligible findings exist."
            ),
            important=False,
        )

        validator_rewards = run_validator_rewards(
            api,
            project_id,
            routing_id,
            budget["task_reward_budget_id"],
        )
        summary["validator_rewards"] = validator_rewards

        ctx.checkpoint(
            "Week 8 validator reward stream",
            frontend_hint="/rewards",
            message=(
                "Show authoritative work units, equal base budgets, 30% completion, "
                "70% VQ² quality, distributed and undistributed validator points."
            ),
        )

        # Final agent score snapshots after one completed audit.
        agent_score_snapshot = {}
        for sub in run_state["submissions"]:
            nid = sub["node_id"]
            if nid in agent_score_snapshot:
                continue
            try:
                agent_score_snapshot[nid] = api.get(
                    f"final_agent_scores_{nid}",
                    f"/nodes/{nid}/category-scores",
                )
            except RuntimeError:
                pass
        summary["final_agent_scores"] = agent_score_snapshot

        final_report = generate_final_report(api, project_id)
        summary["final_report"] = final_report

        ctx.checkpoint(
            "Audit conclusion: E2E protocol + accounting + node learning",
            frontend_hint=f"/audits/{project_id}/{routing_id}",
            message=(
                "Use Presentation Mode. Finish with findings/consensus, miner + validator "
                "reward status, accounting verification, and agent/validator score evolution."
            ),
        )

        summary["presentation_checkpoints"] = ctx.checkpoints
        summary["completed"] = True

    except Exception as exc:
        summary["completed"] = False
        summary["error"] = repr(exc)
        print("\nGOLD DEMO FAILED")
        print(repr(exc), file=sys.stderr)
        print(
            "All successful request/response artifacts have been preserved. "
            "Use the last JSON artifact to diagnose the exact backend stage.",
            file=sys.stderr,
        )

    finally:
        (artifact_dir / "gold_demo_summary.json").write_text(
            pretty(summary), encoding="utf-8"
        )
        (artifact_dir / "presentation_checkpoints.json").write_text(
            pretty(ctx.checkpoints), encoding="utf-8"
        )
        print(f"\nArtifacts: {artifact_dir}")
        print(f"Summary: {artifact_dir / 'gold_demo_summary.json'}")
        print(f"Checkpoints: {artifact_dir / 'presentation_checkpoints.json'}")

    return 0 if summary.get("completed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
