import type {
  AgentExecution,
  AgentMembership,
  AuditBundle,
  AuditRunList,
  CategoryPerformance,
  CategoryScore,
  ClusterList,
  FindingRewardCalculation,
  MinerRewardCycle,
  MinerRewardEvent,
  NodeRecord,
  OperatorRewardCalculation,
  Project,
  ProjectScope,
  ReputationEvent,
  RoutingList,
  RoutingRecord,
  RoutingUsageEvent,
  TaskRewardBudget,
  ValidationAttestation,
  ValidationConsensus,
  ValidationDispute,
  ValidationQualityAssessment,
  ValidatorAssignment,
  ValidatorCategoryPerformance,
  ValidatorCategoryScore,
  ValidatorCommittee,
  ValidatorMembership,
  ValidatorReproduction,
  ValidatorRewardCycle,
  Verification,
} from '../types/protocol'

export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '')

export class ApiError extends Error {
  constructor(public status: number, message: string, public path: string) {
    super(message)
  }
}

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    signal,
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    let detail = `API request failed (${response.status})`
    try {
      const body = await response.json() as { detail?: string }
      if (body.detail) detail = body.detail
    } catch { /* response is not JSON */ }
    throw new ApiError(response.status, detail, path)
  }
  return response.json() as Promise<T>
}

const enc = encodeURIComponent

export const queryKeys = {
  projects: ['projects'] as const,
  overview: ['overview'] as const,
  auditIndex: ['audit-index'] as const,
  audit: (projectId: string, routingId: string) => ['audit', projectId, routingId] as const,
  nodes: ['nodes'] as const,
  node: (nodeId: string) => ['node', nodeId] as const,
  rewards: (projectId: string, routingId: string) => ['rewards', projectId, routingId] as const,
}

type ListEnvelope<T> = Record<string, unknown> & { total?: number; readonly __itemType?: T }
function listFrom<T>(value: ListEnvelope<T>, key: string): T[] {
  const items = value[key]
  return Array.isArray(items) ? items as T[] : []
}

async function safe<T, F>(label: string, promise: Promise<T>, fallback: F, unavailable: string[]): Promise<T | F> {
  try { return await promise }
  catch { unavailable.push(label); return fallback }
}

export const api = {
  projects: (signal?: AbortSignal) => request<Project[]>('/projects', signal),
  project: (projectId: string, signal?: AbortSignal) => request<Project>(`/projects/${enc(projectId)}`, signal),
  scope: (projectId: string, signal?: AbortSignal) => request<ProjectScope>(`/projects/${enc(projectId)}/scope`, signal),
  nodes: (signal?: AbortSignal) => request<NodeRecord[]>('/nodes', signal),
  node: (nodeId: string, signal?: AbortSignal) => request<NodeRecord>(`/nodes/${enc(nodeId)}`, signal),
  routings: (projectId: string, signal?: AbortSignal) => request<RoutingList>(`/projects/${enc(projectId)}/routing`, signal),
  routing: (projectId: string, routingId: string, signal?: AbortSignal) => request<RoutingRecord>(`/projects/${enc(projectId)}/routing/${enc(routingId)}`, signal),
  auditRuns: (projectId: string, signal?: AbortSignal) => request<AuditRunList>(`/projects/${enc(projectId)}/audit-runs`, signal),
  categoryScores: (nodeId: string, signal?: AbortSignal) => request<CategoryScore[]>(`/nodes/${enc(nodeId)}/category-scores`, signal),
  categoryPerformance: (nodeId: string, signal?: AbortSignal) => request<CategoryPerformance[]>(`/nodes/${enc(nodeId)}/category-performance`, signal),
  validatorScore: (nodeId: string, category: string, signal?: AbortSignal) => request<ValidatorCategoryScore>(`/validators/${enc(nodeId)}/categories/${enc(category)}/score`, signal),
  validatorPerformance: (nodeId: string, category: string, signal?: AbortSignal) => request<ValidatorCategoryPerformance>(`/validators/${enc(nodeId)}/categories/${enc(category)}/performance`, signal),
  validatorMembership: (nodeId: string, category: string, signal?: AbortSignal) => request<ValidatorMembership>(`/validators/${enc(nodeId)}/categories/${enc(category)}/membership`, signal),
}

