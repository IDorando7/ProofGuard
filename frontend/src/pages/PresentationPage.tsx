import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import { loadAuditBundle, queryKeys } from '../api/client'
import { RoutingSection } from '../components/RoutingSection'
import { SubmissionsSection } from '../components/SubmissionsSection'
import { Card, ErrorState, LoadingState, Metric, SectionHeading, StatusBadge, Unavailable } from '../components/ui'
import { PresentationFrame } from '../components/PresentationFrame'
import { PresentationCommitteeStory, PresentationConsensusStory, PresentationReproductionStory, PresentationRewardsStory } from '../components/PresentationStories'
import { shortId, titleCase } from '../utils/format'

export function PresentationPage() {
  const { projectId = '', routingId = '' } = useParams()
  const [stage, setStage] = useState(0)
  const query = useQuery({ queryKey: queryKeys.audit(projectId, routingId), queryFn: ({ signal }) => loadAuditBundle(projectId, routingId, signal) })
  const labels = ['Audit Intake','Agent Routing','Candidate Findings','FindingClusters','Validator Committee','Independent Reproduction','Consensus & Escalation','Miner / Validator Rewards','Node Evolution','Audit Conclusion']
  if (query.isLoading) return <div className="presentation-shell"><LoadingState label="Preparing presentation" /></div>
  if (query.error) return <div className="presentation-shell"><ErrorState error={query.error} /></div>
  const bundle = query.data!
  const outcomes = bundle.clusters.map(item => item.cluster.validator_consensus_outcome ?? item.cluster.final_validation_status)
  const validators = new Set(bundle.clusters.flatMap(item => item.assignments.map(assignment => assignment.validator_operator_id)))
  const escalations = bundle.clusters.flatMap(item => item.disputes).filter(dispute => dispute.escalation_round_id).length
  const slides = [
    <div className="presentation-intro"><p className="eyebrow">ProofGuard · End-to-End Audit</p><h1>{bundle.project.project_name}</h1><p className="presentation-lead">Client code enters a category-specialized agent network, findings are clustered by root cause, independently reproduced, resolved by validator consensus, and rewarded through separate streams.</p><div className="metric-grid metric-grid--4"><Metric label="Routing / task" value={shortId(bundle.routing.routing_id, 11)} /><Metric label="Source revision" value={bundle.scope?.commit_hash ?? <Unavailable compact />} /><Metric label="Categories" value={bundle.routing.requested_categories.length} /><Metric label="Status" value={<StatusBadge status={bundle.auditRun?.status ?? bundle.routing.status} />} /></div></div>,
    <RoutingSection routing={bundle.routing} nodes={bundle.nodes} />,
    <SubmissionsSection submissions={bundle.submissions} clusters={bundle.clusters} nodes={bundle.nodes} />,
    <div><SectionHeading eyebrow="Stage 04" title="FindingClusters" detail="Independent submissions converge on immutable root-cause work units without claiming validation truth." /><div className="presentation-clusters">{bundle.clusters.map(item => <Card key={item.cluster.finding_cluster_id}><span className="tag">{titleCase(item.cluster.category)}</span><h3>{titleCase(item.cluster.root_cause_key)}</h3><div className="metric-grid metric-grid--4 compact-metrics"><Metric label="Reports" value={item.cluster.report_count} /><Metric label="Operators" value={item.cluster.distinct_operator_count} /><Metric label="Cluster state" value={<StatusBadge status={item.cluster.status}/>} /><Metric label="Validation state" value={<StatusBadge status={item.cluster.validator_consensus_outcome ?? item.cluster.final_validation_status ?? item.cluster.validation_authority} />} /></div></Card>)}</div></div>,
    <PresentationCommitteeStory bundle={bundle}/>,
    <PresentationReproductionStory bundle={bundle}/>,
    <PresentationConsensusStory bundle={bundle}/>,
    <PresentationRewardsStory bundle={bundle}/>,
    <div><SectionHeading eyebrow="Stage 09" title="Node Evolution" detail="Agent and validator performance remain role-specific; backend history improves future specialization and selection." /><div className="learning-flow"><span>Agent work</span><b>→</b><span>Contribution & quality events</span><b>→</b><span>Category-specific history</span><b>→</b><span>Membership changes</span><b>→</b><span>Better future routing</span></div><div className="metric-grid metric-grid--4"><Metric label="Agent executions" value={bundle.agentExecutions.length} /><Metric label="Agent nodes involved" value={new Set(bundle.agentExecutions.map(item => item.node_id)).size} /><Metric label="Validator operators" value={validators.size} /><Metric label="Quality assessments" value={bundle.clusters.flatMap(item => item.quality).length} /></div></div>,
    <div className="audit-conclusion"><StatusBadge status={bundle.auditRun?.status ?? bundle.routing.status} label={['completed','finalized'].includes((bundle.auditRun?.status ?? bundle.routing.status).toLowerCase()) ? 'AUDIT COMPLETE' : 'AUDIT IN PROGRESS'} /><h1>{bundle.project.project_name}</h1><p>From intake to resolution, economics, accounting, and network learning.</p><div className="metric-grid metric-grid--4"><Metric label="Unique findings" value={bundle.clusters.length} /><Metric label="Confirmed" value={outcomes.filter(item => item === 'confirmed' || item === 'accepted').length} /><Metric label="Rejected" value={outcomes.filter(item => item === 'rejected').length} /><Metric label="Unresolved" value={outcomes.filter(item => ['disputed','no_quorum','insufficient_evidence'].includes(item ?? '')).length} /><Metric label="Validators used" value={validators.size} /><Metric label="Escalations" value={escalations} /><Metric label="Miner rewards" value={bundle.minerCycle?.status ? titleCase(bundle.minerCycle.status) : <Unavailable compact />} /><Metric label="Validator rewards" value={bundle.validatorCycle?.status ? titleCase(bundle.validatorCycle.status) : <Unavailable compact />} /></div><Card className="conclusion-learning"><p className="eyebrow">Network learning</p><h2>Every resolved audit strengthens category-specific history.</h2><p>{bundle.agentExecutions.length} agent executions and {bundle.clusters.flatMap(item => item.quality).length} validation quality assessments are available to the backend’s separate agent and validator performance systems.</p><div className="conclusion-verification"><span>Accounting verification</span>{bundle.minerVerification && bundle.validatorVerification ? <StatusBadge status={bundle.minerVerification.ok && bundle.validatorVerification.ok ? 'pass' : 'fail'} /> : <Unavailable compact />}</div></Card></div>,
  ]
  return <PresentationFrame labels={labels} stage={stage} onStage={setStage} exitPath={`/audits/${projectId}/${routingId}`} projectName={bundle.project.project_name}>{slides[stage]}</PresentationFrame>
}
