import { useEffect, useMemo, useState } from 'react'
import { api, inr, num } from '../api'
import { ErrorBox, Loading, PageHead } from '../ui'
import { Mix, StrategyTable, type StrategyResult } from './Lab'

// The policy is a deep JSON document validated by the backend; the editor works on paths into it.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type Policy = any
type Change = { path: string; before: unknown; after: unknown }
type CheckResult = { ok: boolean; errors: string[]; warnings: string[]; diff: Change[] }
type DryRun = CheckResult & {
  customers: number
  days: number
  current: StrategyResult & { mix: Record<string, number> }
  proposed: StrategyResult & { mix: Record<string, number> }
  fairness_gap_pp: Record<string, { current: number; proposed: number }>
}
type PolicyResponse = {
  policy: Policy
  history: { version: number; author: string; note: string; created_at: string }[]
  margins: Record<string, number>
  ai_available: boolean
}
type AiEdit = {
  ok: boolean
  summary: string
  policy?: Policy
  warnings?: string[]
  errors?: string[]
  attempts: number
}

const UPLIFT_GROUPS = ['persuadable', 'sure_thing', 'lost_cause', 'sleeping_dog']
const LIFECYCLES = ['new', 'vip', 'active', 'at_risk', 'lapsed', 'window_shopper', 'bargain_hunter']
const EXAMPLES = [
  'Diwali week: cap electronics at 6% and double the daily budget',
  'No messages after 8 pm, and at most one offer per customer per week',
  'Lapsed customers should get free delivery as their fallback instead of a reminder',
]

const clone = (p: Policy): Policy => JSON.parse(JSON.stringify(p))
const getIn = (obj: Policy, path: (string | number)[]) => path.reduce((o, k) => o?.[k], obj)
const pretty = (category: string) => category.replaceAll('_', ' ').replace(' and ', ' & ')

