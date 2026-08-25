import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { Icon, type IconName } from './Icon'

const nav = [
  ['/', 'Overview', 'overview'],
  ['/audits', 'Audits', 'audits'],
  ['/findings', 'Findings', 'findings'],
  ['/nodes', 'Nodes', 'nodes'],
  ['/rewards', 'Rewards', 'rewards'],
] as const

export function Layout() {
  const location = useLocation()
  return <div className="app-shell">
    <aside className="sidebar">
      <NavLink to="/" className="brand" aria-label="ProofGuard overview">
        <span className="brand__mark"><Icon name="shield" size={21} /></span>
        <span><strong>ProofGuard</strong><small>Intelligence Console</small></span>
      </NavLink>
      <p className="nav-label">Protocol</p>
      <nav className="main-nav" aria-label="Main navigation">
        {nav.map(([to, label, icon]) => <NavLink key={to} to={to} end={to === '/'} className={({ isActive }) => isActive ? 'nav-link nav-link--active' : 'nav-link'}>
          <Icon name={icon as IconName} size={17} />{label}
        </NavLink>)}
      </nav>
      <div className="sidebar__footer">
        <span className="sidebar__footer-icon"><Icon name="database" size={16} /></span>
        <span><strong>Protocol data</strong><small>Backend authoritative</small></span>
      </div>
    </aside>
    <main className="main">
      <header className="topbar">
        <div className="topbar__context"><span className="topbar__path">Console</span><span className="topbar__sep">/</span><strong>{nav.find(([to]) => location.pathname === to)?.[1] ?? 'Audit Protocol'}</strong></div>
        <div className="topbar__meta"><span className="source-dot" /> Backend-authoritative state</div>
      </header>
      <div className="page"><Outlet /></div>
    </main>
  </div>
}
