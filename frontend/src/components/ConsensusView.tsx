import type { ValidationConsensus } from '../types/protocol'
import { Card, EmptyState, EntityIcon, InfoTip, Metric, StatusBadge } from './ui'
import { titleCase } from '../utils/format'

export function ConsensusView({ consensuses }: { consensuses: ValidationConsensus[] }) {
  if (!consensuses.length) return <EmptyState title="No consensus snapshots" detail="The backend has not exposed a ValidationConsensus for this cluster." />
  return <div className="consensus-stack">{consensuses.map((consensus, index) => {
    const validityCounts = consensus.validity_result.value_counts
    const accept = validityCounts.accepted ?? validityCounts.accept ?? validityCounts.confirmed ?? 0
    const reject = validityCounts.rejected ?? validityCounts.reject ?? 0
    return <Card className="consensus-card" variant="highlight" key={consensus.validation_consensus_id}>
    <div className="consensus-card__header"><div className="consensus-title"><EntityIcon name="consensus" tone="info" size="large"/><div><p className="eyebrow">{consensus.included_round_ids.length > 1 ? 'Cumulative protocol resolution' : `Validation round ${consensus.validation_round_number}`}</p><h4>Validator Consensus</h4></div></div><div className="consensus-outcome"><small>Backend outcome</small><StatusBadge status={consensus.consensus_outcome} /></div></div>
    <div className="consensus-signature" aria-label={`Validity votes: ${accept} accept and ${reject} reject`}>
      <div className="vote-column vote-column--accept"><span>Accept</span><strong>{accept}</strong><small>authoritative votes</small><div className="vote-nodes">{Array.from({ length: accept }).map((_, vote) => <i key={vote}/>)}</div></div>
      <div className="threshold-column"><span className="threshold-column__line"/><small>Resolution threshold</small><strong>{consensus.supermajority_required} / {consensus.authoritative_target_size}</strong><em>Calculated by backend</em></div>
      <div className="vote-column vote-column--reject"><span>Reject</span><strong>{reject}</strong><small>authoritative votes</small><div className="vote-nodes">{Array.from({ length: reject }).map((_, vote) => <i key={vote}/>)}</div></div>
    </div>
    <div className="metric-grid metric-grid--3 compact-metrics consensus-metrics">
      <Metric label="Committee target N" value={consensus.authoritative_target_size} />
      <Metric label="Quorum evidence" value={`${consensus.valid_authoritative_attestation_count} / ${consensus.quorum_required}`} />
      <Metric label="Supermajority required" value={`${consensus.supermajority_required} / ${consensus.authoritative_target_size}`} detail={<InfoTip term="Supermajority">Backend threshold required for protocol resolution.</InfoTip>} />
    </div>
    <div className="component-grid">{[consensus.validity_result, consensus.reproduction_result, consensus.root_cause_result, consensus.severity_result, consensus.impact_result].map(component => <div className="consensus-component" key={component.component_type}>
      <div className="consensus-component__head"><strong>{titleCase(component.component_type)}</strong><StatusBadge status={component.consensus_reached ? component.consensus_value : component.unresolved_reason} /></div>
      <div className="vote-list">{Object.entries(component.value_counts).map(([value, count]) => <span key={value}><strong>{count}</strong> {titleCase(value)}</span>)}</div>
      <small>{component.valid_response_count} valid responses · required {component.supermajority_required}</small>
    </div>)}</div>
    {index === consensuses.length - 1 && consensus.final_normalized_severity && <p className="result-line">Final normalized severity <strong>{titleCase(consensus.final_normalized_severity)}</strong></p>}
  </Card>})}</div>
}

export function DisputeTimeline({ consensuses, disputes }: { consensuses: ValidationConsensus[]; disputes: import('../types/protocol').ValidationDispute[] }) {
  if (!disputes.length) return null
  const first = consensuses[0]
  const final = consensuses.at(-1)
  const firstCounts = first?.validity_result.value_counts ?? {}
  const finalValue = final?.validity_result.consensus_value
  const topFirst = Object.entries(firstCounts).sort((a, b) => b[1] - a[1])[0]?.[0]
  const minorityAligned = Boolean(first && final && consensuses.length > 1 && finalValue && finalValue !== topFirst && (firstCounts[finalValue] ?? 0) > 0 && final.consensus_outcome !== 'disputed')
  return <div className="dispute-timeline">
    {consensuses.map((consensus, index) => <div className="timeline-step" key={consensus.validation_consensus_id}>
      <span className="timeline-step__marker">{index + 1}</span>
      <div><p className="eyebrow">{consensus.included_round_ids.length > 1 ? 'Cumulative consensus' : `Round ${consensus.validation_round_number}`}</p><h4>{Object.entries(consensus.validity_result.value_counts).map(([value,count]) => `${count} ${value.toUpperCase()}`).join(' · ')}</h4><p>N={consensus.authoritative_target_size} · quorum={consensus.quorum_required} · threshold={consensus.supermajority_required}</p><StatusBadge status={consensus.consensus_outcome} /></div>
      {index < consensuses.length - 1 && <span className="timeline-step__connector">Evidence continues</span>}
    </div>)}
    {disputes.map(dispute => <div className="timeline-dispute" key={dispute.validation_dispute_id}><EntityIcon name="route" tone="warning" size="small"/><div><small>Escalation evidence</small><span>{dispute.reason_codes.map(titleCase).join(' · ')}</span></div><StatusBadge status={dispute.status} />{dispute.escalation_round_id && <strong>New escalation round: {dispute.escalation_round_id}</strong>}</div>)}
    {minorityAligned && <div className="insight-callout"><strong>Resolution insight</strong><p>The original minority validators were ultimately aligned with the final resolution.</p></div>}
  </div>
}