export default function RulesPage() {
  const [live, setLive] = useState<PolicyResponse | null>(null)
  const [draft, setDraft] = useState<Policy | null>(null)
  const [check, setCheck] = useState<CheckResult | null>(null)
  const [dry, setDry] = useState<DryRun | null>(null)
  const [busy, setBusy] = useState<'' | 'dry' | 'publish' | 'ai'>('')
  const [error, setError] = useState<unknown>(null)
  const [note, setNote] = useState('')
  const [done, setDone] = useState('')
  const [instruction, setInstruction] = useState('')
  const [ai, setAi] = useState<AiEdit | null>(null)

  const load = () =>
    api<PolicyResponse>('/api/policy')
      .then((r) => {
        setLive(r)
        setDraft(clone(r.policy))
      })
      .catch(setError)
  useEffect(() => {
    load()
  }, [])

  // Validate every edit as you type (debounced): errors, margin warnings and the exact diff.
  useEffect(() => {
    if (!draft) return
    const timer = setTimeout(() => {
      api<CheckResult>('/api/policy/check', { policy: draft }).then(setCheck).catch(setError)
    }, 300)
    setDry(null)
    return () => clearTimeout(timer)
  }, [draft])

  const set = (path: (string | number)[], value: unknown) => {
    setDone('')
    setDraft((d: Policy) => {
      const next = clone(d)
      let o = next
      for (const k of path.slice(0, -1)) o = o[k]
      o[path[path.length - 1]] = value
      return next
    })
  }

  const changed = useMemo(() => new Set((check?.diff ?? []).map((c) => c.path)), [check])
  const dot = (path: (string | number)[]) => path.join('.')

  const runDry = async () => {
    setBusy('dry')
    setError(null)
    try {
      setDry(await api<DryRun>('/api/policy/dry-run', { policy: draft }))
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }
  const publish = async () => {
    if (!window.confirm(`Publish ${check?.diff.length} change(s)? The bot uses the new rules for the next offer.`)) return
    setBusy('publish')
    setError(null)
    try {
      const r = await api<{ version: number }>('/api/policy/publish', { policy: draft, note })
      setDone(`Published as version ${r.version}. The bot is using it now.`)
      setNote('')
      setAi(null)
      setDry(null)
      await load()
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }
  const askClaude = async () => {
    setBusy('ai')
    setError(null)
    setAi(null)
    try {
      const r = await api<AiEdit>('/api/policy/ai-edit', { instruction })
      setAi(r)
      if (r.ok && r.policy) {
        setDraft(r.policy)
        setNote(instruction.slice(0, 300))
      }
    } catch (e) {
      setError(e)
    } finally {
      setBusy('')
    }
  }

  if (error && !live) return <ErrorBox error={error} />
  if (!live || !draft) return <Loading />
  const p = draft
  const margins = live.margins
  const offerIds: string[] = p.offers.map((o: Policy) => o.id)

  const numField = (label: string, path: (string | number)[], opts: { hint?: string; step?: number } = {}) => (
    <label className={`field ${changed.has(dot(path)) ? 'changed' : ''}`}>
      <span>
        {label}
        {opts.hint && <span className="hint">{opts.hint}</span>}
      </span>
      <input
        className="input num"
        type="number"
        step={opts.step ?? 1}
        value={getIn(p, path) ?? ''}
        onChange={(e) => set(path, e.target.value === '' ? 0 : Number(e.target.value))}
      />
    </label>
  )

  return (
    <>
      <PageHead title="Rules Studio">
        The limits every offer must stay within, whether Claude or the rules chose it. Change a number or describe
        the change in plain English; every edit is checked, can be dry-run on the simulated customers, and only goes
        live when you publish it.
      </PageHead>

      <div className="grid" style={{ gridTemplateColumns: 'minmax(0, 1fr) minmax(300px, 360px)', alignItems: 'start' }}>
        <div className="stack">
          <section className="card">
            <h2>Describe a change</h2>
            <p className="sub">
              Claude (Opus) turns it into a proposed edit below. Nothing changes until you review and publish it.
            </p>
            <textarea
              className="input"
              rows={2}
              style={{ width: '100%', resize: 'vertical' }}
              placeholder="e.g. Diwali week: cap electronics at 6%, VIPs get free delivery instead of points"
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
            />
            <div className="row" style={{ marginTop: 8 }}>
              {EXAMPLES.map((ex) => (
                <button key={ex} className="pill" style={{ border: 0, cursor: 'pointer' }} onClick={() => setInstruction(ex)}>
                  {ex}
                </button>
              ))}
              <span className="spacer" />
              <button
                className="btn primary"
                disabled={!live.ai_available || busy !== '' || instruction.trim().length < 3}
                onClick={askClaude}
              >
                {busy === 'ai' ? (
                  <>
                    <span className="spin" /> Claude is drafting…
                  </>
                ) : (
                  'Propose edit'
                )}
              </button>
            </div>
            {!live.ai_available && <p className="small muted">AI editing is off right now; use the form below.</p>}
            {ai && (
              <div className={`msg ${ai.ok ? 'info' : 'warn'}`} style={{ marginTop: 10 }}>
                <b>{ai.ok ? 'Claude proposes: ' : 'Claude could not apply this: '}</b>
                {ai.summary || ai.errors?.join('; ')}
                {ai.ok && <div className="small muted">Review the highlighted fields and the change list, then dry-run.</div>}
              </div>
            )}
          </section>

          <section className="card">
            <h2>Rules</h2>
            <details className="section" open>
              <summary>Maximum discount per category</summary>
              <p className="small muted">Kept below each category's gross margin, or every discounted order loses money.</p>
              <div className="field-grid">
                {Object.keys(p.categories).map((c) =>
                  numField(pretty(c), ['categories', c, 'max_discount_pct'], { hint: `% of order · margin ${margins[c] ?? '?'}%`, step: 0.5 }),
                )}
              </div>
            </details>

            {p.festive.map((f: Policy, i: number) => (
              <details className="section" key={f.name}>
                <summary>
                  Festive override: {f.name} ({f.start} to {f.end})
                </summary>
                <div className="field-grid">
                  {numField('Budget multiplier', ['festive', i, 'budget_multiplier'], { step: 0.1, hint: '× the daily budget' })}
                  {Object.keys(f.category_caps).map((c) =>
                    numField(pretty(c), ['festive', i, 'category_caps', c], { hint: `festive cap % · margin ${margins[c]}%`, step: 0.5 }),
                  )}
                </div>
              </details>
            ))}

            <details className="section">
              <summary>Budget</summary>
              <div className="field-grid">
                {numField('Daily budget (₹)', ['budget', 'daily_inr'], { step: 1000, hint: 'total expected offer cost per day' })}
                {numField('Max cost per offer (₹)', ['budget', 'max_cost_per_offer_inr'], { step: 10 })}
                {numField('Max share of customer value', ['budget', 'max_cost_share_of_clv'], { step: 0.05, hint: 'cost ≤ this × 90-day margin value' })}
                {numField('Always-allowed small offer (₹)', ['budget', 'clv_cap_floor_inr'], { step: 10 })}
              </div>
            </details>

            <details className="section">
              <summary>Contact limits</summary>
              <div className="field-grid">
                {(['start', 'end'] as const).map((k) => (
                  <label key={k} className={`field ${changed.has(`contact.quiet_hours.${k}`) ? 'changed' : ''}`}>
                    <span>
                      Quiet hours {k}
                      <span className="hint">IST, no messages in between</span>
                    </span>
                    <input
                      className="input"
                      type="time"
                      value={String(p.contact.quiet_hours[k]).slice(0, 5)}
                      onChange={(e) => set(['contact', 'quiet_hours', k], e.target.value)}
                    />
                  </label>
                ))}
                {numField('Max offers per 7 days', ['contact', 'max_offers_per_7_days'])}
                {numField('Min days between offers', ['contact', 'min_days_between_offers'])}
                {numField('Cool-down after a purchase (days)', ['contact', 'cooldown_after_purchase_days'])}
                {numField('Holdout share', ['contact', 'holdout_share'], { step: 0.01, hint: 'random group that gets no offers (0–0.5)' })}
              </div>
            </details>

            <details className="section">
              <summary>Targeting</summary>
              <div className="field-grid">
                {numField('Min expected profit (₹)', ['targeting', 'min_expected_profit_inr'], { step: 1 })}
                {numField('Value of preventing churn', ['targeting', 'retention_value_weight'], { step: 0.1, hint: '0–2' })}
                {numField('New-account days', ['targeting', 'new_account_days'])}
                {numField('New-account max cost (₹)', ['targeting', 'new_account_max_cost_inr'], { step: 10 })}
              </div>
              <div className="small" style={{ marginTop: 8 }}>
                Never contact:{' '}
                {UPLIFT_GROUPS.map((g) => (
                  <label key={g} style={{ marginRight: 14 }}>
                    <input
                      type="checkbox"
                      checked={p.targeting.never_contact_uplift_groups.includes(g)}
                      onChange={(e) =>
                        set(
                          ['targeting', 'never_contact_uplift_groups'],
                          e.target.checked
                            ? [...p.targeting.never_contact_uplift_groups, g]
                            : p.targeting.never_contact_uplift_groups.filter((x: string) => x !== g),
                        )
                      }
                    />{' '}
                    {g.replace('_', ' ')}
                  </label>
                ))}
              </div>
            </details>

            <details className="section">
              <summary>Offer menu</summary>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>On</th>
                      <th>Offer</th>
                      <th className="n">Min</th>
                      <th className="n">Default</th>
                      <th className="n">Max</th>
                      <th className="n">Min order ₹</th>
                      <th className="n">Valid days</th>
                    </tr>
                  </thead>
                  <tbody>
                    {p.offers.map((o: Policy, i: number) => (
                      <tr key={o.id}>
                        <td>
                          <input type="checkbox" checked={o.enabled} onChange={(e) => set(['offers', i, 'enabled'], e.target.checked)} />
                        </td>
                        <td>
                          {o.label}
                          <div className="small muted">{o.type}</div>
                        </td>
                        {(['min', 'default', 'max'] as const).map((k) => (
                          <td className="n" key={k}>
                            <input
                              className="input num"
                              style={{ width: 72 }}
                              type="number"
                              value={o.value[k]}
                              onChange={(e) => set(['offers', i, 'value', k], Number(e.target.value))}
                            />
                          </td>
                        ))}
                        <td className="n">
                          <input className="input num" style={{ width: 80 }} type="number" step={50} value={o.min_order_inr}
                            onChange={(e) => set(['offers', i, 'min_order_inr'], Number(e.target.value))} />
                        </td>
                        <td className="n">
                          <input className="input num" style={{ width: 64 }} type="number" value={o.validity_days}
                            onChange={(e) => set(['offers', i, 'validity_days'], Number(e.target.value))} />
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </details>

            <details className="section">
              <summary>Fallback offer per customer stage (used when nothing clears the profit bar)</summary>
              <div className="field-grid">
                {LIFECYCLES.map((l) => (
                  <label key={l} className={`field ${changed.has(`fallbacks.${l}`) ? 'changed' : ''}`}>
                    <span>{l.replace('_', ' ')}</span>
                    <select className="input" value={p.fallbacks[l] ?? 'none'} onChange={(e) => set(['fallbacks', l], e.target.value)}>
                      <option value="none">no offer</option>
                      {offerIds.map((id) => (
                        <option key={id} value={id}>
                          {p.offers.find((o: Policy) => o.id === id)?.label}
                        </option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
            </details>
          </section>
        </div>

        <aside className="stack" style={{ position: 'sticky', top: 16 }}>
          <section className="card">
            <div className="row">
              <h2>Your changes</h2>
              <span className="spacer" />
              <span className="pill">live: v{live.policy.version}</span>
            </div>
            {done && <div className="msg good">{done}</div>}
            {check && !check.ok && (
              <div className="msg bad">
                <b>Not valid yet:</b>
                <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
                  {check.errors.map((e) => (
                    <li key={e}>{e}</li>
                  ))}
                </ul>
              </div>
            )}
            {check?.ok && check.diff.length === 0 && !done && <p className="muted small">No changes yet.</p>}
            {check?.ok &&
              check.diff.map((c) => (
                <div key={c.path} className="diff-row">
                  <div className="muted small">{c.path}</div>
                  <span className="before">{JSON.stringify(c.before)}</span> → <span className="after">{JSON.stringify(c.after)}</span>
                </div>
              ))}
            {[...(check?.warnings ?? []), ...(ai?.ok ? (ai.warnings ?? []) : [])].map((w) => (
              <div key={w} className="msg warn small" style={{ marginTop: 8 }}>
                ⚠ {w}
              </div>
            ))}
            {check?.ok && check.diff.length > 0 && (
              <div className="stack" style={{ gap: 8, marginTop: 12 }}>
                <button className="btn" disabled={busy !== ''} onClick={runDry}>
                  {busy === 'dry' ? (
                    <>
                      <span className="spin" /> Running on simulated customers…
                    </>
                  ) : (
                    'Dry run: what would this do?'
                  )}
                </button>
                <input className="input" placeholder="Note for the change log (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
                <div className="row">
                  <button className="btn" disabled={busy !== ''} onClick={() => setDraft(clone(live.policy))}>
                    Discard
                  </button>
                  <span className="spacer" />
                  <button className="btn primary" disabled={busy !== ''} onClick={publish}>
                    {busy === 'publish' ? 'Publishing…' : 'Approve & publish'}
                  </button>
                </div>
              </div>
            )}
            <ErrorBox error={error} />
          </section>

          {dry && <DryRunCard dry={dry} />}

          <section className="card">
            <h2>Version history</h2>
            {live.history.length === 0 && <p className="small muted">Version 1 (the default file) is live. Nothing published yet.</p>}
            {live.history.map((h) => (
              <div key={h.version} className="diff-row">
                <b>v{h.version}</b> · {h.author} · <span className="muted">{h.created_at.slice(0, 16)}</span>
                <div className="small">{h.note}</div>
              </div>
            ))}
          </section>
        </aside>
      </div>
    </>
  )
}

function DryRunCard({ dry }: { dry: DryRun }) {
  const d = dry.proposed.extra_profit_inr - dry.current.extra_profit_inr
  return (
    <section className="card">
      <h2>Dry run</h2>
      <p className="sub">
        OfferIQ on {num(dry.customers)} simulated customers over {dry.days} days, live rules vs your edit.
      </p>
      <div className={`msg ${d >= 0 ? 'good' : 'warn'}`}>
        Extra profit {inr(dry.current.extra_profit_inr)} → <b>{inr(dry.proposed.extra_profit_inr)}</b> ({d >= 0 ? '+' : ''}
        {inr(d)}). Offers sent {num(dry.current.offers_sent)} → {num(dry.proposed.offers_sent)}.
      </div>
      <div style={{ margin: '10px -4px 0' }}>
        <StrategyTable
          results={[
            { ...dry.current, label: 'Live rules', strategy: 'current' },
            { ...dry.proposed, label: 'Your edit', strategy: 'offeriq' },
          ]}
        />
      </div>
      <div className="small" style={{ marginTop: 10 }}>
        Fairness gap (pp):{' '}
        {Object.entries(dry.fairness_gap_pp).map(([k, v]) => (
          <span key={k} style={{ marginRight: 10 }}>
            {k.replace('_', ' ')} {v.current} → {v.proposed}
          </span>
        ))}
      </div>
      <h2 style={{ marginTop: 14, fontSize: '0.95rem' }}>Offer mix with your edit</h2>
      <Mix mix={dry.proposed.mix} />
    </section>
  )
}
