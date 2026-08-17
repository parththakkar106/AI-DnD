import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { AutoTextarea, Field, StoryCardRow, downloadJSON, npcInitials, pickJSONFile, useToast } from '../components'

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
  world_state_guide: 'World State (stat guide)',
  world_state: 'World State (RPG)',
  world_state_rule: 'World State (reporting rule)',
  world_lore: 'World Lore (story cards)',
  history: 'Story history',
  authors_note: "Author's Note",
  recent_history: 'Recent history',
  front_memory: 'Front memory',
  length_hint: 'Length guidance',
  world_state_reminder: 'World State (emit reminder)',
}

// One colour per context section, and the single source of truth for it: the
// token bar, the legend and each section's own header all read from here, so a
// slice of the bar and the text it stands for always carry the same colour.
// Related sections share a hue family but never an exact shade — in a stacked
// bar two identical colours read as one section.
const SECTION_COLORS = {
  narrator: '#7d8fc9',
  ai_instructions: '#9c8fd6',
  plot_essentials: '#c97dc0',
  script_context: '#d99ad0',
  story_summary: '#7dc9a2',
  used_memories: '#5fb8c9',
  world_lore: '#c9b47d',
  world_state: '#d79a63',
  world_state_guide: '#b8834a',
  world_state_rule: '#9d7a52',
  world_state_reminder: '#8a6f52',
  history: '#74748c',
  recent_history: '#9d9db4',
  authors_note: '#c97d7d',
  front_memory: '#d99a9a',
  length_hint: '#98a06b',
}
const SECTION_FALLBACK = '#6a6a78'
const sectionColor = (label) => SECTION_COLORS[label] || SECTION_FALLBACK

// Share of the prompt, rounded for glanceability. Sections too small to round
// to a whole percent still say so rather than showing a misleading 0%.
const pctLabel = (pct) => (pct > 0 && pct < 1 ? '<1%' : `${Math.round(pct)}%`)

const FIELD_LABELS = {
  memory: 'Plot Essentials (Memory)',
  authors_note: "Author's Note",
  ai_instructions: 'AI Instructions',
}

function clip(text, n = 90) {
  const one = (text || '').replace(/\s+/g, ' ').trim()
  return one.length > n ? `${one.slice(0, n)}…` : (one || '(empty)')
}

// Confirms "Update from scenario" by showing exactly what it would change, and
// collects any ${Placeholder} answers the adventure has no stored value for
// (adventures started before those were saved, or a placeholder the author
// added since). Destructive by design — it overwrites plot text and
// scenario-derived cards — so nothing happens until Update is pressed.
function RefreshModal({ plan, onConfirm, onCancel }) {
  const [values, setValues] = useState(
    Object.fromEntries((plan.placeholders_needed || []).map((n) => [n, ''])),
  )
  const [busy, setBusy] = useState(false)
  const { added = [], updated = [], removed = [] } = plan.cards || {}
  const world = plan.world_state || {}
  const fields = Object.entries(plan.fields || {})

  const submit = (e) => {
    e.preventDefault()
    setBusy(true)
    onConfirm(values).finally(() => setBusy(false))
  }

  // Portalled to <body>: this modal is opened from inside .side-panel, whose
  // panel-in animation (fill mode `both`) makes it the containing block for
  // position:fixed children — an overlay rendered in place would be trapped in
  // the 420px panel and clipped by its overflow. Same trap for any future modal
  // opened from a drawer or panel.
  return createPortal(
    <div className="modal-overlay" onClick={onCancel}>
      <form className="modal refresh-modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h2>Update from scenario</h2>
        {!plan.has_changes ? (
          <p className="modal-hint">
            This adventure already matches “{plan.scenario_title}”. Nothing to update.
          </p>
        ) : (
          <>
            <p className="modal-hint">
              Pull the current content of “{plan.scenario_title}” down over this adventure.
              Your story, its title, summary and your own story cards are untouched — but
              the plot text below is <b>overwritten</b>, including any edits you made here.
            </p>
            <div className="refresh-changes">
              {fields.map(([field, diff]) => (
                <div key={field} className="refresh-change">
                  <b>{FIELD_LABELS[field] || field}</b>
                  <div className="dim refresh-old">− {clip(diff.old)}</div>
                  <div className="refresh-new">+ {clip(diff.new)}</div>
                </div>
              ))}
              {added.length > 0 && (
                <div className="refresh-change">Story cards added: <b>{added.join(', ')}</b></div>
              )}
              {updated.length > 0 && (
                <div className="refresh-change">Story cards overwritten: <b>{updated.join(', ')}</b></div>
              )}
              {removed.length > 0 && (
                <div className="refresh-change">Story cards removed: <b>{removed.join(', ')}</b></div>
              )}
              {world.added?.length > 0 && (
                <div className="refresh-change">
                  New stats (at their starting value): <b>{world.added.join(', ')}</b>
                </div>
              )}
              {world.removed?.length > 0 && (
                <div className="refresh-change">Stats removed: <b>{world.removed.join(', ')}</b></div>
              )}
            </div>
            {(plan.placeholders_needed || []).length > 0 && (
              <>
                <p className="modal-hint">
                  This scenario asks a few questions, and this adventure has no saved
                  answers for them. They'll be remembered from now on.
                </p>
                {plan.placeholders_needed.map((name) => (
                  <label key={name} className="field">
                    <span className="label">{name}</span>
                    <input type="text" value={values[name]}
                      onChange={(e) => setValues({ ...values, [name]: e.target.value })} />
                  </label>
                ))}
              </>
            )}
          </>
        )}
        <div className="modal-buttons">
          <button type="button" onClick={onCancel}>{plan.has_changes ? 'Cancel' : 'Close'}</button>
          {plan.has_changes && (
            <button type="submit" className="primary" disabled={busy}>
              {busy ? 'Updating…' : 'Update'}
            </button>
          )}
        </div>
      </form>
    </div>,
    document.body,
  )
}

