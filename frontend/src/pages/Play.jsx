import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { Field, StoryCardRow } from '../components'

const MODES = ['do', 'say', 'story']
const PLAYER_TYPES = ['do', 'say', 'story']

// Models often emit light markdown emphasis; render **bold** / *italic*
// instead of showing raw asterisks. Everything else stays plain text.
function renderEmphasis(text) {
  const re = /\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g
  const parts = []
  let last = 0
  let match
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index))
    parts.push(match[1] !== undefined
      ? <b key={match.index}>{match[1]}</b>
      : <i key={match.index}>{match[2]}</i>)
    last = match.index + match[0].length
  }
  if (parts.length === 0) return text
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function ReasoningBlock({ text, streaming }) {
  if (!text) return null
  return (
    <details className="reasoning" open={streaming || undefined}>
      <summary>💭 Reasoning{streaming ? '…' : ''}</summary>
      <div className="reasoning-text">{text}</div>
    </details>
  )
}

const SECTION_LABELS = {
  narrator: 'Narrator prompt',
  script_context: 'Script context',
  ai_instructions: 'AI Instructions',
  plot_essentials: 'Plot Essentials',
  story_summary: 'Story Summary',
  used_memories: 'Used Memories (memory bank)',
  world_lore: 'World Lore (story cards)',
  history: 'Story history',
  authors_note: "Author's Note",
  recent_history: 'Recent history',
  front_memory: 'Front memory',
}

function PlotPanel({ adventure, setAdventure }) {
  // One timer per field/card: a single shared timer would cancel the pending
  // save of whatever was edited previously within the debounce window.
  const saveTimers = useRef(new Map())
  const debounceSave = (key, fn) => {
    clearTimeout(saveTimers.current.get(key))
    saveTimers.current.set(key, setTimeout(fn, 600))
  }

  const setField = (field, value) => {
    setAdventure({ ...adventure, [field]: value })
    debounceSave(field, () => api.updateAdventure(adventure.id, { [field]: value }))
  }

  const addCard = async () => {
    const card = await api.createStoryCard({ adventure_id: adventure.id })
    setAdventure({ ...adventure, story_cards: [...adventure.story_cards, card] })
  }

  const updateCard = (card) => {
    setAdventure({
      ...adventure,
      story_cards: adventure.story_cards.map((c) => (c.id === card.id ? card : c)),
    })
    debounceSave(`card-${card.id}`, () => {
      api.updateStoryCard(card.id, {
        name: card.name, type: card.type, keys: card.keys, entry: card.entry, notes: card.notes,
      })
    })
  }

  const deleteCard = async (cardId) => {
    await api.deleteStoryCard(cardId)
    setAdventure({
      ...adventure,
      story_cards: adventure.story_cards.filter((c) => c.id !== cardId),
    })
  }

  return (
    <div>
      <Field label="Plot Essentials (Memory)" value={adventure.memory}
        onChange={(v) => setField('memory', v)} textarea
        placeholder="Key facts the AI should always remember." />
      <Field label="Author's Note" value={adventure.authors_note}
        onChange={(v) => setField('authors_note', v)} textarea rows={2}
        placeholder="Style/theme guidance, injected near the end of context." />
      <Field label="AI Instructions" value={adventure.ai_instructions}
        onChange={(v) => setField('ai_instructions', v)} textarea rows={2}
        placeholder="Behavioral guidance for the model." />
      <Field label="Story Summary" value={adventure.story_summary}
        onChange={(v) => setField('story_summary', v)} textarea
        placeholder="Running summary of events so far. Updated automatically every 15 actions when auto-summarization is on; your edits are kept as the base for the next update." />

      <div className="page-header" style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0 }}>Story Cards</h3>
        <button onClick={addCard}>+ Add</button>
      </div>
      {adventure.story_cards.length === 0 && (
        <div className="empty" style={{ padding: '12px 0' }}>No story cards yet.</div>
      )}
      {adventure.story_cards.map((card) => (
        <StoryCardRow key={card.id} card={card}
          onChange={updateCard} onDelete={() => deleteCard(card.id)} />
      ))}
    </div>
  )
}

