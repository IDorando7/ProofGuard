import type { AuditBundle } from '../types/protocol'
import { titleCase, points, shortId } from '../utils/format'
import { Card, EmptyState, EntityIcon, Metric, StatusBadge, Unavailable } from './ui'

export function PresentationConsensusStory({ bundle }: { bundle: AuditBundle }) {
  const featured = bundle.clusters.find(item => item.disputes.length) ?? bundle.clusters[0]
  if (!featured) return <Card><EmptyState title="No consensus available" detail="FindingClusters have not reached validator consensus."/></Card>
  return <div className="presentation-story">
    <div className="presentation-story__header"><div><p className="eyebrow">Featured validator resolution</p><h2>{titleCase(featured.cluster.root_cause_key)}</h2><p>Each round below is a backend ValidationConsensus snapshot. Escalation adds evidence; it does not replace prior votes.</p></div><StatusBadge status={featured.cluster.validator_consensus_outcome ?? featured.cluster.final_validation_status ?? featured.cluster.validation_authority}/></div>
    <div className="presentation-rounds">{featured.consensuses.map((consensus, index) => {
      const counts = consensus.validity_result.value_counts
      const accept = counts.accepted ?? counts.accept ?? counts.confirmed ?? 0
      const reject = counts.rejected ?? counts.reject ?? 0
      return <Card className="presentation-round" variant={index === featured.consensuses.length - 1 ? 'highlight' : 'standard'} key={consensus.validation_consensus_id}>
        <div className="presentation-round__head"><span>{consensus.included_round_ids.length > 1 ? 'Cumulative final' : `Round ${consensus.validation_round_number}`}</span><StatusBadge status={consensus.consensus_outcome}/></div>
        <div className="presentation-votes"><div className="presentation-vote presentation-vote--accept"><strong>{accept}</strong><span>Accept</span></div><div className="presentation-vote presentation-vote--reject"><strong>{reject}</strong><span>Reject</span></div></div>
        <div className="presentation-threshold"><span>Quorum {consensus.valid_authoritative_attestation_count}/{consensus.quorum_required}</span><span>Threshold {consensus.supermajority_required}/{consensus.authoritative_target_size}</span></div>
      </Card>
    })}</div>
    {featured.disputes.some(item => item.escalation_round_id) && <div className="presentation-escalation"><EntityIcon name="route" tone="warning"/><div><small>Escalation triggered</small><strong>Fresh validator evidence added to cumulative consensus</strong></div><StatusBadge status={featured.disputes.at(-1)?.status}/></div>}
    <div className="presentation-outcomes">{bundle.clusters.map(item => <span key={item.cluster.finding_cluster_id}><small>{titleCase(item.cluster.root_cause_key)}</small><StatusBadge status={item.cluster.validator_consensus_outcome ?? item.cluster.final_validation_status ?? item.cluster.validation_authority}/></span>)}</div>
  </div>
}

export function PresentationCommitteeStory({ bundle }: { bundle: AuditBundle }) {
  const committees = bundle.clusters.flatMap(protocol => protocol.committees.map(committee => ({ protocol, committee })))
  if (!committees.length) return <Card><EmptyState title="No validator committee" detail="No committee records are available for these FindingClusters."/></Card>
  return <div className="presentation-story"><div className="presentation-story__header"><div><p className="eyebrow">Independent validator network</p><h2>Conflict-free committees</h2><p>Authoritative protocol seats remain distinct from shadow observation work.</p></div><StatusBadge status={committees.at(-1)?.committee.status}/></div><div className="presentation-committee-grid">{committees.map(({ protocol, committee }) => {
    const assignments = protocol.assignments.filter(item => item.validator_committee_id === committee.validator_committee_id)
    const reporters = new Set(protocol.cluster.members.map(item => item.operator_id))
    const conflicts = assignments.filter(item => reporters.has(item.validator_operator_id)).length
    return <Card className="presentation-committee-card" key={committee.validator_committee_id}><div className="presentation-round__head"><span>{titleCase(protocol.cluster.category)} · Round {committee.validation_round}</span><StatusBadge status={committee.status}/></div><h3>{titleCase(committee.assurance_mode)} Committee</h3><div className="presentation-committee-metrics"><Metric label="Authoritative" value={assignments.filter(item => item.assignment_role === 'authoritative').length}/><Metric label="Shadow" value={assignments.filter(item => item.assignment_role === 'shadow').length}/><Metric label="Reporter conflicts" value={conflicts} tone={conflicts ? 'negative' : 'positive'}/></div><div className="presentation-seat-row">{assignments.map(item => <span className={`presentation-seat presentation-seat--${item.assignment_role}`} key={item.validator_assignment_id}><EntityIcon name="validator" tone={item.assignment_role === 'authoritative' ? 'info' : 'neutral'} size="small"/><small>{shortId(item.validator_node_id, 4)}</small><StatusBadge status={item.assignment_role}/></span>)}</div></Card>
  })}</div></div>
}

