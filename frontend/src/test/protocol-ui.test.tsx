import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { MemoryRouter } from 'react-router-dom'
import { AuditPipeline } from '../components/AuditPipeline'
import { CommitteePanel } from '../components/CommitteePanel'
import { AccountingCard, RewardsPanel } from '../components/RewardsPanel'
import { ConsensusView, DisputeTimeline } from '../components/ConsensusView'
import { NodeRolePerformance } from '../components/NodeRolePerformance'
import { ErrorState, LoadingState, StatusBadge, Unavailable } from '../components/ui'
import { ClustersSection } from '../components/ClustersSection'
import { PresentationFrame } from '../components/PresentationFrame'
import { auditBundle, clusterProtocol, disputedConsensus, finalConsensus } from './fixtures'

describe('resilient protocol presentation', () => {
  it('renders loading and API error states', () => {
    const { rerender } = render(<LoadingState />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading protocol data')
    rerender(<ErrorState error={new Error('Backend offline')} />)
    expect(screen.getByRole('alert')).toHaveTextContent('Backend offline')
  })

  it('renders the full audit flow from bundle records', () => {
    render(<AuditPipeline bundle={auditBundle()} />)
    expect(screen.getByRole('navigation', { name: /end-to-end audit pipeline/i })).toHaveTextContent('Project Intake')
    expect(screen.getByText('Validator Committees')).toBeInTheDocument()
    expect(screen.getByText('Rewards')).toBeInTheDocument()
  })

  it('visually separates authoritative and shadow committee work', () => {
    render(<CommitteePanel protocol={clusterProtocol()} />)
    expect(screen.getByText('Authoritative')).toBeInTheDocument()
    expect(screen.getByText('Shadow')).toBeInTheDocument()
    expect(screen.getByText('Does not count toward consensus')).toBeInTheDocument()
  })

  it('uses the backend outcome for a 3/2 disputed consensus', () => {
    render(<ConsensusView consensuses={[disputedConsensus()]} />)
    expect(screen.getByText('Disputed')).toBeInTheDocument()
    expect(screen.getByText('Accepted').parentElement).toHaveTextContent('3 Accepted')
    expect(screen.getAllByText('Rejected')[0].parentElement).toHaveTextContent('2 Rejected')
    expect(screen.getAllByText(/required 4/).length).toBeGreaterThan(0)
  })

  it('renders escalation rounds and confirmed minority alignment', () => {
    const protocol = clusterProtocol()
    render(<DisputeTimeline consensuses={[disputedConsensus(), finalConsensus()]} disputes={protocol.disputes} />)
    expect(screen.getByText(/New escalation round: round-2/)).toBeInTheDocument()
    expect(screen.getByText('The original minority validators were ultimately aligned with the final resolution.')).toBeInTheDocument()
  })

  it('keeps hybrid agent and validator scores in separate sections', () => {
    const node = { node_id: 'hybrid-1', display_name: 'Hybrid', node_type: 'hybrid', operator_id: 'op', status: 'active', supported_categories: ['access_control'], reputation_score: .5, statistics: {}, created_at: '', updated_at: '' }
    render(<NodeRolePerformance node={node} agentScores={[]} agentMemberships={[]} validatorScores={[]} validatorPerformance={[]} validatorMemberships={[]} />)
    expect(screen.getByRole('heading', { name: 'Agent Performance' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Validator Performance' })).toBeInTheDocument()
  })

  it('never mixes miner and validator reward streams', () => {
    render(<RewardsPanel bundle={auditBundle()} />)
    const miner = screen.getByRole('heading', { name: 'Miner Rewards' })
    const validator = screen.getByRole('heading', { name: 'Validator Rewards' })
    expect(miner.closest('.reward-stream')).toHaveClass('reward-stream--miner')
    expect(validator.closest('.reward-stream')).toHaveClass('reward-stream--validator')
  })

  it('renders accounting PASS/FAIL only from verification and marks missing data unavailable', () => {
    const { rerender } = render(<AccountingCard title="Validator Pool Conservation" verification={{ verification_status: 'clean_finalized', ok: true, accounting_conserved: true, event_set_complete: true, errors: [] }} pool="20" distributed="18" undistributed="2" />)
    expect(screen.getAllByText('Pass').length).toBeGreaterThan(0)
    rerender(<AccountingCard title="Validator Pool Conservation" verification={{ verification_status: 'finalized_event_mismatch', ok: false, accounting_conserved: false, event_set_complete: false, errors: ['mismatch'] }} pool="20" distributed="18" undistributed="2" />)
    expect(screen.getAllByText('Fail').length).toBeGreaterThan(0)
    rerender(<Unavailable />)
    expect(screen.getByText('Data unavailable')).toBeInTheDocument()
    expect(screen.queryByText('Pass')).not.toBeInTheDocument()
  })

  it('keeps frozen cluster membership separate from pending validation truth', () => {
    const bundle = auditBundle()
    bundle.clusters[0].cluster.final_validation_status = null
    bundle.clusters[0].cluster.validator_consensus_outcome = null
    bundle.clusters[0].cluster.validation_authority = 'pending_validator_consensus'
    bundle.clusters[0].consensuses = []
    render(<ClustersSection bundle={bundle}/>)
    expect(screen.getAllByText('Cluster membership').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Validation truth').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Finalized').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Pending Validator Consensus').length).toBeGreaterThan(0)
    expect(screen.queryByText('Accepted')).not.toBeInTheDocument()
  })

  it('keeps confirmed and rejected protocol outcomes visually distinct', () => {
    render(<><span data-testid="confirmed"><StatusBadge status="confirmed"/></span><span data-testid="rejected"><StatusBadge status="rejected"/></span></>)
    expect(screen.getByTestId('confirmed').firstChild).toHaveClass('status--positive')
    expect(screen.getByTestId('rejected').firstChild).toHaveClass('status--negative')
  })

  it('navigates Presentation Mode locally without issuing backend mutations', () => {
    const fetchSpy = vi.spyOn(globalThis, 'fetch')
    function Harness() {
      const [stage, setStage] = useState(0)
      return <PresentationFrame labels={['Intake', 'Consensus']} stage={stage} onStage={setStage} exitPath="/audits/project/routing" projectName="Audit">Stage {stage + 1}</PresentationFrame>
    }
    render(<MemoryRouter><Harness/></MemoryRouter>)
    expect(screen.getByText('Stage 1')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /next/i }))
    expect(screen.getByText('Stage 2')).toBeInTheDocument()
    fireEvent.keyDown(window, { key: 'ArrowLeft' })
    expect(screen.getByText('Stage 1')).toBeInTheDocument()
    expect(fetchSpy).not.toHaveBeenCalled()
    fetchSpy.mockRestore()
  })
})
