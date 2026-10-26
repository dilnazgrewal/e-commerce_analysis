import { useEffect, useState } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api, num, pct } from '../api'
import { axis, ErrorBox, Loading, PageHead, Stat, Tip } from '../ui'

type Arm = { effect: number; ci95: [number, number]; treated_rate: number; control_rate: number; n_treated: number }
type Qini = { qini_curve: number[]; random_curve: number[]; qini_gain: number; targeting_multiplier: number; uplift_top30: number; uplift_overall: number }
type Check = { name: string; synthetic: number; real: number; statistic: number; threshold: number; passed: boolean; detail: string }
type Evidence = {
  models: {
    as_of: string
    churn: { auc: number; brier: number; brier_baseline: number; base_rate: number; calibration: { pred: number; obs: number }[] }
    uplift_arms: Record<string, Arm>
    hillstrom: { source: string; rows: number; visit: Qini; conversion: Qini }
    segments: { uplift_group: Record<string, number>; lifecycle: Record<string, number> }
    ground_truth: Record<string, number>
  }
  realism: {
    passed: boolean
    checks: Check[]
    summary: Record<string, number | Record<string, number>>
    charts: { purchase_gaps: { bins: number[]; synthetic: number[]; uci_real: number[]; amazon_real: number[] } }
  }
  live: { treated_users: number; holdout_users: number; offers: number; claimed: number; dismissed: number; why: number }
}

const ARM_LABELS: Record<string, string> = {
  nudge: 'Reminder (no discount)',
  points2x: '2× points',
  freeship: 'Free delivery',
  pct10: '10% off',
  flat100: '₹100 off',
}

