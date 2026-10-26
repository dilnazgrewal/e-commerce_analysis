import { useEffect, useRef, useState } from 'react'
import { Bar, BarChart, CartesianGrid, Cell, LabelList, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, inr, num, OFFER_LABELS, pct } from '../api'
import { axis, ErrorBox, Loading, PageHead, Tip } from '../ui'

export type StrategyResult = {
  strategy: string
  label: string
  offers_sent: number
  offers_used: number
  orders: number
  revenue_inr: number
  offer_cost_inr: number
  profit_inr: number
  wasted_discount_inr: number
  extra_orders: number
  extra_revenue_inr: number
  extra_profit_inr: number
  roi: number | null
}

type FairGroup = { customers: number; offer_rate: number; avg_expected_cost: number } & Record<string, string | number>
type LabResult = {
  as_of: string
  days: number
  customers: number
  results: StrategyResult[]
  offeriq_offer_mix: Record<string, number>
  fairness: Record<string, { groups: FairGroup[]; max_gap_pp: number }>
  seconds: number
}

const SHORT: Record<string, string> = {
  none: 'No offers',
  blanket20: '20% off all',
  at_risk15: '15% off at-risk',
  offeriq: 'OfferIQ',
}

export default function LabPage() {
  const [discount, setDiscount] = useState(1)
  const [reward, setReward] = useState(1)
  const [days, setDays] = useState(14)
  const [data, setData] = useState<LabResult | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const request = useRef(0)

  useEffect(() => {
    const id = ++request.current
    const timer = setTimeout(() => {
      setBusy(true)
      setError(null)
      api<LabResult>('/api/lab/compare', { discount_strength: discount, reward_strength: reward, days })
        .then((r) => id === request.current && setData(r))
        .catch((e) => id === request.current && setError(e))
        .finally(() => id === request.current && setBusy(false))
    }, 350)
    return () => clearTimeout(timer)
  }, [discount, reward, days])

  const by = Object.fromEntries((data?.results ?? []).map((r) => [r.strategy, r]))
  const chart = (data?.results ?? []).filter((r) => r.strategy !== 'none')
  const best = chart.reduce<StrategyResult | null>((b, r) => (!b || r.extra_profit_inr > b.extra_profit_inr ? r : b), null)

  return (
    <>
      <PageHead title="Policy Lab">
        Try a strategy on the simulated customers before trying it on real ones. Because they are synthetic, we
        know how each one truly behaves, so the Lab measures what each strategy <i>causes</i>: extra orders and
        profit compared with sending nothing, and how much discount goes to people who would have bought anyway.
      </PageHead>
      <div className="stack">
        <section className="card">
          <div className="row" style={{ gap: 28, alignItems: 'flex-end' }}>
            <label className="slider">
              <span>How strongly discounts work</span>
              <b>{discount.toFixed(2)}×</b>
              <input type="range" min={0.25} max={3} step={0.25} value={discount} onChange={(e) => setDiscount(+e.target.value)} />
            </label>
            <label className="slider">
              <span>How strongly points/free delivery work</span>
              <b>{reward.toFixed(2)}×</b>
              <input type="range" min={0.25} max={3} step={0.25} value={reward} onChange={(e) => setReward(+e.target.value)} />
            </label>
            <label className="slider" style={{ maxWidth: 220 }}>
              <span>Days measured</span>
              <b>{days}</b>
              <input type="range" min={7} max={30} step={1} value={days} onChange={(e) => setDays(+e.target.value)} />
            </label>
          </div>
          <p className="small muted" style={{ margin: '12px 0 0' }}>
            1× is the simulator's calibrated assumption (from the real Hillstrom experiment). Move the sliders to test
            whether the conclusion survives if offers work better or worse than we assumed.{' '}
            {busy ? (
              <>
                <span className="spin" /> simulating…
              </>
            ) : (
              data && `${num(data.customers)} customers, ${data.days} days from ${data.as_of}, ${data.seconds}s.`
            )}
          </p>
        </section>

        <ErrorBox error={error} />
        {!data && !error && <Loading what="Simulating every strategy" />}

        {data && best && (
          <>
            <div className="msg info">
              <b>{best.label}</b> earns the most extra profit: {inr(best.extra_profit_inr)} over {data.days} days
              {best.roi !== null && <> (every ₹1 of offer cost returns ₹{(best.roi + 1).toFixed(2)})</>}.{' '}
              {by.blanket20 && (
                <>
                  20% off for everyone {by.blanket20.extra_profit_inr < 0 ? 'loses' : 'makes'}{' '}
                  {inr(Math.abs(by.blanket20.extra_profit_inr))}, and {inr(by.blanket20.wasted_discount_inr)} of its
                  discount goes to people who would have bought anyway.
                </>
              )}
            </div>

            <div className="grid cols-2">
              <section className="card">
                <h2>Extra profit vs sending nothing</h2>
                <p className="sub">Gross margin of the extra orders minus every rupee of offer cost.</p>
                <div className="chart">
                  <ResponsiveContainer>
                    <BarChart data={chart} margin={{ top: 22, right: 8, bottom: 0, left: 8 }}>
                      <CartesianGrid vertical={false} stroke="var(--grid)" />
                      <XAxis dataKey="strategy" tickFormatter={(s) => SHORT[s] ?? s} {...axis} />
                      <YAxis tickFormatter={(v) => inr(v / 1000) + 'k'} {...axis} width={60} />
                      <Tooltip
                        cursor={{ fill: 'var(--surface-2)' }}
                        content={({ active, payload }) => {
                          const r = payload?.[0]?.payload as StrategyResult | undefined
                          return active && r ? (
                            <Tip
                              title={r.label}
                              rows={[
                                ['Extra profit', inr(r.extra_profit_inr)],
                                ['Extra orders', num(r.extra_orders, 1)],
                                ['Offer cost', inr(r.offer_cost_inr)],
                              ]}
                            />
                          ) : null
                        }}
                      />
                      <Bar isAnimationActive={false} dataKey="extra_profit_inr" radius={[4, 4, 4, 4]} maxBarSize={56}>
                        {chart.map((r) => (
                          <Cell key={r.strategy} fill={r.strategy === 'offeriq' ? 'var(--series-1)' : 'var(--muted)'} />
                        ))}
                        <LabelList
                          dataKey="extra_profit_inr"
                          position="top"
                          formatter={(v) => inr(Number(v))}
                          style={{ fill: 'var(--text-2)', fontSize: 12 }}
                        />
                      </Bar>
                    </BarChart>
                  </ResponsiveContainer>
                </div>
              </section>

              <section className="card">
                <h2>What OfferIQ sent</h2>
                <p className="sub">
                  It picks the cheapest offer that changes behaviour, so most offers cost little or nothing.
                </p>
                <Mix mix={data.offeriq_offer_mix} />
              </section>
            </div>

            <section className="card">
              <h2>All strategies</h2>
              <p className="sub">“Wasted” is discount paid on orders that would have happened without the offer.</p>
              <StrategyTable results={data.results} />
            </section>

            <section className="card">
              <h2>Fairness audit</h2>
              <p className="sub">
                Share of each group that OfferIQ would send an offer to. Gender and age are audited, never used for
                targeting.
              </p>
              <div className="grid cols-4">
                {Object.entries(data.fairness).map(([column, f]) => (
                  <div key={column}>
                    <div className="row small" style={{ marginBottom: 6 }}>
                      <b>{column.replace('_', ' ')}</b>
                      <span className="spacer" />
                      <span className={`pill ${f.max_gap_pp <= 5 ? 'good' : 'warn'}`}>gap {f.max_gap_pp} pp</span>
                    </div>
                    {f.groups.map((g) => (
                      <div key={String(g[column])} className="small" style={{ marginBottom: 6 }}>
                        <div className="row">
                          {String(g[column])}
                          <span className="spacer" />
                          {pct(g.offer_rate, 0)}
                        </div>
                        <div className="bar-track">
                          <div className="bar-fill" style={{ width: `${g.offer_rate * 100}%` }} />
                        </div>
                      </div>
                    ))}
                  </div>
                ))}
              </div>
            </section>
          </>
        )}
      </div>
    </>
  )
}

