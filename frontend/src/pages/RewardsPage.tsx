import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { loadAuditBundle, loadAuditIndex, queryKeys } from '../api/client'
import { RewardsPanel } from '../components/RewardsPanel'
import { Card, EmptyState, ErrorState, LoadingState, PageHeader, SectionHeading } from '../components/ui'

export function RewardsPage() {
  const index = useQuery({ queryKey: queryKeys.auditIndex, queryFn: ({ signal }) => loadAuditIndex(signal) })
  const options = useMemo(() => (index.data ?? []).filter(row => row.routing), [index.data])
  const [selectedOverride, setSelected] = useState('')
  const selected = selectedOverride || (() => {
    if (!options.length) return ''
    const demo = localStorage.getItem('proofguard-demo-audit')
    const preferred = options.find(row => `${row.project.project_id}/${row.routing!.routing_id}` === demo) ?? options[0]
    return `${preferred.project.project_id}/${preferred.routing!.routing_id}`
  })()
  const [projectId = '', routingId = ''] = selected.split('/')
  const detail = useQuery({ queryKey: queryKeys.rewards(projectId, routingId), queryFn: ({ signal }) => loadAuditBundle(projectId, routingId, signal), enabled: Boolean(projectId && routingId) })
  if (index.isLoading) return <LoadingState label="Loading reward streams" />
  if (index.error) return <ErrorState error={index.error} onRetry={() => index.refetch()} />
  return <>
    <PageHeader eyebrow="Protocol accounting" title="Rewards" detail="Task budget allocation, separate miner and validator work streams, and backend verification." action={options.length > 0 ? <select className="audit-select" aria-label="Select audit" value={selected} onChange={event => setSelected(event.target.value)}>{options.map(row => <option key={`${row.project.project_id}/${row.routing!.routing_id}`} value={`${row.project.project_id}/${row.routing!.routing_id}`}>{row.project.project_name} · {row.routing!.routing_id}</option>)}</select> : undefined} />
    {!options.length ? <Card><EmptyState title="No routed audits" detail="Reward records require a project routing." /></Card> : detail.error ? <ErrorState error={detail.error} onRetry={() => detail.refetch()} /> : !projectId || !routingId || detail.isLoading || !detail.data ? <LoadingState label="Loading TaskRewardBudget and reward cycles" /> : <>
      <Card><SectionHeading eyebrow="Selected audit" title={detail.data!.project.project_name} detail={`Routing ${detail.data!.routing.routing_id}`} /></Card>
      <RewardsPanel bundle={detail.data!} standalone />
    </>}
  </>
}
