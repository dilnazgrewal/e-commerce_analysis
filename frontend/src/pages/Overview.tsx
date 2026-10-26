import { useEffect, useState } from 'react'
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, inr, num, OFFER_LABELS } from '../api'
import { axis, ErrorBox, Loading, PageHead, Stat, Tip } from '../ui'

type Overview = {
  decisions: number
  by_status: Record<string, number>
  by_tier: Record<string, number>
  by_channel: Record<string, number>
  by_offer_type: { type: string; n: number; cost_inr: number }[]
  offers_sent: number
  rule_violations_in_sent: number
  expected_cost_inr: number
  expected_profit_inr: number
  daily: Record<string, number | string>[]
  bot: { users: number; holdout: number; opted_out: number; feedback: Record<string, number> }
  policy: { version: number; name: string }
  ai_enabled: boolean
}

// Fixed order, so a status keeps its colour whatever else is on the chart.
const STATUS = [
  { key: 'sent', label: 'Sent', color: 'var(--series-1)' },
  { key: 'claimed', label: 'Claimed', color: 'var(--series-3)' },
  { key: 'suppressed', label: 'Suppressed (rules said no)', color: 'var(--series-2)' },
  { key: 'holdout', label: 'Holdout (control group)', color: 'var(--muted)' },
]

const TIERS: Record<string, string> = {
  T1: 'Claude agent',
  T2: 'Claude retry with rule feedback',
  T3: 'Haiku over rules’ top 3',
  T4: 'Rules-only best offer',
  T5: 'Marketer’s segment fallback',
  T6: 'No offer',
}

export default function OverviewPage() {
  const [data, setData] = useState<Overview | null>(null)
  const [error, setError] = useState<unknown>(null)
  useEffect(() => {
    const load = () => api<Overview>('/api/overview').then(setData).catch(setError)
    load()
    const timer = setInterval(load, 15000) // live: classmates' taps show up within seconds
    return () => clearInterval(timer)
  }, [])

  if (error) return <ErrorBox error={error} />
  if (!data) return <Loading />
  const fb = data.bot.feedback
  const daily = data.daily.map((d) => ({ ...d, day: String(d.day).slice(5) }))
  const sentTiers = Object.entries(data.by_tier).sort()
  const tierTotal = sentTiers.reduce((s, [, n]) => s + n, 0)

  return (
    <>
      <PageHead title="Overview">
        Every decision OfferIQ has made, live from the bot and the platform. Policy v{data.policy.version} is
        live; Claude is {data.ai_enabled ? 'on' : 'switched off (rules-only mode)'}.
      </PageHead>
      <div className="stack">
        <div className="grid cols-4">
          <Stat label="Decisions logged" value={num(data.decisions)} note={`${num(data.offers_sent)} offers sent`} />
          <Stat
            label="Rule violations in sent offers"
            value={
              <span style={{ color: data.rule_violations_in_sent ? 'var(--bad)' : 'var(--good)' }}>
                {data.rule_violations_in_sent === 0 ? '✓ 0' : `✗ ${data.rule_violations_in_sent}`}
              </span>
            }
            note="Every sent offer passed every rule"
          />
          <Stat
            label="Expected offer cost"
            value={inr(data.expected_cost_inr)}
            note={`expected extra profit ${inr(data.expected_profit_inr)}`}
          />
          <Stat
            label="People on the bot"
            value={num(data.bot.users)}
            note={`${num(data.bot.holdout)} in holdout · ${num(fb.claimed ?? 0)} claims · ${num(fb.dismissed ?? 0)} “not for me”`}
          />
        </div>

        <div className="grid cols-2">
          <section className="card">
            <h2>Decisions per day</h2>
            <p className="sub">Suppressed means the rules stopped an offer (quiet hours, frequency cap, no profit…).</p>
            <div className="legend">
              {STATUS.map((s) => (
                <span key={s.key}>
                  <i style={{ background: s.color }} />
                  {s.label}
                </span>
              ))}
            </div>
            <div className="chart">
              <ResponsiveContainer>
                <BarChart data={daily} margin={{ top: 4, right: 4, bottom: 0, left: -18 }}>
                  <CartesianGrid vertical={false} stroke="var(--grid)" />
                  <XAxis dataKey="day" {...axis} />
                  <YAxis allowDecimals={false} {...axis} />
                  <Tooltip
                    cursor={{ fill: 'var(--surface-2)' }}
                    content={({ active, payload, label }) =>
                      active && payload?.length ? (
                        <Tip
                          title={label}
                          rows={payload.map((p) => [
                            STATUS.find((s) => s.key === p.dataKey)?.label ?? String(p.dataKey),
                            num(Number(p.value)),
                          ])}
                        />
                      ) : null
                    }
                  />
                  {STATUS.map((s, i) => (
                    <Bar
                      key={s.key}
                      dataKey={s.key}
                      stackId="a"
                      isAnimationActive={false}
                      fill={s.color}
                      stroke="var(--surface)"
                      strokeWidth={2}
                      radius={i === STATUS.length - 1 ? [4, 4, 0, 0] : 0}
                      maxBarSize={36}
                    />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          </section>

          <section className="card">
            <h2>Who decided the sent offers</h2>
            <p className="sub">The fallback chain: when Claude can't produce a valid offer, the next tier takes over.</p>
            {sentTiers.length === 0 && <p className="muted">No offers sent yet.</p>}
            <div className="stack" style={{ gap: 10 }}>
              {sentTiers.map(([tier, n]) => (
                <div key={tier}>
                  <div className="row small">
                    <b>{tier}</b> {TIERS[tier] ?? ''}
                    <span className="spacer" />
                    {num(n)} ({Math.round((n / tierTotal) * 100)}%)
                  </div>
                  <div className="bar-track">
                    <div className="bar-fill" style={{ width: `${(n / tierTotal) * 100}%` }} />
                  </div>
                </div>
              ))}
            </div>
            <h2 style={{ marginTop: 20 }}>Offer types sent</h2>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Offer</th>
                    <th className="n">Sent</th>
                    <th className="n">Expected cost</th>
                  </tr>
                </thead>
                <tbody>
                  {data.by_offer_type.map((o) => (
                    <tr key={o.type}>
                      <td>{OFFER_LABELS[o.type] ?? o.type}</td>
                      <td className="n">{num(o.n)}</td>
                      <td className="n">{inr(o.cost_inr)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      </div>
    </>
  )
}