export interface AuditIndexItem {
  project: Project
  routing: RoutingRecord | null
  run: AuditRunList['audit_runs'][number] | null
  submissionCount: number | null
  clusterCount: number | null
  validationStatus: string | null
  rewardStatus: string | null
}

export async function loadAuditIndex(signal?: AbortSignal): Promise<AuditIndexItem[]> {
  const projects = await api.projects(signal)
  const rows = await Promise.all(projects.map(async project => {
    const [routingResult, runResult] = await Promise.allSettled([
      api.routings(project.project_id, signal),
      api.auditRuns(project.project_id, signal),
    ])
    const routings = routingResult.status === 'fulfilled' ? routingResult.value.records : []
    const runs = runResult.status === 'fulfilled' ? runResult.value.audit_runs : []
    if (!routings.length) return [{ project, routing: null, run: runs[0] ?? null, submissionCount: null, clusterCount: null, validationStatus: null, rewardStatus: null }]
    const submissionResult = await Promise.allSettled([request<AuditBundle['submissions']>(`/projects/${enc(project.project_id)}/submissions`, signal)])
    const submissionCount = submissionResult[0].status === 'fulfilled' ? submissionResult[0].value.length : null
    return Promise.all(routings.map(async routing => {
      const base = `/projects/${enc(project.project_id)}/routing/${enc(routing.routing_id)}`
      const [clusters, miner, validators] = await Promise.allSettled([
        request<ClusterList>(`${base}/finding-clusters`, signal),
        request<MinerRewardCycle>(`${base}/task-reward-cycle`, signal),
        request<ListEnvelope<ValidatorRewardCycle>>(`${base}/validator-reward-cycles`, signal),
      ])
      const clusterList = clusters.status === 'fulfilled' ? clusters.value.clusters : null
      const outcomes = clusterList?.map(cluster => cluster.validator_consensus_outcome).filter(Boolean) as string[] | undefined
      const validatorList = validators.status === 'fulfilled' ? listFrom<ValidatorRewardCycle>(validators.value, 'cycles') : []
      return {
        project,
        routing,
        run: runs.find(run => run.routing_id === routing.routing_id) ?? null,
        submissionCount,
        clusterCount: clusterList?.length ?? null,
        validationStatus: outcomes?.includes('disputed') ? 'disputed' : outcomes?.length ? 'resolved' : null,
        rewardStatus: miner.status === 'fulfilled' && validatorList.length
          ? `${miner.value.status} / ${validatorList.at(-1)?.status}`
          : miner.status === 'fulfilled' ? miner.value.status : validatorList.at(-1)?.status ?? null,
      }
    }))
  }))
  return (await Promise.all(rows)).flat(2)
}

export interface OverviewData {
  projects: Project[]
  nodes: NodeRecord[]
  auditRows: AuditIndexItem[]
  clusterCount: number | null
  confirmed: number | null
  rejected: number | null
  disputed: number | null
  escalations: number | null
  minerRewardEvents: number | null
  validatorRewardEvents: number | null
}

