export type ProtocolRecord = Record<string, unknown>

export interface Project {
  project_id: string
  project_name: string
  created_at: string
  status: string
  source_type: string
  github_url: string | null
  workspace_path?: string
}

export interface ProjectScope {
  project_name: string
  language?: string
  framework?: string
  chain?: string
  commit_hash?: string
  contracts_in_scope?: string[]
  attack_categories?: string[]
}

export interface NodeRecord {
  node_id: string
  display_name: string
  node_type: 'agent' | 'validator' | 'hybrid' | string
  operator_id: string
  status: string
  supported_categories: string[]
  reputation_score: number
  statistics?: Record<string, number>
  created_at: string
  updated_at: string
}

export interface CategoryScore {
  score_id: string
  node_id: string
  category: string
  category_score: number
  score_band: string
  finalized_submissions: number
  accepted_unique_submissions: number
  calculated_at: string
}

export interface CategoryPerformance extends ProtocolRecord {
  node_id: string
  category: string
}

export interface ValidatorCategoryScore {
  validator_node_id: string
  operator_id: string
  category: string
  resolved_validations: number
  final_score: string
  experience_confidence: string
  calculated_at: string
}

export interface ValidatorCategoryPerformance extends ProtocolRecord {
  validator_node_id: string
  category: string
  resolved_validations: number
  average_quality_score: string
  completed_assignments: number
}

export interface ValidatorMembership {
  validator_node_id: string
  category: string
  status: string
  reason_codes: string[]
  evaluated_at: string
}

export interface AgentMembership {
  subnet_id: string
  node_id: string
  category: string
  status: string
  category_score: number
  rank: number | null
  finalized_submissions: number
  accepted_unique_submissions: number
  exploration_assignments: number
  status_reason_codes: string[]
  last_evaluated_at: string | null
}

export interface ReputationEvent {
  event_id: string
  category: string
  project_id: string
  submission_id: string
  validation_status: string
  reproduction_status: string
  event_type: string
  previous_reputation: number
  new_reputation: number
  reason: string
  applied_at: string | null
  created_at: string
}

export interface RoutingUsageEvent {
  usage_event_id: string
  routing_id: string
  project_id: string
  category: string
  selection_type: string
  assignment_mode: string
  applied_at: string
}

export interface RoutingAssignment {
  assignment_id: string
  node_id: string
  category: string
  selection_type: string
  assignment_mode: string
  membership_status: string
  category_score: number
  experience_confidence: number
  position: number
  selection_reasons: string[]
  candidate_snapshot?: ProtocolRecord
}

export interface CategoryRoutingResult {
  category: string
  subnet_id: string | null
  complete: boolean
  assignments: RoutingAssignment[]
  ranked_selected: number
  exploration_selected: number
  total_selected: number
  shortage_reasons: string[]
  warnings: string[]
}

export interface RoutingRecord {
  routing_id: string
  project_id: string
  requested_categories: string[]
  status: string
  results: CategoryRoutingResult[]
  total_assignments: number
  production_assignments: number
  shadow_assignments: number
  source_fingerprint: string
  calculated_at: string
  finalized_at: string | null
}

export interface RoutingList {
  project_id: string
  total: number
  records: RoutingRecord[]
}

export interface AuditRun {
  audit_run_id: string
  project_id: string
  status: string
  current_stage: string | null
  routing_id: string | null
  task_reward_budget_id: string | null
  reward_cycle_id: string | null
  progress: Record<string, unknown>
  stage_states: ProtocolRecord[]
  event_count: number
  created_at: string
  completed_at: string | null
}

export interface AuditRunList { project_id: string; total: number; audit_runs: AuditRun[] }

export interface AgentExecution {
  agent_execution_id: string
  node_id: string
  operator_id: string
  category: string
  status: string
  duration_ms: number | null
  finding_ids: string[]
  submission_ids: string[]
  finding_count: number
  submission_count: number
}

export interface Submission {
  submission_id: string
  project_id: string
  finding_id: string
  node_id: string
  node_type: string
  category: string
  finding_hash: string
  status: string
  reward_status: string
  routing_id: string | null
  submitted_at: string
}

