import type { NodeDetailData } from '../api/client'
import { Card, EmptyState, InfoTip, ProgressBar, SectionHeading, StatusBadge, Unavailable } from './ui'
import { percent, titleCase } from '../utils/format'

type Props = Pick<NodeDetailData, 'node' | 'agentScores' | 'agentMemberships' | 'validatorScores' | 'validatorPerformance' | 'validatorMemberships'>

export function NodeRolePerformance({ node, agentScores, agentMemberships, validatorScores, validatorPerformance, validatorMemberships }: Props) {
  const hasAgentRole = node.node_type === 'agent' || node.node_type === 'hybrid'
  const hasValidatorRole = node.node_type === 'validator' || node.node_type === 'hybrid'
  return <div className={node.node_type === 'hybrid' ? 'role-grid role-grid--hybrid' : 'role-grid'}>
    {hasAgentRole && <Card className="role-card role-card--agent">
      <SectionHeading eyebrow="Agent role" title="Agent Performance" detail="CategoryScore, confidence, and agent subnet membership remain separate from validator history." />
      {!agentScores.length ? <EmptyState title="No Agent CategoryScores" detail="Current agent metrics are unavailable; no history is fabricated." /> : <div className="score-stack">{agentScores.map(score => <div className="score-row" key={score.score_id}>
        <div className="score-row__head"><strong>{titleCase(score.category)}</strong><span className="tag-list"><StatusBadge status={score.score_band} />{agentMemberships.find(item => item.category === score.category) ? <StatusBadge status={agentMemberships.find(item => item.category === score.category)!.status} /> : <Unavailable compact />}</span></div>
        <div className="score-row__value"><span>{percent(score.category_score)}</span><ProgressBar label={`${score.category} CategoryScore`} value={score.category_score} /></div>
        <div className="score-row__meta"><span><InfoTip term="CategoryScore">Backend-normalized category specialization.</InfoTip> CategoryScore</span><span>{score.finalized_submissions} finalized · {score.accepted_unique_submissions} accepted unique</span></div>
      </div>)}</div>}
    </Card>}

    {hasValidatorRole && <Card className="role-card role-card--validator">
      <SectionHeading eyebrow="Validator role" title="Validator Performance" detail="ValidatorCategoryScore, confidence, and ValidatorMembership use validator-only history." />
      {!validatorScores.length ? <EmptyState title="No ValidatorCategoryScores" detail="Current validator metrics are unavailable; no history is fabricated." /> : <div className="score-stack">{validatorScores.map(score => {
        const membership = validatorMemberships.find(item => item.category === score.category)
        const performance = validatorPerformance.find(item => item.category === score.category)
        return <div className="score-row" key={`${score.validator_node_id}-${score.category}`}>
          <div className="score-row__head"><strong>{titleCase(score.category)}</strong>{membership ? <StatusBadge status={membership.status} /> : <Unavailable compact />}</div>
          <div className="score-row__value"><span>{percent(score.final_score)}</span><ProgressBar label={`${score.category} ValidatorCategoryScore`} value={Number(score.final_score)} /></div>
          <div className="score-row__meta"><span>Confidence {percent(score.experience_confidence)}</span><span>{performance?.resolved_validations ?? score.resolved_validations} resolved · VQ {performance ? percent(performance.average_quality_score) : 'Unavailable'}</span></div>
        </div>
      })}</div>}
    </Card>}
  </div>
}
