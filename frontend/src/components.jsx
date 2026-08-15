import { createContext, useCallback, useContext, useEffect, useRef, useState } from 'react'
import { api } from './api'

export function downloadJSON(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function pickJSONFile() {
  return new Promise((resolve, reject) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.json,application/json'
    input.onchange = () => {
      const file = input.files[0]
      if (!file) return reject(new Error('No file selected'))
      const reader = new FileReader()
      reader.onload = () => {
        try { resolve(JSON.parse(reader.result)) }
        catch { reject(new Error('Not valid JSON')) }
      }
      reader.onerror = () => reject(new Error('Could not read file'))
      reader.readAsText(file)
    }
    input.click()
  })
}

// ---------- Scenario art ----------

// FNV-1a. Any stable hash works; the point is that a given title always maps to
// the same plate, so the library looks the same on every visit and every device.
function hashString(str) {
  let hash = 2166136261
  for (let i = 0; i < str.length; i++) {
    hash ^= str.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return hash >>> 0
}

// Deep jewel ramps that sit under gold without competing with it — the accent
// stays the brightest thing on the card. Ordered so adjacent library entries
// rarely land on neighbouring hues.
const ART_RAMPS = [
  ['#14424a', '#0b2328'], // drowned teal
  ['#4d2130', '#250f19'], // wine
  ['#2b2a5c', '#13122c'], // indigo
  ['#4a2a17', '#231309'], // rust
  ['#24401f', '#0f1c0c'], // moss
  ['#3a2b52', '#1b1328'], // violet ash
  ['#1f3346', '#0f1a25'], // cold slate
  ['#4a3a16', '#231b09'], // ochre
]

// Up to two letters from the title's most significant words.
function monogram(title) {
  const words = (title || '')
    .replace(/^\[[^\]]*\]\s*/, '') // drop a leading "[Demo]" style label
    .split(/\s+/)
    .filter((word) => word.length > 2 && !/^(the|and|of|a|an|from|for)$/i.test(word))
  const letters = (words.length ? words : ['?']).slice(0, 2).map((w) => w[0])
  return letters.join('').toUpperCase()
}

/** Initials for an NPC's avatar disc — "Bandit Leader" → BL, "gwen" → GW.
 *
 * Deliberately not `monogram`: that one drops short words, which is right for
 * scenario titles and wrong for names, and it has no id to fall back on.
 */
export function npcInitials(name, id) {
  const src = String(name || id || '?').trim() || '?'
  const words = src.split(/[\s_-]+/).filter(Boolean)
  const letters = words.length > 1 ? words[0][0] + words[1][0] : src.slice(0, 2)
  return letters.toUpperCase()
}

/** A scenario's plate: uploaded picture, else emoji, else generated art.
 *
 * `large` is for the Continue cards, where the plate carries more weight. The
 * generated tier means no card is ever an empty box, so a fresh library still
 * reads as a shelf of distinct things.
 */
export function ScenarioArt({ image, icon, title, large = false }) {
  const [failed, setFailed] = useState(false)
  const ramp = ART_RAMPS[hashString(title || '') % ART_RAMPS.length]
  const className = `art${large ? ' art-lg' : ''}`

  // A stored image can 404 (scenario deleted its art in another tab, or an
  // external URL rotted) — fall through to the generated tier rather than
  // showing a broken-image glyph.
  if (image && !failed) {
    return (
      <span className={`${className} art-photo`}>
        <img src={image} alt="" loading="lazy" onError={() => setFailed(true)} />
      </span>
    )
  }

  const style = { background: `linear-gradient(150deg, ${ramp[0]}, ${ramp[1]})` }
  if (icon) {
    return <span className={`${className} art-icon`} style={style} aria-hidden="true">{icon}</span>
  }
  return (
    <span className={`${className} art-mono`} style={style} aria-hidden="true">
      {monogram(title)}
    </span>
  )
}

// ---------- Loading skeletons ----------