export function StrategyTable({ results }: { results: StrategyResult[] }) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Strategy</th>
            <th className="n">Offers sent</th>
            <th className="n">Used</th>
            <th className="n">Extra orders</th>
            <th className="n">Extra revenue</th>
            <th className="n">Offer cost</th>
            <th className="n">Wasted</th>
            <th className="n">Extra profit</th>
            <th className="n">Return per ₹1</th>
          </tr>
        </thead>
        <tbody>
          {results.map((r) => (
            <tr key={r.strategy} className={r.strategy === 'offeriq' ? 'best' : ''}>
              <td>{r.label}</td>
              <td className="n">{num(r.offers_sent)}</td>
              <td className="n">{num(r.offers_used)}</td>
              <td className="n">{r.strategy === 'none' ? '—' : num(r.extra_orders, 1)}</td>
              <td className="n">{r.strategy === 'none' ? '—' : inr(r.extra_revenue_inr)}</td>
              <td className="n">{inr(r.offer_cost_inr)}</td>
              <td className="n">{inr(r.wasted_discount_inr)}</td>
              <td className="n" style={{ color: r.extra_profit_inr < 0 ? 'var(--bad)' : undefined, fontWeight: 600 }}>
                {r.strategy === 'none' ? '—' : inr(r.extra_profit_inr)}
              </td>
              <td className="n">{r.roi === null ? '—' : `₹${(r.roi + 1).toFixed(2)}`}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function Mix({ mix }: { mix: Record<string, number> }) {
  const entries = Object.entries(mix).sort((a, b) => b[1] - a[1])
  const total = entries.reduce((s, [, n]) => s + n, 0)
  if (!total) return <p className="muted">No offers.</p>
  return (
    <div className="stack" style={{ gap: 9 }}>
      {entries.map(([type, n]) => (
        <div key={type}>
          <div className="row small">
            {OFFER_LABELS[type] ?? type}
            <span className="spacer" />
            {num(n)} ({Math.round((n / total) * 100)}%)
          </div>
          <div className="bar-track">
            <div className="bar-fill" style={{ width: `${(n / total) * 100}%` }} />
          </div>
        </div>
      ))}
    </div>
  )
}
