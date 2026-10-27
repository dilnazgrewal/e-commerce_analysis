import { useState } from 'react'
import { api, inr, num, pct } from '../api'
import { ErrorBox, PageHead } from '../ui'

type Check = { rule: string; passed: boolean; reason: string }
type Considered = {
  offer_id: string
  type: string
  label: string
  value?: number
  category?: string | null
  allowed: boolean
  blocked_by: string[]
  uplift_pp?: number | null
  cost_if_used_inr?: number | null
  expected_cost_inr?: number | null
  expected_profit_inr?: number | null
}
type Trace = {
  clock: string
  policy: { version: number; name: string }
  event: Record<string, unknown>
  customer: Record<string, unknown>
  activity: { recent_views: string[]; recent_searches: string[]; left_in_cart: string[]; visits_14d: number }
  scores: {
    churn_60d: number
    p_buy_14d_without_offer: number
    uplift_any_offer: number
    uplift_group: string
    lifecycle: string
    clv_margin_90d_inr: number
    aov_inr: number
    top_category: string | null
    points_expiring_30d: number
  }
  trigger: { name: string; why: string }
  history: { offers_last_30d: number; spent_today_inr: number }
  contact_checks: Check[]
  considered: Considered[]
  decision: {
    tier: string
    status: string
    offer: { label: string; value: number; code: string | null; valid_until: string } | null
    message: { lang: string; text: string } | null
    economics: { uplift: number; p_buy_without: number; p_buy_with: number; expected_cost_inr: number; expected_profit_inr: number } | null
    rule_checks: Check[]
    explanation: { why: string }
    ai: { used?: boolean; skipped?: string; calls?: { tier: string; model: string; latency_ms: number; ok: boolean; error?: string }[] } | null
  }
  output: Record<string, unknown>
}
type Row = {
  customer_id: string
  name: string
  city: string
  lifecycle: string
  uplift_group: string
  churn_60d: number
  trigger: string
  tier: string
  status: string
  offer: string | null
  value: number | null
  expected_cost_inr: number | null
  expected_profit_inr: number | null
  checks_passed: number
  checks_total: number
  why: string
}

const TIERS: Record<string, string> = {
  T1: 'T1 · Claude agent chose it',
  T2: 'T2 · Claude, after a rule rejected its first try',
  T3: 'T3 · Claude Haiku from the rules’ top 3',
  T4: 'T4 · Rules engine: best expected profit',
  T5: 'T5 · Marketing team’s fallback for this segment',
  T6: 'T6 · No offer',
}

const GROUPS: Record<string, string> = {
  persuadable: 'Persuadable: an offer changes what they do',
  sure_thing: 'Sure thing: buys anyway, so no discount',
  lost_cause: 'Lost cause: unlikely to respond, spend little',
  sleeping_dog: 'Sleeping dog: contact makes them buy less',
}

