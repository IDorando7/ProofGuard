import type { NodeRecord, RoutingRecord } from '../types/protocol'
import { Card, EmptyState, EntityIcon, InfoTip, SectionHeading, StatusBadge } from './ui'
import { percent, shortId, titleCase } from '../utils/format'

export function RoutingSection({ routing, nodes }: { routing: RoutingRecord; nodes: NodeRecord[] }) {
  return <section id="routing" className="audit-section">
    <SectionHeading eyebrow="Stage 02" title="Agent Routing" detail="Backend-ranked production agents and exploration/shadow capacity by vulnerability category." />
    <div className="routing-grid">{routing.results.map(result => <Card key={result.category} className="routing-card">
      <div className="routing-card__header"><div className="routing-title"><EntityIcon name="route" tone="info"/><div><span className="tag">{titleCase(result.category)}</span><h3>{result.subnet_id ?? 'Subnet unavailable'}</h3></div></div><StatusBadge status={result.complete ? 'complete' : 'partial'} /></div>
      {!result.assignments.length ? <EmptyState title="No selected agents" detail={result.shortage_reasons.map(titleCase).join(' · ') || 'No assignments returned.'} /> : <div className="assignment-list">{result.assignments.map(assignment => {
        const node = nodes.find(item => item.node_id === assignment.node_id)
        return <div className={`assignment assignment--${assignment.assignment_mode}`} key={assignment.assignment_id}>
          <div className="assignment__rank">#{assignment.position}</div>
          <div className="assignment__identity"><strong>{node?.display_name ?? shortId(assignment.node_id)}</strong><small>{shortId(assignment.node_id)} · {shortId(node?.operator_id, 8)}</small></div>
          <div className="assignment__score"><strong>{percent(assignment.category_score)}</strong><small><InfoTip term="CategoryScore">Backend-derived specialization score for routing.</InfoTip> score · {percent(assignment.experience_confidence)} confidence</small></div>
          <div className="assignment__status"><StatusBadge status={assignment.assignment_mode} /><StatusBadge status={assignment.membership_status} /></div>
          <p className="assignment__reason">{assignment.selection_reasons.join(' · ')}</p>
        </div>
      })}</div>}
    </Card>)}</div>
  </section>
}