function MemoryRow({ memory, onChange, onDelete }) {
  const [editText, setEditText] = useState(null)

  const save = () => {
    const text = editText.trim()
    setEditText(null)
    if (text && text !== memory.text) onChange({ text })
  }

  return (
    <div className={`memory-row ${memory.forgotten ? 'forgotten' : ''}`}>
      {editText !== null ? (
        <div className="action-edit" style={{ margin: 0 }}>
          <textarea autoFocus value={editText} rows={3}
            onChange={(e) => setEditText(e.target.value)} />
          <div className="edit-buttons">
            <button className="primary" onClick={save}>Save</button>
            <button onClick={() => setEditText(null)}>Cancel</button>
          </div>
        </div>
      ) : (
        <>
          <div className="memory-text">{memory.text}</div>
          <div className="memory-meta">
            {memory.pinned && <span className="memory-badge">📌 pinned</span>}
            {memory.forgotten && <span className="memory-badge">forgotten</span>}
            {!memory.embedded && !memory.forgotten && (
              <span className="memory-badge" title="Embedded on the next turn">not embedded yet</span>
            )}
            {memory.source_start != null && (
              <span className="dim">actions {memory.source_start}–{memory.source_end}</span>
            )}
            <span className="dim">used {memory.use_count}×</span>
            <span className="action-tools">
              <button title={memory.pinned ? 'Unpin' : 'Pin (always included in context)'}
                onClick={() => onChange({ pinned: !memory.pinned })}>📌</button>
              {memory.forgotten && (
                <button title="Restore this memory"
                  onClick={() => onChange({ forgotten: false })}>↩</button>
              )}
              <button title="Edit" onClick={() => setEditText(memory.text)}>✎</button>
              <button title="Delete" onClick={onDelete}>✕</button>
            </span>
          </div>
        </>
      )}
    </div>
  )
}

function MemoryPanel({ adventure, setAdventure, refreshKey }) {
  const [memories, setMemories] = useState(null)
  const [newText, setNewText] = useState('')

  const load = useCallback(() => {
    api.listMemories(adventure.id).then(setMemories).catch(() => setMemories([]))
  }, [adventure.id])

  // Summarization runs in the background after a turn, so also refresh shortly after.
  useEffect(() => {
    load()
    const timer = setTimeout(load, 4000)
    return () => clearTimeout(timer)
  }, [load, refreshKey])

  const setFlag = async (field, value) => {
    setAdventure({ ...adventure, [field]: value })
    await api.updateAdventure(adventure.id, { [field]: value })
  }

  const change = async (memory, data) => {
    const updated = await api.updateMemory(adventure.id, memory.id, data)
    setMemories((prev) => prev.map((m) => (m.id === memory.id ? updated : m)))
  }

  const remove = async (memory) => {
    await api.deleteMemory(adventure.id, memory.id)
    setMemories((prev) => prev.filter((m) => m.id !== memory.id))
  }

  const add = async () => {
    const text = newText.trim()
    if (!text) return
    setNewText('')
    const memory = await api.createMemory(adventure.id, text)
    setMemories((prev) => [...prev, memory])
  }

  const active = memories?.filter((m) => !m.forgotten) ?? []
  const forgotten = memories?.filter((m) => m.forgotten) ?? []

  return (
    <div>
      <label className="script-attach">
        <input type="checkbox" checked={adventure.auto_summarize}
          onChange={(e) => setFlag('auto_summarize', e.target.checked)} />
        <span>Auto-summarization</span>
        <span className="dim">memories every 6 actions, Story Summary every 15</span>
      </label>
      <label className="script-attach">
        <input type="checkbox" checked={adventure.memory_bank_enabled}
          onChange={(e) => setFlag('memory_bank_enabled', e.target.checked)} />
        <span>Memory Bank</span>
        <span className="dim">retrieve relevant memories into context (needs an embedding model)</span>
      </label>

      <div className="page-header" style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0 }}>Memories {memories && `(${active.length})`}</h3>
        <button onClick={load}>Refresh</button>
      </div>
      {!memories && <div className="empty" style={{ padding: '12px 0' }}>Loading…</div>}
      {memories && active.length === 0 && (
        <div className="empty" style={{ padding: '12px 0' }}>
          No memories yet. They are generated automatically as you play (from action 12
          onward), or add one below.
        </div>
      )}
      {active.map((m) => (
        <MemoryRow key={m.id} memory={m}
          onChange={(data) => change(m, data)} onDelete={() => remove(m)} />
      ))}

      <div className="input-bar" style={{ marginTop: 10 }}>
        <input type="text" value={newText} placeholder="Add a memory manually…"
          onChange={(e) => setNewText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') add() }} />
        <button onClick={add}>+ Add</button>
      </div>

      {forgotten.length > 0 && (
        <>
          <div className="page-header" style={{ marginTop: 18 }}>
            <h3 style={{ margin: 0 }}>Forgotten ({forgotten.length})</h3>
          </div>
          {forgotten.map((m) => (
            <MemoryRow key={m.id} memory={m}
              onChange={(data) => change(m, data)} onDelete={() => remove(m)} />
          ))}
        </>
      )}
    </div>
  )
}

