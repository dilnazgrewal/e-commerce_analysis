import { useEffect, useState } from 'react'

type Health = {
  status: string
  version: string
  env: string
  integrations: { telegram: boolean; claude: boolean }
}

function App() {
  const [health, setHealth] = useState<Health | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    fetch('/api/health')
      .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(setHealth)
      .catch(() => setError(true))
  }, [])

  return (
    <main className="shell">
      <h1>OfferIQ</h1>
      <p className="tagline">Which offer, for whom, and why. Every AI choice is checked against your rules.</p>

      <section className="card">
        <h2>System status</h2>
        {error && <p className="bad">Backend not reachable. Start it with <code>uv run uvicorn app.main:app</code>.</p>}
        {!error && !health && <p>Checking…</p>}
        {health && (
          <ul>
            <li>API: <span className="good">{health.status}</span> (v{health.version}, {health.env})</li>
            <li>Telegram bot: <Flag on={health.integrations.telegram} /></li>
            <li>Claude API: <Flag on={health.integrations.claude} /></li>
          </ul>
        )}
      </section>
    </main>
  )
}

function Flag({ on }: { on: boolean }) {
  return on ? <span className="good">configured</span> : <span className="bad">not configured</span>
}

export default App