export async function loadOverview(signal?: AbortSignal): Promise<OverviewData> {
  const [projects, nodes, auditRows] = await Promise.all([api.projects(signal), api.nodes(signal), loadAuditIndex(signal)])
  const routed = auditRows.filter((row): row is AuditIndexItem & { routing: RoutingRecord } => Boolean(row.routing))
  const detailRows = await Promise.all(routed.map(async row => {
    const base = `/projects/${enc(row.project.project_id)}/routing/${enc(row.routing.routing_id)}`
    const clusterResult = await Promise.allSettled([request<ClusterList>(`${base}/finding-clusters`, signal)])
    if (clusterResult[0].status !== 'fulfilled') return null
    const clusters = clusterResult[0].value.clusters
    const protocol = await Promise.all(clusters.map(async cluster => {
      const clusterBase = `${base}/finding-clusters/${enc(cluster.finding_cluster_id)}`
      const [disputes, consensus] = await Promise.allSettled([
        request<ListEnvelope<ValidationDispute>>(`${clusterBase}/validation-disputes`, signal),
        request<ListEnvelope<ValidationConsensus>>(`${clusterBase}/validation-consensus`, signal),
      ])
      return {
        disputes: disputes.status === 'fulfilled' ? listFrom<ValidationDispute>(disputes.value, 'disputes') : null,
        consensus: consensus.status === 'fulfilled' ? listFrom<ValidationConsensus>(consensus.value, 'consensus') : null,
      }
    }))
    const [miner, validators] = await Promise.allSettled([
      request<MinerRewardCycle>(`${base}/task-reward-cycle`, signal),
      request<ListEnvelope<ValidatorRewardCycle>>(`${base}/validator-reward-cycles`, signal),
    ])
    return { clusters, protocol, miner: miner.status === 'fulfilled' ? miner.value : null, validators: validators.status === 'fulfilled' ? listFrom<ValidatorRewardCycle>(validators.value, 'cycles') : null }
  }))
  const available = detailRows.filter(row => row !== null)
  const clusters = available.flatMap(row => row.clusters)
  const outcomes = clusters.map(cluster => cluster.validator_consensus_outcome ?? cluster.final_validation_status)
  const protocolAvailable = available.every(row => row.protocol.every(item => item.disputes !== null && item.consensus !== null))
  return {
    projects, nodes, auditRows,
    clusterCount: available.length === routed.length ? clusters.length : null,
    confirmed: available.length === routed.length ? outcomes.filter(x => x === 'confirmed' || x === 'accepted').length : null,
    rejected: available.length === routed.length ? outcomes.filter(x => x === 'rejected').length : null,
    disputed: available.length === routed.length ? outcomes.filter(x => x === 'disputed').length : null,
    escalations: protocolAvailable ? available.flatMap(row => row.protocol).flatMap(item => item.disputes ?? []).filter(dispute => dispute.escalation_round_id).length : null,
    minerRewardEvents: available.every(row => row.miner !== null) ? available.reduce((sum, row) => sum + (row.miner?.reward_event_count ?? 0), 0) : null,
    validatorRewardEvents: available.every(row => row.validators !== null) ? available.reduce((sum, row) => sum + (row.validators?.reduce((inner, cycle) => inner + cycle.reward_event_count, 0) ?? 0), 0) : null,
  }
}

