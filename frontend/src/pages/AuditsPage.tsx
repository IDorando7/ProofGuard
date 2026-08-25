import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { loadAuditIndex, queryKeys } from '../api/client'
import { Card, EmptyState, ErrorState, LoadingState, PageHeader, SectionHeading, StatusBadge, Unavailable } from '../components/ui'
import { Icon } from '../components/Icon'
import { formatDate, shortId, titleCase } from '../utils/format'

export function AuditsPage() {
  const query = useQuery({ queryKey: queryKeys.auditIndex, queryFn: ({ signal }) => loadAuditIndex(signal) })
  if (query.isLoading) return <LoadingState label="Loading audit registry" />
  if (query.error) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  const rows = query.data!
  return <>
    <PageHeader eyebrow="Protocol executions" title="Audits" detail="Projects, routing plans, validation outcomes, and reward lifecycle state." action={<button className="button button--secondary" onClick={() => query.refetch()}><Icon name="refresh" size={15}/>Refresh</button>} />
    <Card variant="elevated">
      <SectionHeading title="Audit Registry" detail={`${rows.length} project/routing record${rows.length === 1 ? '' : 's'}`} />
      {!rows.length ? <EmptyState title="No audits available" detail="The backend project registry returned no records." /> : <div className="table-wrap"><table>
        <thead><tr><th>Project</th><th>Routing / Task</th><th>Categories</th><th>Status</th><th>Findings</th><th>Validation</th><th>Rewards</th><th>Created</th></tr></thead>
        <tbody>{rows.map((row, index) => <tr key={`${row.project.project_id}-${row.routing?.routing_id ?? index}`}>
          <td><strong>{row.project.project_name}</strong><small className="cell-sub">{shortId(row.project.project_id)}</small></td>
          <td>{row.routing ? <Link className="mono-link" to={`/audits/${row.project.project_id}/${row.routing.routing_id}`}>{shortId(row.routing.routing_id, 7)}</Link> : <Unavailable compact />}</td>
          <td><div className="tag-list">{row.routing?.requested_categories.map(category => <span className="tag" key={category}>{titleCase(category)}</span>) ?? <Unavailable compact />}</div></td>
          <td><StatusBadge status={row.run?.status ?? row.routing?.status ?? row.project.status} /></td>
          <td>{row.submissionCount ?? <Unavailable compact />} submissions<br/><small className="cell-sub">{row.clusterCount ?? <Unavailable compact />} clusters</small></td>
          <td>{row.validationStatus ? <StatusBadge status={row.validationStatus} /> : <Unavailable compact />}</td>
          <td>{row.rewardStatus ? <span className="cell-sub">{titleCase(row.rewardStatus)}</span> : <Unavailable compact />}</td>
          <td>{formatDate(row.routing?.calculated_at ?? row.project.created_at)}</td>
        </tr>)}</tbody>
      </table></div>}
    </Card>
  </>
}
