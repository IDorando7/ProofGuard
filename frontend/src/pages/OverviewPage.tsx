import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from 'recharts'
import { loadOverview, queryKeys } from '../api/client'
import { Card, EmptyState, EntityIcon, ErrorState, LoadingState, Metric, PageHeader, SectionHeading, StatusBadge, Unavailable } from '../components/ui'
import { Icon } from '../components/Icon'
import { formatDate, titleCase } from '../utils/format'

export function OverviewPage() {
  const query = useQuery({ queryKey: queryKeys.overview, queryFn: ({ signal }) => loadOverview(signal) })
  if (query.isLoading) return <LoadingState label="Loading network overview" />
  if (query.error) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  const data = query.data!
  const active = data.nodes.filter(node => node.status === 'active').length
  const nodeTypes = [
    { name: 'Agents', value: data.nodes.filter(node => node.node_type === 'agent').length, color: '#5cc8ff' },
    { name: 'Validators', value: data.nodes.filter(node => node.node_type === 'validator').length, color: '#39d39f' },
    { name: 'Hybrid', value: data.nodes.filter(node => node.node_type === 'hybrid').length, color: '#a78bfa' },
  ]
  return <>
    <PageHeader eyebrow="Network intelligence" title="ProofGuard Network" detail="Autonomous security analysis and independent validation, rendered from authoritative protocol state." action={<button className="button button--secondary" onClick={() => query.refetch()} disabled={query.isFetching}><Icon name="refresh" size={15} />{query.isFetching ? 'Refreshing…' : 'Refresh'}</button>} />

    <div className="metric-grid metric-grid--4 overview-metrics">
      <Metric label="Active Nodes" value={active} detail={`${nodeTypes[0].value} agents · ${nodeTypes[1].value} validators · ${nodeTypes[2].value} hybrid`} tone="info" />
      <Metric label="Audits" value={data.projects.length} detail={`${data.auditRows.filter(row => row.routing).length} routed protocol runs`} />
      <Metric label="FindingClusters" value={data.clusterCount ?? <Unavailable compact />} detail="Canonical root-cause work units" />
      <Metric label="Validation Activity" value={data.validatorRewardEvents ?? <Unavailable compact />} detail="Validator reward work events" tone="validator" />
    </div>

    <div className="content-grid content-grid--2-1">
      <Card variant="elevated">
        <SectionHeading eyebrow="Operations" title="Recent Audits" detail="Projects and routings ordered by backend creation time." action={<Link className="text-link" to="/audits">View all audits →</Link>} />
        {!data.auditRows.length ? <EmptyState title="No audits registered" detail="Create a project through the API to begin observing an audit." /> : <div className="recent-list">
          {data.auditRows.slice(0, 5).map((row, index) => <Link className={`recent-row ${!row.routing ? 'recent-row--disabled' : ''}`} to={row.routing ? `/audits/${row.project.project_id}/${row.routing.routing_id}` : '/audits'} key={`${row.project.project_id}-${row.routing?.routing_id ?? index}`}>
            <span className="recent-row__index">{String(index + 1).padStart(2, '0')}</span>
            <span className="recent-row__main"><strong>{row.project.project_name}</strong><small>{row.routing ? `${titleCase(row.routing.status)} · ${row.routing.requested_categories.map(titleCase).join(', ')}` : 'No routing available'}</small></span>
            <span className="recent-row__date">{formatDate(row.run?.created_at ?? row.project.created_at)}</span>
            <StatusBadge status={row.run?.status ?? row.routing?.status ?? row.project.status} />
          </Link>)}
        </div>}
      </Card>

      <Card className="network-card">
        <SectionHeading eyebrow="Registry" title="Network Composition" detail="Role separation across registered participants." />
        {data.nodes.length ? <>
          <div className="donut-wrap">
            <ResponsiveContainer width="100%" height={190}>
              <PieChart><Pie data={nodeTypes} dataKey="value" innerRadius={55} outerRadius={78} paddingAngle={4} stroke="none">{nodeTypes.map(item => <Cell key={item.name} fill={item.color} />)}</Pie><Tooltip /></PieChart>
            </ResponsiveContainer>
            <div className="donut-label"><strong>{data.nodes.length}</strong><span>nodes</span></div>
          </div>
          <div className="legend">{nodeTypes.map(item => <div key={item.name}><i style={{ background: item.color }} /><span>{item.name}</span><strong>{item.value}</strong></div>)}</div>
        </> : <EmptyState title="Registry is empty" detail="No nodes are currently registered." />}
      </Card>
    </div>

    <Card className="signals-card">
      <SectionHeading eyebrow="Protocol outcomes" title="Finding & Reward Signals" detail="Unavailable values are never replaced with synthetic zeroes." />
      <div className="metric-grid metric-grid--6">
        <Metric label="Confirmed findings" value={data.confirmed ?? <Unavailable compact />} tone="positive" />
        <Metric label="Rejected findings" value={data.rejected ?? <Unavailable compact />} tone="negative" />
        <Metric label="Disputed findings" value={data.disputed ?? <Unavailable compact />} tone="warning" />
        <Metric label="Escalations" value={data.escalations ?? <Unavailable compact />} tone="warning" />
        <Metric label="Miner reward events" value={data.minerRewardEvents ?? <Unavailable compact />} />
        <Metric label="Validator reward events" value={data.validatorRewardEvents ?? <Unavailable compact />} />
      </div>
    </Card>

    <Card className="architecture-strip" variant="subtle">
      <div className="architecture-strip__intro"><EntityIcon name="shield" tone="info" size="large"/><div><p className="eyebrow">Validation architecture</p><h3>Discovery becomes protocol truth through independent evidence.</h3></div></div>
      <div className="architecture-strip__flow"><span><Icon name="agent"/>Agent analysis</span><i/><span><Icon name="cluster"/>Root-cause clusters</span><i/><span><Icon name="validator"/>Independent validation</span><i/><span><Icon name="consensus"/>Consensus</span></div>
    </Card>
  </>
}
