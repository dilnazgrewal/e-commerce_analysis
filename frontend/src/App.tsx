import { useEffect, useState } from 'react'
import { api, ApiError, getToken, setToken } from './api'
import Decisions from './pages/Decisions'
import Engine from './pages/Engine'
import Evidence from './pages/Evidence'
import Lab from './pages/Lab'
import Overview from './pages/Overview'
import Rules from './pages/Rules'

const PAGES = [
  { id: 'engine', label: 'Live engine', Page: Engine },
  { id: 'decisions', label: 'Decision log', Page: Decisions },
  { id: 'overview', label: 'Overview', Page: Overview },
  { id: 'lab', label: 'Policy Lab', Page: Lab },
  { id: 'rules', label: 'Rules Studio', Page: Rules },
  { id: 'evidence', label: 'Evidence', Page: Evidence },
]

function useHashPage() {
  const read = () => window.location.hash.replace('#/', '') || 'engine'
  const [page, setPage] = useState(read)
  useEffect(() => {
    const on = () => setPage(read())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return page
}

export default function App() {
  const page = useHashPage()
  const [authed, setAuthed] = useState<boolean | null>(null)
  const [loginRequired, setLoginRequired] = useState(true)

  useEffect(() => {
    api<{ login_required: boolean }>('/api/session')
      .then((s) => {
        setLoginRequired(s.login_required)
        setAuthed(true)
      })
      .catch(() => setAuthed(false))
    const out = () => setAuthed(false)
    window.addEventListener('offeriq-logout', out)
    return () => window.removeEventListener('offeriq-logout', out)
  }, [])

  if (authed === null) return <div className="login muted">Loading…</div>
  if (!authed) return <Login onDone={() => setAuthed(true)} />

  const current = PAGES.find((p) => p.id === page) ?? PAGES[0]
  return (
    <div className="app">
      <aside className="side">
        <div className="brand">
          Offer<span>IQ</span>
        </div>
        <div className="brand-sub">Who gets which offer, and why</div>
        <nav className="nav">
          {PAGES.map((p) => (
            <a key={p.id} href={`#/${p.id}`} className={p.id === current.id ? 'on' : ''}>
              {p.label}
            </a>
          ))}
        </nav>
        <div className="side-foot">
          Synthetic customers only. Bot:{' '}
          <a href="https://t.me/Dilnazecom_bot" target="_blank" rel="noreferrer">
            @Dilnazecom_bot
          </a>
          {loginRequired && getToken() && (
            <p>
              <button
                className="btn small"
                onClick={() => {
                  setToken('')
                  setAuthed(false)
                }}
              >
                Sign out
              </button>
            </p>
          )}
        </div>
      </aside>
      <main className="main">
        <current.Page />
      </main>
    </div>
  )
}

function Login({ onDone }: { onDone: () => void }) {
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const r = await api<{ token: string }>('/api/login', { password })
      setToken(r.token)
      onDone()
    } catch (err) {
      setError(err instanceof ApiError && err.status === 401 ? 'Wrong password.' : 'Could not reach OfferIQ.')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="login">
      <form className="card stack" onSubmit={submit}>
        <div>
          <div className="brand" style={{ padding: 0 }}>
            Offer<span>IQ</span>
          </div>
          <p className="sub" style={{ marginTop: 6 }}>
            Marketing dashboard. Sign in to see decisions, test strategies and edit the offer rules.
          </p>
        </div>
        <input
          className="input"
          type="password"
          placeholder="Dashboard password"
          value={password}
          autoFocus
          onChange={(e) => setPassword(e.target.value)}
        />
        {error && <div className="msg bad">{error}</div>}
        <button className="btn primary" disabled={busy || !password}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
    </div>
  )
}