function PlotPanel({ adventure, setAdventure, onWorldStateChanged }) {
  const toast = useToast()
  const [plan, setPlan] = useState(null)      // non-null while the modal is open
  const [planning, setPlanning] = useState(false)
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

  const exportCards = async () => {
    const cards = await api.exportStoryCards({ adventure_id: adventure.id })
    downloadJSON(cards, `${(adventure.title || 'adventure').replace(/\W+/g, '-')}-cards.json`)
  }

  const importCards = async () => {
    try {
      const parsed = await pickJSONFile()
      const cards = Array.isArray(parsed) ? parsed : (parsed.cards || parsed.storyCards)
      if (!Array.isArray(cards)) return toast('Expected a JSON array of story cards.', 'error')
      const created = await api.importStoryCards({ adventure_id: adventure.id, cards })
      setAdventure({ ...adventure, story_cards: [...adventure.story_cards, ...created] })
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  // Ask the server what a refresh would do, then let the player confirm it.
  const openRefresh = async () => {
    setPlanning(true)
    try {
      setPlan(await api.previewRefresh(adventure.id))
    } catch (err) {
      // 404 = the scenario was deleted or unshared; there's nothing to sync to.
      toast(err.message, 'error')
    } finally {
      setPlanning(false)
    }
  }

  const applyRefresh = async (placeholders) => {
    try {
      const updated = await api.refreshFromScenario(adventure.id, placeholders)
      setAdventure(updated)
      setPlan(null)
      onWorldStateChanged?.()
      toast('Updated from scenario.')
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  return (
    <div>
      {adventure.scenario_id != null && (
        <div className="plot-source">
          <span className="dim">
            Copied from its scenario when the adventure began; later scenario edits don't
            reach it on their own.
          </span>
          <button className="linklike" onClick={openRefresh} disabled={planning}
            title="Replace this adventure's plot text and scenario story cards with the scenario's current content">
            {planning ? 'Checking…' : '⟳ Update from scenario'}
          </button>
        </div>
      )}
      {plan && (
        <RefreshModal plan={plan} onConfirm={applyRefresh} onCancel={() => setPlan(null)} />
      )}

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
        <div style={{ display: 'flex', gap: 6 }}>
          <button onClick={exportCards} disabled={adventure.story_cards.length === 0}>Export</button>
          <button onClick={importCards}>Import</button>
          <button onClick={addCard}>+ Add</button>
        </div>
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
          <AutoTextarea autoFocus value={editText} rows={3}
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

const SCRIPT_HOOKS = [
  ['Shared library', 'library_js'],
  ['onInput', 'input_js'],
  ['onModelContext', 'context_js'],
  ['onOutput', 'output_js'],
]

function ScriptsPanel({ advId }) {
  const [scripts, setScripts] = useState(null)
  const [openId, setOpenId] = useState(null)

  useEffect(() => {
    api.listAdventureScripts(advId).then(setScripts).catch(() => setScripts([]))
  }, [advId])

  const [syncingId, setSyncingId] = useState(null)

  const toggle = async (script) => {
    const updated = await api.updateAdventureScript(advId, script.id, { enabled: !script.enabled })
    setScripts((prev) => prev.map((s) => (s.id === script.id ? updated : s)))
  }

  // Pull the latest code from the library script this copy was made from.
  const sync = async (script) => {
    setSyncingId(script.id)
    try {
      const updated = await api.syncAdventureScript(advId, script.id)
      setScripts((prev) => prev.map((s) => (s.id === script.id ? updated : s)))
    } finally {
      setSyncingId(null)
    }
  }

  // Download as an import-compatible bundle (matches /scripts export), so demo
  // scripts can be forked into your own library via the Scripts page's Import.
  const download = (s) => {
    downloadJSON(
      {
        name: s.name,
        description: s.description,
        library: s.library_js,
        input: s.input_js,
        context: s.context_js,
        output: s.output_js,
      },
      `${(s.name || 'script').replace(/\W+/g, '-')}.json`,
    )
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
      {scripts.map((s) => {
        const hooks = SCRIPT_HOOKS.filter(([, f]) => (s[f] || '').trim())
        const open = openId === s.id
        return (
          <div key={s.id} className="adv-script">
            <div className="adv-script-head">
              <label className="script-attach" style={{ margin: 0 }}>
                <input type="checkbox" checked={s.enabled} onChange={() => toggle(s)} />
                <span>{s.name}</span>
              </label>
              <div style={{ display: 'flex', gap: 12, flexShrink: 0 }}>
                {s.out_of_date && (
                  <button
                    className="linklike"
                    title="Replace this adventure's copy with the latest from your script library"
                    disabled={syncingId === s.id}
                    onClick={() => sync(s)}
                  >
                    {syncingId === s.id ? 'Syncing…' : '⟳ Sync from library'}
                  </button>
                )}
                <button className="linklike" onClick={() => setOpenId(open ? null : s.id)}>
                  {open ? 'Hide code' : 'View code'}
                </button>
                <button className="linklike" onClick={() => download(s)}>Download</button>
              </div>
            </div>
            {s.description && <div className="dim adv-script-desc">{s.description}</div>}
            {open && (
              <div className="adv-script-code">
                {hooks.length === 0 ? (
                  <div className="empty">This script has no code.</div>
                ) : (
                  hooks.map(([label, f]) => (
                    <div key={f}>
                      <div className="adv-script-hook">{label}</div>
                      <pre>{s[f]}</pre>
                    </div>
                  ))
                )}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// Renders one script-state value: primitives inline (typed/coloured), and
// objects/arrays as a collapsible, indented tree — recursing into nesting so
// deep state shows structure instead of a flat JSON blob.
function StateValue({ value, depth = 0 }) {
  if (value === null || value === undefined) return <span className="jv-null">null</span>
  if (typeof value === 'boolean') return <span className="jv-bool">{String(value)}</span>
  if (typeof value === 'number') return <span className="jv-number">{value}</span>
  if (typeof value === 'string') return <span className="jv-string">{value}</span>
  if (Array.isArray(value)) return <StateTree entries={value.map((v, i) => [i, v])} empty="[ ]" depth={depth} />
  if (typeof value === 'object') return <StateTree entries={Object.entries(value)} empty="{ }" depth={depth} />
  return <span className="jv-string">{String(value)}</span>
}

// Only the first level is expanded by default (depth 0); nested trees start
// collapsed and can be opened on demand.
function StateTree({ entries, empty, depth = 0 }) {
  const [open, setOpen] = useState(depth < 1)
  if (entries.length === 0) return <span className="jv-empty">{empty}</span>
  return (
    <div className="jv-tree">
      <button className="jv-toggle" onClick={() => setOpen((o) => !o)}>
        {open ? '▾' : '▸'} {entries.length} {entries.length === 1 ? 'item' : 'items'}
      </button>
      {open && (
        <ul className="jv-children">
          {entries.map(([k, v]) => (
            <li key={k}>
              <span className="jv-key">{k}</span>
              <StateValue value={v} depth={depth + 1} />
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

// Collapsible left rail showing the scripting `state` object — every variable
// scripts read/write via state.x, refreshed after each turn.
function StatusDrawer({ advId, refreshKey }) {
  const [open, setOpen] = useState(false)
  const [state, setState] = useState(null)
  const [failed, setFailed] = useState(false)

  const load = useCallback(() => {
    api.getScriptState(advId)
      .then((r) => { setState(r.state || {}); setFailed(false) })
      .catch(() => setFailed(true))
  }, [advId])

  // Only fetch while open; re-fetch after each turn so values stay live.
  useEffect(() => { if (open) load() }, [open, refreshKey, load])

  const entries = state ? Object.entries(state) : []

  return (
    <div className={`status-drawer ${open ? 'open' : ''}`}>
      <button className="status-toggle" onClick={() => setOpen((o) => !o)}
        title="Script state variables">
        {open ? '‹' : '›'}<span className="status-toggle-label">State</span>
      </button>
      {open && (
        <div className="status-body">
          <div className="side-panel-header">
            <h2>Script State</h2>
            <button onClick={load} title="Refresh">↻</button>
          </div>
          {failed ? (
            <div className="empty">Couldn’t load state.</div>
          ) : entries.length === 0 ? (
            <div className="empty">
              No variables yet. Scripts that use <code>state</code> will appear here after a turn.
            </div>
          ) : (
            <ul className="status-vars">
              {entries.map(([k, v]) => (
                <li key={k}>
                  <span className="status-key">{k}</span>
                  <div className="status-val"><StateValue value={v} /></div>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}

// Word label for a value from a stat def's bands (mirrors worldstate.band_label).
function bandLabel(def, value) {
  const bands = def?.bands
  if (!Array.isArray(bands) || typeof value !== 'number') return null
  for (const b of bands) {
    if (Array.isArray(b) && b.length === 3 && value >= b[0] && value < b[1]) return b[2]
  }
  const last = bands[bands.length - 1]
  if (last && value === last[1]) return last[2]
  return null
}

function StatRow({ name, def, value, editing, onChange }) {
  const isText = def?.type === 'text'
  if (editing) {
    return (
      <div className="ws-stat">
        <div className="ws-stat-head">
          <span className="ws-stat-name" title={def?.desc || undefined}>{name}</span>
        </div>
        {isText ? (
          <input className="ws-edit-input ws-edit-text" type="text" value={value ?? ''}
            onChange={(e) => onChange(e.target.value)} />
        ) : (
          <input className="ws-edit-input ws-edit-num" type="number" value={value ?? 0}
            min={typeof def?.min === 'number' ? def.min : undefined}
            max={typeof def?.max === 'number' ? def.max : undefined}
            onChange={(e) => onChange(e.target.value === '' ? 0 : Number(e.target.value))} />
        )}
      </div>
    )
  }
  if (isText) {
    const text = typeof value === 'string' ? value : (def?.initial ?? '')
    return (
      <div className="ws-stat">
        <div className="ws-stat-head">
          <span className="ws-stat-name" title={def?.desc || undefined}>{name}</span>
          <span className="ws-stat-val">{text || '(unset)'}</span>
        </div>
      </div>
    )
  }
  const val = typeof value === 'number' ? value : (def?.initial ?? 0)
  const { min, max } = def || {}
  const hasRange = typeof min === 'number' && typeof max === 'number' && max > min
  const pct = hasRange ? Math.max(0, Math.min(100, ((val - min) / (max - min)) * 100)) : null
  const label = bandLabel(def, val)
  return (
    <div className="ws-stat">
      <div className="ws-stat-head">
        <span className="ws-stat-name" title={def?.desc || undefined}>{name}</span>
        <span className="ws-stat-val">
          {val}{typeof max === 'number' ? `/${max}` : ''}
          {label ? <span className="ws-band"> · {label}</span> : null}
        </span>
      </div>
      {pct != null && (
        <div className="ws-bar"><div className="ws-bar-fill" style={{ width: `${pct}%` }} /></div>
      )}
    </div>
  )
}

// `values`/`draft` are both plain {statName: value} maps — `draft` (edit mode)
// is a slice of the drawer's flat path->value map for this group's prefix.
// `nested` = the caller already drew a heading (an NPC card), so this drops the
// group's own spacing and says "nothing here" rather than vanishing and leaving
// that heading dangling over empty space.
function StatGroup({ title, defs, values, desc, editing, draft, onEdit, nested }) {
  const entries = Object.entries(defs || {}).filter(([, d]) => d && typeof d === 'object')
  if (entries.length === 0) {
    return nested ? <div className="ws-none">No tracked stats.</div> : null
  }
  return (
    <div className={nested ? 'ws-stats' : 'ws-group'}>
      {title && <h3 className="ws-group-title" title={desc || undefined}>{title}</h3>}
      {entries.map(([name, def]) => (
        <StatRow key={name} name={name} def={def}
          value={editing ? draft?.[name] : values?.[name]}
          editing={editing}
          onChange={editing ? (v) => onEdit(name, v) : undefined} />
      ))}
    </div>
  )
}

// Flatten a schema+state into path->value pairs ("player.hp", "npc.gwen.trust",
// "flags.has_key", "milestones.escaped") to seed an edit-mode draft.
function buildWorldStateDraft(schema, state) {
  const draft = {}
  const addStats = (prefix, defs, values) => {
    for (const [name, def] of Object.entries(defs || {})) {
      if (!def || typeof def !== 'object') continue
      const fallback = def.type === 'text' ? '' : 0
      draft[`${prefix}.${name}`] = values?.[name] ?? def.initial ?? fallback
    }
  }
  addStats('world', schema.world, state.world)
  addStats('player', schema.player, state.player)
  const npcState = state.npc || {}
  for (const [id, ndef] of Object.entries(schema.npcs || {})) {
    addStats(`npc.${id}`, ndef?.stats, npcState[id])
  }
  const flagState = state.flags || {}
  for (const [name, def] of Object.entries(schema.flags || {})) {
    draft[`flags.${name}`] = flagState[name] ?? !!def?.initial
  }
  const reached = state.milestones || {}
  for (const mid of Object.keys(schema.milestones || {})) {
    draft[`milestones.${mid}`] = !!reached[mid]?.reached
  }
  return draft
}

// Slice a flat path->value draft down to one group's {statName: value} map.
function sliceDraft(draft, prefix) {
  const out = {}
  const p = `${prefix}.`
  for (const [k, v] of Object.entries(draft || {})) {
    if (k.startsWith(p)) out[k.slice(p.length)] = v
  }
  return out
}

// Collapsible left rail showing the RPG world state (Phase 12): world/player/NPC
// stats with bands + bars, and a milestones checklist. Renders nothing unless
// the adventure's scenario defines a stat_schema. An edit mode lets the
// player/author directly override the live values (a manual correction, not
// a turn) — it never adds new stats, only edits ones the schema already defines.
function WorldStateDrawer({ advId, refreshKey }) {
  const [open, setOpen] = useState(false)
  const [data, setData] = useState(null) // { state, schema }
  const [failed, setFailed] = useState(false)
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(null) // flat path->value, only while editing
  const [saving, setSaving] = useState(false)

  const load = useCallback(() => {
    api.getWorldState(advId)
      .then((r) => { setData(r); setFailed(false) })
      .catch(() => setFailed(true))
  }, [advId])

  // Load once to learn whether there's an RPG layer, then refresh after turns.
  useEffect(() => { load() }, [load, refreshKey])

  const schema = data?.schema
  if (!schema) return null // no RPG layer for this adventure

  const state = data?.state || {}
  const npcState = state.npc || {}
  const npcs = Object.entries(schema.npcs || {}) // [id, def] — each with its own stats
  const flags = Object.entries(schema.flags || {})
  const flagState = state.flags || {}
  const milestones = Object.entries(schema.milestones || {})
  const reached = state.milestones || {}

  const startEdit = () => { setDraft(buildWorldStateDraft(schema, state)); setEditing(true) }
  const cancelEdit = () => { setEditing(false); setDraft(null) }
  const setPath = (path, value) => setDraft((d) => ({ ...d, [path]: value }))
  const saveEdit = () => {
    setSaving(true)
    api.overrideWorldState(advId, draft)
      .then(() => { setEditing(false); setDraft(null); load() })
      .catch(() => setFailed(true))
      .finally(() => setSaving(false))
  }

  return (
    <div className={`status-drawer ws-drawer ${open ? 'open' : ''}`}>
      <button className="status-toggle" onClick={() => setOpen((o) => !o)}
        title="RPG world state">
        {open ? '‹' : '›'}<span className="status-toggle-label">World</span>
      </button>
      {open && (
        <div className="status-body">
          <div className="side-panel-header">
            <h2>World State</h2>
            {editing ? (
              <div className="ws-edit-actions">
                <button onClick={cancelEdit} disabled={saving} title="Discard changes">Cancel</button>
                <button onClick={saveEdit} disabled={saving} title="Save overrides">
                  {saving ? 'Saving…' : 'Save'}
                </button>
              </div>
            ) : (
              <div className="ws-edit-actions">
                <button onClick={startEdit} title="Edit current values">✎</button>
                <button onClick={load} title="Refresh">↻</button>
              </div>
            )}
          </div>
          {failed ? (
            <div className="empty">Couldn’t load world state.</div>
          ) : (
            <>
              <StatGroup title={null} defs={schema.world} values={state.world}
                editing={editing} draft={sliceDraft(draft, 'world')}
                onEdit={(name, v) => setPath(`world.${name}`, v)} />
              <StatGroup title="You" defs={schema.player} values={state.player}
                editing={editing} draft={sliceDraft(draft, 'player')}
                onEdit={(name, v) => setPath(`player.${name}`, v)} />
              {npcs.length > 0 && (
                <div className="ws-group">
                  <h3 className="ws-group-title">Cast</h3>
                  {npcs.map(([id, def]) => (
                    <div key={id} className="ws-npc">
                      <div className="ws-npc-head" title={def.desc || undefined}>
                        <span className="ws-npc-avatar" aria-hidden="true">
                          {npcInitials(def.name, id)}
                        </span>
                        <span className="ws-npc-name">{def.name || id}</span>
                      </div>
                      <StatGroup defs={def.stats} values={npcState[id]}
                        editing={editing} draft={sliceDraft(draft, `npc.${id}`)}
                        onEdit={(name, v) => setPath(`npc.${id}.${name}`, v)}
                        nested />
                    </div>
                  ))}
                </div>
              )}
              {flags.length > 0 && (
                <div className="ws-group">
                  <h3 className="ws-group-title">Flags</h3>
                  <ul className="ws-flags">
                    {flags.map(([fid, def]) => {
                      const on = editing ? !!draft?.[`flags.${fid}`] : (flagState[fid] ?? !!def.initial)
                      return (
                        <li key={fid} title={def.desc || undefined}>
                          {editing ? (
                            <label className="ws-flag-edit">
                              <input type="checkbox" checked={on}
                                onChange={(e) => setPath(`flags.${fid}`, e.target.checked)} />
                              <span className="ws-flag-name">{fid}</span>
                            </label>
                          ) : (
                            <>
                              <span className={`ws-flag-dot ${on ? 'on' : ''}`} />
                              <span className="ws-flag-name">{fid}</span>
                              <span className="ws-flag-val">{on ? 'yes' : 'no'}</span>
                            </>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </div>
              )}
              {milestones.length > 0 && (
                <div className="ws-group">
                  <h3 className="ws-group-title">Milestones</h3>
                  <ul className="ws-milestones">
                    {milestones.map(([mid, def]) => {
                      const done = editing ? !!draft?.[`milestones.${mid}`] : !!reached[mid]?.reached
                      return (
                        <li key={mid} className={done ? 'done' : ''}>
                          {editing ? (
                            <label className="ws-flag-edit">
                              <input type="checkbox" checked={done}
                                onChange={(e) => setPath(`milestones.${mid}`, e.target.checked)} />
                              <span>{def.desc || mid}</span>
                            </label>
                          ) : (
                            <>
                              <span className="ws-check">{done ? '☑' : '☐'}</span>
                              {def.desc || mid}
                            </>
                          )}
                        </li>
                      )
                    })}
                  </ul>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

// ChatGPT-style ‹ 2/3 › pager under an AI message that has been retried.
//
// The last message can actually be switched (the server restores the stats that
// attempt produced); earlier ones are read-only, because the turns after them
// were written as a continuation of whatever is active now. Browsing an
// earlier one is a local preview, so `onPreview` hands the text up to the story
// renderer rather than the pager drawing it.
function VariantPager({ advId, action, isLast, busy, previewIndex, onPreview, onSwitched, onError }) {
  const [variants, setVariants] = useState(null)
  const [loading, setLoading] = useState(false)
  const count = action.variant_count
  const current = previewIndex ?? action.variant_index

  async function go(delta) {
    const next = current + delta
    if (next < 0 || next >= count || loading || busy) return
    setLoading(true)
    try {
      if (isLast) {
        onPreview(null)
        onSwitched(await api.selectVariant(advId, action.id, next))
      } else {
        // Fetched once per message, then cached — paging back and forth
        // shouldn't re-hit the server.
        const list = variants || await api.listVariants(advId, action.id)
        if (!variants) setVariants(list)
        onPreview(next === action.variant_index
          ? null
          : { actionId: action.id, index: next, text: list[next].text, reasoning: list[next].reasoning })
      }
    } catch (err) {
      onError(err.message)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="variant-pager">
      <button type="button" onClick={() => go(-1)} disabled={current === 0 || busy || loading}
        title="Previous attempt">‹</button>
      <span className="variant-count">{current + 1}/{count}</span>
      <button type="button" onClick={() => go(1)} disabled={current === count - 1 || busy || loading}
        title="Next attempt">›</button>
      {previewIndex !== null && (
        <span className="variant-note">
          earlier attempt — the story continued from {action.variant_index + 1}
        </span>
      )}
    </div>
  )
}

// Compact chips shown under an AI message summarizing what state changed.
function StateChangeChips({ changes }) {
  if (!changes?.length) return null
  const nice = (s) => String(s).replace(/_/g, ' ')
  return (
    <div className="turn-changes">
      {changes.map((c, i) => {
        if (c.kind === 'flag') {
          return <span key={i} className="chg chg-flag">{nice(c.label)}: {c.on ? 'on' : 'off'}</span>
        }
        if (c.kind === 'milestone') {
          return <span key={i} className="chg chg-ms">✓ {nice(c.label)}</span>
        }
        const d = c.delta
        const dir = typeof d === 'number' ? (d > 0 ? 'up' : d < 0 ? 'down' : 'flat') : 'flat'
        const txt = typeof d === 'number' ? (d > 0 ? `+${d}` : `${d}`) : `→ ${c.value}`
        return <span key={i} className={`chg chg-stat chg-${dir}`}>{nice(c.label)} {txt}</span>
      })}
    </div>
  )
}

// Per-turn RPG state change, shown when inspecting a past turn's snapshot.
function WorldStateReport({ worldState }) {
  if (!worldState) return null
  const delta = worldState.delta || {}
  const report = worldState.report || {}
  const paths = Object.keys(delta)
  const rejected = report.rejected || []
  const clamped = new Set((report.clamped || []).map((c) => c.path))
  if (paths.length === 0 && rejected.length === 0) {
    return (
      <div className="script-report">
        <div className="ctx-header" style={{ padding: '4px 0 2px' }}><span>World State</span></div>
        <div className="dim" style={{ fontSize: '0.82rem' }}>No changes this turn.</div>
      </div>
    )
  }
  return (
    <div className="script-report">
      <div className="ctx-header" style={{ padding: '4px 0 2px' }}><span>World State changes</span></div>
      <ul className="ws-report">
        {paths.map((p) => (
          <li key={p}>
            <code>{p}</code> <span className="ws-delta">{String(delta[p])}</span>
            {clamped.has(p) && <span className="ws-flag"> clamped</span>}
          </li>
        ))}
        {rejected.map((r, i) => (
          <li key={`r${i}`} className="ws-rejected">
            <code>{r.path}</code> rejected — {r.reason}
          </li>
        ))}
      </ul>
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
          <div className="ctx-section" style={{ borderLeftColor: sectionColor('script_context') }}>
            <div className="ctx-header" style={{ color: sectionColor('script_context') }}>
              <span>Context before script</span>
            </div>
            <pre>{script.context_before}</pre>
          </div>
          <div className="ctx-section" style={{ borderLeftColor: sectionColor('script_context') }}>
            <div className="ctx-header" style={{ color: sectionColor('script_context') }}>
              <span>Context after script (sent to AI)</span>
            </div>
            <pre>{script.context_after}</pre>
          </div>
        </>
      )}
    </div>
  )
}

const LEGEND_VISIBLE = 6 // enough to cover what actually moves the budget

// Where the prompt's tokens actually went: one stacked bar scaled to the
// budget (so leftover width IS the remaining headroom) plus a matching legend.
// Bar and legend both run biggest-share-first — the order that answers "what is
// eating my context?" — rather than the prompt order the sections below use.
function TokenBreakdown({ sections, tokens, used, onJump }) {
  const [hovered, setHovered] = useState(null)
  const [expanded, setExpanded] = useState(false)
  if (used <= 0) return null
  const budget = tokens.budget || 0
  // Over budget there is no headroom to draw, so the bar scales to what the
  // prompt actually costs and the total line above it carries the warning.
  const scale = Math.max(budget, used)
  const free = Math.max(0, budget - used)
  const ranked = sections
    .map((s, i) => ({ ...s, i, pct: (s.tokens / used) * 100 }))
    .filter((s) => s.tokens > 0)
    .sort((a, b) => b.tokens - a.tokens)
  // The panel is narrow, so the legend is one column: list the sections that
  // actually move the budget and fold the long tail behind a count.
  const shown = expanded ? ranked : ranked.slice(0, LEGEND_VISIBLE)
  const rest = ranked.slice(shown.length)
  const restPct = rest.reduce((n, s) => n + s.pct, 0)
  const describe = (s) =>
    `${SECTION_LABELS[s.label] || s.label} — ${s.tokens} tok · ${pctLabel(s.pct)}`

  return (
    <>
      <div
        className={`token-split ${tokens.total > budget ? 'over' : ''}`}
        role="img"
        aria-label={`Context breakdown: ${ranked.map(describe).join(', ')}`}
      >
        {ranked.map((s) => (
          <div
            key={s.i}
            className={`token-seg ${hovered !== null && hovered !== s.i ? 'faded' : ''}`}
            style={{ width: `${(s.tokens / scale) * 100}%`, background: sectionColor(s.label) }}
            title={describe(s)}
            onMouseEnter={() => setHovered(s.i)}
            onMouseLeave={() => setHovered(null)}
          />
        ))}
        {free > 0 && (
          <div
            className="token-seg token-seg-free"
            style={{ width: `${(free / scale) * 100}%` }}
            title={`${free} tokens of budget unused`}
          />
        )}
      </div>
      <div className="token-legend">
        {shown.map((s) => (
          <button
            key={s.i}
            type="button"
            className={`token-legend-item ${hovered !== null && hovered !== s.i ? 'faded' : ''}`}
            title={`${describe(s)} — click to jump to this section`}
            onMouseEnter={() => setHovered(s.i)}
            onMouseLeave={() => setHovered(null)}
            onClick={() => onJump(s.i)}
          >
            <span className="token-swatch" style={{ background: sectionColor(s.label) }} />
            <span className="token-legend-name">{SECTION_LABELS[s.label] || s.label}</span>
            <span className="token-legend-pct">{pctLabel(s.pct)}</span>
            <span className="token-legend-tok">{s.tokens}</span>
          </button>
        ))}
        {rest.length > 0 && (
          <button type="button" className="token-legend-more" onClick={() => setExpanded(true)}>
            + {rest.length} smaller {rest.length === 1 ? 'section' : 'sections'} ({pctLabel(restPct)})
          </button>
        )}
        {expanded && ranked.length > LEGEND_VISIBLE && (
          <button type="button" className="token-legend-more" onClick={() => setExpanded(false)}>
            Show less
          </button>
        )}
      </div>
    </>
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
  // The per-section sum, not tokens.total: the total also counts the separators
  // between sections, and percentages have to add up to 100 for the reader.
  const sectionTotal = sections.reduce((n, s) => n + s.tokens, 0)
  const jumpToSection = (i) => {
    document.getElementById(`ctx-sec-${i}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

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
        <TokenBreakdown
          sections={sections}
          tokens={tokens}
          used={sectionTotal}
          onJump={jumpToSection}
        />
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
        <div
          key={i}
          id={`ctx-sec-${i}`}
          className={`ctx-section ctx-${s.label}`}
          style={{ borderLeftColor: sectionColor(s.label) }}
        >
          <div className="ctx-header" style={{ color: sectionColor(s.label) }}>
            <span>{SECTION_LABELS[s.label] || s.label}</span>
            <span>
              {s.tokens} tok
              {sectionTotal > 0 && ` · ${pctLabel((s.tokens / sectionTotal) * 100)}`}
            </span>
          </div>
          <pre>{s.text}</pre>
        </div>
      ))}
      <ScriptReport script={report.script} />
      <WorldStateReport worldState={report.world_state} />
      {report.raw_output && (
        <div className="ctx-section">
          <div className="ctx-header">
            <span>Raw AI output (before state block was stripped)</span>
          </div>
          <pre>{report.raw_output}</pre>
        </div>
      )}
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
  // Bumped when something outside the turn loop changes the drawers' state
  // (currently "Update from scenario"), which no action count would reflect.
  const [stateKey, setStateKey] = useState(0)
  const [inspectActionId, setInspectActionId] = useState(null)
  // Read-only browsing of an earlier attempt at a past turn (see VariantPager).
  // One at a time; null when every message is showing its active version.
  const [preview, setPreview] = useState(null)
  // The transcript is a window on the story, not the whole of it: the page
  // load brings the newest page and older ones arrive as the reader scrolls
  // up. `total` is the story's real length, for the "N earlier" line.
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const storyEndRef = useRef(null)
  const abortRef = useRef(null)
  const pinnedRef = useRef(true) // autoscroll only while the reader is at the bottom
  const inputRef = useRef(null)
  // Set just before older actions are prepended, read once afterwards to put
  // the reader back where they were. See the layout effect below.
  const restoreScrollRef = useRef(null)
  const loadingOlderRef = useRef(false)

  // The drop cap belongs to the story's first narrated beat. `start` is the
  // scenario's opening prompt, so it's usually that; an adventure begun blank
  // has no `start` action and the first AI reply takes it instead. Tracked by
  // id rather than position so deleting earlier turns moves the cap correctly
  // instead of stranding it on a removed row.
  const firstNarrationId = useMemo(
    () => actions.find((a) => a.type === 'start' || a.type === 'ai')?.id ?? null,
    [actions],
  )
  // send() sets streaming to '' before the request goes out; reasoningStream
  // stays null until reasoning tokens (if any) arrive. Both still at those
  // values means the request is in flight with nothing to show yet.
  const waitingForFirstToken = streaming === '' && reasoningStream === null

  // Grow the action box with its content (CSS caps it at ~4 lines, then
  // scrolls); shrinks back after send() clears the text.
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [input])

  useEffect(() => {
    api.getAdventure(id)
      .then((adv) => {
        setAdventure(adv)
        setActions(adv.actions)
        setTotal(adv.action_count ?? adv.actions.length)
        setHasMore(adv.actions.length < (adv.action_count ?? adv.actions.length))
      })
      .catch(() => navigate('/'))
  }, [id, navigate])

  // Fetch the page above the one on screen and prepend it.
  //
  // Anchored on the oldest action we hold rather than on a count, so a turn
  // landing while the reader scrolls cannot shift the page. Guarded by a ref
  // as well as state because scroll fires far faster than React re-renders,
  // and two in-flight requests would fetch the same page twice.
  const loadOlder = useCallback(async () => {
    if (loadingOlderRef.current || !hasMore) return
    const oldest = actions[0]
    if (!oldest) return
    loadingOlderRef.current = true
    setLoadingOlder(true)
    try {
      const page = await api.getActions(id, { beforeId: oldest.id })
      if (page.actions.length) {
        // Record the height before the prepend; the layout effect below uses
        // it to keep the reader looking at the same paragraph.
        restoreScrollRef.current = {
          height: document.documentElement.scrollHeight,
          top: window.scrollY,
        }
        setActions((prev) => {
          // Defensive: never let a page the reader already holds duplicate a
          // message. Cheap, and the alternative is a visibly doubled turn.
          const known = new Set(prev.map((a) => a.id))
          return [...page.actions.filter((a) => !known.has(a.id)), ...prev]
        })
      }
      setTotal(page.total)
      setHasMore(page.has_more)
    } catch {
      // Leave hasMore alone: a failed fetch should let the reader try again
      // by scrolling, not permanently hide the rest of their story.
    } finally {
      loadingOlderRef.current = false
      setLoadingOlder(false)
    }
  }, [actions, hasMore, id])

  // Put the viewport back after a prepend. useLayoutEffect, not useEffect:
  // this has to run before the browser paints, or the reader sees the story
  // jump and then snap back.
  useLayoutEffect(() => {
    const mark = restoreScrollRef.current
    if (!mark) return
    restoreScrollRef.current = null
    const grown = document.documentElement.scrollHeight - mark.height
    if (grown > 0) window.scrollTo({ top: mark.top + grown })
  }, [actions])

  useEffect(() => {
    const onScroll = () => {
      pinnedRef.current =
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120
      // Start the next page before the reader reaches the top, so the story
      // is usually already there by the time they would have noticed its end.
      if (window.scrollY < 400) loadOlder()
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [loadOlder])

  useEffect(() => {
    // Snap to the real document bottom (below the sticky composer), not to
    // storyEndRef — that ref sits above the composer, so block:'end' would
    // stop short and fight a reader scrolling down. Instant, not smooth: at
    // streaming speed a queued smooth animation never settles.
    if (pinnedRef.current) {
      window.scrollTo({ top: document.documentElement.scrollHeight })
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
      // The window grew at the bottom, so the story did too. Kept in step by
      // hand because nothing re-reads the count between turns.
      setTotal((n) => n + 1)
    } else if (event.type === 'chunk') {
      setStreaming((prev) => (prev ?? '') + event.text)
    } else if (event.type === 'reasoning') {
      setReasoningStream((prev) => (prev ?? '') + event.text)
    } else if (event.type === 'done') {
      setStreaming(null)
      setReasoningStream(null)
      setActions((prev) => [...prev, event.action])
      setTotal((n) => n + 1)
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
    setPreview(null)
    setActions((prev) =>
      prev.length && prev[prev.length - 1].type === 'ai' ? prev.slice(0, -1) : prev)
    runTurn(async (signal) => {
      try {
        await api.retry(id, handleEvent, signal)
      } catch (err) {
        // Failed retry (409, network): the optimistically removed action may
        // still exist server-side — resync instead of guessing. Resyncing
        // collapses the transcript back to the newest window, which is the
        // right call: the reader's place is already lost by the failure.
        api.getAdventure(id).then((adv) => {
          setActions(adv.actions)
          setTotal(adv.action_count ?? adv.actions.length)
          setHasMore(adv.actions.length < (adv.action_count ?? adv.actions.length))
        }).catch(() => {})
        throw err
      }
    })
  }

  async function undo() {
    setToast(null)
    setPreview(null)
    try {
      // A window, not the whole story — undo is the action most likely to be
      // repeated several times running, so it must not re-fetch everything.
      const page = await api.undo(id)
      setActions(page.actions)
      setTotal(page.total)
      setHasMore(page.has_more)
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
      setTotal((n) => Math.max(0, n - 1))
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
      <WorldStateDrawer advId={id} refreshKey={`${actions.length}:${stateKey}`} />
      <StatusDrawer advId={id} refreshKey={actions.length} />
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
          {/* Scrolling up loads the rest. The button is not decoration: on a
              short viewport the story may not be tall enough to scroll at all,
              and a reader who cannot scroll must still be able to get back to
              the beginning. */}
          {hasMore && (
            <div className="story-earlier">
              {loadingOlder ? (
                <span className="dim">Turning back the pages…</span>
              ) : (
                <button type="button" onClick={loadOlder}>
                  {Math.max(total - actions.length, 0)} earlier
                  {total - actions.length === 1 ? ' moment' : ' moments'}
                </button>
              )}
            </div>
          )}
          {actions.map((action, i) => {
            const isPlayer = PLAYER_TYPES.includes(action.type)
            // A player action opens a new turn, so that's where the ornamental
            // break belongs — never above the very first line on the page.
            const sceneBreak = isPlayer && i > 0
            // Drop cap goes on the first narrated beat only. `firstNarrationId`
            // is derived once above rather than per row.
            const opening = action.id === firstNarrationId
            // Non-null while the reader is browsing an older attempt of this
            // message without making it active (earlier turns only).
            const previewing = preview?.actionId === action.id ? preview : null

            return editing?.id === action.id ? (
              <div key={action.id} className="action-edit">
                <AutoTextarea
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
              <Fragment key={action.id}>
                {sceneBreak && <div className="scene-break" aria-hidden="true">❖</div>}
                <div className={`action ${isPlayer ? 'player' : ''}${opening ? ' opening' : ''}`}>
                  <ReasoningBlock text={previewing ? previewing.reasoning : action.reasoning} />
                  {renderEmphasis(previewing ? previewing.text : action.text)}
                  {/* The chips describe the *active* attempt's state changes,
                      which a previewed one didn't make — so they're hidden
                      rather than shown against the wrong text. */}
                  {action.type === 'ai' && !previewing && (
                    <StateChangeChips changes={action.world_changes} />
                  )}
                  {action.type === 'ai' && action.variant_count > 1 && (
                    <VariantPager
                      advId={id}
                      action={action}
                      isLast={i === actions.length - 1}
                      busy={busy}
                      previewIndex={previewing ? previewing.index : null}
                      onPreview={setPreview}
                      onSwitched={(updated) =>
                        setActions((prev) => prev.map((a) => (a.id === updated.id ? updated : a)))}
                      onError={(message) => setToast({ text: message, isError: true })}
                    />
                  )}
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
              </Fragment>
            )
          })}
          {waitingForFirstToken && (
            <div className="thinking" role="status">
              <i /><i /><i />
              <span>Weaving</span>
            </div>
          )}
          {streaming !== null && !waitingForFirstToken && (
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
            <textarea
              ref={inputRef}
              rows={1}
              value={input}
              disabled={busy}
              placeholder={
                mode === 'do' ? 'What do you do?'
                  : mode === 'say' ? 'What do you say?'
                  : 'What happens next?'
              }
              onChange={(e) => setInput(e.target.value)}
              // Enter sends; Shift+Enter inserts a newline.
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !busy) { e.preventDefault(); send() }
              }}
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
            <PlotPanel adventure={adventure} setAdventure={setAdventure}
              onWorldStateChanged={() => setStateKey((k) => k + 1)} />
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
        <div className={`play-toast ${toast.isError ? '' : 'ok'}`}>
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