export async function loadAuditBundle(projectId: string, routingId: string, signal?: AbortSignal): Promise<AuditBundle> {
  const unavailable: string[] = []
  const [project, routing] = await Promise.all([
    api.project(projectId, signal),
    api.routing(projectId, routingId, signal),
  ])
  const base = `/projects/${enc(projectId)}/routing/${enc(routingId)}`
  const [scope, runs, submissions, clusterEnvelope, budget, findingRewards, operatorRewards, minerCycle, validatorCycles, nodes] = await Promise.all([
    safe('Project scope', api.scope(projectId, signal), null, unavailable),
    safe('Audit runs', api.auditRuns(projectId, signal), { project_id: projectId, total: 0, audit_runs: [] }, unavailable),
    safe('Finding submissions', request<AuditBundle['submissions']>(`/projects/${enc(projectId)}/submissions`, signal), [], unavailable),
    safe('FindingClusters', request<ClusterList>(`${base}/finding-clusters`, signal), { project_id: projectId, routing_id: routingId, total: 0, clusters: [] }, unavailable),
    safe('TaskRewardBudget', request<TaskRewardBudget>(`${base}/task-reward-budget`, signal), null, unavailable),
    safe('Finding rewards', request<FindingRewardCalculation>(`${base}/task-rewards/findings/latest`, signal), null, unavailable),
    safe('Operator rewards', request<OperatorRewardCalculation>(`${base}/task-rewards/operators/latest`, signal), null, unavailable),
    safe('Miner reward cycle', request<MinerRewardCycle>(`${base}/task-reward-cycle`, signal), null, unavailable),
    safe('Validator reward cycles', request<ListEnvelope<ValidatorRewardCycle>>(`${base}/validator-reward-cycles`, signal), { total: 0, cycles: [] }, unavailable),
    safe('Node registry', api.nodes(signal), [], unavailable),
  ])

  const auditRun = runs.audit_runs.find(run => run.routing_id === routingId) ?? null
  const agentExecutions = auditRun
    ? await safe('Agent executions', request<ListEnvelope<AgentExecution>>(`/projects/${enc(projectId)}/audit-runs/${enc(auditRun.audit_run_id)}/agent-executions`, signal).then(x => listFrom<AgentExecution>(x, 'agent_executions')), [], unavailable)
    : []

  const clusters = await Promise.all(clusterEnvelope.clusters.map(async cluster => {
    const clusterBase = `${base}/finding-clusters/${enc(cluster.finding_cluster_id)}`
    const [committeeEnvelope, assignmentEnvelope, attestationEnvelope, consensusEnvelope, disputeEnvelope, qualityEnvelope] = await Promise.all([
      safe(`Committees · ${cluster.finding_cluster_id}`, request<ListEnvelope<ValidatorCommittee>>(`${clusterBase}/validator-committees`, signal), { total: 0, committees: [] }, unavailable),
      safe(`Assignments · ${cluster.finding_cluster_id}`, request<ListEnvelope<ValidatorAssignment>>(`${clusterBase}/validator-assignments`, signal), { total: 0, assignments: [] }, unavailable),
      safe(`Attestations · ${cluster.finding_cluster_id}`, request<ListEnvelope<ValidationAttestation>>(`${clusterBase}/attestations`, signal), { total: 0, attestations: [] }, unavailable),
      safe(`Consensus · ${cluster.finding_cluster_id}`, request<ListEnvelope<ValidationConsensus>>(`${clusterBase}/validation-consensus`, signal), { total: 0, consensus: [] }, unavailable),
      safe(`Disputes · ${cluster.finding_cluster_id}`, request<ListEnvelope<ValidationDispute>>(`${clusterBase}/validation-disputes`, signal), { total: 0, disputes: [] }, unavailable),
      safe(`Validator quality · ${cluster.finding_cluster_id}`, request<ListEnvelope<ValidationQualityAssessment>>(`${clusterBase}/validation-quality-assessments`, signal), { total: 0, assessments: [] }, unavailable),
    ])
    const committees = listFrom<ValidatorCommittee>(committeeEnvelope, 'committees')
    const reproductionGroups = await Promise.all(committees.map(committee =>
      safe(`Reproductions · ${committee.validator_committee_id}`, request<ListEnvelope<ValidatorReproduction>>(`${base}/validator-committees/${enc(committee.validator_committee_id)}/reproductions`, signal).then(x => listFrom<ValidatorReproduction>(x, 'reproductions')), [], unavailable)
    ))
    return {
      cluster,
      committees,
      assignments: listFrom<ValidatorAssignment>(assignmentEnvelope, 'assignments'),
      reproductions: reproductionGroups.flat(),
      attestations: listFrom<ValidationAttestation>(attestationEnvelope, 'attestations'),
      consensuses: listFrom<ValidationConsensus>(consensusEnvelope, 'consensus'),
      disputes: listFrom<ValidationDispute>(disputeEnvelope, 'disputes'),
      quality: listFrom<ValidationQualityAssessment>(qualityEnvelope, 'assessments'),
    }
  }))

  const validatorCycleList = listFrom<ValidatorRewardCycle>(validatorCycles, 'cycles')
  const validatorCycle = validatorCycleList[validatorCycleList.length - 1] ?? null
  const [minerEvents, minerVerification, validatorVerification] = await Promise.all([
    minerCycle ? safe('Miner reward events', request<ListEnvelope<MinerRewardEvent>>(`/projects/${enc(projectId)}/task-reward-cycles/${enc(minerCycle.reward_cycle_id)}/events`, signal).then(x => listFrom<MinerRewardEvent>(x, 'events')), [], unavailable) : [],
    minerCycle ? safe('Miner accounting verification', request<Verification>(`/projects/${enc(projectId)}/task-reward-cycles/${enc(minerCycle.reward_cycle_id)}/verify`, signal), null, unavailable) : null,
    validatorCycle ? safe('Validator accounting verification', request<Verification>(`${base}/validator-reward-cycles/${enc(validatorCycle.reward_cycle_id)}/verify`, signal), null, unavailable) : null,
  ])

  return { project, scope, routing, auditRun, agentExecutions, submissions, clusters, budget, findingRewards, operatorRewards, minerCycle, minerEvents, minerVerification, validatorCycle, validatorVerification, nodes, unavailable }
}