export interface ClusterMember {
  submission_id: string
  finding_id: string
  node_id: string
  operator_id: string
  validation_id: string | null
  reproduction_id: string | null
  submitted_at: string
  relation: string
  source_fingerprint: string
}

export interface FindingCluster {
  finding_cluster_id: string
  project_id: string
  routing_id: string
  category: string
  canonical_finding_id: string
  canonical_submission_id: string
  final_validation_status: string | null
  claimed_severity: string | null
  final_severity: string | null
  validation_authority: string
  final_validation_consensus_id: string | null
  validator_consensus_outcome: string | null
  validator_consensus_severity: string | null
  validation_resolution_source_fingerprint: string | null
  root_cause_key: string
  root_cause_fingerprint: string
  members: ClusterMember[]
  report_count: number
  distinct_operator_count: number
  status: string
  source_fingerprint: string
  created_at: string
  updated_at: string
  finalized_at: string | null
}

export interface ClusterList { project_id: string; routing_id: string; total: number; clusters: FindingCluster[] }

export interface ValidatorCommittee {
  validator_committee_id: string
  validation_round: number
  category: string
  assurance_mode: string
  authoritative_target_size: number
  shadow_target_size: number
  actual_shadow_size: number
  excluded_operator_ids: string[]
  authoritative_seats: CommitteeSeat[]
  shadow_seats: CommitteeSeat[]
  status: string
}

export interface CommitteeSeat {
  assignment_role: string
  seat_index: number
  validator_node_id: string
  operator_id: string
  validator_assignment_id: string
}

export interface ValidatorAssignment {
  validator_assignment_id: string
  validator_committee_id: string | null
  validation_round: number
  validator_node_id: string
  validator_operator_id: string
  category: string
  assignment_role: string
  status: string
}

export interface ValidatorReproduction {
  validator_reproduction_id: string
  validator_assignment_id: string
  validator_node_id: string
  validator_operator_id: string
  assignment_role: string | null
  reproduction_mode: string
  reproduction_status: string
  poc_artifact_fingerprint: string | null
  environment_fingerprint: string | null
  source_revision: string | null
  source_snapshot_fingerprint: string | null
}

export interface ValidationAttestation {
  attestation_id: string
  validator_assignment_id: string
  validator_node_id: string
  validator_operator_id: string
  assignment_role: string
  validity_decision: string
  root_cause_decision: string
  reproduction_decision: string
  normalized_severity: string | null
  impact_decision: string
  reason_codes: string[]
}

export interface ConsensusEvidence { value: string; count: number; operator_ids: string[] }
export interface ConsensusComponent {
  component_type: string
  authoritative_target_size: number
  valid_response_count: number
  quorum_required: number
  supermajority_required: number
  value_counts: Record<string, number>
  value_evidence: ConsensusEvidence[]
  consensus_reached: boolean
  consensus_value: string | null
  unresolved_reason: string | null
}

export interface ValidationConsensus {
  validation_consensus_id: string
  validation_round_number: number
  included_round_ids: string[]
  included_committee_ids: string[]
  authoritative_target_size: number
  valid_authoritative_attestation_count: number
  quorum_required: number
  supermajority_required: number
  lifecycle_status: string
  consensus_outcome: string
  validity_result: ConsensusComponent
  reproduction_result: ConsensusComponent
  root_cause_result: ConsensusComponent
  severity_result: ConsensusComponent
  impact_result: ConsensusComponent
  final_normalized_severity: string | null
  dispute_reason_codes: string[]
}

export interface ValidationDispute {
  validation_dispute_id: string
  triggering_consensus_id: string
  triggering_round_id: string
  reason_codes: string[]
  status: string
  escalation_round_id: string | null
  escalation_committee_id: string | null
  resolution_consensus_id: string | null
}

