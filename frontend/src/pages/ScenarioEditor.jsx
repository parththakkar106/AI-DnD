import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { Field, StoryCardRow, downloadJSON, pickJSONFile } from '../components'
import SchemaEditor from '../SchemaEditor'

export default function ScenarioEditor() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [scenario, setScenario] = useState(null)
  const [allScripts, setAllScripts] = useState([])
  const [status, setStatus] = useState('')
  // Raw text buffer for the stat_schema JSON editor + a live parse error.
  const [schemaText, setSchemaText] = useState('')
  const [schemaError, setSchemaError] = useState('')
  // Last successfully-parsed schema (drives the form editor + preview).
  const [parsedSchema, setParsedSchema] = useState(null)
  const [schemaView, setSchemaView] = useState('form') // 'form' | 'json'
  // One timer per field/card: a single shared timer would cancel the pending
  // save of whatever was edited previously within the debounce window.
  const saveTimers = useRef(new Map())
  const debounceSave = (key, fn) => {
    clearTimeout(saveTimers.current.get(key))
    saveTimers.current.set(key, setTimeout(fn, 600))
  }

  useEffect(() => {
    api.getScenario(id).then((s) => {
      setScenario(s)
      setSchemaText(s.stat_schema ? JSON.stringify(s.stat_schema, null, 2) : '')
      setParsedSchema(s.stat_schema || null)
    }).catch(() => navigate('/scenarios'))
    api.listScripts().then(setAllScripts).catch(() => {})
  }, [id, navigate])

  const setField = (field, value) => {
    const next = { ...scenario, [field]: value }
    setScenario(next)
    debounceSave(field, async () => {
      await api.updateScenario(id, { [field]: value })
      setStatus('Saved')
      setTimeout(() => setStatus(''), 1500)
    })
  }

  // JSON view: typing only updates the text buffer; nothing is parsed or saved
  // until the user clicks Save (so a half-typed edit doesn't spam errors).
  const onJsonText = (text) => {
    setSchemaText(text)
    if (schemaError) setSchemaError('')
  }

  // Parse the JSON box on demand: apply + save if valid, else show the error.
  const commitJson = () => {
    const trimmed = schemaText.trim()
    if (!trimmed) {
      setSchemaError('')
      setParsedSchema(null)
      saveSchema(null)
      return
    }
    let parsed
    try {
      parsed = JSON.parse(trimmed)
    } catch (err) {
      setSchemaError(`Invalid JSON: ${err.message}`)
      return
    }
    if (typeof parsed !== 'object' || Array.isArray(parsed)) {
      setSchemaError('The schema must be a JSON object (e.g. { "player": { … } }).')
      return
    }
    setSchemaError('')
    setParsedSchema(parsed)
    setSchemaText(JSON.stringify(parsed, null, 2))  // normalize / pretty-print
    saveSchema(parsed)
  }

  const saveSchema = async (parsed) => {
    await api.updateScenario(id, { stat_schema: parsed })
    setScenario((s) => ({ ...s, stat_schema: parsed }))
    setStatus('Saved')
    setTimeout(() => setStatus(''), 1500)
  }

  // Edits from the form editor: keep the JSON text in sync and save. An empty
  // schema clears the RPG layer (null).
  const applySchema = (next) => {
    const cleaned = next && Object.keys(next).length ? next : null
    setParsedSchema(cleaned)
    setSchemaText(cleaned ? JSON.stringify(cleaned, null, 2) : '')
    setSchemaError('')
    debounceSave('stat_schema', () => saveSchema(cleaned))
  }

  const addCard = async () => {
    const card = await api.createStoryCard({ scenario_id: Number(id) })
    setScenario({ ...scenario, story_cards: [...scenario.story_cards, card] })
  }

  const updateCard = (card) => {
    setScenario({
      ...scenario,
      story_cards: scenario.story_cards.map((c) => (c.id === card.id ? card : c)),
    })
    debounceSave(`card-${card.id}`, () => {
      api.updateStoryCard(card.id, {
        name: card.name, type: card.type, keys: card.keys, entry: card.entry, notes: card.notes,
      })
    })
  }

  const deleteCard = async (cardId) => {
    await api.deleteStoryCard(cardId)
    setScenario({ ...scenario, story_cards: scenario.story_cards.filter((c) => c.id !== cardId) })
  }

  const exportCards = async () => {
    const cards = await api.exportStoryCards({ scenario_id: id })
    downloadJSON(cards, `${scenario.title.replace(/\W+/g, '-')}-cards.json`)
  }

  const importCards = async () => {
    try {
      const parsed = await pickJSONFile()
      const cards = Array.isArray(parsed) ? parsed : (parsed.cards || parsed.storyCards)
      if (!Array.isArray(cards)) return alert('Expected a JSON array of story cards.')
      const created = await api.importStoryCards({ scenario_id: Number(id), cards })
      setScenario({ ...scenario, story_cards: [...scenario.story_cards, ...created] })
    } catch (err) {
      alert(err.message)
    }
  }

  const toggleScript = async (scriptId) => {
    const current = scenario.scripts.map((s) => s.id)
    const next = current.includes(scriptId)
      ? current.filter((sid) => sid !== scriptId)
      : [...current, scriptId]
    const updated = await api.updateScenario(id, { script_ids: next })
    setScenario(updated)
  }

  const exportScenario = async () => {
    downloadJSON(await api.exportScenario(id), `${scenario.title.replace(/\W+/g, '-')}.json`)
  }

  const deleteScenario = async () => {
    if (!confirm('Delete this scenario permanently?')) return
    await api.deleteScenario(id)
    navigate('/scenarios')
  }

  const play = async () => {
    const adv = await api.createAdventure({ scenario_id: Number(id) })
    navigate(`/play/${adv.id}`)
  }

  if (!scenario) return null
  // Shared demo scenarios (Phase 8) are visible to everyone but owned by no
  // one; the backend rejects edits, so present them read-only.
  const readOnly = !!scenario.is_public

  return (
    <div className="page">
      <div className="page-header">
        <h1>{readOnly ? 'Scenario (read-only)' : 'Edit Scenario'}</h1>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span style={{ color: 'var(--text-dim)', fontSize: '0.85rem' }}>{status}</span>
          <button onClick={exportScenario}>Export</button>
          {!readOnly && <button className="danger" onClick={deleteScenario}>Delete</button>}
          <button className="primary" onClick={play}>Play</button>
        </div>
      </div>

      {readOnly && (
        <div className="demo-banner">
          This is a shared demo scenario — it can’t be edited, but you can hit
          <strong> Play</strong> to start your own adventure from it, or <strong>Export</strong> and
          re-import it as your own copy.
        </div>
      )}

      <fieldset disabled={readOnly} style={{ border: 'none', padding: 0, margin: 0, minWidth: 0 }}>
      <Field label="Title" value={scenario.title} onChange={(v) => setField('title', v)} />
      <Field label="Description" value={scenario.description} onChange={(v) => setField('description', v)}
        textarea placeholder="Shown in the scenario list; not sent to the AI." />
      <Field label="Opening Prompt" value={scenario.prompt} onChange={(v) => setField('prompt', v)}
        textarea rows={6} placeholder="The opening story text. Supports ${placeholders} (Phase 5)." />
      <Field label="Plot Essentials (Memory)" value={scenario.memory} onChange={(v) => setField('memory', v)}
        textarea placeholder="Key facts the AI should always remember." />
      <Field label="Author's Note" value={scenario.authors_note} onChange={(v) => setField('authors_note', v)}
        textarea rows={2} placeholder="Style/theme guidance, injected near the end of context." />
      <Field label="AI Instructions" value={scenario.ai_instructions} onChange={(v) => setField('ai_instructions', v)}
        textarea rows={2} placeholder="Behavioral guidance for the model (always included)." />
      <Field label="Tags" value={scenario.tags} onChange={(v) => setField('tags', v)} placeholder="fantasy, mystery" />

      <div className="page-header" style={{ marginTop: 28 }}>
        <h2 style={{ margin: 0, fontFamily: 'Georgia, serif', fontSize: '1.2rem' }}>Story Cards</h2>
        <div style={{ display: 'flex', gap: 8 }}>
          <button onClick={exportCards} disabled={scenario.story_cards.length === 0}>Export</button>
          {!readOnly && <button onClick={importCards}>Import</button>}
          <button onClick={addCard}>+ Add Card</button>
        </div>
      </div>
      {scenario.story_cards.length === 0 && (
        <div className="empty" style={{ padding: '20px 0' }}>
          No story cards. Cards inject their Entry into context when a trigger word appears in the story.
        </div>
      )}
      {scenario.story_cards.map((card) => (
        <StoryCardRow key={card.id} card={card}
          onChange={updateCard} onDelete={() => deleteCard(card.id)} />
      ))}

      <div className="page-header" style={{ marginTop: 28 }}>
        <h2 style={{ margin: 0, fontFamily: 'Georgia, serif', fontSize: '1.2rem' }}>World State (RPG)</h2>
        <div className="panel-toggles">
          <button type="button" className={schemaView === 'form' ? 'active' : ''}
            onClick={() => setSchemaView('form')}>Editor</button>
          <button type="button" className={schemaView === 'json' ? 'active' : ''}
            onClick={() => setSchemaView('json')}>JSON</button>
        </div>
      </div>
      <p className="dim" style={{ margin: '0 0 10px', fontSize: '0.85rem' }}>
        Optional. Define stats (with bands and rules), NPCs, flags, and milestones, and
        the AI will track them each turn — HP, mana, an NPC’s trust, quest objectives.
        Each NPC has its own name, trigger keys, description, and its own stats; a story
        card is created for it automatically. Leave blank for a plain narrative scenario.
      </p>
      {schemaView === 'form' ? (
        <SchemaEditor schema={parsedSchema} onChange={applySchema} />
      ) : (
        <>
          <textarea
            className="schema-editor"
            value={schemaText}
            onChange={(e) => onJsonText(e.target.value)}
            rows={14}
            spellCheck={false}
            placeholder={'{\n  "player": { "hp": { "min": 0, "max": 100, "initial": 100 } },\n  "npcs": {\n    "gwen": { "name": "Gwen", "keys": "Gwen, ranger", "desc": "...",\n      "stats": { "trust": { "min": -100, "max": 100, "initial": 20 } } }\n  },\n  "flags": { "has_key": { "desc": "...", "initial": false } },\n  "milestones": { "goal": { "desc": "..." } }\n}'}
          />
          <div className="schema-json-actions">
            <button type="button" className="primary" onClick={commitJson}>Save JSON</button>
            {schemaError
              ? <span className="schema-error">⚠ {schemaError}</span>
              : <span className="dim" style={{ fontSize: '0.8rem' }}>
                  Edits apply when you click Save.
                </span>}
          </div>
          <SchemaPreview schema={parsedSchema} />
        </>
      )}

      <div className="page-header" style={{ marginTop: 28 }}>
        <h2 style={{ margin: 0, fontFamily: 'Georgia, serif', fontSize: '1.2rem' }}>Attached Scripts</h2>
      </div>
      {allScripts.length === 0 ? (
        <div className="empty" style={{ padding: '20px 0' }}>
          No scripts in your library. Create some on the Scripts page.
        </div>
      ) : (
        allScripts.map((s) => (
          <label key={s.id} className="script-attach">
            <input
              type="checkbox"
              checked={scenario.scripts.some((att) => att.id === s.id)}
              onChange={() => toggleScript(s.id)}
            />
            <span>{s.name}</span>
            <span className="dim">{s.description}</span>
          </label>
        ))
      )}
      </fieldset>
    </div>
  )
}