export interface NodeDetailData {
  node: NodeRecord
  agentScores: CategoryScore[]
  agentPerformance: CategoryPerformance[]
  agentMemberships: AgentMembership[]
  validatorScores: ValidatorCategoryScore[]
  validatorPerformance: ValidatorCategoryPerformance[]
  validatorMemberships: ValidatorMembership[]
  reputationHistory: ReputationEvent[]
  routingUsage: RoutingUsageEvent[]
  unavailable: string[]
}

export async function loadNodeDetail(nodeId: string, signal?: AbortSignal): Promise<NodeDetailData> {
  const unavailable: string[] = []
  const node = await api.node(nodeId, signal)
  const isAgent = node.node_type === 'agent' || node.node_type === 'hybrid'
  const isValidator = node.node_type === 'validator' || node.node_type === 'hybrid'
  const [agentScores, agentPerformance, reputationHistory, routingUsage] = isAgent ? await Promise.all([
    safe('Agent CategoryScores', api.categoryScores(nodeId, signal), [], unavailable),
    safe('Agent category performance', api.categoryPerformance(nodeId, signal), [], unavailable),
    safe('Reputation event history', request<ReputationEvent[]>(`/nodes/${enc(nodeId)}/reputation/history`, signal), [], unavailable),
    safe('Routing usage history', request<RoutingUsageEvent[]>(`/nodes/${enc(nodeId)}/routing-usage`, signal), [], unavailable),
  ]) : [[], [], [], []]
  const agentMemberships = isAgent ? await Promise.all(node.supported_categories.map(async category => {
    const subnet = await safe(`Agent subnet · ${category}`, request<{ subnet_id: string }>(`/subnets/by-category/${enc(category)}`, signal), null, unavailable)
    if (!subnet) return null
    return safe(`Agent membership · ${category}`, request<AgentMembership>(`/subnets/${enc(subnet.subnet_id)}/members/${enc(nodeId)}`, signal), null, unavailable)
  })) : []
  const validatorRows = isValidator ? await Promise.all(node.supported_categories.map(async category => {
    const [score, performance, membership] = await Promise.all([
      safe(`ValidatorCategoryScore · ${category}`, api.validatorScore(nodeId, category, signal), null, unavailable),
      safe(`Validator performance · ${category}`, api.validatorPerformance(nodeId, category, signal), null, unavailable),
      safe(`Validator membership · ${category}`, api.validatorMembership(nodeId, category, signal), null, unavailable),
    ])
    return { score, performance, membership }
  })) : []
  return {
    node,
    agentScores,
    agentPerformance,
    agentMemberships: agentMemberships.flatMap(item => item ? [item] : []),
    validatorScores: validatorRows.flatMap(row => row.score ? [row.score] : []),
    validatorPerformance: validatorRows.flatMap(row => row.performance ? [row.performance] : []),
    validatorMemberships: validatorRows.flatMap(row => row.membership ? [row.membership] : []),
    reputationHistory,
    routingUsage,
    unavailable,
  }
}
