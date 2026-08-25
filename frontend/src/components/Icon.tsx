import type { ReactNode, SVGProps } from 'react'

export type IconName =
  | 'overview' | 'audits' | 'findings' | 'nodes' | 'rewards'
  | 'shield' | 'route' | 'agent' | 'cluster' | 'validator'
  | 'consensus' | 'check' | 'alert' | 'clock' | 'refresh'
  | 'presentation' | 'arrow-right' | 'arrow-left' | 'external'
  | 'copy' | 'search' | 'database' | 'chevron-down' | 'close'

const paths: Record<IconName, ReactNode> = {
  overview: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
  audits: <><path d="M7 3h10l4 4v14H3V3h4Z"/><path d="M14 3v5h5M7 12h10M7 16h7"/></>,
  findings: <><path d="M12 3 3.5 7.5v9L12 21l8.5-4.5v-9L12 3Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/></>,
  nodes: <><circle cx="12" cy="5" r="2.5"/><circle cx="5" cy="18" r="2.5"/><circle cx="19" cy="18" r="2.5"/><path d="m10.7 7.2-4.4 8.6M13.3 7.2l4.4 8.6M7.5 18h9"/></>,
  rewards: <><circle cx="12" cy="12" r="9"/><path d="M8 9h5.2a2.3 2.3 0 0 1 0 4.6H8m4-7v11"/></>,
  shield: <><path d="M12 2.5 20 6v5.8c0 4.6-3.2 8-8 9.7-4.8-1.7-8-5.1-8-9.7V6l8-3.5Z"/><path d="m8.5 12 2.2 2.2 4.8-5"/></>,
  route: <><circle cx="5" cy="5" r="2"/><circle cx="19" cy="19" r="2"/><path d="M7 5h4a4 4 0 0 1 4 4v6a4 4 0 0 0 4 4M11 9l4-4 4 4"/></>,
  agent: <><rect x="4" y="6" width="16" height="13" rx="3"/><path d="M9 11h.01M15 11h.01M8 15h8M12 6V3"/></>,
  cluster: <><circle cx="8" cy="8" r="4"/><circle cx="16" cy="8" r="4"/><circle cx="12" cy="16" r="4"/></>,
  validator: <><path d="M12 3 5 6v5c0 4 2.7 7 7 8.5 4.3-1.5 7-4.5 7-8.5V6l-7-3Z"/><path d="m9 11.5 2 2 4-4"/></>,
  consensus: <><path d="M4 6h16M4 12h16M4 18h16"/><circle cx="8" cy="6" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="11" cy="18" r="2"/></>,
  check: <path d="m5 12 4 4L19 6"/>,
  alert: <><path d="M12 3 2.8 20h18.4L12 3Z"/><path d="M12 9v5M12 17h.01"/></>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M18.2 10a7 7 0 0 0-12-3L4 9m2 5a7 7 0 0 0 12 3l2-2"/></>,
  presentation: <><rect x="3" y="4" width="18" height="13" rx="2"/><path d="M8 21l4-4 4 4M12 17v4"/></>,
  'arrow-right': <path d="m9 5 7 7-7 7"/>,
  'arrow-left': <path d="m15 5-7 7 7 7"/>,
  external: <><path d="M14 4h6v6M20 4l-9 9"/><path d="M18 13v7H4V6h7"/></>,
  copy: <><rect x="8" y="8" width="11" height="11" rx="2"/><path d="M16 8V5a2 2 0 0 0-2-2H5a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h3"/></>,
  search: <><circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/></>,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v7c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12v7c0 1.7 3.6 3 8 3s8-1.3 8-3v-7"/></>,
  'chevron-down': <path d="m6 9 6 6 6-6"/>,
  close: <path d="M6 6l12 12M18 6 6 18"/>,
}

export function Icon({ name, size = 18, ...props }: SVGProps<SVGSVGElement> & { name: IconName; size?: number }) {
  return <svg aria-hidden="true" className="icon" fill="none" height={size} viewBox="0 0 24 24" width={size} stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.7" {...props}>{paths[name]}</svg>
}