function ScriptsPanel({ advId }) {
  const [scripts, setScripts] = useState(null)

  useEffect(() => {
    api.listAdventureScripts(advId).then(setScripts).catch(() => setScripts([]))
  }, [advId])

  const toggle = async (script) => {
    const updated = await api.updateAdventureScript(advId, script.id, { enabled: !script.enabled })
    setScripts((prev) => prev.map((s) => (s.id === script.id ? updated : s)))
  }

  if (!scripts) return <div className="empty">Loading…</div>
  if (scripts.length === 0) {
    return (
      <div className="empty">
        No scripts on this adventure. Attach scripts to a scenario before starting an
        adventure from it.
      </div>
    )
  }
  return (
    <div>
      {scripts.map((s) => (
        <label key={s.id} className="script-attach">
          <input type="checkbox" checked={s.enabled} onChange={() => toggle(s)} />
          <span>{s.name}</span>
          <span className="dim">{s.description}</span>
        </label>
      ))}
    </div>
  )
}

function ScriptReport({ script }) {
  if (!script || (!script.logs?.length && !script.errors?.length && !script.context_changed)) {
    return null
  }
  return (
    <div className="script-report">
      <div className="ctx-header" style={{ padding: '4px 0 2px' }}><span>Scripts</span></div>
      {script.errors?.map((e, i) => <div key={i} className="script-error">⚠ {e}</div>)}
      {script.logs?.length > 0 && (
        <pre className="script-logs">{script.logs.join('\n')}</pre>
      )}
      {script.context_changed && (
        <>
          <div className="ctx-section ctx-script_context">
            <div className="ctx-header"><span>Context before script</span></div>
            <pre>{script.context_before}</pre>
          </div>
          <div className="ctx-section ctx-script_context">
            <div className="ctx-header"><span>Context after script (sent to AI)</span></div>
            <pre>{script.context_after}</pre>
          </div>
        </>
      )}
    </div>
  )
}