// ---- Structured preview of a parsed stat_schema (read-only) ----

function statMeta(def) {
  const parts = []
  if (def.initial !== undefined) parts.push(`start ${def.initial}`)
  if (typeof def.min === 'number' && typeof def.max === 'number') parts.push(`${def.min}–${def.max}`)
  if (def.type === 'counter') parts.push('counter')
  if (typeof def.max_delta_per_turn === 'number') parts.push(`±${def.max_delta_per_turn}/turn`)
  if (def.cooldown) parts.push(`cooldown ${def.cooldown}`)
  return parts.join(' · ')
}

function StatDefRow({ name, def }) {
  if (!def || typeof def !== 'object') return null
  const bands = Array.isArray(def.bands)
    ? def.bands.filter((b) => Array.isArray(b) && b.length === 3)
        .map((b) => `${b[0]}–${b[1]} ${b[2]}`).join(', ')
    : null
  return (
    <div className="schema-stat">
      <div className="schema-stat-head">
        <span className="schema-stat-name">{name}</span>
        <span className="schema-stat-meta">{statMeta(def)}</span>
      </div>
      {def.desc && <div className="schema-desc">{def.desc}</div>}
      {bands && <div className="schema-bands">bands: {bands}</div>}
    </div>
  )
}

function StatDefGroup({ title, defs }) {
  const entries = Object.entries(defs || {}).filter(([, d]) => d && typeof d === 'object')
  if (entries.length === 0) return null
  return (
    <div className="schema-group">
      <div className="schema-group-title">{title}</div>
      {entries.map(([n, d]) => <StatDefRow key={n} name={n} def={d} />)}
    </div>
  )
}