export function PresentationReproductionStory({ bundle }: { bundle: AuditBundle }) {
  if (!bundle.clusters.some(item => item.reproductions.length)) return <Card><EmptyState title="No independent reproduction" detail="Validator reproduction records are not available yet."/></Card>
  return <div className="presentation-story"><div className="presentation-story__header"><div><p className="eyebrow">Safety-scoped validation infrastructure</p><h2>Independent Reproduction</h2><p>Every result stays attributable to a validator assignment and backend execution status.</p></div></div><div className="presentation-reproduction-grid">{bundle.clusters.map(protocol => <Card key={protocol.cluster.finding_cluster_id}><div className="presentation-reproduction__head"><div><span className="tag">{titleCase(protocol.cluster.category)}</span><h3>{titleCase(protocol.cluster.root_cause_key)}</h3></div><strong>{protocol.reproductions.length}<small>results</small></strong></div><div className="presentation-repro-list">{protocol.reproductions.map(item => <span className={item.assignment_role === 'shadow' ? 'presentation-repro presentation-repro--shadow' : 'presentation-repro'} key={item.validator_reproduction_id}><EntityIcon name="check" tone={item.reproduction_status === 'reproduced' ? 'positive' : 'neutral'} size="small"/><span><strong>{shortId(item.validator_node_id, 4)}</strong><small>{titleCase(item.assignment_role)}</small></span><StatusBadge status={item.reproduction_status}/></span>)}</div></Card>)}</div></div>
}

export function PresentationRewardsStory({ bundle }: { bundle: AuditBundle }) {
  return <div className="presentation-story">
    <div className="presentation-story__header"><div><p className="eyebrow">Separate protocol accounting</p><h2>Miner + Validator Rewards</h2><p>Backend-calculated pools remain separate from allocation through verification.</p></div>{bundle.budget ? <StatusBadge status={bundle.budget.status}/> : <Unavailable/>}</div>
    <div className="presentation-budget"><Card variant="highlight"><EntityIcon name="rewards" tone="info" size="large"/><Metric label="Task budget" value={bundle.budget ? points(bundle.budget.total_budget_points) : <Unavailable compact/>} detail={bundle.budget?.reward_unit ? titleCase(bundle.budget.reward_unit) : undefined}/></Card><span className="presentation-budget__branch"/><div className="presentation-reward-streams">
      <Card className="presentation-reward-stream presentation-reward-stream--miner"><div className="stream-title"><EntityIcon name="findings" tone="info"/><div><p className="eyebrow">Miner pool</p><h3>{bundle.budget ? points(bundle.budget.miner_pool_points) : 'Unavailable'}</h3></div></div><div className="presentation-stream-values"><Metric label="Distributed" value={bundle.minerCycle ? points(bundle.minerCycle.distributed_miner_points) : <Unavailable compact/>}/><Metric label="Undistributed" value={bundle.minerCycle ? points(bundle.minerCycle.undistributed_miner_points) : <Unavailable compact/>}/></div><StatusBadge status={bundle.minerCycle?.status}/></Card>
      <Card className="presentation-reward-stream presentation-reward-stream--validator"><div className="stream-title"><EntityIcon name="validator" tone="positive"/><div><p className="eyebrow">Validator pool</p><h3>{bundle.budget ? points(bundle.budget.validator_pool_points) : 'Unavailable'}</h3></div></div><div className="presentation-stream-values"><Metric label="Distributed" value={bundle.validatorCycle ? points(bundle.validatorCycle.distributed_validator_points) : <Unavailable compact/>}/><Metric label="Undistributed" value={bundle.validatorCycle ? points(bundle.validatorCycle.undistributed_validator_points) : <Unavailable compact/>}/></div><StatusBadge status={bundle.validatorCycle?.status}/></Card>
    </div></div>
    <div className="presentation-verification"><span><small>Miner accounting</small>{bundle.minerVerification ? <StatusBadge status={bundle.minerVerification.ok ? 'pass' : 'fail'}/> : <Unavailable compact/>}</span><span><small>Validator accounting</small>{bundle.validatorVerification ? <StatusBadge status={bundle.validatorVerification.ok ? 'pass' : 'fail'}/> : <Unavailable compact/>}</span><p>PASS is shown only when a backend verification record reports success.</p></div>
  </div>
}
