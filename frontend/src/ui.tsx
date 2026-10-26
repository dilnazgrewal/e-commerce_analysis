import type { ReactNode } from 'react'

export function PageHead({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <header className="page-head">
      <h1>{title}</h1>
      {children && <p>{children}</p>}
    </header>
  )
}

export function Stat({ label, value, note }: { label: string; value: ReactNode; note?: ReactNode }) {
  return (
    <div className="card stat">
      <div className="label">{label}</div>
      <div className="value">{value}</div>
      {note && <div className="note">{note}</div>}
    </div>
  )
}

export function Loading({ what = 'Loading' }: { what?: string }) {
  return (
    <p className="muted">
      <span className="spin" /> {what}…
    </p>
  )
}

export function ErrorBox({ error }: { error: unknown }) {
  if (!error) return null
  return <div className="msg bad">{error instanceof Error ? error.message : String(error)}</div>
}

export function Tip({ title, rows }: { title: ReactNode; rows: [string, ReactNode][] }) {
  return (
    <div className="tip">
      <b>{title}</b>
      {rows.map(([k, v]) => (
        <div key={k}>
          <span className="muted">{k}: </span>
          {v}
        </div>
      ))}
    </div>
  )
}

export const axis = { stroke: 'var(--muted)', fontSize: 12, tickLine: false, axisLine: false } as const
