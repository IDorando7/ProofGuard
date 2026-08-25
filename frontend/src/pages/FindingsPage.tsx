import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { loadAuditBundle, loadAuditIndex } from '../api/client'
import type { ClusterProtocolData, Project, RoutingRecord } from '../types/protocol'
import { Card, EmptyState, EntityIcon, ErrorState, LoadingState, PageHeader, SectionHeading, SeverityBadge, StatusBadge } from '../components/ui'
import { shortId, titleCase } from '../utils/format'

interface FindingRow { project: Project; routing: RoutingRecord; protocol: ClusterProtocolData }

export function FindingsPage() {
  const [category, setCategory] = useState('all')
  const [status, setStatus] = useState('all')
  const query = useQuery({
    queryKey: ['findings-index'],
    queryFn: async ({ signal }) => {
      const index = await loadAuditIndex(signal)
      const bundles = await Promise.all(index.filter(row => row.routing).map(row => loadAuditBundle(row.project.project_id, row.routing!.routing_id, signal)))
      return bundles.flatMap(bundle => bundle.clusters.map(protocol => ({ project: bundle.project, routing: bundle.routing, protocol })))
    },
  })
  const rows = useMemo(() => (query.data ?? []).filter(row =>
    (category === 'all' || row.protocol.cluster.category === category) &&
    (status === 'all' || (row.protocol.cluster.validator_consensus_outcome ?? row.protocol.cluster.final_validation_status) === status)
  ), [query.data, category, status])
  const categories = [...new Set((query.data ?? []).map(row => row.protocol.cluster.category))]
  const statuses = [...new Set((query.data ?? []).map(row => row.protocol.cluster.validator_consensus_outcome ?? row.protocol.cluster.final_validation_status).filter((item): item is string => Boolean(item)))]
  if (query.isLoading) return <LoadingState label="Loading FindingClusters" />
  if (query.error) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  return <>
    <PageHeader eyebrow="Security intelligence" title="FindingClusters" detail="Immutable root-cause groupings and their independently resolved validation state across audits." />
    <Card variant="elevated">
      <SectionHeading title="FindingClusters" detail={`${rows.length} visible clusters`} action={<div className="filter-row">
        <select aria-label="Filter category" value={category} onChange={event => setCategory(event.target.value)}><option value="all">All categories</option>{categories.map(item => <option value={item} key={item}>{titleCase(item)}</option>)}</select>
        <select aria-label="Filter consensus status" value={status} onChange={event => setStatus(event.target.value)}><option value="all">All outcomes</option>{statuses.map(item => <option value={item} key={item}>{titleCase(item)}</option>)}</select>
      </div>} />
      {!rows.length ? <EmptyState title="No FindingClusters" detail="No clusters match the current backend data and filters." /> : <div className="cluster-grid">{rows.map(row => <FindingIndexCard key={row.protocol.cluster.finding_cluster_id} row={row} />)}</div>}
    </Card>
  </>
}

function FindingIndexCard({ row }: { row: FindingRow }) {
  const cluster = row.protocol.cluster
  const outcome = cluster.validator_consensus_outcome ?? cluster.final_validation_status
  return <Link className="cluster-card" to={`/audits/${row.project.project_id}/${row.routing.routing_id}#cluster-${cluster.finding_cluster_id}`}>
    <div className="cluster-card__top"><EntityIcon name="cluster" tone="info"/><div className="cluster-card__badges"><span className="tag">{titleCase(cluster.category)}</span><StatusBadge status={outcome} /></div></div>
    <div className="cluster-card__title"><h3>{titleCase(cluster.root_cause_key)}</h3><p>Canonical root cause</p></div>
    <p className="mono-muted">{shortId(cluster.finding_cluster_id, 10)}</p>
    <div className="cluster-card__states"><span><small>Cluster state</small><StatusBadge status={cluster.status}/></span><span><small>Validation state</small><StatusBadge status={outcome}/></span></div>
    <div className="cluster-card__stats"><span><strong><SeverityBadge severity={cluster.final_severity ?? cluster.claimed_severity}/></strong>severity</span><span><strong>{cluster.report_count}</strong>reports</span><span><strong>{cluster.distinct_operator_count}</strong>operators</span></div>
    <small className="cluster-card__project">{row.project.project_name}</small>
  </Link>
}
