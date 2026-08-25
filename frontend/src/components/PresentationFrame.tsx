import { useEffect, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { Icon } from './Icon'

export function PresentationFrame({ labels, stage, onStage, exitPath, projectName, children }: { labels: string[]; stage: number; onStage: (stage: number) => void; exitPath: string; projectName: string; children: ReactNode }) {
  const navigate = useNavigate()
  useEffect(() => {
    const key = (event: KeyboardEvent) => {
      if (event.key === 'ArrowRight' || event.key === 'PageDown') onStage(Math.min(labels.length - 1, stage + 1))
      if (event.key === 'ArrowLeft' || event.key === 'PageUp') onStage(Math.max(0, stage - 1))
      if (event.key === 'Escape') navigate(exitPath)
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [exitPath, labels.length, navigate, onStage, stage])

  return <div className="presentation-shell">
    <header className="presentation-header">
      <div className="presentation-brand"><span className="brand__mark"><Icon name="shield" size={20}/></span><span><strong>ProofGuard</strong><small>{projectName}</small></span></div>
      <div className="presentation-progress" role="progressbar" aria-label="Presentation progress" aria-valuemin={1} aria-valuemax={labels.length} aria-valuenow={stage + 1}><span style={{ width: `${((stage + 1) / labels.length) * 100}%` }} /></div>
      <span className="presentation-count">{String(stage + 1).padStart(2, '0')} / {String(labels.length).padStart(2, '0')}</span>
      <Link className="button button--ghost" to={exitPath}><Icon name="close" size={14}/>Exit</Link>
    </header>
    <main className="presentation-stage"><div className="presentation-stage__label"><span>Stage {String(stage + 1).padStart(2,'0')}</span><strong>{labels[stage]}</strong></div>{children}</main>
    <footer className="presentation-controls">
      <button className="button button--secondary" disabled={stage === 0} onClick={() => onStage(stage - 1)}><Icon name="arrow-left" size={14}/>Previous</button>
      <div className="presentation-dots" aria-label="Presentation stages">{labels.map((label,index) => <button key={label} aria-label={`Go to ${label}`} aria-current={index === stage ? 'step' : undefined} className={`${index === stage ? 'presentation-dot presentation-dot--active' : 'presentation-dot'} ${index < stage ? 'presentation-dot--complete' : ''}`} onClick={() => onStage(index)}><span>{index + 1}</span></button>)}</div>
      <button className="button button--primary" disabled={stage === labels.length - 1} onClick={() => onStage(stage + 1)}>Next<Icon name="arrow-right" size={14}/></button>
    </footer>
  </div>
}
