import { useState, type ReactNode } from 'react'
import { statusTone, titleCase } from '../utils/format'
import { Icon, type IconName } from './Icon'

export function StatusBadge({ status, label }: { status?: string | null; label?: string }) {
  return <span className={`status status--${statusTone(status)}`}>{label ?? titleCase(status)}</span>
}

export function Card({ children, className = '', id, variant = 'standard' }: { children: ReactNode; className?: string; id?: string; variant?: 'standard' | 'elevated' | 'subtle' | 'highlight' }) {
  return <section className={`card card--${variant} ${className}`} id={id}>{children}</section>
}

export function PageHeader({ eyebrow, title, detail, action, breadcrumb }: { eyebrow?: string; title: string; detail: string; action?: ReactNode; breadcrumb?: ReactNode }) {
  return <header className="page-header">
    <div className="page-header__copy">{breadcrumb && <div className="breadcrumb">{breadcrumb}</div>}{eyebrow && <p className="eyebrow">{eyebrow}</p>}<h1>{title}</h1><p>{detail}</p></div>
    {action && <div className="page-header__actions">{action}</div>}
  </header>
}

export function SectionHeading({ eyebrow, title, detail, action }: { eyebrow?: string; title: string; detail?: string; action?: ReactNode }) {
  return <div className="section-heading">
    <div>
      {eyebrow && <p className="eyebrow">{eyebrow}</p>}
      <h2>{title}</h2>
      {detail && <p className="section-detail">{detail}</p>}
    </div>
    {action && <div className="section-action">{action}</div>}
  </div>
}

export function Metric({ label, value, detail, tone = 'default' }: { label: string; value: ReactNode; detail?: ReactNode; tone?: string }) {
  return <div className={`metric metric--${tone}`}>
    <span className="metric__label">{label}</span>
    <strong className="metric__value">{value ?? <Unavailable />}</strong>
    {detail && <span className="metric__detail">{detail}</span>}
  </div>
}

export function Unavailable({ compact = false }: { compact?: boolean }) {
  return <span className={compact ? 'unavailable unavailable--compact' : 'unavailable'}>Data unavailable</span>
}

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <div className="empty-state"><span className="empty-state__mark"><Icon name="database" size={18} /></span><h3>{title}</h3><p>{detail}</p></div>
}

export function ErrorState({ error, onRetry }: { error: Error; onRetry?: () => void }) {
  return <div className="error-state" role="alert">
    <span className="error-state__mark"><Icon name="alert" size={18} /></span>
    <div><h3>Data unavailable</h3><p>{error.message}</p></div>
    {onRetry && <button className="button button--secondary" onClick={onRetry}>Try again</button>}
  </div>
}

export function LoadingState({ label = 'Loading protocol data' }: { label?: string }) {
  return <div className="loading-state" role="status"><div className="loading-skeleton"><i/><i/><i/></div><span>{label}</span></div>
}

export function InfoTip({ term, children }: { term: string; children: ReactNode }) {
  return <span className="info-tip" tabIndex={0} aria-label={`${term}: ${String(children)}`}>i<span className="info-tip__bubble"><strong>{term}</strong>{children}</span></span>
}

export function Fingerprint({ value }: { value?: string | null }) {
  if (!value) return <Unavailable compact />
  return <code className="fingerprint" title={value}>{value.slice(0, 10)}…{value.slice(-8)}</code>
}

export function CopyableId({ value, visible = 10 }: { value?: string | null; visible?: number }) {
  const [copied, setCopied] = useState(false)
  if (!value) return <Unavailable compact />
  const label = value.length > visible * 2 ? `${value.slice(0, visible)}…${value.slice(-visible)}` : value
  async function copy() {
    try { await navigator.clipboard.writeText(value!); setCopied(true); window.setTimeout(() => setCopied(false), 1400) } catch { /* clipboard may be unavailable */ }
  }
  return <span className="copyable-id"><code title={value}>{label}</code><button type="button" onClick={copy} aria-label={`Copy identifier ${value}`} title="Copy full identifier"><Icon name={copied ? 'check' : 'copy'} size={13} /></button></span>
}

export function EntityIcon({ name, tone = 'info', size = 'medium' }: { name: IconName; tone?: string; size?: 'small' | 'medium' | 'large' }) {
  return <span className={`entity-icon entity-icon--${tone} entity-icon--${size}`}><Icon name={name} /></span>
}

export function SeverityBadge({ severity }: { severity?: string | null }) {
  if (!severity) return <Unavailable compact />
  return <span className={`severity severity--${severity.toLowerCase()}`}><i />{titleCase(severity)}</span>
}

export function ProgressBar({ value, label }: { value: number; label: string }) {
  const safe = Math.max(0, Math.min(1, Number.isFinite(value) ? value : 0))
  return <div className="progress" aria-label={`${label}: ${Math.round(safe * 100)}%`}>
    <span className="progress__fill" style={{ width: `${safe * 100}%` }} />
  </div>
}

export function DataNotice({ labels }: { labels: string[] }) {
  if (!labels.length) return null
  return <details className="data-notice"><summary>{labels.length} optional data source{labels.length === 1 ? '' : 's'} unavailable</summary><p>{labels.join(' · ')}</p></details>
}
