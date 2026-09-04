import { useEffect, useState } from 'react'
import './App.css'

const API_BASE = import.meta.env.VITE_API_BASE || 'http://127.0.0.1:8000'

function App() {
  const [health, setHealth] = useState(null)
  const [digest, setDigest] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    fetch(`${API_BASE}/health`)
      .then((r) => r.json())
      .then(setHealth)
      .catch((e) => setError(String(e)))

    fetch(`${API_BASE}/digest`)
      .then((r) => r.json())
      .then(setDigest)
      .catch((e) => setError(String(e)))
  }, [])

  return (
    <section style={{ fontFamily: 'system-ui', padding: '2rem', maxWidth: 640, margin: '0 auto' }}>
      <h1>Watermark</h1>
      <p style={{ color: '#666' }}>Markets drift. Attention shouldn't have to.</p>

      {error && <p style={{ color: 'crimson' }}>API unreachable: {error}</p>}

      <h2>Backend status</h2>
      <pre>{health ? JSON.stringify(health, null, 2) : 'loading...'}</pre>

      <h2>Digest (placeholder until detection layer lands)</h2>
      <pre>{digest ? JSON.stringify(digest, null, 2) : 'loading...'}</pre>
    </section>
  )
}

export default App
