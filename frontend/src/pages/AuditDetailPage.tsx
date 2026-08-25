import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { loadAuditBundle, queryKeys } from '../api/client'
import { AuditPipeline } from '../components/AuditPipeline'
import { ClustersSection } from '../components/ClustersSection'
import { RewardsPanel } from '../components/RewardsPanel'
import { RoutingSection } from '../components/RoutingSection'
import { SubmissionsSection } from '../components/SubmissionsSection'
import { Card, CopyableId, DataNotice, ErrorState, Fingerprint, LoadingState, Metric, SectionHeading, StatusBadge, Unavailable } from '../components/ui'
import { Icon } from '../components/Icon'
import { formatDate, shortId, titleCase } from '../utils/format'

const terminal = new Set(['completed', 'failed', 'finalized', 'cancelled'])

export function AuditDetailPage() {
  const { projectId = '', routingId = '' } = useParams()
  const [isDemo, setIsDemo] = useState(() => localStorage.getItem('proofguard-demo-audit') === `${projectId}/${routingId}`)
  const query = useQuery({
    queryKey: queryKeys.audit(projectId, routingId),
    queryFn: ({ signal }) => loadAuditBundle(projectId, routingId, signal),
    enabled: Boolean(projectId && routingId),
    refetchInterval: current => {
      const run = current.state.data?.auditRun
      return run && !terminal.has(run.status.toLowerCase()) ? 4000 : false
    },
  })
  if (query.isLoading) return <LoadingState label="Assembling end-to-end audit state" />
  if (query.error) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  const bundle = query.data!
  const consensuses = bundle.clusters.flatMap(item => item.consensuses)
  const outcomes = bundle.clusters.map(item => item.cluster.validator_consensus_outcome ?? item.cluster.final_validation_status)
  const validators = new Set(bundle.clusters.flatMap(item => item.assignments.map(assignment => assignment.validator_operator_id)))
  const escalations = bundle.clusters.flatMap(item => item.disputes).filter(dispute => dispute.escalation_round_id).length
  const unresolved = outcomes.filter(outcome => ['disputed', 'no_quorum', 'insufficient_evidence'].includes(outcome ?? '')).length
  function toggleDemo() {
    const key = `${projectId}/${routingId}`
    if (isDemo) localStorage.removeItem('proofguard-demo-audit')
    else localStorage.setItem('proofguard-demo-audit', key)
    setIsDemo(!isDemo)
  }
  return <>
    <div className="audit-hero" id="intake">
      <div className="audit-hero__main"><Link className="back-link back-link--on-dark" to="/audits"><Icon name="arrow-left" size={13}/>Audit registry</Link><div className="audit-hero__eyebrow"><span>Audit protocol</span><StatusBadge status={bundle.auditRun?.status ?? bundle.routing.status} />{isDemo && <span className="demo-label">Presentation audit</span>}</div><h1>{bundle.project.project_name}</h1><p>{bundle.scope?.language ?? 'Source'} · {bundle.scope?.framework ?? titleCase(bundle.project.source_type)} · {bundle.scope?.chain ?? 'Chain unavailable'}</p><div className="tag-list tag-list--large">{bundle.routing.requested_categories.map(category => <span className="tag" key={category}>{titleCase(category)}</span>)}</div></div>
      <div className="hero-actions"><button className="button button--ghost" onClick={toggleDemo}>{isDemo ? 'Remove presentation marker' : 'Mark for presentation'}</button><button className="button button--secondary" onClick={() => query.refetch()} disabled={query.isFetching}><Icon name="refresh" size={15}/>{query.isFetching ? 'Refreshing…' : 'Refresh'}</button><Link className="button button--primary" to={`/audits/${projectId}/${routingId}/present`}><Icon name="presentation" size={16}/>Presentation Mode</Link></div>
    </div>

    <Card className="audit-meta-card">
      <dl className="audit-meta"><div><dt>Project ID</dt><dd><CopyableId value={projectId} visible={7}/></dd></div><div><dt>Routing / Task ID</dt><dd><CopyableId value={routingId} visible={7}/></dd></div><div><dt>Source</dt><dd>{bundle.project.github_url ? <a className="text-link" href={bundle.project.github_url} rel="noreferrer" target="_blank">Repository <Icon name="external" size={12}/></a> : titleCase(bundle.project.source_type)}</dd></div><div><dt>Revision</dt><dd>{bundle.scope?.commit_hash ?? <Unavailable compact />}</dd></div><div><dt>Source fingerprint</dt><dd><Fingerprint value={bundle.routing.source_fingerprint} /></dd></div><div><dt>Calculated</dt><dd>{formatDate(bundle.routing.calculated_at)}</dd></div></dl>
    </Card>

    <Card className="executive-card" variant="elevated">
      <SectionHeading eyebrow="Executive summary" title="What happened?" detail="Counts below are presentation-only summaries of authoritative backend records." />
      <div className="executive-summary">
        <Metric label="Agent submissions" value={bundle.submissions.length} />
        <Metric label="Unique FindingClusters" value={bundle.clusters.length} />
        <Metric label="Confirmed" value={outcomes.filter(item => item === 'confirmed' || item === 'accepted').length} tone="positive" />
        <Metric label="Rejected" value={outcomes.filter(item => item === 'rejected').length} tone="negative" />
        <Metric label="Unresolved" value={unresolved} tone={unresolved ? 'warning' : 'positive'} />
        <Metric label="Escalations" value={escalations} tone={escalations ? 'warning' : 'default'} />
        <Metric label="Validator operators" value={validators.size} />
        <Metric label="Miner rewards" value={bundle.minerCycle?.status ? titleCase(bundle.minerCycle.status) : <Unavailable compact />} />
        <Metric label="Validator rewards" value={bundle.validatorCycle?.status ? titleCase(bundle.validatorCycle.status) : <Unavailable compact />} />
        <Metric label="Accounting" value={bundle.minerVerification && bundle.validatorVerification ? <StatusBadge status={bundle.minerVerification.ok && bundle.validatorVerification.ok ? 'pass' : 'fail'} /> : <Unavailable compact />} />
      </div>
    </Card>

    <section className="audit-section protocol-section"><SectionHeading eyebrow="End-to-end observability" title="Audit Protocol Pipeline" detail="Every stage reflects backend records. Select a stage to inspect its evidence." /><AuditPipeline bundle={bundle} /></section>
    <RoutingSection routing={bundle.routing} nodes={bundle.nodes} />
    <section id="analysis" className="audit-section"><Card><SectionHeading eyebrow="Stage 03" title="Agent Analysis" detail="Execution records emitted by routed local agents." />{bundle.agentExecutions.length ? <div className="table-wrap"><table><thead><tr><th>Agent execution</th><th>Node / operator</th><th>Category</th><th>Status</th><th>Runtime</th><th>Findings</th><th>Submissions</th></tr></thead><tbody>{bundle.agentExecutions.map(execution => <tr key={execution.agent_execution_id}><td><code>{shortId(execution.agent_execution_id)}</code></td><td><code>{shortId(execution.node_id)}</code><small className="cell-sub">{shortId(execution.operator_id)}</small></td><td>{titleCase(execution.category)}</td><td><StatusBadge status={execution.status} /></td><td>{execution.duration_ms === null ? <Unavailable compact /> : `${execution.duration_ms} ms`}</td><td>{execution.finding_count}</td><td>{execution.submission_count}</td></tr>)}</tbody></table></div> : <Unavailable />}</Card></section>
    <SubmissionsSection submissions={bundle.submissions} clusters={bundle.clusters} nodes={bundle.nodes} />
    <ClustersSection bundle={bundle} />
    <RewardsPanel bundle={bundle} />
    <section className="audit-section"><Card><SectionHeading eyebrow="Network learning" title="Better Future Routing" detail="Immutable work and quality records update role-specific history on the backend." /><div className="metric-grid metric-grid--4"><Metric label="Agent executions" value={bundle.agentExecutions.length} /><Metric label="Quality assessments" value={bundle.clusters.flatMap(item => item.quality).length} /><Metric label="Consensus snapshots" value={consensuses.length} /><Metric label="Rewarded work events" value={(bundle.minerCycle?.reward_event_count ?? 0) + (bundle.validatorCycle?.reward_event_count ?? 0)} /></div></Card></section>
    <DataNotice labels={bundle.unavailable} />
  </>
}
