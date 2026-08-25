import { lazy, Suspense } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { LoadingState } from './components/ui'

const OverviewPage = lazy(() => import('./pages/OverviewPage').then(module => ({ default: module.OverviewPage })))
const AuditsPage = lazy(() => import('./pages/AuditsPage').then(module => ({ default: module.AuditsPage })))
const AuditDetailPage = lazy(() => import('./pages/AuditDetailPage').then(module => ({ default: module.AuditDetailPage })))
const FindingsPage = lazy(() => import('./pages/FindingsPage').then(module => ({ default: module.FindingsPage })))
const NodesPage = lazy(() => import('./pages/NodesPage').then(module => ({ default: module.NodesPage })))
const NodeDetailPage = lazy(() => import('./pages/NodeDetailPage').then(module => ({ default: module.NodeDetailPage })))
const RewardsPage = lazy(() => import('./pages/RewardsPage').then(module => ({ default: module.RewardsPage })))
const PresentationPage = lazy(() => import('./pages/PresentationPage').then(module => ({ default: module.PresentationPage })))

export function App() {
  return <Suspense fallback={<LoadingState label="Loading dashboard view" />}><Routes>
    <Route element={<Layout />}>
      <Route index element={<OverviewPage />} />
      <Route path="audits" element={<AuditsPage />} />
      <Route path="audits/:projectId/:routingId" element={<AuditDetailPage />} />
      <Route path="findings" element={<FindingsPage />} />
      <Route path="nodes" element={<NodesPage />} />
      <Route path="nodes/:nodeId" element={<NodeDetailPage />} />
      <Route path="rewards" element={<RewardsPage />} />
    </Route>
    <Route path="audits/:projectId/:routingId/present" element={<PresentationPage />} />
    <Route path="*" element={<Navigate to="/" replace />} />
  </Routes></Suspense>
}
