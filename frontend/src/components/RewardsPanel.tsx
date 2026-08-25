import type { AuditBundle, Verification } from '../types/protocol'
import { Card, EmptyState, EntityIcon, InfoTip, Metric, SectionHeading, StatusBadge, Unavailable } from './ui'
import { Icon } from './Icon'
import { points, shortId, titleCase } from '../utils/format'

function RewardFlow({ items }: { items: string[] }) {
  return <div className="reward-flow">{items.map((item, index) => <div key={item} className="reward-flow__unit"><span>{item}</span>{index < items.length - 1 && <b><Icon name="arrow-right" size={13}/></b>}</div>)}</div>
}

export function AccountingCard({ title, verification, distributed, undistributed, pool }: { title: string; verification: Verification | null; distributed?: string; undistributed?: string; pool?: string }) {
  return <Card className="accounting-card">
    <div className="accounting-card__head"><div className="accounting-title"><EntityIcon name="check" tone={verification?.ok ? 'positive' : 'neutral'}/><div><p className="eyebrow">Backend verification</p><h3>{title}</h3></div></div>{verification ? <StatusBadge status={verification.ok ? 'pass' : 'fail'} /> : <Unavailable compact />}</div>
    <div className="accounting-equation"><span>{points(distributed)}<small>Distributed</small></span><b>+</b><span>{points(undistributed)}<small>Undistributed</small></span><b>=</b><span>{points(pool)}<small>Pool</small></span></div>
    {verification ? <dl className="check-list"><div><dt>Verification state</dt><dd>{titleCase(verification.verification_status)}</dd></div><div><dt>Accounting conserved</dt><dd><StatusBadge status={(verification.accounting_conserved ?? verification.budget_conserved) ? 'pass' : 'fail'} /></dd></div><div><dt>Event set complete</dt><dd><StatusBadge status={verification.event_set_complete ? 'pass' : 'fail'} /></dd></div>{verification.duplicate_events_found !== undefined && <div><dt>Duplicate reward events</dt><dd>{verification.duplicate_events_found ? 'Found' : 'None found'}</dd></div>}</dl> : <p className="muted">A backend verification record is not available. No PASS/FAIL claim is made.</p>}
  </Card>
}

