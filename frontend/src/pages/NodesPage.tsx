import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api, queryKeys } from '../api/client'
import { Card, EmptyState, EntityIcon, ErrorState, LoadingState, Metric, PageHeader, SectionHeading, StatusBadge } from '../components/ui'
import { Icon } from '../components/Icon'
import { shortId, titleCase } from '../utils/format'

export function NodesPage() {
  const [type, setType] = useState('all')
  const [search, setSearch] = useState('')
  const query = useQuery({ queryKey: queryKeys.nodes, queryFn: ({ signal }) => api.nodes(signal) })
  const nodes = useMemo(() => (query.data ?? []).filter(node => {
    const matchesType = type === 'all' || node.node_type === type
    const needle = search.toLowerCase()
    return matchesType && (!needle || `${node.node_id} ${node.operator_id} ${node.display_name}`.toLowerCase().includes(needle))
  }), [query.data, type, search])
  if (query.isLoading) return <LoadingState label="Loading node registry" />
  if (query.error) return <ErrorState error={query.error} onRetry={() => query.refetch()} />
  return <>
    <PageHeader eyebrow="Network registry" title="Nodes" detail="Specialized agents, independent validators, and hybrid participants with role-specific performance." action={<button className="button button--secondary" onClick={() => query.refetch()}><Icon name="refresh" size={15}/>Refresh</button>} />
    <div className="metric-grid metric-grid--4"><Metric label="Registered" value={query.data?.length ?? 0}/><Metric label="Agents" value={query.data?.filter(item => item.node_type === 'agent').length ?? 0}/><Metric label="Validators" value={query.data?.filter(item => item.node_type === 'validator').length ?? 0}/><Metric label="Hybrid" value={query.data?.filter(item => item.node_type === 'hybrid').length ?? 0}/></div>
    <Card variant="elevated">
      <SectionHeading title="Network Participants" detail={`${nodes.length} visible of ${query.data?.length ?? 0}`} action={<label className="search-box"><Icon name="search" size={15}/><input className="search-input" value={search} onChange={event => setSearch(event.target.value)} placeholder="Search node or operator" aria-label="Search nodes" /></label>} />
      <div className="tabs" role="tablist">{[['all','All'],['agent','Agents'],['validator','Validators'],['hybrid','Hybrid']].map(([value,label]) => <button role="tab" aria-selected={type === value} className={type === value ? 'tab tab--active' : 'tab'} onClick={() => setType(value)} key={value}>{label}</button>)}</div>
      {!nodes.length ? <EmptyState title="No matching nodes" detail="The node registry has no records matching these filters." /> : <div className="table-wrap"><table>
        <thead><tr><th>Node</th><th>Operator</th><th>Role</th><th>Status</th><th>Supported categories</th><th>Global reputation</th></tr></thead>
        <tbody>{nodes.map(node => <tr key={node.node_id}>
          <td><div className="node-identity"><EntityIcon name={node.node_type === 'validator' ? 'validator' : 'agent'} tone={node.node_type === 'validator' ? 'positive' : 'info'} size="small"/><span><Link className="table-primary" to={`/nodes/${node.node_id}`}>{node.display_name}</Link><small className="cell-sub">{shortId(node.node_id)}</small></span></div></td>
          <td><code>{shortId(node.operator_id, 10)}</code></td><td><StatusBadge status={node.node_type} /></td><td><StatusBadge status={node.status} /></td>
          <td><div className="tag-list">{node.supported_categories.map(item => <span className="tag" key={item}>{titleCase(item)}</span>)}</div></td>
          <td><strong>{Math.round(node.reputation_score * 100)}</strong><span className="cell-sub"> / 100</span></td>
        </tr>)}</tbody>
      </table></div>}
    </Card>
  </>
}