/** Placeholder cards shown while a list loads, so the grid doesn't pop in. */
export function CardSkeleton({ count = 3, lines = 2 }) {
  return (
    <div className="card-grid" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <div key={i} className="card skeleton-card" style={{ animationDelay: `${i * 70}ms` }}>
          <div className="sk sk-plate" />
          <div className="sk sk-title" />
          {Array.from({ length: lines }, (_, line) => (
            <div key={line} className="sk sk-line" style={{ width: line === lines - 1 ? '62%' : '100%' }} />
          ))}
        </div>
      ))}
    </div>
  )
}

// ---------- Toasts ----------

const ToastContext = createContext(() => {})

/** `const toast = useToast()` then `toast('Imported')` or `toast(msg, 'error')`. */
export function useToast() {
  return useContext(ToastContext)
}

const TOAST_MS = 4200

export function ToastHost({ children }) {
  const [toasts, setToasts] = useState([])
  const nextId = useRef(1)
  const timers = useRef(new Map())

  const dismiss = useCallback((id) => {
    setToasts((current) => current.filter((t) => t.id !== id))
    const timer = timers.current.get(id)
    if (timer) {
      clearTimeout(timer)
      timers.current.delete(id)
    }
  }, [])

  const push = useCallback((message, kind = 'info') => {
    const id = nextId.current++
    setToasts((current) => [...current, { id, message: String(message), kind }])
    timers.current.set(id, setTimeout(() => dismiss(id), TOAST_MS))
  }, [dismiss])

  // Clear pending timers if the host ever unmounts mid-flight.
  useEffect(() => () => timers.current.forEach(clearTimeout), [])

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toast-host" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <button
            key={toast.id}
            type="button"
            className={`toast toast-${toast.kind}`}
            onClick={() => dismiss(toast.id)}
            title="Dismiss"
          >
            <span className="toast-mark" aria-hidden="true">{toast.kind === 'error' ? '✕' : '❖'}</span>
            <span>{toast.message}</span>
          </button>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

// Unique ${Placeholder} names, in order of first appearance, across the given texts.
export function extractPlaceholders(...texts) {
  const names = []
  for (const text of texts) {
    for (const match of (text || '').matchAll(/\$\{([^}]+)\}/g)) {
      const name = match[1].trim()
      if (name && !names.includes(name)) names.push(name)
    }
  }
  return names
}

export function PlaceholderModal({ title, names, onSubmit, onCancel }) {
  const [values, setValues] = useState(Object.fromEntries(names.map((n) => [n, ''])))

  const submit = (e) => {
    e.preventDefault()
    onSubmit(values)
  }

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h2>{title}</h2>
        <p className="modal-hint">This scenario asks a few questions before you begin.</p>
        {names.map((name, i) => (
          <label key={name} className="field">
            <span className="label">{name}</span>
            <input
              type="text"
              autoFocus={i === 0}
              value={values[name]}
              onChange={(e) => setValues({ ...values, [name]: e.target.value })}
            />
          </label>
        ))}
        <div className="modal-buttons">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="submit" className="primary">Begin Adventure</button>
        </div>
      </form>
    </div>
  )
}

