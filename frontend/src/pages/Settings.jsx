import { useEffect, useState } from 'react'
import { api } from '../api'

function DebugLog() {
  const [entries, setEntries] = useState(null)
  const [openId, setOpenId] = useState(null)

  const load = () => api.getDebugRequests().then(setEntries).catch(() => setEntries([]))
  useEffect(() => { load() }, [])

  if (!entries) return null
  return (
    <div className="debug-log">
      <div className="page-header" style={{ marginTop: 28 }}>
        <h3 style={{ margin: 0 }}>Recent AI requests</h3>
        <button onClick={load}>Refresh</button>
      </div>
      {entries.length === 0 && (
        <div className="empty" style={{ padding: '16px 0' }}>
          No requests yet this session. Play a turn and refresh.
        </div>
      )}
      {entries.map((e) => (
        <div key={e.id} className="debug-entry">
          <div className="debug-entry-head" onClick={() => setOpenId(openId === e.id ? null : e.id)}>
            <span className={`debug-status ${e.status}`}>{e.status}</span>
            <span>{e.model}</span>
            <span className="dim">{new Date(e.time).toLocaleTimeString()}</span>
            <span className="dim" style={{ marginLeft: 'auto' }}>{openId === e.id ? '▾' : '▸'}</span>
          </div>
          {openId === e.id && (
            <div className="debug-entry-body">
              <div className="dim">{e.url}</div>
              {e.error && <div className="test-error">{e.error}</div>}
              <div className="ctx-header" style={{ padding: '6px 0 2px' }}><span>Request</span></div>
              <pre>{JSON.stringify(e.request, null, 2)}</pre>
              <div className="ctx-header" style={{ padding: '6px 0 2px' }}><span>Response</span></div>
              <pre>{e.response || '(empty)'}</pre>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

export default function Settings() {
  const [settings, setSettings] = useState(null)
  const [testResult, setTestResult] = useState(null)
  const [saved, setSaved] = useState('')

  useEffect(() => {
    api.getSettings().then(setSettings)
  }, [])

  const setField = (field, value) => setSettings({ ...settings, [field]: value })

  const save = async () => {
    await api.updateSettings(settings)
    setSaved('Settings saved')
    setTimeout(() => setSaved(''), 2000)
  }

  const test = async () => {
    setTestResult({ pending: true })
    await api.updateSettings(settings)
    const result = await api.testConnection()
    setTestResult(result)
  }

  if (!settings) return null

  return (
    <div className="page" style={{ maxWidth: 640 }}>
      <div className="page-header">
        <h1>Settings</h1>
        <span style={{ color: 'var(--text-dim)', fontSize: '0.85rem' }}>{saved}</span>
      </div>

      <label className="field">
        <span className="label">Endpoint URL (OpenAI-compatible)</span>
        <input type="text" value={settings.endpoint_url}
          placeholder="http://localhost:11434/v1"
          onChange={(e) => setField('endpoint_url', e.target.value)} />
      </label>
      <label className="field">
        <span className="label">API Key</span>
        <input type="password" value={settings.api_key}
          placeholder="Leave empty for local endpoints"
          onChange={(e) => setField('api_key', e.target.value)} />
      </label>
      <label className="field">
        <span className="label">Model</span>
        <input type="text" value={settings.model} placeholder="e.g. llama3.1"
          onChange={(e) => setField('model', e.target.value)} />
      </label>

      <div style={{ display: 'flex', gap: 14 }}>
        <label className="field" style={{ flex: 1 }}>
          <span className="label">Temperature</span>
          <input type="number" step="0.1" min="0" max="2" value={settings.temperature}
            onChange={(e) => setField('temperature', Number(e.target.value))} />
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span className="label">Max output tokens</span>
          <input type="number" value={settings.max_output_tokens}
            onChange={(e) => setField('max_output_tokens', Number(e.target.value))} />
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span className="label">Context budget (tokens)</span>
          <input type="number" value={settings.context_token_budget}
            onChange={(e) => setField('context_token_budget', Number(e.target.value))} />
        </label>
      </div>
      <label className="field">
        <span className="label">Reasoning budget (tokens)</span>
        <input type="number" min="0" value={settings.reasoning_max_tokens}
          onChange={(e) => setField('reasoning_max_tokens', Number(e.target.value))} />
        <span className="label" style={{ marginTop: 4 }}>
          For reasoning models: separate thinking budget on top of max output tokens,
          and thinking is shown collapsed above each response. 0 = off (nothing extra
          is sent — keep 0 for endpoints/models without reasoning support).
        </span>
      </label>

      <label className="field">
        <span className="label">API mode</span>
        <select value={settings.api_mode} onChange={(e) => setField('api_mode', e.target.value)}>
          <option value="chat">Chat (/v1/chat/completions)</option>
          <option value="completion">Completion (/v1/completions)</option>
        </select>
      </label>

      <div className="page-header" style={{ marginTop: 20 }}>
        <h3 style={{ margin: 0 }}>Memory system</h3>
      </div>
      <div style={{ display: 'flex', gap: 14 }}>
        <label className="field" style={{ flex: 1 }}>
          <span className="label">Summarization model</span>
          <input type="text" value={settings.summary_model}
            placeholder="Empty = use main model"
            onChange={(e) => setField('summary_model', e.target.value)} />
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span className="label">Embedding model</span>
          <input type="text" value={settings.embedding_model}
            placeholder="e.g. nomic-embed-text (empty = memory bank off)"
            onChange={(e) => setField('embedding_model', e.target.value)} />
        </label>
      </div>
      <div style={{ display: 'flex', gap: 14 }}>
        <label className="field" style={{ flex: 1 }}>
          <span className="label">Memory bank capacity</span>
          <input type="number" min="1" value={settings.memory_bank_capacity}
            onChange={(e) => setField('memory_bank_capacity', Number(e.target.value))} />
        </label>
        <label className="field" style={{ flex: 1 }}>
          <span className="label">Memories per turn (top-K)</span>
          <input type="number" min="1" value={settings.memory_top_k}
            onChange={(e) => setField('memory_top_k', Number(e.target.value))} />
        </label>
      </div>

      <label className="field">
        <span className="label">Narrator system prompt</span>
        <textarea rows={4} value={settings.narrator_prompt}
          onChange={(e) => setField('narrator_prompt', e.target.value)} />
      </label>

      <div style={{ display: 'flex', gap: 10, marginTop: 8 }}>
        <button className="primary" onClick={save}>Save</button>
        <button onClick={test}>Test connection</button>
      </div>

      {testResult && (
        <div style={{ marginTop: 16, color: testResult.ok ? 'var(--accent)' : 'var(--danger)' }}>
          {testResult.pending ? 'Testing…'
            : testResult.ok
              ? `Connected. Models: ${testResult.models?.slice(0, 8).join(', ') || '(none listed)'}`
              : testResult.detail}
        </div>
      )}

      <DebugLog />
    </div>
  )
}
