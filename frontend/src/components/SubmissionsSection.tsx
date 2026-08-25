import { useMemo, useState } from 'react'
import type { ClusterProtocolData, NodeRecord, Submission } from '../types/protocol'
import { Card, EmptyState, SectionHeading, StatusBadge } from './ui'
import { shortId, titleCase } from '../utils/format'

export function SubmissionsSection({ submissions, clusters, nodes }: { submissions: Submission[]; clusters: ClusterProtocolData[]; nodes: NodeRecord[] }) {
  const [category, setCategory] = useState('all')
  const [state, setState] = useState('all')
  const [search, setSearch] = useState('')
  const rows = useMemo(() => submissions.filter(submission => {
    const node = nodes.find(item => item.node_id === submission.node_id)
    const needle = `${submission.node_id} ${node?.operator_id ?? ''}`.toLowerCase()
    return (category === 'all' || submission.category === category) && (state === 'all' || submission.status === state) && (!search || needle.includes(search.toLowerCase()))
  }), [submissions, nodes, category, state, search])
  const clusterFor = (submissionId: string) => clusters.find(item => item.cluster.members.some(member => member.submission_id === submissionId))?.cluster
  return <section id="submissions" className="audit-section">
    <Card>
      <SectionHeading eyebrow="Stages 03–04" title="Candidate Findings" detail="Agent submissions retain their own lifecycle and link to FindingClusters when clustered." action={<div className="filter-row">
        <select aria-label="Submission category" value={category} onChange={event => setCategory(event.target.value)}><option value="all">All categories</option>{[...new Set(submissions.map(item => item.category))].map(item => <option key={item}>{item}</option>)}</select>
        <select aria-label="Validation state" value={state} onChange={event => setState(event.target.value)}><option value="all">All states</option>{[...new Set(submissions.map(item => item.status))].map(item => <option key={item}>{item}</option>)}</select>
        <input aria-label="Node or operator" placeholder="Node / operator" value={search} onChange={event => setSearch(event.target.value)} />
      </div>} />
      {!rows.length ? <EmptyState title="No candidate submissions" detail="No backend submission records match the current filters." /> : <div className="table-wrap"><table>
        <thead><tr><th>Submission</th><th>Node / operator</th><th>Category</th><th>Validation state</th><th>Finding hash</th><th>FindingCluster</th><th>Reward</th></tr></thead>
        <tbody>{rows.map(submission => {
          const node = nodes.find(item => item.node_id === submission.node_id)
          const cluster = clusterFor(submission.submission_id)
          return <tr key={submission.submission_id}><td><code>{shortId(submission.submission_id)}</code></td><td><strong>{node?.display_name ?? shortId(submission.node_id)}</strong><small className="cell-sub">{shortId(node?.operator_id, 9)}</small></td><td>{titleCase(submission.category)}</td><td><StatusBadge status={submission.status} /></td><td><code>{shortId(submission.finding_hash, 9)}</code></td><td>{cluster ? <a className="mono-link" href={`#cluster-${cluster.finding_cluster_id}`}>{shortId(cluster.finding_cluster_id, 7)}</a> : <span className="cell-sub">Unclustered</span>}</td><td><StatusBadge status={submission.reward_status} /></td></tr>
        })}</tbody>
      </table></div>}
    </Card>
  </section>
}