export function RewardsPanel({ bundle, standalone = false }: { bundle: AuditBundle; standalone?: boolean }) {
  const budget = bundle.budget
  return <section id="rewards" className={standalone ? '' : 'audit-section'}>
    {!standalone && <SectionHeading eyebrow="Stage 09" title="Miner & Validator Rewards" detail="Separate client-task reward streams with backend-computed allocations and verification." />}
    <Card className="budget-card" variant="highlight">
      <div className="budget-total"><EntityIcon name="rewards" tone="info" size="large"/><p className="eyebrow">TaskRewardBudget</p><span>Total budget</span><strong>{budget ? points(budget.total_budget_points) : <Unavailable compact />}</strong><small>{budget?.reward_unit ? titleCase(budget.reward_unit) : 'Protocol points'}</small>{budget && <StatusBadge status={budget.status} />}</div>
      <div className="budget-branch" aria-label="Task budget pool allocation"><span className="budget-branch__stem"/><div className="budget-pools"><Metric label="Miner Pool" value={budget ? points(budget.miner_pool_points) : <Unavailable compact />} tone="miner" /><Metric label="Validator Pool" value={budget ? points(budget.validator_pool_points) : <Unavailable compact />} tone="validator" /><Metric label="Protocol Reserve" value={budget ? points(budget.protocol_pool_points) : <Unavailable compact />} tone="protocol" /></div></div>
    </Card>

    <div className="reward-stream reward-stream--miner">
      <div className="stream-heading"><div className="stream-title"><EntityIcon name="findings" tone="info" size="large"/><div><p className="eyebrow">Separate economic stream</p><h2>Miner Rewards</h2><p>FindingCluster value and report quality drive operator allocations.</p></div></div><StatusBadge status={bundle.minerCycle?.status} /></div>
      <RewardFlow items={['FindingCluster value', 'Cluster allocation', 'ReportQualityAssessment', 'Top-K', 'Q²', 'Chief Finder', 'RewardEvents']} />
      {!bundle.minerCycle ? <Card><EmptyState title="No miner reward cycle" detail="The backend has not returned a Week 7 task reward cycle for this routing." /></Card> : <>
        <div className="metric-grid metric-grid--4"><Metric label="Miner pool" value={points(bundle.minerCycle.miner_pool_points)} /><Metric label="Distributed" value={points(bundle.minerCycle.distributed_miner_points)} /><Metric label="Undistributed" value={points(bundle.minerCycle.undistributed_miner_points)} /><Metric label="Reward events" value={bundle.minerCycle.reward_event_count} detail={`${bundle.minerCycle.chief_finder_count} Chief Finders`} /></div>
        {bundle.minerEvents.length > 0 && <Card><div className="table-wrap"><table><thead><tr><th>Operator / node</th><th>Cluster</th><th>FindingScore</th><th>Report quality</th><th>Rank</th><th>Chief Finder</th><th>Total reward</th></tr></thead><tbody>{bundle.minerEvents.map(event => <tr key={event.reward_event_id}><td><code>{shortId(event.operator_id)}</code><small className="cell-sub">{shortId(event.node_id)}</small></td><td><code>{shortId(event.finding_cluster_id)}</code></td><td>{points(event.finding_score)} <InfoTip term="FindingScore">Backend-computed severity and uniqueness value.</InfoTip></td><td>{points(event.quality_score)}</td><td>#{event.quality_rank}</td><td>{event.chief_finder ? <StatusBadge status="confirmed" label="CHIEF FINDER" /> : '—'}</td><td><strong>{points(event.total_reward_points)}</strong></td></tr>)}</tbody></table></div></Card>}
      </>}
    </div>

    <div className="reward-stream reward-stream--validator">
      <div className="stream-heading"><div className="stream-title"><EntityIcon name="validator" tone="positive" size="large"/><div><p className="eyebrow">Separate economic stream</p><h2>Validator Rewards</h2><p>Authoritative work units receive completion and quality allocations.</p></div></div><StatusBadge status={bundle.validatorCycle?.status} /></div>
      <RewardFlow items={['Authoritative Work Units', 'Equal Base Budget', '30% Completion', '+ 70% Quality', 'VQ²', 'ValidatorRewardAllocation']} />
      {!bundle.validatorCycle ? <Card><EmptyState title="No validator reward cycle" detail="The backend has not returned a validator-pool cycle for this routing. Shadow validators remain client reward ineligible by protocol policy when exposed." /></Card> : <>
        <div className="metric-grid metric-grid--4"><Metric label="Authoritative work units" value={bundle.validatorCycle.authoritative_work_units} /><Metric label="Completed units" value={bundle.validatorCycle.completed_work_units} /><Metric label="Distributed" value={points(bundle.validatorCycle.distributed_validator_points)} /><Metric label="Undistributed" value={points(bundle.validatorCycle.undistributed_validator_points)} /></div>
        <Card><div className="table-wrap"><table><thead><tr><th>Validator / operator</th><th>Cluster / round</th><th>Base budget</th><th>30% completion</th><th>VQ</th><th>70% quality</th><th>Total</th><th>Undistributed</th></tr></thead><tbody>{bundle.validatorCycle.allocations.map(allocation => <tr key={allocation.validator_reward_allocation_id}><td><code>{shortId(allocation.validator_node_id)}</code><small className="cell-sub">{shortId(allocation.validator_operator_id)}</small></td><td><code>{shortId(allocation.finding_cluster_id)}</code><small className="cell-sub">{shortId(allocation.validation_round_id)}</small></td><td>{points(allocation.base_work_unit_budget)}</td><td>{points(allocation.completion_reward)}</td><td>{allocation.validation_quality_score !== null ? `${Math.round(Number(allocation.validation_quality_score) * 100)}%` : <Unavailable compact />}</td><td>{points(allocation.quality_reward)}</td><td><strong>{points(allocation.total_reward)}</strong></td><td>{points(allocation.undistributed_points)}</td></tr>)}</tbody></table></div></Card>
        <div className="shadow-ineligible"><StatusBadge status="shadow" /><strong>Client reward ineligible</strong><span>Shadow validator work does not enter ValidatorRewardAllocation when the backend policy reports shadow_reward_eligible=false.</span></div>
      </>}
    </div>

    <div className="accounting-grid">
      <AccountingCard title="Miner Pool Conservation" verification={bundle.minerVerification} pool={bundle.minerCycle?.miner_pool_points} distributed={bundle.minerCycle?.distributed_miner_points} undistributed={bundle.minerCycle?.undistributed_miner_points} />
      <AccountingCard title="Validator Pool Conservation" verification={bundle.validatorVerification} pool={bundle.validatorCycle?.validator_pool_points} distributed={bundle.validatorCycle?.distributed_validator_points} undistributed={bundle.validatorCycle?.undistributed_validator_points} />
    </div>
  </section>
}