export default function EnginePage() {
  const [customer, setCustomer] = useState('')
  const [trigger, setTrigger] = useState('auto')
  const [useAi, setUseAi] = useState(false)
  const [trace, setTrace] = useState<Trace | null>(null)
  const [rows, setRows] = useState<Row[] | null>(null)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState<unknown>(null)

  const run = async (id?: string) => {
    setBusy('trace')
    setError(null)
    try {
      const t = await api<Trace>('/api/engine/trace', { customer_id: id ?? (customer.trim() || null), trigger, use_ai: useAi })
      setTrace(t)
      setCustomer(String(t.customer.customer_id))
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }
  const burst = async () => {
    setBusy('batch')
    setError(null)
    try {
      setRows((await api<{ items: Row[] }>('/api/engine/batch', { n: 25 })).items)
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  return (
    <>
      <PageHead title="Live engine">
        The core of OfferIQ, step by step: a customer event comes in from the platform, the models score the customer,
        the rules decide which offers are allowed, the best one is chosen (by Claude or the rules), every rule is
        checked, and a Decision is sent back. Dry run: nothing is sent to anyone.
      </PageHead>

      <section className="card">
        <div className="row">
          <input className="input" style={{ width: 130 }} placeholder="Customer ID" value={customer} onChange={(e) => setCustomer(e.target.value)} />
          <select className="input" value={trigger} onChange={(e) => setTrigger(e.target.value)}>
            <option value="auto">Trigger: detect automatically</option>
            <option value="cart_abandoned">Cart abandoned</option>
            <option value="drifting">Drifting away (churn risk)</option>
            <option value="points_expiring">Points expiring</option>
            <option value="browsing">Routine check</option>
          </select>
          <label className="small">
            <input type="checkbox" checked={useAi} onChange={(e) => setUseAi(e.target.checked)} /> Let Claude choose (T1–T3)
          </label>
          <span className="spacer" />
          <button className="btn" disabled={busy !== ''} onClick={() => run('')}>
            Random customer
          </button>
          <button className="btn primary" disabled={busy !== ''} onClick={() => run()}>
            {busy === 'trace' ? <><span className="spin" /> Deciding…</> : 'Run decision'}
          </button>
        </div>
      </section>

      <ErrorBox error={error} />
      {trace && <TraceView t={trace} />}

      <section className="card" style={{ marginTop: 16 }}>
        <div className="row">
          <div>
            <h2>Platform traffic: 25 customers at once</h2>
            <p className="sub" style={{ margin: 0 }}>
              Random customers through the rules engine (no AI). Click a row to open its full trace above.
            </p>
          </div>
          <span className="spacer" />
          <button className="btn" disabled={busy !== ''} onClick={burst}>
            {busy === 'batch' ? <><span className="spin" /> Running…</> : 'Run 25 customers'}
          </button>
        </div>
        {rows && (
          <div className="table-wrap" style={{ marginTop: 12 }}>
            <table>
              <thead>
                <tr>
                  <th>Customer</th>
                  <th>Segment</th>
                  <th className="n">Churn risk</th>
                  <th>Trigger</th>
                  <th>Decision</th>
                  <th>Offer</th>
                  <th className="n">Exp. cost</th>
                  <th className="n">Exp. profit</th>
                  <th>Rules</th>
                  <th>Why</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.customer_id} className="click" onClick={() => { setCustomer(r.customer_id); run(r.customer_id); window.scrollTo({ top: 0, behavior: 'smooth' }) }}>
                    <td>
                      <b>{r.customer_id}</b>
                      <div className="small muted">{r.name}, {r.city}</div>
                    </td>
                    <td className="small">{r.lifecycle.replace('_', ' ')} · {r.uplift_group.replace('_', ' ')}</td>
                    <td className="n">{pct(r.churn_60d, 0)}</td>
                    <td className="small">{r.trigger.replace('_', ' ')}</td>
                    <td><span className={`pill ${r.status === 'sent' ? 'accent' : r.status === 'holdout' ? '' : 'warn'}`}>{r.tier} {r.status}</span></td>
                    <td className="small">{r.offer ?? '—'}{r.value ? ` (${num(r.value)})` : ''}</td>
                    <td className="n">{inr(r.expected_cost_inr, 1)}</td>
                    <td className="n">{inr(r.expected_profit_inr, 1)}</td>
                    <td className="small">{r.checks_passed}/{r.checks_total} ✓</td>
                    <td className="small muted" style={{ maxWidth: 320 }}>{r.why}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </>
  )
}

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <section className="card step">
      <div className="step-n">{n}</div>
      <div style={{ minWidth: 0, flex: 1 }}>
        <h2>{title}</h2>
        {children}
      </div>
    </section>
  )
}

function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>
}

function Checks({ checks }: { checks: Check[] }) {
  return (
    <ul className="checks">
      {checks.map((c, i) => (
        <li key={c.rule + i}>
          <span style={{ color: c.passed ? 'var(--good)' : 'var(--bad)' }}>{c.passed ? '✓' : '✗'}</span> <code>{c.rule}</code>{' '}
          <span className="muted">{c.reason}</span>
        </li>
      ))}
    </ul>
  )
}

function TraceView({ t }: { t: Trace }) {
  const s = t.scores
  const c = t.customer
  const d = t.decision
  const blockedNow = t.contact_checks.filter((x) => !x.passed)
  const allowed = t.considered.filter((o) => o.allowed).length
  return (
    <div className="stack" style={{ marginTop: 16 }}>
      <p className="small muted" style={{ margin: 0 }}>
        Engine clock {t.clock.slice(0, 16).replace('T', ' ')} IST · policy v{t.policy.version} · data as of the last model run
      </p>

      <Step n={1} title="The platform sends an event (input)">
        <p className="sub">This is the JSON an e-commerce site posts to OfferIQ when something happens.</p>
        <Json value={t.event} />
      </Step>

      <Step n={2} title={`Who is this? ${c.name}, ${c.city} (${c.city_tier})`}>
        <dl className="kv">
          <dt>Customer</dt><dd>{String(c.customer_id)} · age {String(c.age_band)} · since {String(c.signup_date)}</dd>
          <dt>Membership</dt><dd>{c.prime_member ? 'Prime member' : 'Not Prime'} · pays by {String(c.preferred_payment)} · {num(Number(c.points_balance))} points</dd>
          <dt>Language</dt><dd>{String(c.language_pref)}</dd>
          <dt>Last 14 days</dt><dd>{t.activity.visits_14d} visits{t.activity.recent_searches.length ? ` · searched “${t.activity.recent_searches.join('”, “')}”` : ''}</dd>
          {t.activity.left_in_cart.length > 0 && (<><dt>Left in cart</dt><dd>{t.activity.left_in_cart.join(' · ')}</dd></>)}
          <dt>Offers received</dt><dd>{t.history.offers_last_30d} in the last 30 days</dd>
        </dl>
      </Step>

      <Step n={3} title="What the models say about them">
        <div className="grid cols-4">
          <div><div className="small muted">Chance they stop buying (60 days)</div><div className="big">{pct(s.churn_60d, 0)}</div></div>
          <div><div className="small muted">Chance they buy in 14 days, no offer</div><div className="big">{pct(s.p_buy_14d_without_offer, 1)}</div></div>
          <div><div className="small muted">Extra chance if we send an offer (uplift)</div><div className="big">+{(s.uplift_any_offer * 100).toFixed(1)} pp</div></div>
          <div><div className="small muted">Value over next 90 days (margin)</div><div className="big">{inr(s.clv_margin_90d_inr)}</div></div>
        </div>
        <p className="small" style={{ marginBottom: 0 }}>
          Segment: <b>{GROUPS[s.uplift_group] ?? s.uplift_group}</b> · lifecycle <b>{s.lifecycle.replace('_', ' ')}</b> · usual order {inr(s.aov_inr)}
          {s.top_category ? ` · mostly buys ${s.top_category.replaceAll('_', ' ')}` : ''}
        </p>
      </Step>

      <Step n={4} title={`Trigger: ${t.trigger.name.replace('_', ' ')}`}>
        <p style={{ margin: 0 }}>{t.trigger.why}</p>
      </Step>

      <Step n={5} title={blockedNow.length ? 'Can we contact them now? No' : 'Can we contact them now? Yes'}>
        <p className="sub">Opt-out, holdout group, frequency cap, cool-down after a purchase, quiet hours.</p>
        <Checks checks={t.contact_checks} />
      </Step>

      <Step n={6} title={`Every offer considered: ${allowed} of ${t.considered.length} allowed`}>
        <p className="sub">
          For each offer the engine works out the extra chance to buy, what it would cost, and the expected profit
          (extra margin minus cost). The rules block offers that break a limit.
        </p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Offer</th>
                <th></th>
                <th className="n">Extra chance</th>
                <th className="n">Cost if used</th>
                <th className="n">Expected cost</th>
                <th className="n">Expected profit</th>
                <th>Blocked by</th>
              </tr>
            </thead>
            <tbody>
              {t.considered.map((o) => (
                <tr key={o.offer_id} className={d.offer && o.label === d.offer.label ? 'best' : ''}>
                  <td>{o.label}{o.value ? ` (${num(o.value)})` : ''}<div className="small muted">{o.type}</div></td>
                  <td>{o.allowed ? <span className="pill good">allowed</span> : <span className="pill bad">blocked</span>}</td>
                  <td className="n">{o.uplift_pp == null ? '—' : `+${o.uplift_pp} pp`}</td>
                  <td className="n">{inr(o.cost_if_used_inr, 1)}</td>
                  <td className="n">{inr(o.expected_cost_inr, 1)}</td>
                  <td className="n">{inr(o.expected_profit_inr, 1)}</td>
                  <td className="small muted">{o.blocked_by.join('; ')}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Step>

      <Step n={7} title={`Decision: ${TIERS[d.tier] ?? d.tier}`}>
        <div className={`msg ${d.status === 'sent' ? 'good' : 'warn'}`}>
          <b>{d.status === 'sent' ? `Send: ${d.offer?.label}` : `No offer (${d.status})`}</b> — {d.explanation.why}
        </div>
        {d.offer && (
          <p className="small">
            Value {num(d.offer.value)} · code <code>{d.offer.code ?? 'none needed'}</code> · valid until {d.offer.valid_until.slice(0, 16).replace('T', ' ')}
            {d.economics && <> · chance to buy {pct(d.economics.p_buy_without)} → {pct(d.economics.p_buy_with)}</>}
          </p>
        )}
        {d.message && (
          <div className="msg" style={{ background: 'var(--surface-2)' }}>
            <span className="small muted">Message ({d.message.lang}):</span> {d.message.text}
          </div>
        )}
        {d.ai && (
          <p className="small" style={{ marginBottom: 0 }}>
            AI: {d.ai.used === false ? `not used (${d.ai.skipped})` : (d.ai.calls ?? []).map((x) => `${x.tier} ${x.model} ${num(x.latency_ms)} ms ${x.ok ? 'ok' : x.error}`).join(' → ')}
          </p>
        )}
      </Step>

      <Step n={8} title={`Final rule check: ${d.rule_checks.filter((x) => x.passed).length} of ${d.rule_checks.length} passed`}>
        <p className="sub">The chosen offer and its exact message are checked against every rule before anything can be sent.</p>
        <Checks checks={d.rule_checks} />
      </Step>

      <Step n={9} title="The Decision sent back to the platform (output)">
        <Json value={t.output} />
      </Step>
    </div>
  )
}