function InsightsPanel({ advId, inspectActionId, onClearInspect, refreshKey }) {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let stale = false // a slow earlier request must not clobber a newer one
    setError(null)
    const load = inspectActionId
      ? api.getActionContext(advId, inspectActionId)
      : api.getAdventureContext(advId)
    load
      .then((r) => { if (!stale) setReport(r) })
      .catch((err) => { if (!stale) { setReport(null); setError(err.message) } })
    return () => { stale = true }
  }, [advId, inspectActionId, refreshKey])

  if (error) return <div className="empty">{error}</div>
  if (!report) return <div className="empty">Loading…</div>

  const { tokens, cards, history, sections } = report
  const overBudget = tokens.total > tokens.budget

  return (
    <div className="insights">
      <div className="insights-meta">
        {inspectActionId ? (
          <div className="insights-mode">
            Snapshot of a past turn
            <button onClick={onClearInspect} style={{ marginLeft: 8 }}>Back to next turn</button>
          </div>
        ) : (
          <div className="insights-mode">What will be sent on the next turn</div>
        )}
        <div className={`token-total ${overBudget ? 'over' : ''}`}>
          {tokens.total} / {tokens.budget} tokens
        </div>
        <div className="token-bar">
          <div className="token-bar-fill"
            style={{ width: `${Math.min(100, (tokens.total / tokens.budget) * 100)}%` }} />
        </div>
        <div className="insights-history">
          History: {history.included} of {history.total} actions in context
          {history.total > history.included && ' (older history trimmed)'}
          {history.oldest_truncated && ' — oldest entry cut mid-text'}
        </div>
        {cards.length > 0 && (
          <div className="insights-cards">
            {cards.map((c, i) => (
              <div key={i} className={c.included ? '' : 'dropped'}>
                ▸ <b>{c.name || '(unnamed card)'}</b> triggered on “{c.keyword}”
                {!c.included && ' — dropped (over card budget)'}
              </div>
            ))}
          </div>
        )}
        {report.memories && (
          <div className="insights-cards">
            {report.memories.error && (
              <div className="dropped">⚠ Memory bank: {report.memories.error}</div>
            )}
            {report.memories.used?.map((m, i) => (
              <div key={i}>
                ▸ memory retrieved ({m.pinned ? 'pinned' : `similarity ${m.similarity.toFixed(2)}`}):
                {' '}{m.text.length > 90 ? m.text.slice(0, 90) + '…' : m.text}
              </div>
            ))}
            {!report.memories.error && report.memories.used?.length === 0 && (
              <div className="dim">Memory bank on — no memories retrieved.</div>
            )}
          </div>
        )}
      </div>

      {sections.map((s, i) => (
        <div key={i} className={`ctx-section ctx-${s.label}`}>
          <div className="ctx-header">
            <span>{SECTION_LABELS[s.label] || s.label}</span>
            <span>{s.tokens} tok</span>
          </div>
          <pre>{s.text}</pre>
        </div>
      ))}
      <ScriptReport script={report.script} />
    </div>
  )
}