export interface ValidationQualityAssessment {
  validation_quality_assessment_id: string
  validator_node_id: string
  validator_operator_id: string
  assignment_role: string
  validity_accuracy: string
  reproduction_accuracy: string | null
  root_cause_accuracy: string | null
  severity_accuracy: string | null
  impact_accuracy: string | null
  protocol_compliance: string
  validation_quality_score: string
  status: string
}

export interface TaskRewardBudget {
  task_reward_budget_id: string
  total_budget_points: string
  miner_pool_points: string
  validator_pool_points: string
  protocol_pool_points: string
  status: string
  reward_unit: string
}

export interface FindingRewardAllocation extends ProtocolRecord {
  finding_cluster_id: string
  severity_weight: string
  uniqueness: string
  finding_score: string
  finding_cluster_reward_points: string
}

export interface FindingRewardCalculation {
  status: string
  outcome: string
  cluster_allocations: FindingRewardAllocation[]
  miner_pool_points: string
  distributed_cluster_points: string
  undistributed_cluster_points: string
}

export interface OperatorClusterPayout extends ProtocolRecord { finding_cluster_id: string }
export interface OperatorRewardCalculation {
  status: string
  cluster_payouts: OperatorClusterPayout[]
  distributed_operator_points: string
  undistributed_cluster_points: string
}

export interface MinerRewardCycle {
  reward_cycle_id: string
  status: string
  miner_pool_points: string
  validator_pool_points: string
  protocol_pool_points: string
  distributed_miner_points: string
  undistributed_miner_points: string
  reward_event_count: number
  rewarded_operator_count: number
  chief_finder_count: number
}

export interface MinerRewardEvent extends ProtocolRecord {
  reward_event_id: string
  finding_cluster_id: string
  operator_id: string
  node_id: string
  severity_weight: string
  uniqueness: string
  finding_score: string
  finding_cluster_reward_points: string
  quality_score: string
  quality_rank: number
  chief_finder: boolean
  total_reward_points: string
}

export interface Verification {
  verification_status: string
  ok: boolean
  duplicate_events_found?: boolean
  budget_conserved?: boolean
  accounting_conserved?: boolean
  event_set_complete: boolean
  errors: string[]
}

export interface ValidatorRewardAllocation {
  validator_reward_allocation_id: string
  finding_cluster_id: string
  validation_round_id: string
  validator_node_id: string
  validator_operator_id: string
  assignment_role: string
  base_work_unit_budget: string
  completion_eligible: boolean
  completion_reward: string
  validation_quality_score: string | null
  quality_squared_factor: string | null
  quality_reward: string
  total_reward: string
  undistributed_points: string
  undistributed_reason_codes: string[]
}

export interface ValidatorRewardCycle {
  reward_cycle_id: string
  status: string
  reward_policy: { completion_share: string; quality_share: string; quality_exponent: number; shadow_reward_eligible: boolean }
  validator_pool_points: string
  authoritative_work_units: number
  completed_work_units: number
  resolved_quality_units: number
  unresolved_units: number
  distributed_validator_points: string
  undistributed_validator_points: string
  reward_event_count: number
  allocations: ValidatorRewardAllocation[]
}

export interface ClusterProtocolData {
  cluster: FindingCluster
  committees: ValidatorCommittee[]
  assignments: ValidatorAssignment[]
  reproductions: ValidatorReproduction[]
  attestations: ValidationAttestation[]
  consensuses: ValidationConsensus[]
  disputes: ValidationDispute[]
  quality: ValidationQualityAssessment[]
}

export interface AuditBundle {
  project: Project
  scope: ProjectScope | null
  routing: RoutingRecord
  auditRun: AuditRun | null
  agentExecutions: AgentExecution[]
  submissions: Submission[]
  clusters: ClusterProtocolData[]
  budget: TaskRewardBudget | null
  findingRewards: FindingRewardCalculation | null
  operatorRewards: OperatorRewardCalculation | null
  minerCycle: MinerRewardCycle | null
  minerEvents: MinerRewardEvent[]
  minerVerification: Verification | null
  validatorCycle: ValidatorRewardCycle | null
  validatorVerification: Verification | null
  nodes: NodeRecord[]
  unavailable: string[]
}
