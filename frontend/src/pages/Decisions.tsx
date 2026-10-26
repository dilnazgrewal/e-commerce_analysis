import { useEffect, useState } from 'react'
import { api, inr, num, OFFER_LABELS } from '../api'
import { ErrorBox, Loading, PageHead } from '../ui'

type Item = {
  decision_id: string
  customer_id: string
  created_at: string
  trigger: string
  tier: string
  status: string
  offer_type: string | null
  offer_value: number | null
  message_text: string | null
  expected_cost_inr: number | null
  expected_profit_inr: number | null
  policy_version: number
  channel: string | null
  checks_total: number
  checks_failed: number
}
type Detail = Item & {
  rule_checks: { rule: string; passed: boolean; reason: string }[]
  explanation: Record<string, unknown> | null
  ai: { used?: boolean; calls?: { tier: string; model: string; latency_ms: number; ok: boolean; error?: string }[]; skipped?: string; reason?: string } | null
  code: string | null
  valid_until: string | null
  feedback: { action: string; detail: string | null; created_at: string }[]
}

const STATUS_PILL: Record<string, string> = { sent: 'accent', claimed: 'good', suppressed: 'warn', holdout: '', dismissed: '' }

export default function DecisionsPage() {
  const [status, setStatus] = useState('')
  const [tier, setTier] = useState('')
  const [channel, setChannel] = useState('')
  const [page, setPage] = useState(0)
  const [data, setData] = useState<{ total: number; items: Item[] } | null>(null)
  const [error, setError] = useState<unknown>(null)
  const [open, setOpen] = useState<Detail | null>(null)
  const size = 50

  useEffect(() => {
    const q = new URLSearchParams({ limit: String(size), offset: String(page * size) })
    if (status) q.set('status', status)
    if (tier) q.set('tier', tier)
    if (channel) q.set('channel', channel)
    const load = () => api<{ total: number; items: Item[] }>(`/api/decisions?${q}`).then(setData).catch(setError)
    load()
    const timer = setInterval(load, 15000)
    return () => clearInterval(timer)
  }, [status, tier, channel, page])

  const show = (id: string) => api<Detail>(`/api/decisions/${id}`).then(setOpen).catch(setError)
  const filter = (value: string, set: (v: string) => void, options: string[], label: string) => (
    <select
      className="input"
      value={value}
      onChange={(e) => {
        set(e.target.value)
        setPage(0)
      }}
    >
      <option value="">{label}: all</option>
      {options.map((o) => (
        <option key={o}>{o}</option>
      ))}
    </select>
  )

  return (
    <>
      <PageHead title="Decision log">
        Every decision, including the ones where OfferIQ chose to send nothing, with each rule it was checked
        against. Click a row for the full audit trail.
      </PageHead>
      <div className="row" style={{ marginBottom: 12 }}>
        {filter(status, setStatus, ['sent', 'claimed', 'dismissed', 'suppressed', 'holdout', 'queued'], 'Status')}
        {filter(tier, setTier, ['T1', 'T2', 'T3', 'T4', 'T5', 'T6'], 'Tier')}
        {filter(channel, setChannel, ['telegram', 'simulator', 'cli'], 'Channel')}
        <span className="spacer" />
        {data && <span className="muted small">{num(data.total)} decisions</span>}
      </div>
      <ErrorBox error={error} />
      {!data && <Loading />}
      {data && (
        <section className="card" style={{ padding: 0 }}>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>When (IST)</th>
                  <th>Customer</th>
                  <th>Trigger</th>
                  <th>Tier</th>
                  <th>Status</th>
                  <th>Offer</th>
                  <th className="n">Exp. cost</th>
                  <th>Rules</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((d) => (
                  <tr key={d.decision_id} className={`click ${open?.decision_id === d.decision_id ? 'sel' : ''}`} onClick={() => show(d.decision_id)}>
                    <td className="small">{d.created_at.slice(0, 16)}</td>
                    <td>{d.customer_id}</td>
                    <td className="small">{d.trigger.replaceAll('_', ' ')}</td>
                    <td>{d.tier}</td>
                    <td>
                      <span className={`pill ${STATUS_PILL[d.status] ?? ''}`}>{d.status}</span>
                    </td>
                    <td className="small">{d.offer_type ? `${OFFER_LABELS[d.offer_type] ?? d.offer_type} ${d.offer_value ? num(d.offer_value) : ''}` : '—'}</td>
                    <td className="n">{inr(d.expected_cost_inr)}</td>
                    <td className="small">
                      {d.status === 'sent' || d.status === 'claimed' || d.status === 'dismissed' ? (
                        <span className="pill good">✓ {d.checks_total} passed</span>
                      ) : (
                        <span className="muted">
                          {d.checks_failed} of {d.checks_total} blocked
                        </span>
                      )}
                    </td>
                  </tr>
                ))}
                {data.items.length === 0 && (
                  <tr>
                    <td colSpan={8} className="muted">
                      No decisions match.
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
          <div className="row" style={{ padding: 12 }}>
            <button className="btn" disabled={page === 0} onClick={() => setPage(page - 1)}>
              ← Newer
            </button>
            <span className="small muted">page {page + 1}</span>
            <button className="btn" disabled={(page + 1) * size >= data.total} onClick={() => setPage(page + 1)}>
              Older →
            </button>
          </div>
        </section>
      )}
      {open && <DetailDrawer d={open} onClose={() => setOpen(null)} />}
    </>
  )
}

function DetailDrawer({ d, onClose }: { d: Detail; onClose: () => void }) {
  const why = d.explanation?.why
  return (
    <div className="drawer" role="dialog" aria-label="Decision details">
      <div className="row">
        <h2 style={{ margin: 0 }}>{d.decision_id}</h2>
        <span className="spacer" />
        <button className="btn" onClick={onClose}>
          Close
        </button>
      </div>
      <p className="row small" style={{ marginTop: 8 }}>
        <span className={`pill ${STATUS_PILL[d.status] ?? ''}`}>{d.status}</span>
        <span className="pill">{d.tier}</span>
        <span className="pill">policy v{d.policy_version}</span>
        <span className="pill">{d.channel ?? 'cli'}</span>
      </p>
      {typeof why === 'string' && <div className="msg info">{why}</div>}
      <dl className="kv" style={{ marginTop: 14 }}>
        <dt>Customer</dt>
        <dd>{d.customer_id}</dd>
        <dt>Trigger</dt>
        <dd>{d.trigger.replaceAll('_', ' ')}</dd>
        <dt>When</dt>
        <dd>{d.created_at} IST</dd>
        {d.offer_type && (
          <>
            <dt>Offer</dt>
            <dd>
              {OFFER_LABELS[d.offer_type] ?? d.offer_type} {d.offer_value ? num(d.offer_value) : ''} · code {d.code} · until{' '}
              {d.valid_until?.slice(0, 16)}
            </dd>
            <dt>Expected</dt>
            <dd>
              cost {inr(d.expected_cost_inr, 1)}, profit {inr(d.expected_profit_inr, 1)}
            </dd>
          </>
        )}
      </dl>
      {d.message_text && (
        <>
          <h3 className="small" style={{ marginTop: 16 }}>
            Message
          </h3>
          <div className="msg" style={{ background: 'var(--surface-2)' }}>
            {d.message_text}
          </div>
        </>
      )}
      <h3 className="small" style={{ marginTop: 16 }}>
        Rule checks
      </h3>
      <ul className="checks">
        {d.rule_checks.map((c) => (
          <li key={c.rule}>
            <span style={{ color: c.passed ? 'var(--good)' : 'var(--bad)' }}>{c.passed ? '✓' : '✗'}</span> <b>{c.rule}</b>{' '}
            <span className="muted">{c.reason}</span>
          </li>
        ))}
      </ul>
      {d.ai && (
        <>
          <h3 className="small" style={{ marginTop: 16 }}>
            AI
          </h3>
          {d.ai.used === false && <p className="small muted">Not used: {d.ai.skipped ?? d.ai.reason ?? 'rules-only'}</p>}
          {d.ai.calls?.map((c, i) => (
            <div key={i} className="small">
              {c.tier} · {c.model} · {num(c.latency_ms)} ms · {c.ok ? <span style={{ color: 'var(--good)' }}>ok</span> : <span style={{ color: 'var(--bad)' }}>{c.error}</span>}
            </div>
          ))}
        </>
      )}
      {d.feedback.length > 0 && (
        <>
          <h3 className="small" style={{ marginTop: 16 }}>
            What the person did
          </h3>
          {d.feedback.map((f, i) => (
            <div key={i} className="small">
              {f.created_at.slice(0, 16)} · <b>{f.action}</b> {f.detail ?? ''}
            </div>
          ))}
        </>
      )}
    </div>
  )
}