function SchemaPreview({ schema }) {
  if (!schema || typeof schema !== 'object' || Array.isArray(schema)) return null
  const world = schema.world || {}
  const player = schema.player || {}
  const npcs = schema.npcs || {}
  const flags = schema.flags || {}
  const milestones = schema.milestones || {}
  const count = [world, player, npcs, flags, milestones]
    .reduce((n, o) => n + Object.keys(o || {}).length, 0)
  if (count === 0) return null

  return (
    <div className="schema-preview">
      <div className="schema-preview-title">Parsed structure</div>
      <StatDefGroup title="World" defs={world} />
      <StatDefGroup title="Player" defs={player} />
      {Object.entries(npcs).map(([npcId, npc]) => (
        npc && typeof npc === 'object' ? (
          <div key={npcId} className="schema-group">
            <div className="schema-group-title">
              {npc.name || npcId} <span className="schema-npc-id">npc.{npcId}</span>
            </div>
            {npc.keys && <div className="schema-desc">triggers: {npc.keys}</div>}
            {npc.desc && <div className="schema-desc">{npc.desc}</div>}
            {Object.entries(npc.stats || {}).map(([n, d]) => (
              <StatDefRow key={n} name={n} def={d} />
            ))}
          </div>
        ) : null
      ))}
      {Object.keys(flags).length > 0 && (
        <div className="schema-group">
          <div className="schema-group-title">Flags</div>
          {Object.entries(flags).map(([fid, f]) => (
            <div key={fid} className="schema-stat">
              <div className="schema-stat-head">
                <span className="schema-stat-name">{fid}</span>
                <span className="schema-stat-meta">{f?.initial ? 'on by default' : 'off by default'}</span>
              </div>
              {f?.desc && <div className="schema-desc">{f.desc}</div>}
            </div>
          ))}
        </div>
      )}
      {Object.keys(milestones).length > 0 && (
        <div className="schema-group">
          <div className="schema-group-title">Milestones</div>
          {Object.entries(milestones).map(([mid, m]) => (
            <div key={mid} className="schema-stat">
              <div className="schema-stat-head">
                <span className="schema-stat-name">{mid}</span>
              </div>
              {m?.desc && <div className="schema-desc">{m.desc}</div>}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