// Phase 8: register/login for the hosted multi-user mode. `onAuthed(me)` gets
// the fresh /auth/me payload after success.
export function AuthModal({ mode: initialMode, onClose, onAuthed, retentionDays }) {
  const [mode, setMode] = useState(initialMode || 'register')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [reveal, setReveal] = useState(false)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const registering = mode === 'register'

  // Esc dismisses, like the overlay click already does.
  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const switchTo = (next) => {
    if (next === mode) return
    setMode(next)
    setError('')
    setReveal(false)
  }

  const submit = async (e) => {
    e.preventDefault()
    setBusy(true)
    setError('')
    try {
      const address = email.trim()
      const me = registering
        ? await api.register(address, password)
        : await api.login(address, password)
      onAuthed(me, mode)
    } catch (err) {
      setError(err.message)
      setBusy(false)
    }
  }

  return (
    <div className="modal-overlay" onClick={onClose}>
      <form className="modal auth-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}
        aria-labelledby="auth-title">
        <div className="auth-crest" aria-hidden="true">❖</div>
        <h2 id="auth-title">{registering ? 'Create an account' : 'Welcome back'}</h2>
        <p className="modal-hint auth-hint">
          {registering
            ? 'Everything you’ve played as a guest stays with your new account, and you can pick it up from any device.'
            : 'Log in to reach your adventures.'}
          {/* Guest data really is deleted, so say so where the decision is
              being made. The window comes from the server (see cleanup.py) so
              it can't drift from what's enforced. */}
          {registering && retentionDays ? (
            <> Guest adventures are deleted after {retentionDays} days without a visit.</>
          ) : null}
        </p>

        <div className="auth-tabs" role="tablist">
          <button type="button" role="tab" aria-selected={!registering}
            className={`auth-tab${registering ? '' : ' active'}`}
            onClick={() => switchTo('login')}>Log in</button>
          <button type="button" role="tab" aria-selected={registering}
            className={`auth-tab${registering ? ' active' : ''}`}
            onClick={() => switchTo('register')}>Sign up</button>
        </div>

        <label className="field">
          <span className="label">Email</span>
          <input type="email" autoFocus required value={email}
            autoComplete="email" placeholder="you@example.com"
            onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label className="field">
          <span className="label">Password</span>
          <div className="auth-password">
            <input type={reveal ? 'text' : 'password'} required
              minLength={registering ? 8 : undefined} value={password}
              autoComplete={registering ? 'new-password' : 'current-password'}
              onChange={(e) => setPassword(e.target.value)} />
            <button type="button" className="auth-reveal" tabIndex={-1}
              aria-label={reveal ? 'Hide password' : 'Show password'}
              onClick={() => setReveal((r) => !r)}>
              {reveal ? 'Hide' : 'Show'}
            </button>
          </div>
          {registering && <span className="auth-help">At least 8 characters.</span>}
        </label>

        {error && <div className="auth-error" role="alert">{error}</div>}

        <div className="modal-buttons">
          <button type="button" onClick={onClose}>Cancel</button>
          <button type="submit" className="primary" disabled={busy}>
            {busy ? 'Please wait…' : registering ? 'Sign up' : 'Log in'}
          </button>
        </div>
      </form>
    </div>
  )
}

/** A textarea that grows to fit its content instead of sitting at a fixed
    height. Editing an AI beat means editing a few paragraphs, and a fixed box
    made that a keyhole. CSS min/max-height still bound it (the cap scrolls),
    so callers control the floor and ceiling from the stylesheet. */
export function AutoTextarea({ value, ...props }) {
  const ref = useRef(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    // Reset first: scrollHeight only ever grows while an explicit height is set,
    // so without this the box could never shrink back down.
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [value])

  return <textarea ref={ref} value={value} {...props} />
}

export function Field({ label, value, onChange, textarea, rows, placeholder }) {
  return (
    <label className="field">
      <span className="label">{label}</span>
      {textarea ? (
        <textarea rows={rows || 3} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input type="text" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      )}
    </label>
  )
}

export function StoryCardRow({ card, onChange, onDelete }) {
  return (
    <div className="storycard">
      <div className="row">
        <input type="text" placeholder="Name (not sent to AI)" value={card.name}
          onChange={(e) => onChange({ ...card, name: e.target.value })} />
        <input type="text" placeholder="Type (e.g. Character)" value={card.type}
          onChange={(e) => onChange({ ...card, type: e.target.value })} />
      </div>
      <div className="row">
        <input type="text" placeholder="Triggers, comma-separated" value={card.keys}
          onChange={(e) => onChange({ ...card, keys: e.target.value })} />
      </div>
      <textarea rows={2} placeholder="Entry — sent to the AI when a trigger matches" value={card.entry}
        onChange={(e) => onChange({ ...card, entry: e.target.value })} />
      <div style={{ textAlign: 'right', marginTop: 6 }}>
        <button className="danger" style={{ padding: '3px 10px', fontSize: '0.78rem' }} onClick={onDelete}>
          Remove
        </button>
      </div>
    </div>
  )
}
