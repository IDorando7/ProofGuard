import type { AuditBundle } from '../types/protocol'
import { StatusBadge } from './ui'
import { Icon, type IconName } from './Icon'

export interface PipelineStage { id: string; label: string; status: string; count?: number; summary: string }

function backendStage(bundle: AuditBundle, candidates: string[], fallback: string): string {
  const state = bundle.auditRun?.stage_states.find(item => candidates.includes(String(item.stage ?? item.name ?? '').toLowerCase()))
  return String(state?.status ?? fallback)
}

export function auditPipelineStages(bundle: AuditBundle): PipelineStage[] {
  const committees = bundle.clusters.flatMap(item => item.committees)
  const reproductions = bundle.clusters.flatMap(item => item.reproductions)
  const consensuses = bundle.clusters.flatMap(item => item.consensuses)
  const rewardStatuses = [bundle.minerCycle?.status, bundle.validatorCycle?.status].filter(Boolean)
  return [
    { id: 'intake', label: 'Project Intake', status: bundle.project.status, summary: bundle.scope ? `${bundle.scope.contracts_in_scope?.length ?? 0} contracts in scope` : 'Scope unavailable' },
    { id: 'routing', label: 'Routing', status: bundle.routing.status, count: bundle.routing.total_assignments, summary: `${bundle.routing.production_assignments} production · ${bundle.routing.shadow_assignments} shadow` },
    { id: 'analysis', label: 'Agent Analysis', status: backendStage(bundle, ['agent_analysis', 'agents'], bundle.auditRun?.status ?? 'unavailable'), count: bundle.agentExecutions.length, summary: `${bundle.agentExecutions.length} agent executions` },
    { id: 'submissions', label: 'Findings', status: backendStage(bundle, ['findings', 'submissions'], bundle.submissions.length ? 'submitted' : 'unavailable'), count: bundle.submissions.length, summary: `${bundle.submissions.length} candidate submissions` },
    { id: 'clusters', label: 'Finding Clusters', status: bundle.clusters[0]?.cluster.status ?? 'unavailable', count: bundle.clusters.length, summary: `${bundle.clusters.length} unique root causes` },
    { id: 'committees', label: 'Validator Committees', status: committees.at(-1)?.status ?? 'unavailable', count: committees.length, summary: `${committees.length} committee records` },
    { id: 'reproduction', label: 'Independent Reproduction', status: reproductions.length ? 'evidence_ready' : 'unavailable', count: reproductions.length, summary: `${reproductions.length} attributable results` },
    { id: 'consensus', label: 'Consensus', status: consensuses.at(-1)?.consensus_outcome ?? 'unavailable', count: consensuses.length, summary: `${consensuses.length} consensus snapshots` },
    { id: 'rewards', label: 'Rewards', status: rewardStatuses.length ? rewardStatuses.join(' / ') : 'unavailable', count: (bundle.minerCycle?.reward_event_count ?? 0) + (bundle.validatorCycle?.reward_event_count ?? 0), summary: 'Miner and validator streams' },
  ]
}

export function AuditPipeline({ bundle, onStage }: { bundle: AuditBundle; onStage?: (id: string) => void }) {
  const stageIcons: Record<string, IconName> = { intake: 'shield', routing: 'route', analysis: 'agent', submissions: 'findings', clusters: 'cluster', committees: 'validator', reproduction: 'check', consensus: 'consensus', rewards: 'rewards' }
  return <nav className="pipeline" aria-label="End-to-end audit pipeline">
    {auditPipelineStages(bundle).map((stage, index, stages) => <div className="pipeline__unit" key={stage.id}>
      <button className="pipeline__stage" onClick={() => onStage ? onStage(stage.id) : document.getElementById(stage.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })}>
        <span className="pipeline__top"><span className="pipeline__number">{String(index + 1).padStart(2, '0')}</span><span className="pipeline__icon"><Icon name={stageIcons[stage.id]}/></span></span>
        <span className="pipeline__content"><strong>{stage.label}</strong><small>{stage.summary}</small><StatusBadge status={stage.status} /></span>
      </button>
      {index < stages.length - 1 && <span className="pipeline__connector" aria-hidden="true"><Icon name="arrow-right" size={14}/></span>}
    </div>)}
  </nav>
}
