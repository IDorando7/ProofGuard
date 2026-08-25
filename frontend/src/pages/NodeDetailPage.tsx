import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { loadNodeDetail, queryKeys } from '../api/client'
import { NodeRolePerformance } from '../components/NodeRolePerformance'
import { Card, DataNotice, ErrorState, LoadingState, Metric, PageHeader, SectionHeading, StatusBadge } from '../components/ui'
import { Icon } from '../components/Icon'
import { formatDate, shortId, titleCase } from '../utils/format'

export function NodeDetailPage() {
  const { nodeId = '' } = useParams()
  const query = useQuery({ queryKey: queryKeys.node(nodeId), queryFn: ({ signal }) => loadNodeDetail(nodeId, signal), enabled: Boolean(nodeId) })
  if (query.isLoading) return <LoadingState label="Loading node performance" />
  if (query.error) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  const data = query.data!
  const node = data.node
  const activity = [
    ...data.reputationHistory.map(event => ({ id: event.event_id, at: event.applied_at ?? event.created_at, type: 'Reputation event', category: event.category, status: event.validation_status, detail: `${Math.round(event.previous_reputation * 100)} → ${Math.round(event.new_reputation * 100)} · ${event.reason}` })),
    ...data.routingUsage.map(event => ({ id: event.usage_event_id, at: event.applied_at, type: 'Routing selection', category: event.category, status: event.assignment_mode, detail: `${titleCase(event.selection_type)} · routing ${shortId(event.routing_id)}` })),
  ].sort((a, b) => new Date(b.at).valueOf() - new Date(a.at).valueOf())
  return <>
    <PageHeader eyebrow={`${titleCase(node.node_type)} node`} title={node.display_name} detail={node.node_id} breadcrumb={<Link className="back-link" to="/nodes"><Icon name="arrow-left" size={13}/>Nodes</Link>} action={<><StatusBadge status={node.status} /><button className="button button--secondary" onClick={() => query.refetch()}><Icon name="refresh" size={15}/>Refresh</button></>} />
    <div className="metric-grid metric-grid--4">
      <Metric label="Operator" value={shortId(node.operator_id, 11)} />
      <Metric label="Role" value={titleCase(node.node_type)} />
      <Metric label="Global reputation" value={`${Math.round(node.reputation_score * 100)} / 100`} />
      <Metric label="Registered" value={formatDate(node.created_at)} />
    </div>
    <Card>
      <SectionHeading title="Supported Categories" detail="Declared registry capabilities; score cards below show only backend-derived history." />
      <div className="tag-list tag-list--large">{node.supported_categories.map(category => <span className="tag" key={category}>{titleCase(category)}</span>)}</div>
    </Card>
    <NodeRolePerformance {...data} />
    <Card>
      <SectionHeading eyebrow="Immutable state" title="Node Evolution" detail="Current score snapshots are paired with real reputation and routing-usage events; no synthetic time-series points are introduced." />
      <div className="metric-grid metric-grid--4">
        <Metric label="Agent score snapshots" value={data.agentScores.length} />
        <Metric label="Validator categories" value={data.validatorScores.length} />
        <Metric label="Resolved validations" value={data.validatorPerformance.reduce((sum, item) => sum + item.resolved_validations, 0)} />
        <Metric label="Historical events" value={activity.length} detail={`${data.routingUsage.length} routing selections`} />
      </div>
      {activity.length > 0 && <div className="table-wrap node-activity"><table><thead><tr><th>Event</th><th>Category</th><th>Protocol state</th><th>Detail</th><th>Applied</th></tr></thead><tbody>{activity.slice(0, 20).map(event => <tr key={event.id}><td>{event.type}<small className="cell-sub"><code>{shortId(event.id)}</code></small></td><td>{titleCase(event.category)}</td><td><StatusBadge status={event.status} /></td><td>{event.detail}</td><td>{formatDate(event.at)}</td></tr>)}</tbody></table></div>}
    </Card>
    <DataNotice labels={data.unavailable} />
  </>
}
