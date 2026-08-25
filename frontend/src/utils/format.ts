export function titleCase(value: string | null | undefined): string {
  if (!value) return 'Unavailable'
  return value.replaceAll('_', ' ').replace(/\b\w/g, letter => letter.toUpperCase())
}

export function shortId(value: string | null | undefined, length = 8): string {
  if (!value) return 'Unavailable'
  if (value.length <= length * 2 + 1) return value
  return `${value.slice(0, length)}…${value.slice(-length)}`
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return 'Unavailable'
  const date = new Date(value)
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat('en', {
    dateStyle: 'medium', timeStyle: 'short',
  }).format(date)
}

export function points(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'Unavailable'
  const number = Number(value)
  if (!Number.isFinite(number)) return String(value)
  return new Intl.NumberFormat('en', { maximumFractionDigits: 6 }).format(number)
}

export function percent(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return 'Unavailable'
  const number = Number(value)
  if (!Number.isFinite(number)) return String(value)
  return `${Math.round(number * 100)}%`
}

export function statusTone(status: string | null | undefined): 'positive' | 'warning' | 'negative' | 'neutral' | 'info' {
  const value = status?.toLowerCase() ?? ''
  if (['confirmed', 'accepted', 'reproduced', 'active', 'expert', 'pass', 'clean_finalized', 'complete', 'completed', 'resolved'].includes(value)) return 'positive'
  if (['disputed', 'timeout', 'insufficient', 'insufficient_evidence', 'probation', 'partial', 'running', 'preparing', 'escalated', 'no_quorum'].includes(value)) return 'warning'
  if (['rejected', 'out_of_scope', 'unsupported', 'unsafe', 'unsafe_poc', 'rejected_unsafe', 'banned', 'fail', 'failed', 'error', 'sandbox_error'].includes(value)) return 'negative'
  if (['calculated', 'production', 'authoritative', 'high_assurance', 'validator_consensus'].includes(value)) return 'info'
  return 'neutral'
}