export default function Play() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [adventure, setAdventure] = useState(null)
  const [actions, setActions] = useState([])
  const [mode, setMode] = useState('do')
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(null)
  const [reasoningStream, setReasoningStream] = useState(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)
  const [editing, setEditing] = useState(null)
  const [panel, setPanel] = useState(null) // null | 'plot' | 'insights'
  const [inspectActionId, setInspectActionId] = useState(null)
  const storyEndRef = useRef(null)
  const abortRef = useRef(null)
  const pinnedRef = useRef(true) // autoscroll only while the reader is at the bottom

  useEffect(() => {
    api.getAdventure(id)
      .then((adv) => { setAdventure(adv); setActions(adv.actions) })
      .catch(() => navigate('/'))
  }, [id, navigate])

  useEffect(() => {
    const onScroll = () => {
      pinnedRef.current =
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [])

  useEffect(() => {
    if (pinnedRef.current) {
      storyEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' })
    }
  }, [actions, streaming, reasoningStream])

  const handleScriptReport = useCallback((script) => {
    if (!script) return
    if (script.errors?.length) {
      setToast({ text: `Script error: ${script.errors[0]}`, isError: true })
    } else if (script.message) {
      setToast({ text: script.message, isError: false })
    }
  }, [])

  const handleEvent = useCallback((event) => {
    if (event.type === 'player') {
      setActions((prev) => [...prev, event.action])
    } else if (event.type === 'chunk') {
      setStreaming((prev) => (prev ?? '') + event.text)
    } else if (event.type === 'reasoning') {
      setReasoningStream((prev) => (prev ?? '') + event.text)
    } else if (event.type === 'done') {
      setStreaming(null)
      setReasoningStream(null)
      setActions((prev) => [...prev, event.action])
      handleScriptReport(event.script)
    } else if (event.type === 'stopped') {
      setStreaming(null)
      setReasoningStream(null)
      handleScriptReport(event.script)
    } else if (event.type === 'error') {
      setStreaming(null)
      setReasoningStream(null)
      setToast({ text: event.detail, isError: true })
    }
  }, [handleScriptReport])

  async function runTurn(run) {
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(true)
    setToast(null)
    setStreaming('')
    pinnedRef.current = true
    try {
      await run(controller.signal)
    } catch (err) {
      if (err.name === 'AbortError') {
        setToast({ text: 'Generation stopped.', isError: false })
      } else {
        setToast({ text: err.message, isError: true })
      }
    } finally {
      abortRef.current = null
      setStreaming(null)
      setReasoningStream(null)
      setBusy(false)
    }
  }

  function stopGeneration() {
    abortRef.current?.abort()
  }

  function send(type = mode) {
    const text = input.trim()
    if (type === 'continue') {
      // Continue never consumes typed text — leave it in the box.
      runTurn((signal) => api.sendAction(id, { type: 'continue', text: '' }, handleEvent, signal))
      return
    }
    const payload = { type: text ? type : 'continue', text }
    setInput('')
    runTurn((signal) => api.sendAction(id, payload, handleEvent, signal))
  }

  function retry() {
    setActions((prev) =>
      prev.length && prev[prev.length - 1].type === 'ai' ? prev.slice(0, -1) : prev)
    runTurn(async (signal) => {
      try {
        await api.retry(id, handleEvent, signal)
      } catch (err) {
        // Failed retry (409, network): the optimistically removed action may
        // still exist server-side — resync instead of guessing.
        api.getAdventure(id).then((adv) => setActions(adv.actions)).catch(() => {})
        throw err
      }
    })
  }

  async function undo() {
    setToast(null)
    try {
      setActions(await api.undo(id))
    } catch (err) {
      setToast({ text: err.message, isError: true })
    }
  }

  // Ctrl+Z undo / Ctrl+R retry, ignored while typing in a field.
  useEffect(() => {
    const lastIsAi = actions.length > 0 && actions[actions.length - 1].type === 'ai'
    const canUndo = actions.length > 0 && actions[actions.length - 1].type !== 'start'
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey) || busy) return
      if (e.target.closest?.('input, textarea, select, [contenteditable]')) return
      if (e.key.toLowerCase() === 'z' && canUndo) {
        e.preventDefault()
        undo()
      } else if (e.key.toLowerCase() === 'r' && lastIsAi) {
        e.preventDefault()
        retry()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  async function saveEdit() {
    const { id: actionId, text } = editing
    setEditing(null)
    try {
      const updated = await api.updateAction(id, actionId, text)
      setActions((prev) => prev.map((a) => (a.id === actionId ? updated : a)))
    } catch (err) {
      setToast({ text: err.message, isError: true })
    }
  }

  async function removeAction(actionId) {
    try {
      await api.deleteAction(id, actionId)
      setActions((prev) => prev.filter((a) => a.id !== actionId))
    } catch (err) {
      setToast({ text: err.message, isError: true })
    }
  }

  function inspect(actionId) {
    setInspectActionId(actionId)
    setPanel('insights')
  }

  if (!adventure) return null

  const lastIsAi = actions.length > 0 && actions[actions.length - 1].type === 'ai'
  const canUndo = actions.length > 0 && actions[actions.length - 1].type !== 'start'

  return (
    <div className={`play-layout ${panel ? 'with-panel' : ''}`}>
      <div className="page play-page">
        <div className="page-header">
          <h1>{adventure.title}</h1>
          <div className="panel-toggles">
            <button className={panel === 'plot' ? 'active' : ''}
              onClick={() => setPanel(panel === 'plot' ? null : 'plot')}>Plot</button>
            <button className={panel === 'memory' ? 'active' : ''}
              onClick={() => setPanel(panel === 'memory' ? null : 'memory')}>Memory</button>
            <button className={panel === 'scripts' ? 'active' : ''}
              onClick={() => setPanel(panel === 'scripts' ? null : 'scripts')}>Scripts</button>
            <button className={panel === 'insights' ? 'active' : ''}
              onClick={() => { setInspectActionId(null); setPanel(panel === 'insights' ? null : 'insights') }}>
              Insights
            </button>
          </div>
        </div>

        <div className="story">
          {actions.length === 0 && streaming === null && (
            <div className="empty">A blank page. Type something below to begin your story.</div>
          )}
          {actions.map((action) =>
            editing?.id === action.id ? (
              <div key={action.id} className="action-edit">
                <textarea
                  autoFocus
                  value={editing.text}
                  onChange={(e) => setEditing({ ...editing, text: e.target.value })}
                />
                <div className="edit-buttons">
                  <button className="primary" onClick={saveEdit}>Save</button>
                  <button onClick={() => setEditing(null)}>Cancel</button>
                </div>
              </div>
            ) : (
              <div key={action.id}
                className={`action ${PLAYER_TYPES.includes(action.type) ? 'player' : ''}`}>
                <ReasoningBlock text={action.reasoning} />
                {renderEmphasis(action.text)}
                {!busy && (
                  <span className="action-tools">
                    {action.type === 'ai' && (
                      <button title="View the exact prompt that produced this"
                        onClick={() => inspect(action.id)}>🔍</button>
                    )}
                    <button title="Edit"
                      onClick={() => setEditing({ id: action.id, text: action.text })}>✎</button>
                    <button title="Delete" onClick={() => removeAction(action.id)}>✕</button>
                  </span>
                )}
              </div>
            )
          )}
          {streaming !== null && (
            <div className="action streaming">
              <ReasoningBlock text={reasoningStream} streaming />
              {renderEmphasis(streaming)}
              <span className="cursor">▋</span>
            </div>
          )}
          <div ref={storyEndRef} />
        </div>

        <div className="play-controls">
          <div className="turn-buttons">
            <button onClick={() => send('continue')} disabled={busy}>Continue ▸</button>
            <button onClick={retry} disabled={busy || !lastIsAi} title="Ctrl+R">↻ Retry</button>
            <button onClick={undo} disabled={busy || !canUndo} title="Ctrl+Z">↶ Undo</button>
          </div>
          <div className="input-bar">
            <div className="mode-select">
              {MODES.map((m) => (
                <button key={m} className={mode === m ? 'active' : ''}
                  onClick={() => setMode(m)} disabled={busy}>
                  {m[0].toUpperCase() + m.slice(1)}
                </button>
              ))}
            </div>
            <input
              type="text"
              value={input}
              disabled={busy}
              placeholder={
                mode === 'do' ? 'What do you do?'
                  : mode === 'say' ? 'What do you say?'
                  : 'What happens next?'
              }
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter' && !busy) send() }}
            />
            {busy ? (
              <button className="danger" onClick={stopGeneration}>■ Stop</button>
            ) : (
              <button className="primary" onClick={() => send()}>Send</button>
            )}
          </div>
        </div>
      </div>

      {panel && (
        <div className="side-panel">
          <div className="side-panel-header">
            <h2>{{ plot: 'Plot Components', memory: 'Memory Bank', scripts: 'Scripts', insights: 'Insights' }[panel]}</h2>
            <button onClick={() => setPanel(null)}>✕</button>
          </div>
          {panel === 'plot' ? (
            <PlotPanel adventure={adventure} setAdventure={setAdventure} />
          ) : panel === 'memory' ? (
            <MemoryPanel adventure={adventure} setAdventure={setAdventure}
              refreshKey={actions.length} />
          ) : panel === 'scripts' ? (
            <ScriptsPanel advId={id} />
          ) : (
            <InsightsPanel advId={id} inspectActionId={inspectActionId}
              onClearInspect={() => setInspectActionId(null)} refreshKey={actions.length} />
          )}
        </div>
      )}

      {toast && (
        <div className={`toast ${toast.isError ? '' : 'ok'}`}>
          {toast.text}
          {toast.isError && (
            <button style={{ marginLeft: 12 }} onClick={retry}>Retry</button>
          )}
          <button style={{ marginLeft: 8 }} onClick={() => setToast(null)}>✕</button>
        </div>
      )}
    </div>
  )
}