export default function EvidencePage() {
  const [data, setData] = useState<Evidence | null>(null)
  const [error, setError] = useState<unknown>(null)
  useEffect(() => {
    api<Evidence>('/api/evidence').then(setData).catch(setError)
  }, [])
  if (error) return <ErrorBox error={error} />
  if (!data) return <Loading />
  const { models: m, realism: r, live } = data
  const qini = m.hillstrom.visit.qini_curve.map((v, i) => ({
    share: i * 10,
    model: v,
    random: m.hillstrom.visit.random_curve[i],
  }))
  const calib = m.churn.calibration.map((c) => ({ pred: Math.round(c.pred * 100), obs: c.obs * 100, ideal: c.pred * 100 }))
  const gaps = r.charts.purchase_gaps
  const gapRows = gaps.bins.slice(0, -1).map((b, i) => ({
    bin: b,
    synthetic: gaps.synthetic[i],
    uci: gaps.uci_real[i],
    amazon: gaps.amazon_real[i],
  }))
  const gt = m.ground_truth
  const claimRate = live.offers ? live.claimed / live.offers : null

  return (
    <>
      <PageHead title="Evidence">
        Why you can trust the numbers: how well the models predict, how real the synthetic data is, and what we
        honestly cannot claim yet. Models were trained on {m.as_of} and never see the simulator's hidden truth.
      </PageHead>
      <div className="stack">
        <div className="grid cols-4">
          <Stat label="Churn model AUC" value={m.churn.auc.toFixed(3)} note={`Brier ${m.churn.brier.toFixed(3)} vs ${m.churn.brier_baseline.toFixed(3)} guessing the base rate`} />
          <Stat
            label="Uplift targeting (real RCT)"
            value={`${m.hillstrom.visit.targeting_multiplier.toFixed(2)}×`}
            note={`top 30% by model: +${pct(m.hillstrom.visit.uplift_top30)} visits vs +${pct(m.hillstrom.visit.uplift_overall)} average`}
          />
          <Stat
            label="Realism checks"
            value={<span style={{ color: r.passed ? 'var(--good)' : 'var(--bad)' }}>{r.checks.filter((c) => c.passed).length}/{r.checks.length} pass</span>}
            note="synthetic vs real UCI / Amazon data"
          />
          <Stat
            label="Live bot test"
            value={num(live.offers)}
            note={`offers to ${num(live.treated_users)} people (+${num(live.holdout_users)} holdout); claim rate ${claimRate === null ? '—' : pct(claimRate, 0)}`}
          />
        </div>

        <div className="grid cols-2">
          <section className="card">
            <h2>Uplift model on a real experiment</h2>
            <p className="sub">
              Hillstrom: {num(m.hillstrom.rows)} real customers randomly emailed or not. Targeting by our model's
              ranking (Qini curve) finds the people the email moves faster than random.
            </p>
            <div className="chart">
              <ResponsiveContainer>
                <LineChart data={qini} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
                  <CartesianGrid vertical={false} stroke="var(--grid)" />
                  <XAxis dataKey="share" tickFormatter={(v) => `${v}%`} {...axis} />
                  <YAxis {...axis} />
                  <Tooltip
                    content={({ active, payload, label }) =>
                      active && payload?.length ? (
                        <Tip title={`Top ${label}% targeted`} rows={payload.map((p) => [String(p.name), `${num(Number(p.value))} extra visits`])} />
                      ) : null
                    }
                  />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Line isAnimationActive={false} name="Our uplift model" dataKey="model" stroke="var(--series-1)" strokeWidth={2} dot={{ r: 3 }} />
                  <Line isAnimationActive={false} name="Random targeting" dataKey="random" stroke="var(--muted)" strokeWidth={2} strokeDasharray="5 4" dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </section>

          <section className="card">
            <h2>Churn probabilities are calibrated</h2>
            <p className="sub">When the model says 70%, about 70% of those customers really stop buying (held-out data).</p>
            <div className="chart">
              <ResponsiveContainer>
                <LineChart data={calib} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
                  <CartesianGrid vertical={false} stroke="var(--grid)" />
                  <XAxis dataKey="pred" type="number" domain={[0, 100]} tickFormatter={(v) => `${v}%`} {...axis} />
                  <YAxis domain={[0, 100]} tickFormatter={(v) => `${v}%`} {...axis} />
                  <Tooltip
                    content={({ active, payload }) => {
                      const row = payload?.[0]?.payload
                      return active && row ? <Tip title={`Predicted ${row.pred}%`} rows={[['Observed', `${row.obs.toFixed(0)}%`]]} /> : null
                    }}
                  />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Line isAnimationActive={false} name="Perfect calibration" dataKey="ideal" stroke="var(--muted)" strokeDasharray="5 4" strokeWidth={2} dot={false} />
                  <Line isAnimationActive={false} name="Observed churn" dataKey="obs" stroke="var(--series-1)" strokeWidth={2} dot={{ r: 3 }} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </section>
        </div>

        <section className="card">
          <h2>Measured effect of each offer (past randomised campaigns)</h2>
          <p className="sub">
            This is how OfferIQ chooses <i>which</i> offer: measured lift in purchase rate vs a control group, with
            95% confidence intervals.
          </p>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Offer</th>
                  <th className="n">Bought (offer)</th>
                  <th className="n">Bought (control)</th>
                  <th className="n">Lift</th>
                  <th className="n">95% CI</th>
                  <th className="n">People offered</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(m.uplift_arms).map(([arm, a]) => (
                  <tr key={arm}>
                    <td>{ARM_LABELS[arm] ?? arm}</td>
                    <td className="n">{pct(a.treated_rate)}</td>
                    <td className="n">{pct(a.control_rate)}</td>
                    <td className="n">
                      <b>+{(a.effect * 100).toFixed(1)} pp</b>
                    </td>
                    <td className="n">
                      {(a.ci95[0] * 100).toFixed(1)} to {(a.ci95[1] * 100).toFixed(1)} pp
                    </td>
                    <td className="n">{num(a.n_treated)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <div className="grid cols-2">
          <section className="card">
            <h2>Synthetic data vs real data</h2>
            <p className="sub">Time between a customer's orders (scaled to each customer's average), share of gaps.</p>
            <div className="chart">
              <ResponsiveContainer>
                <LineChart data={gapRows} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
                  <CartesianGrid vertical={false} stroke="var(--grid)" />
                  <XAxis dataKey="bin" tickFormatter={(v) => `${v}×`} {...axis} />
                  <YAxis tickFormatter={(v) => `${Math.round(v * 100)}%`} {...axis} />
                  <Tooltip
                    content={({ active, payload, label }) =>
                      active && payload?.length ? (
                        <Tip title={`Gap ≈ ${label}× average`} rows={payload.map((p) => [String(p.name), pct(Number(p.value))])} />
                      ) : null
                    }
                  />
                  <Legend wrapperStyle={{ fontSize: 12 }} />
                  <Line isAnimationActive={false} name="Synthetic" dataKey="synthetic" stroke="var(--series-1)" strokeWidth={2} dot={false} />
                  <Line isAnimationActive={false} name="UCI Online Retail (real)" dataKey="uci" stroke="var(--series-2)" strokeWidth={2} dot={false} />
                  <Line isAnimationActive={false} name="Amazon reviews (real)" dataKey="amazon" stroke="var(--series-3)" strokeWidth={2} dot={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </section>

          <section className="card">
            <h2>Realism checks</h2>
            <p className="sub">Each compares the generated customers with a real dataset; all must pass.</p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Check</th>
                    <th className="n">Synthetic</th>
                    <th className="n">Real</th>
                    <th />
                  </tr>
                </thead>
                <tbody>
                  {r.checks.map((c) => (
                    <tr key={c.name} title={c.detail}>
                      <td>{c.name.replaceAll('_', ' ')}</td>
                      <td className="n">{num(c.synthetic, 3)}</td>
                      <td className="n">{num(c.real, 3)}</td>
                      <td>
                        <span className={`pill ${c.passed ? 'good' : 'bad'}`}>{c.passed ? '✓ pass' : '✗ fail'}</span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>

        <section className="card">
          <h2>What we can't claim yet (honest limits)</h2>
          <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
            <li>
              Per-person uplift is weak: predicted vs true uplift rank correlation is {gt.spearman_pred_vs_true_uplift_pct10?.toFixed(2)}.
              That is why OfferIQ uses the pooled model for <i>who</i> and measured campaign effects for <i>which</i> offer.
            </li>
            <li>
              Sleeping dogs (people an offer puts off) are hard to find: {pct(gt.true_sleeping_dogs_flagged_share)} of
              them are flagged, at {pct(gt.flagged_sleeping_dogs_precision)} precision.
            </li>
            <li>
              The live bot test is small ({num(live.treated_users + live.holdout_users)} people) and classmates don't
              really buy, so true lift is measured in the Policy Lab, where the ground truth is known.
            </li>
          </ul>
        </section>
      </div>
    </>
  )
}
