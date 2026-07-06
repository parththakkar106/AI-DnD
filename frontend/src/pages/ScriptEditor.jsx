import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import CodeMirror from '@uiw/react-codemirror'
import { javascript } from '@codemirror/lang-javascript'
import { oneDark } from '@codemirror/theme-one-dark'
import { api } from '../api'
import { Field, downloadJSON } from '../components'

const SLOTS = [
  { key: 'library_js', label: 'Library', hint: 'Shared code prepended to all three hooks.' },
  { key: 'input_js', label: 'Input', hint: "onInput — modifies the player's input before context construction." },
  { key: 'context_js', label: 'Context', hint: 'onModelContext — modifies the assembled text sent to the model.' },
  { key: 'output_js', label: 'Output', hint: 'onOutput — modifies the model output before it is shown.' },
]

export default function ScriptEditor() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [script, setScript] = useState(null)
  const [slot, setSlot] = useState('input_js')
  const [status, setStatus] = useState('')
  const saveTimer = useRef(null)

  // Test-run state
  const [testHook, setTestHook] = useState('input')
  const [testText, setTestText] = useState('> You look around.')
  const [testState, setTestState] = useState('{}')
  const [testResult, setTestResult] = useState(null)

  useEffect(() => {
    api.getScript(id).then(setScript).catch(() => navigate('/scripts'))
  }, [id, navigate])

  const setField = (field, value) => {
    setScript((prev) => ({ ...prev, [field]: value }))
    clearTimeout(saveTimer.current)
    saveTimer.current = setTimeout(async () => {
      await api.updateScript(id, { [field]: value })
      setStatus('Saved')
      setTimeout(() => setStatus(''), 1500)
    }, 600)
  }

  const runTest = async () => {
    let state
    try {
      state = JSON.parse(testState || '{}')
    } catch {
      setTestResult({ error: 'Test state is not valid JSON' })
      return
    }
    try {
      setTestResult(await api.testScript(id, { hook: testHook, text: testText, state }))
    } catch (err) {
      setTestResult({ error: err.message })
    }
  }

  const exportScript = async () => {
    downloadJSON(await api.exportScript(id), `${script.name.replace(/\W+/g, '-')}.json`)
  }

  const deleteScript = async () => {
    if (!confirm('Delete this script permanently?')) return
    await api.deleteScript(id)
    navigate('/scripts')
  }

  if (!script) return null

  const activeSlot = SLOTS.find((s) => s.key === slot)

  return (
    <div className="page">
      <div className="page-header">
        <h1>Edit Script</h1>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span style={{ color: 'var(--text-dim)', fontSize: '0.85rem' }}>{status}</span>
          <button onClick={exportScript}>Export</button>
          <button className="danger" onClick={deleteScript}>Delete</button>
        </div>
      </div>

      <Field label="Name" value={script.name} onChange={(v) => setField('name', v)} />
      <Field label="Description" value={script.description} onChange={(v) => setField('description', v)}
        textarea rows={2} placeholder="What this script does." />

      <div className="slot-tabs">
        {SLOTS.map((s) => (
          <button key={s.key} className={slot === s.key ? 'active' : ''} onClick={() => setSlot(s.key)}>
            {s.label}{script[s.key].trim() ? ' •' : ''}
          </button>
        ))}
      </div>
      <div className="slot-hint">{activeSlot.hint}</div>
      <CodeMirror
        value={script[slot]}
        height="360px"
        theme={oneDark}
        extensions={[javascript()]}
        onChange={(value) => setField(slot, value)}
      />

      <div className="page-header" style={{ marginTop: 26 }}>
        <h2 style={{ margin: 0, fontFamily: 'Georgia, serif', fontSize: '1.2rem' }}>Test Run</h2>
      </div>
      <div className="test-run">
        <div className="row">
          <label className="field" style={{ flex: '0 0 140px' }}>
            <span className="label">Hook</span>
            <select value={testHook} onChange={(e) => setTestHook(e.target.value)}>
              <option value="input">Input</option>
              <option value="context">Context</option>
              <option value="output">Output</option>
            </select>
          </label>
          <label className="field" style={{ flex: 1 }}>
            <span className="label">Sample text</span>
            <input type="text" value={testText} onChange={(e) => setTestText(e.target.value)} />
          </label>
        </div>
        <label className="field">
          <span className="label">State (JSON)</span>
          <input type="text" value={testState} onChange={(e) => setTestState(e.target.value)} />
        </label>
        <button className="primary" onClick={runTest}>Run</button>
        {testResult && (
          <div className="test-result">
            {testResult.error ? (
              <div className="test-error">{testResult.error}</div>
            ) : (
              <>
                <div><span className="label">text</span> <pre>{testResult.text}</pre></div>
                {testResult.stop && <div><span className="label">stop</span> true — the AI call would be skipped</div>}
                <div><span className="label">state</span> <pre>{JSON.stringify(testResult.state)}</pre></div>
                {testResult.storyCards.length > 0 && (
                  <div><span className="label">storyCards</span> <pre>{JSON.stringify(testResult.storyCards, null, 1)}</pre></div>
                )}
                {testResult.logs.length > 0 && (
                  <div><span className="label">logs</span> <pre>{testResult.logs.join('\n')}</pre></div>
                )}
              </>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
