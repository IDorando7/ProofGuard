import type { AuditBundle, ClusterProtocolData, ValidationConsensus } from '../types/protocol'

const component = (type: string, counts: Record<string, number>, reached: boolean, value: string | null) => ({
  component_type: type,
  authoritative_target_size: 5,
  valid_response_count: Object.values(counts).reduce((sum, count) => sum + count, 0),
  quorum_required: 4,
  supermajority_required: 4,
  value_counts: counts,
  value_evidence: [],
  consensus_reached: reached,
  consensus_value: value,
  unresolved_reason: reached ? null : 'no_supermajority',
})

export function disputedConsensus(): ValidationConsensus {
  return {
    validation_consensus_id: 'consensus-round-1', validation_round_number: 1,
    included_round_ids: ['round-1'], included_committee_ids: ['committee-1'],
    authoritative_target_size: 5, valid_authoritative_attestation_count: 5,
    quorum_required: 4, supermajority_required: 4, lifecycle_status: 'finalized',
    consensus_outcome: 'disputed',
    validity_result: component('validity', { accepted: 3, rejected: 2 }, false, null),
    reproduction_result: component('reproduction', { reproduced: 3, failed: 2 }, false, null),
    root_cause_result: component('root_cause', { confirmed: 3, mismatch: 2 }, false, null),
    severity_result: component('severity', { high: 3, medium: 2 }, false, null),
    impact_result: component('impact', { validated: 3, rejected: 2 }, false, null),
    final_normalized_severity: null, dispute_reason_codes: ['validity_disagreement'],
  }
}

export function finalConsensus(): ValidationConsensus {
  const result = disputedConsensus()
  const finalComponent = component('validity', { accepted: 3, rejected: 6 }, true, 'rejected')
  return {
    ...result, validation_consensus_id: 'consensus-final', validation_round_number: 2,
    included_round_ids: ['round-1','round-2'], included_committee_ids: ['committee-1','committee-2'],
    authoritative_target_size: 9, valid_authoritative_attestation_count: 9, quorum_required: 8, supermajority_required: 6,
    consensus_outcome: 'rejected', validity_result: { ...finalComponent, authoritative_target_size: 9, valid_response_count: 9, quorum_required: 8, supermajority_required: 6 },
    reproduction_result: { ...finalComponent, component_type: 'reproduction', authoritative_target_size: 9, valid_response_count: 9, quorum_required: 8, supermajority_required: 6 },
    root_cause_result: { ...finalComponent, component_type: 'root_cause', authoritative_target_size: 9, valid_response_count: 9, quorum_required: 8, supermajority_required: 6 },
    severity_result: { ...finalComponent, component_type: 'severity', authoritative_target_size: 9, valid_response_count: 9, quorum_required: 8, supermajority_required: 6 },
    impact_result: { ...finalComponent, component_type: 'impact', authoritative_target_size: 9, valid_response_count: 9, quorum_required: 8, supermajority_required: 6 },
  }
}

export function clusterProtocol(): ClusterProtocolData {
  return {
    cluster: {
      finding_cluster_id: 'cluster-1', project_id: 'project-1', routing_id: 'routing-1', category: 'access_control',
      canonical_finding_id: 'finding-1', canonical_submission_id: 'submission-1', final_validation_status: 'accepted', claimed_severity: 'high', final_severity: 'high',
      validation_authority: 'validator_consensus', final_validation_consensus_id: 'consensus-final', validator_consensus_outcome: 'rejected', validator_consensus_severity: null, validation_resolution_source_fingerprint: 'c'.repeat(64),
      root_cause_key: 'missing access control', root_cause_fingerprint: 'a'.repeat(64), report_count: 2, distinct_operator_count: 2, status: 'finalized', source_fingerprint: 'd'.repeat(64), created_at: '2026-01-01T00:00:00Z', updated_at: '2026-01-01T00:00:00Z', finalized_at: '2026-01-01T00:00:00Z',
      members: [
        { submission_id: 'submission-1', finding_id: 'finding-1', node_id: 'agent-1', operator_id: 'reporter-1', validation_id: 'validation-1', reproduction_id: 'rep-1', submitted_at: '2026-01-01T00:00:00Z', relation: 'canonical', source_fingerprint: 'e'.repeat(64) },
        { submission_id: 'submission-2', finding_id: 'finding-2', node_id: 'agent-2', operator_id: 'reporter-2', validation_id: 'validation-2', reproduction_id: 'rep-2', submitted_at: '2026-01-01T00:01:00Z', relation: 'independent_duplicate', source_fingerprint: 'f'.repeat(64) },
      ],
    },
    committees: [{ validator_committee_id: 'committee-1', validation_round: 1, category: 'access_control', assurance_mode: 'standard', authoritative_target_size: 5, shadow_target_size: 1, actual_shadow_size: 1, excluded_operator_ids: [], authoritative_seats: [], shadow_seats: [], status: 'finalized' }],
    assignments: [
      { validator_assignment_id: 'assignment-a', validator_committee_id: 'committee-1', validation_round: 1, validator_node_id: 'validator-a', validator_operator_id: 'validator-op-a', category: 'access_control', assignment_role: 'authoritative', status: 'attested' },
      { validator_assignment_id: 'assignment-s', validator_committee_id: 'committee-1', validation_round: 1, validator_node_id: 'validator-s', validator_operator_id: 'validator-op-s', category: 'access_control', assignment_role: 'shadow', status: 'attested' },
    ],
    reproductions: [], attestations: [], consensuses: [disputedConsensus(), finalConsensus()],
    disputes: [{ validation_dispute_id: 'dispute-1', triggering_consensus_id: 'consensus-round-1', triggering_round_id: 'round-1', reason_codes: ['validity_disagreement'], status: 'resolved', escalation_round_id: 'round-2', escalation_committee_id: 'committee-2', resolution_consensus_id: 'consensus-final' }], quality: [],
  }
}

export function auditBundle(): AuditBundle {
  return {
    project: { project_id: 'project-1', project_name: 'Treasury Vault', created_at: '2026-01-01T00:00:00Z', status: 'prepared', source_type: 'github', github_url: null },
    scope: { project_name: 'Treasury Vault', contracts_in_scope: ['src/Vault.sol'], attack_categories: ['access_control'] },
    routing: { routing_id: 'routing-1', project_id: 'project-1', requested_categories: ['access_control'], status: 'finalized', results: [], total_assignments: 2, production_assignments: 1, shadow_assignments: 1, source_fingerprint: 'b'.repeat(64), calculated_at: '2026-01-01T00:00:00Z', finalized_at: '2026-01-01T00:00:00Z' },
    auditRun: null, agentExecutions: [], submissions: [], clusters: [clusterProtocol()], budget: null, findingRewards: null, operatorRewards: null,
    minerCycle: null, minerEvents: [], minerVerification: null, validatorCycle: null, validatorVerification: null, nodes: [], unavailable: [],
  }
}
