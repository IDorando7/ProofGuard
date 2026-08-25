import type { AuditBundle, ClusterProtocolData, MinerRewardEvent } from '../types/protocol'
import { AttestationTable, CommitteePanel, ReproductionTable, ValidatorQuality } from './CommitteePanel'
import { ConsensusView, DisputeTimeline } from './ConsensusView'
import { Card, CopyableId, EmptyState, EntityIcon, Fingerprint, InfoTip, SectionHeading, SeverityBadge, StatusBadge, Unavailable } from './ui'
import { formatDate, points, shortId, titleCase } from '../utils/format'

export function ClustersSection({ bundle }: { bundle: AuditBundle }) {
  return <section id="clusters" className="audit-section">
    <SectionHeading eyebrow="Stages 05–08" title="FindingClusters & Validation" detail="Immutable report groupings become validator work units; consensus—not cluster finalization—establishes protocol truth." />
    {!bundle.clusters.length ? <Card><EmptyState title="No FindingClusters" detail="The backend has not returned clusters for this routing." /></Card> : <div className="cluster-detail-list">{bundle.clusters.map((protocol, index) => <ClusterDetail key={protocol.cluster.finding_cluster_id} protocol={protocol} rewardEvent={bundle.minerEvents.find(event => event.finding_cluster_id === protocol.cluster.finding_cluster_id)} initiallyOpen={index === 0} />)}</div>}
  </section>
}

function ClusterDetail({ protocol, rewardEvent, initiallyOpen }: { protocol: ClusterProtocolData; rewardEvent?: MinerRewardEvent; initiallyOpen: boolean }) {
  const cluster = protocol.cluster
  const operators = [...new Set(cluster.members.map(member => member.operator_id))]
  return <details className="cluster-detail" id={`cluster-${cluster.finding_cluster_id}`} open={initiallyOpen}>
    <summary>
      <div className="cluster-summary__main"><EntityIcon name="cluster" tone="info" size="large"/><div><span className="tag">{titleCase(cluster.category)}</span><h3>{titleCase(cluster.root_cause_key)}</h3><code>{shortId(cluster.finding_cluster_id, 12)}</code></div></div>
      <div className="cluster-summary__facts"><span><small>Severity</small><strong><SeverityBadge severity={cluster.validator_consensus_severity ?? cluster.final_severity ?? cluster.claimed_severity}/></strong></span><span><small>Reports</small><strong>{cluster.report_count}</strong></span><span><small>Operators</small><strong>{cluster.distinct_operator_count}</strong></span><span className="cluster-state-pair"><small>Cluster state</small><StatusBadge status={cluster.status}/></span><span className="cluster-state-pair"><small>Validation state</small><StatusBadge status={cluster.validator_consensus_outcome ?? cluster.final_validation_status ?? cluster.validation_authority}/></span><span className="details-chevron" aria-hidden="true">⌄</span></div>
    </summary>
    <div className="cluster-detail__body">
      <div className="cluster-overview-grid">
        <Card className="canonical-card"><p className="eyebrow">Canonical root cause</p><h4>{titleCase(cluster.root_cause_key)}</h4><dl className="definition-list"><div><dt>Canonical finding</dt><dd><CopyableId value={cluster.canonical_finding_id} visible={6}/></dd></div><div><dt>Canonical submission</dt><dd><CopyableId value={cluster.canonical_submission_id} visible={6}/></dd></div><div><dt>Root cause fingerprint</dt><dd><Fingerprint value={cluster.root_cause_fingerprint} /></dd></div><div><dt>Source snapshot</dt><dd><Fingerprint value={cluster.source_fingerprint} /></dd></div></dl></Card>
        <Card className="truth-card"><p className="eyebrow">Two independent states</p><div className="truth-state"><span>Cluster membership</span><StatusBadge status={cluster.status}/><small>{cluster.finalized_at ? `Frozen ${formatDate(cluster.finalized_at)}` : 'Membership snapshot remains open'}</small></div><div className="truth-state"><span>Validation truth</span><StatusBadge status={cluster.validator_consensus_outcome ?? cluster.final_validation_status ?? cluster.validation_authority}/><small>{cluster.final_validation_consensus_id ? `Consensus ${shortId(cluster.final_validation_consensus_id, 6)}` : titleCase(cluster.validation_authority)}</small></div></Card>
        <Card><p className="eyebrow">Duplicate semantics</p><h4>{cluster.report_count} independently submitted reports</h4><p>{cluster.distinct_operator_count} distinct reporting operators contributed to this root-cause cluster.</p><div className="operator-chips">{operators.map(operator => <code key={operator}>{shortId(operator, 7)}</code>)}</div></Card>
        <Card><p className="eyebrow">Miner economics</p>{rewardEvent ? <dl className="definition-list"><div><dt>SeverityWeight</dt><dd>{points(rewardEvent.severity_weight)}</dd></div><div><dt>Uniqueness <InfoTip term="Uniqueness">Backend reward factor for independent reporting.</InfoTip></dt><dd>{points(rewardEvent.uniqueness)}</dd></div><div><dt>FindingScore <InfoTip term="FindingScore">Backend-computed cluster economic score.</InfoTip></dt><dd>{points(rewardEvent.finding_score)}</dd></div><div><dt>Cluster reward</dt><dd>{points(rewardEvent.finding_cluster_reward_points)}</dd></div></dl> : <Unavailable />}</Card>
      </div>

      <div className="subsection" id="committees"><h3>Validator Committee</h3><p>Authoritative validators determine consensus; shadow work remains explicitly separate.</p><CommitteePanel protocol={protocol} /></div>
      <div className="subsection" id="reproduction"><h3>Independent Reproduction</h3><p>Each result is attributable to its validator assignment. Internal storage references and host paths are not rendered.</p><ReproductionTable protocol={protocol} /></div>
      <div className="subsection"><h3>Structured ValidationAttestations</h3><p>Independent component decisions preserved exactly as returned by the backend.</p><AttestationTable protocol={protocol} /></div>
      <div className="subsection" id="consensus"><h3>Consensus Components</h3><p>Thresholds, vote counts, and final outcomes come from ValidationConsensus records.</p><ConsensusView consensuses={protocol.consensuses} /></div>
      {protocol.disputes.length > 0 && <div className="subsection"><h3>Dispute & Escalation</h3><p>Historical and cumulative consensus snapshots make changes across rounds explicit.</p><DisputeTimeline consensuses={protocol.consensuses} disputes={protocol.disputes} /></div>}
      <div className="subsection"><h3>Validation Quality</h3><p>Recent component accuracy and protocol compliance with backend-computed VQ.</p><ValidatorQuality protocol={protocol} /></div>
      <div className="subsection"><h3>Cluster Members</h3><div className="table-wrap"><table><thead><tr><th>Submission</th><th>Finding</th><th>Node</th><th>Operator</th><th>Relation</th><th>Reproduction</th></tr></thead><tbody>{cluster.members.map(member => <tr key={member.submission_id}><td><code>{shortId(member.submission_id)}</code></td><td><code>{shortId(member.finding_id)}</code></td><td><code>{shortId(member.node_id)}</code></td><td><code>{shortId(member.operator_id)}</code></td><td><StatusBadge status={member.relation} /></td><td>{member.reproduction_id ? <code>{shortId(member.reproduction_id)}</code> : <Unavailable compact />}</td></tr>)}</tbody></table></div></div>
    </div>
  </details>
}
