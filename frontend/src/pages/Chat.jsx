/* AI Chat — a plain scratchpad for talking to a model, with none of the game's
   context assembly in the way. Power users only (the backend 404s the routes
   for everyone else, and the nav link is hidden).

   Deliberately client-side: the conversation lives in localStorage, not the
   database. Nothing here is part of an adventure, so there's nothing worth a
   migration — and a refresh still keeps what you were poking at. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../api'
import { useToast } from '../components'

const STORAGE_KEY = 'aidnd.chat.v1'

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}')
    return {
      messages: Array.isArray(saved.messages) ? saved.messages : [],
      system: typeof saved.system === 'string' ? saved.system : '',
      model: typeof saved.model === 'string' ? saved.model : '',
      temperature: saved.temperature ?? '',
      maxTokens: saved.maxTokens ?? '',
    }
  } catch {
    return { messages: [], system: '', model: '', temperature: '', maxTokens: '' }
  }
}

const ROLE_LABEL = { user: 'You', assistant: 'AI', system: 'System' }

function ReasoningBlock({ text, streaming }) {
  if (!text) return null
  return (
    <details className="reasoning" open={streaming || undefined}>
      <summary>💭 Reasoning{streaming ? '…' : ''}</summary>
      <div className="reasoning-text">{text}</div>
    </details>
  )
}

function Message({ message, onDelete }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    navigator.clipboard?.writeText(message.content).then(
      () => { setCopied(true); setTimeout(() => setCopied(false), 1500) },
      () => {},
    )
  }
  return (
    <div className={`chat-msg ${message.role}`}>
      <div className="chat-msg-head">
        <span className="chat-role">{ROLE_LABEL[message.role] || message.role}</span>
        {message.model && <span className="dim chat-model-tag">{message.model}</span>}
        <span className="chat-msg-actions">
          <button className="linklike" onClick={copy}>{copied ? 'copied' : 'copy'}</button>
          <button className="linklike" onClick={onDelete}>delete</button>
        </span>
      </div>
      <ReasoningBlock text={message.reasoning} />
      <div className="chat-msg-body">{message.content}</div>
    </div>
  )
}

export default function Chat() {
  const { me } = useOutletContext() ?? {}
  const navigate = useNavigate()
  const toast = useToast()

  const initial = useRef(load()).current
  const [messages, setMessages] = useState(initial.messages)
  const [system, setSystem] = useState(initial.system)
  const [model, setModel] = useState(initial.model)
  const [temperature, setTemperature] = useState(initial.temperature)
  const [maxTokens, setMaxTokens] = useState(initial.maxTokens)

  const [input, setInput] = useState('')
  const [config, setConfig] = useState(null)
  const [showOptions, setShowOptions] = useState(false)
  // Streaming reply in progress: null when idle, else the text so far ('' before
  // the first token). `busy` covers the whole request, including the wait.
  const [streaming, setStreaming] = useState(null)
  const [reasoningStream, setReasoningStream] = useState(null)
  const [busy, setBusy] = useState(false)

  const abortRef = useRef(null)
  const inputRef = useRef(null)
  const pinnedRef = useRef(true)

  // me is null until /auth/me resolves; only bounce once we know.
  useEffect(() => {
    if (me && !me.power_user) navigate('/', { replace: true })
  }, [me, navigate])

  useEffect(() => {
    api.getChatConfig().then(setConfig).catch(() => setConfig(null))
  }, [])

  useEffect(() => {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ messages, system, model, temperature, maxTokens }),
    )
  }, [messages, system, model, temperature, maxTokens])

  // Grow the composer with its content (CSS caps the height, then it scrolls).
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [input])

  useEffect(() => {
    const onScroll = () => {
      pinnedRef.current =
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (pinnedRef.current) window.scrollTo({ top: document.documentElement.scrollHeight })
  }, [messages, streaming, reasoningStream])

  // Abort any in-flight stream when leaving the page.
  useEffect(() => () => abortRef.current?.abort(), [])

  const send = useCallback(async (history) => {
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(true)
    setStreaming('')
    setReasoningStream(null)
    pinnedRef.current = true

    const payload = {
      messages: [
        ...(system.trim() ? [{ role: 'system', content: system.trim() }] : []),
        ...history.map(({ role, content }) => ({ role, content })),
      ],
    }
    if (model.trim()) payload.model = model.trim()
    if (temperature !== '' && temperature !== null) payload.temperature = Number(temperature)
    if (maxTokens !== '' && maxTokens !== null) payload.max_tokens = Number(maxTokens)

    let reasoning = ''
    try {
      await api.chatStream(payload, (event) => {
        if (event.type === 'chunk') {
          setStreaming((prev) => (prev ?? '') + event.text)
        } else if (event.type === 'reasoning') {
          reasoning += event.text
          setReasoningStream((prev) => (prev ?? '') + event.text)
        } else if (event.type === 'note') {
          toast(event.detail)
        } else if (event.type === 'done') {
          setMessages((prev) => [...prev, {
            role: 'assistant',
            content: event.text,
            reasoning: event.reasoning || reasoning || undefined,
            model: event.model,
          }])
        } else if (event.type === 'error') {
          toast(event.detail, 'error')
        }
      }, controller.signal)
    } catch (err) {
      if (err.name !== 'AbortError') toast(err.message, 'error')
    } finally {
      abortRef.current = null
      setBusy(false)
      setStreaming(null)
      setReasoningStream(null)
    }
  }, [system, model, temperature, maxTokens, toast])

  const submit = () => {
    const text = input.trim()
    if (!text || busy) return
    const history = [...messages, { role: 'user', content: text }]
    setMessages(history)
    setInput('')
    send(history)
  }

  const regenerate = () => {
    if (busy) return
    // Drop trailing assistant replies and re-send from the last user message.
    let history = [...messages]
    while (history.length && history[history.length - 1].role === 'assistant') history.pop()
    if (!history.length) return
    setMessages(history)
    send(history)
  }

  const stop = () => {
    abortRef.current?.abort()
    // Keep whatever streamed in — a cut-off reply is often the thing you wanted.
    const partial = streaming?.trim()
    if (partial) {
      setMessages((prev) => [...prev, {
        role: 'assistant',
        content: partial,
        reasoning: reasoningStream || undefined,
        model: config?.model,
        stopped: true,
      }])
    }
  }

  const clear = () => {
    if (busy || !messages.length) return
    setMessages([])
    toast('Conversation cleared')
  }

  const deleteAt = (index) => setMessages((prev) => prev.filter((_, i) => i !== index))

  const onKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      submit()
    }
  }

  if (me && !me.power_user) return null
  const waitingForFirstToken = streaming === '' && reasoningStream === null
  const canRegenerate = !busy && messages.some((m) => m.role === 'user')

  return (
    <div className="page chat-page">
      <div className="page-header">
        <h1>AI Chat</h1>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <button className="linklike" onClick={() => setShowOptions((o) => !o)}>
            {showOptions ? 'hide options' : 'options'}
          </button>
          <button onClick={regenerate} disabled={!canRegenerate}>Regenerate</button>
          <button className="danger" onClick={clear} disabled={busy || !messages.length}>Clear</button>
        </div>
      </div>

      <div className="chat-meta dim">
        {config
          ? <>
              {/* The override wins when set, so show what a send would actually use. */}
              {model.trim() || config.model || '(no model set)'} · {config.endpoint_url}
              {config.using_demo && ' · shared demo key (whitelisted models only)'}
              {config.api_mode === 'completion' && ' · completion mode (messages are flattened)'}
            </>
          : 'Loading provider config…'}
      </div>

      {showOptions && (
        <div className="chat-options">
          <label className="field">
            <span className="label">System prompt (sent first, every turn — empty = none)</span>
            <textarea rows={3} value={system} placeholder="You are a helpful assistant."
              onChange={(e) => setSystem(e.target.value)} />
          </label>
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
            <label className="field" style={{ flex: '2 1 240px' }}>
              <span className="label">Model {config?.using_demo ? '(demo whitelist)' : '(empty = Settings default)'}</span>
              <input type="text" list="chat-models" value={model} placeholder={config?.model || 'model slug'}
                onChange={(e) => setModel(e.target.value)} />
              <datalist id="chat-models">
                {(config?.models || []).map((m) => <option key={m} value={m} />)}
              </datalist>
            </label>
            <label className="field" style={{ flex: '1 1 110px' }}>
              <span className="label">Temperature</span>
              <input type="number" step="0.1" min="0" max="5" value={temperature}
                placeholder={config?.temperature ?? ''}
                onChange={(e) => setTemperature(e.target.value)} />
            </label>
            <label className="field" style={{ flex: '1 1 130px' }}>
              <span className="label">Max tokens</span>
              <input type="number" min="1" value={maxTokens}
                placeholder={config?.max_tokens ?? ''}
                onChange={(e) => setMaxTokens(e.target.value)} />
            </label>
          </div>
          {config?.models_error && (
            <div className="dim" style={{ fontSize: '0.82rem' }}>
              Couldn't list models from the endpoint: {config.models_error}
            </div>
          )}
        </div>
      )}

      <div className="chat-transcript">
        {!messages.length && streaming === null && (
          <div className="empty">
            Nothing here yet — no story, no scripts, no world state. Just you and the model.
          </div>
        )}
        {messages.map((m, i) => (
          <Message key={i} message={m} onDelete={() => deleteAt(i)} />
        ))}
        {streaming !== null && (
          <div className="chat-msg assistant">
            <div className="chat-msg-head">
              <span className="chat-role">AI</span>
              <span className="dim chat-model-tag">{model.trim() || config?.model}</span>
            </div>
            <ReasoningBlock text={reasoningStream} streaming />
            {waitingForFirstToken
              ? <div className="thinking" role="status"><i /><i /><i /><span>Thinking</span></div>
              : <div className="chat-msg-body">{streaming}<span className="cursor">▋</span></div>}
          </div>
        )}
      </div>

      <div className="chat-composer">
        <textarea ref={inputRef} rows={1} value={input} onChange={(e) => setInput(e.target.value)}
          onKeyDown={onKeyDown} placeholder="Message the model…  (Enter to send, Shift+Enter for a new line)" />
        {busy
          ? <button className="danger" onClick={stop}>Stop</button>
          : <button className="primary" onClick={submit} disabled={!input.trim()}>Send</button>}
      </div>
    </div>
  )
}
