import { useState } from 'react'
import { api } from '../api'

export default function Feedback() {
  const [message, setMessage] = useState('')
  const [email, setEmail] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)
  const [sent, setSent] = useState(false)

  const submit = async (event) => {
    event.preventDefault()
    if (!message.trim() || busy) return
    setBusy(true)
    setError(null)
    try {
      await api.sendFeedback({ message, email })
      setSent(true)
      setMessage('')
      setEmail('')
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page" style={{ maxWidth: 640 }}>
      <div className="page-header">
        <h1>Feedback</h1>
      </div>
      <p style={{ color: 'var(--text-dim)', lineHeight: 1.6, marginTop: 0 }}>
        Thanks for playing! I read every message that comes in. AI D&D is built
        by one person, so I can't promise every idea makes it into the game, but
        all of them help me decide what to work on next.
      </p>
      {sent && (
        <div className="demo-banner" role="status">
          <span>Thanks, your feedback was sent.</span>
          <button onClick={() => setSent(false)} aria-label="Dismiss">✕</button>
        </div>
      )}
      <form onSubmit={submit}>
        <label className="field">
          <span className="label">Your feedback</span>
          <textarea rows={8} maxLength={5000} required value={message}
            placeholder="Bugs, ideas, things you liked or didn't…"
            onChange={(event) => setMessage(event.target.value)} />
        </label>
        <label className="field">
          <span className="label">Email (optional, if you'd like a reply)</span>
          <input type="email" maxLength={320} value={email} autoComplete="email"
            onChange={(event) => setEmail(event.target.value)} />
        </label>
        {error && <div className="test-error" role="alert">{error}</div>}
        <button type="submit" className="primary" disabled={busy || !message.trim()}>
          {busy ? 'Sending…' : 'Send feedback'}
        </button>
      </form>
    </div>
  )
}
