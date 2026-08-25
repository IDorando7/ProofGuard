import type { ClusterProtocolData } from '../types/protocol'
import { Card, EmptyState, EntityIcon, Fingerprint, InfoTip, StatusBadge, Unavailable } from './ui'
import { shortId, titleCase } from '../utils/format'

export function CommitteePanel({ protocol }: { protocol: ClusterProtocolData }) {
  const reporters = new Set(protocol.cluster.members.map(member => member.operator_id))
  const validators = new Set(protocol.assignments.map(item => item.validator_operator_id))
  const conflicts = [...reporters].filter(operator => validators.has(operator))
  if (!protocol.committees.length) return <EmptyState title="No validator committee" detail="No committee record is available for this FindingCluster." />
  return <div className="committee-stack">{protocol.committees.map(committee => {
    const assignments = protocol.assignments.filter(item => item.validator_committee_id === committee.validator_committee_id)
    return <Card className="committee-card" key={committee.validator_committee_id}>
      <div className="committee-card__header"><div className="committee-title"><EntityIcon name="validator" tone="positive"/><div><p className="eyebrow">Validation round {committee.validation_round}</p><h4>{titleCase(committee.assurance_mode)} Committee</h4></div></div><StatusBadge status={committee.status} /></div>
      <div className="committee-summary"><span><strong>{committee.authoritative_target_size}</strong> authoritative target</span><span><strong>{assignments.filter(item => item.assignment_role === 'authoritative').length}</strong> actual authoritative</span><span><strong>{committee.actual_shadow_size}</strong> shadow</span><span><strong>{protocol.cluster.distinct_operator_count}</strong> reporting operators</span><span className={conflicts.length ? 'conflict conflict--found' : 'conflict'}><strong>{conflicts.length}</strong> reporter/validator operator conflicts</span></div>
      {!assignments.length ? <EmptyState title="No finalized assignments" detail="The committee plan exists but no assignment records are exposed." /> : <div className="table-wrap"><table className="committee-table">
        <thead><tr><th>Role</th><th>Validator</th><th>Operator</th><th>Assignment</th><th>Reproduction</th><th>Validity</th><th>Severity</th></tr></thead>
        <tbody>{assignments.map(assignment => {
          const reproduction = protocol.reproductions.find(item => item.validator_assignment_id === assignment.validator_assignment_id)
          const attestation = protocol.attestations.find(item => item.validator_assignment_id === assignment.validator_assignment_id)
          const shadow = assignment.assignment_role === 'shadow'
          return <tr className={shadow ? 'shadow-row' : 'authoritative-row'} key={assignment.validator_assignment_id}>
            <td><div className={`role-indicator role-indicator--${assignment.assignment_role}`}><StatusBadge status={assignment.assignment_role} />{shadow ? <small className="shadow-note">Does not count toward consensus</small> : <small className="shadow-note">Authoritative protocol seat</small>}</div></td>
            <td><code>{shortId(assignment.validator_node_id)}</code></td><td><code>{shortId(assignment.validator_operator_id)}</code></td><td><StatusBadge status={assignment.status} /></td><td>{reproduction ? <StatusBadge status={reproduction.reproduction_status} /> : <Unavailable compact />}</td><td>{attestation ? <StatusBadge status={attestation.validity_decision} /> : <Unavailable compact />}</td><td>{attestation?.normalized_severity ? titleCase(attestation.normalized_severity) : <Unavailable compact />}</td>
          </tr>
        })}</tbody>
      </table></div>}
    </Card>
  })}</div>
}

export function ReproductionTable({ protocol }: { protocol: ClusterProtocolData }) {
  if (!protocol.reproductions.length) return <EmptyState title="No independent reproductions" detail="No independently attributable validator reproduction records are available." />
  return <div className="table-wrap"><table>
    <thead><tr><th>Validator</th><th>Operator</th><th>Assignment</th><th>Mode</th><th>Result</th><th>Artifact fingerprint</th><th>Environment / source</th><th>Authority</th></tr></thead>
    <tbody>{protocol.reproductions.map(item => <tr className={item.assignment_role === 'shadow' ? 'shadow-row' : ''} key={item.validator_reproduction_id}>
      <td><code>{shortId(item.validator_node_id)}</code></td><td><code>{shortId(item.validator_operator_id)}</code></td><td><code>{shortId(item.validator_assignment_id)}</code></td><td>{titleCase(item.reproduction_mode)}</td><td><StatusBadge status={item.reproduction_status} /></td><td><Fingerprint value={item.poc_artifact_fingerprint} /></td><td><Fingerprint value={item.environment_fingerprint ?? item.source_snapshot_fingerprint} /></td><td><StatusBadge status={item.assignment_role} />{item.assignment_role === 'shadow' && <small className="shadow-note">No consensus weight</small>}</td>
    </tr>)}</tbody>
  </table></div>
}

export function AttestationTable({ protocol }: { protocol: ClusterProtocolData }) {
  if (!protocol.attestations.length) return <EmptyState title="No structured attestations" detail="ValidationAttestation records are not yet available." />
  return <div className="table-wrap"><table>
    <thead><tr><th>Validator</th><th>Role</th><th>Validity</th><th>Reproduction</th><th>Root cause</th><th>Severity</th><th>Impact</th><th>Reason codes</th></tr></thead>
    <tbody>{protocol.attestations.map(item => <tr className={item.assignment_role === 'shadow' ? 'shadow-row' : ''} key={item.attestation_id}>
      <td><code>{shortId(item.validator_node_id)}</code><small className="cell-sub">{shortId(item.validator_operator_id)}</small></td><td><StatusBadge status={item.assignment_role} /></td><td><StatusBadge status={item.validity_decision} /></td><td><StatusBadge status={item.reproduction_decision} /></td><td><StatusBadge status={item.root_cause_decision} /></td><td>{item.normalized_severity ? titleCase(item.normalized_severity) : <Unavailable compact />}</td><td><StatusBadge status={item.impact_decision} /></td><td><span className="reason-list">{item.reason_codes.map(titleCase).join(' · ') || 'None'}</span></td>
    </tr>)}</tbody>
  </table></div>
}

export function ValidatorQuality({ protocol }: { protocol: ClusterProtocolData }) {
  if (!protocol.quality.length) return <EmptyState title="No ValidationQualityAssessments" detail="Quality is shown only after backend retrospective evaluation." />
  const components = ['validity_accuracy','reproduction_accuracy','root_cause_accuracy','severity_accuracy','impact_accuracy','protocol_compliance'] as const
  return <div className="quality-list">{protocol.quality.map(item => <div className="quality-row" key={item.validation_quality_assessment_id}>
    <div className="quality-row__identity"><strong>{shortId(item.validator_node_id)}</strong><small>{shortId(item.validator_operator_id)} · {titleCase(item.assignment_role)}</small></div>
    <div className="quality-breakdown">{components.map(component => {
      const value = item[component]
      return <span key={component} title={`${titleCase(component)}: ${value ?? 'not applicable'}`} style={{ flex: value === null ? 0.15 : Math.max(0.05, Number(value)) }} className={value === null ? 'quality-na' : ''}><i>{titleCase(component).replace(' Accuracy','')}</i></span>
    })}</div>
    <div className="quality-row__score"><strong>{Math.round(Number(item.validation_quality_score) * 100)}</strong><small>VQ <InfoTip term="Validation Quality">Backend quality across applicable decision components and compliance.</InfoTip></small></div>
  </div>)}</div>
}
