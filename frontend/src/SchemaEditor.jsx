import { useEffect, useState } from 'react'
import { npcInitials } from './components'

// Rebuild an object with one key renamed, preserving order. Returns null on a
// no-op or a collision (so the caller keeps the old object).
function withRenamedKey(obj, oldKey, newKey) {
  if (!newKey || oldKey === newKey || obj[newKey] !== undefined) return null
  const next = {}
  for (const [k, v] of Object.entries(obj)) next[k === oldKey ? newKey : k] = v
  return next
}

// A key/id input that commits on blur (renaming a key mid-keystroke would
// rebuild the parent object and steal focus).
function KeyInput({ value, onCommit, placeholder }) {
  const [v, setV] = useState(value)
  useEffect(() => { setV(value) }, [value])
  const commit = () => {
    const trimmed = v.trim()
    if (trimmed && trimmed !== value) onCommit(trimmed)
    else setV(value)
  }
  return (
    <input className="se-key" value={v} placeholder={placeholder}
      onChange={(e) => setV(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => { if (e.key === 'Enter') e.target.blur() }} />
  )
}

// Every control in this editor wears the same tiny caption — that consistency
// is what keeps the mixed row types (stats, flags, milestones, NPCs) reading
// as one form rather than five.
function Field({ label, className = '', children }) {
  return (
    <label className={`se-field ${className}`}>
      <span>{label}</span>
      {children}
    </label>
  )
}

function Num({ label, value, onChange }) {
  return (
    <Field label={label} className="se-num">
      <input type="number" value={value ?? ''}
        onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))} />
    </Field>
  )
}

function StatEditor({ statKey, def, onRenameKey, onChange, onRemove }) {
  const set = (field, val) => {
    const next = { ...def }
    if (val === undefined || val === '') delete next[field]
    else next[field] = val
    onChange(next)
  }
  const isText = def.type === 'text'
  const bands = Array.isArray(def.bands) ? def.bands : []
  const setBands = (nb) => set('bands', nb.length ? nb : undefined)
  const updBand = (i, j, raw) => {
    const nb = bands.map((b) => (Array.isArray(b) ? [...b] : [0, 0, '']))
    while (nb[i].length < 3) nb[i].push(j < 2 ? 0 : '')
    nb[i][j] = j < 2 ? (raw === '' ? 0 : Number(raw)) : raw
    setBands(nb)
  }
  const setType = (val) => {
    const next = { ...def, type: val || undefined }
    if (val === 'text') {
      // Numeric-only fields don't apply to free text.
      delete next.min; delete next.max; delete next.max_delta_per_turn; delete next.bands
      if (typeof next.initial !== 'string') next.initial = ''
    } else if (typeof next.initial === 'string') {
      delete next.initial
    }
    onChange(next)
  }
  return (
    <div className="se-row">
      <div className="se-row-top">
        <KeyInput value={statKey} onCommit={onRenameKey} placeholder="stat_name" />
        <button type="button" className="se-remove" onClick={onRemove} title="Remove stat">✕</button>
      </div>
      <div className="se-fields">
        {isText ? (
          <Field label="initial" className="se-num se-initial-text">
            <input type="text" value={def.initial ?? ''}
              onChange={(e) => set('initial', e.target.value)} />
          </Field>
        ) : (
          <>
            <Num label="min" value={def.min} onChange={(v) => set('min', v)} />
            <Num label="max" value={def.max} onChange={(v) => set('max', v)} />
            <Num label="initial" value={def.initial} onChange={(v) => set('initial', v)} />
            <Num label="±/turn" value={def.max_delta_per_turn} onChange={(v) => set('max_delta_per_turn', v)} />
          </>
        )}
        <Num label="cooldown" value={def.cooldown} onChange={(v) => set('cooldown', v)} />
        <Field label="kind" className="se-num se-kind">
          <select value={isText ? 'text' : (def.type === 'counter' ? 'counter' : 'number')}
            onChange={(e) => setType(e.target.value === 'number' ? undefined : e.target.value)}>
            <option value="number">number</option>
            <option value="counter">counts up only</option>
            <option value="text">free text</option>
          </select>
        </Field>
      </div>
      <Field label="description (shown to the AI)">
        <input className="se-text" value={def.desc || ''} placeholder="e.g. how badly wounded you are"
          onChange={(e) => set('desc', e.target.value || undefined)} />
      </Field>
      {!isText && (
        <div className="se-bands">
          <div className="se-sub-head">Bands — low, high, label</div>
          {bands.map((b, i) => (
            <div key={i} className="se-band">
              <input type="number" className="se-band-n" value={b?.[0] ?? ''}
                onChange={(e) => updBand(i, 0, e.target.value)} />
              <input type="number" className="se-band-n" value={b?.[1] ?? ''}
                onChange={(e) => updBand(i, 1, e.target.value)} />
              <input className="se-band-l" value={b?.[2] ?? ''} placeholder="label"
                onChange={(e) => updBand(i, 2, e.target.value)} />
              <button type="button" className="se-remove"
                onClick={() => setBands(bands.filter((_, k) => k !== i))} title="Remove band">✕</button>
            </div>
          ))}
          <button type="button" className="se-add-sm"
            onClick={() => setBands([...bands, [0, 0, '']])}>+ band</button>
        </div>
      )}
    </div>
  )
}

function StatSection({ title, hint, defs, onChange, addLabel = '+ stat', nested }) {
  const entries = Object.entries(defs || {})
  const rename = (o, n) => { const x = withRenamedKey(defs, o, n); if (x) onChange(x) }
  const setDef = (k, val) => onChange({ ...defs, [k]: val })
  const remove = (k) => { const x = { ...defs }; delete x[k]; onChange(x) }
  const add = () => {
    let i = 1, key = 'stat'
    while (defs[key]) key = `stat${i++}`
    onChange({ ...defs, [key]: { min: 0, max: 100, initial: 0 } })
  }
  return (
    <div className={nested ? 'se-section se-section-nested' : 'se-section'}>
      <div className="se-section-head">
        <span className="se-section-title">{title}</span>
        <button type="button" className="se-add" onClick={add}>{addLabel}</button>
      </div>
      {hint && <p className="se-hint">{hint}</p>}
      {entries.length === 0 && <div className="se-empty">None yet.</div>}
      {entries.map(([k, d]) => (
        <StatEditor key={k} statKey={k} def={d || {}}
          onRenameKey={(nk) => rename(k, nk)}
          onChange={(nd) => setDef(k, nd)}
          onRemove={() => remove(k)} />
      ))}
    </div>
  )
}

function FlagSection({ flags, onChange }) {
  const entries = Object.entries(flags || {})
  const rename = (o, n) => { const x = withRenamedKey(flags, o, n); if (x) onChange(x) }
  const setFlag = (k, val) => onChange({ ...flags, [k]: val })
  const remove = (k) => { const x = { ...flags }; delete x[k]; onChange(x) }
  const add = () => {
    let i = 1, key = 'flag'
    while (flags[key]) key = `flag${i++}`
    onChange({ ...flags, [key]: { initial: false, desc: '' } })
  }
  return (
    <div className="se-section">
      <div className="se-section-head">
        <span className="se-section-title">Flags</span>
        <button type="button" className="se-add" onClick={add}>+ flag</button>
      </div>
      <p className="se-hint">On/off switches the AI can flip either way — a door unlocked, an alarm raised.</p>
      {entries.length === 0 && <div className="se-empty">No flags.</div>}
      {entries.map(([k, f]) => (
        <div key={k} className="se-row">
          <div className="se-row-top">
            <KeyInput value={k} onCommit={(nk) => rename(k, nk)} placeholder="flag_name" />
            <label className="se-check">
              <input type="checkbox" checked={!!f.initial}
                onChange={(e) => setFlag(k, { ...f, initial: e.target.checked })} />
              <span>on by default</span>
            </label>
            <button type="button" className="se-remove" onClick={() => remove(k)} title="Remove flag">✕</button>
          </div>
          <Field label="description (shown to the AI)">
            <input className="se-text" value={f.desc || ''} placeholder="e.g. the cellar door is unlocked"
              onChange={(e) => setFlag(k, { ...f, desc: e.target.value })} />
          </Field>
        </div>
      ))}
    </div>
  )
}

function MilestoneSection({ milestones, onChange }) {
  const entries = Object.entries(milestones || {})
  const rename = (o, n) => { const x = withRenamedKey(milestones, o, n); if (x) onChange(x) }
  const setM = (k, val) => onChange({ ...milestones, [k]: val })
  const remove = (k) => { const x = { ...milestones }; delete x[k]; onChange(x) }
  const add = () => {
    let i = 1, key = 'goal'
    while (milestones[key]) key = `goal${i++}`
    onChange({ ...milestones, [key]: { desc: '' } })
  }
  return (
    <div className="se-section">
      <div className="se-section-head">
        <span className="se-section-title">Milestones</span>
        <button type="button" className="se-add" onClick={add}>+ milestone</button>
      </div>
      <p className="se-hint">Objectives that stick once reached — they never un-tick on their own.</p>
      {entries.length === 0 && <div className="se-empty">No milestones.</div>}
      {entries.map(([k, m]) => (
        <div key={k} className="se-row">
          <div className="se-row-top">
            <KeyInput value={k} onCommit={(nk) => rename(k, nk)} placeholder="milestone_id" />
            <button type="button" className="se-remove" onClick={() => remove(k)} title="Remove milestone">✕</button>
          </div>
          <Field label="objective">
            <input className="se-text" value={m?.desc || ''} placeholder="e.g. escaped the bandit camp"
              onChange={(e) => setM(k, { ...m, desc: e.target.value })} />
          </Field>
        </div>
      ))}
    </div>
  )
}

// ---- NPCs ----------------------------------------------------------------
// NPCs live inside the same stat_schema (`schema.npcs`) but get their own
// top-level section in the editor: each one is a small character sheet, which
// doesn't fit the flat stat rows the other sections use.

// Next `npcs` object with a fresh entry appended, and the id it used.
export function addNpc(npcs) {
  let i = 1, key = 'npc'
  while (npcs?.[key]) key = `npc${i++}`
  return { ...(npcs || {}), [key]: { name: '', keys: '', desc: '', stats: {} } }
}

function NpcCard({ npcId, npc, onRenameKey, onChange, onRemove }) {
  const set = (field, val) => onChange({ ...npc, [field]: val })
  const statCount = Object.keys(npc.stats || {}).length
  return (
    <div className="se-npc">
      <div className="se-npc-head">
        <span className="se-avatar" aria-hidden="true">{npcInitials(npc.name, npcId)}</span>
        <div className="se-npc-ident">
          <input className="se-npc-name" value={npc.name || ''} placeholder="Display name"
            onChange={(e) => set('name', e.target.value)} />
          <code className="se-npc-addr">npc.{npcId}</code>
        </div>
        <span className="se-npc-count">{statCount} {statCount === 1 ? 'stat' : 'stats'}</span>
        <button type="button" className="se-remove" onClick={onRemove} title="Remove NPC">✕</button>
      </div>
      <div className="se-npc-body">
        <Field label="id (how the AI addresses them)" className="se-npc-idfield">
          <KeyInput value={npcId} onCommit={onRenameKey} placeholder="npc_id" />
        </Field>
        <Field label="trigger words, comma-separated">
          <input className="se-text" value={npc.keys || ''} placeholder="Gwen, ranger, the scout"
            onChange={(e) => set('keys', e.target.value)} />
        </Field>
        <Field label="description (lore + shown to the AI)">
          <textarea className="se-text se-area" rows={2} value={npc.desc || ''}
            placeholder="A wary ranger who owes you a debt."
            onChange={(e) => set('desc', e.target.value)} />
        </Field>
        <StatSection title="Their stats" defs={npc.stats || {}} nested
          onChange={(nd) => set('stats', nd)} />
      </div>
    </div>
  )
}

// The NPC roster. Takes/returns just the `npcs` slice of a stat_schema.
export function NpcEditor({ npcs, onChange }) {
  const entries = Object.entries(npcs || {})
  const rename = (o, n) => { const x = withRenamedKey(npcs, o, n); if (x) onChange(x) }
  const setNpc = (k, val) => onChange({ ...npcs, [k]: val })
  const remove = (k) => { const x = { ...npcs }; delete x[k]; onChange(x) }
  if (entries.length === 0) {
    return (
      <div className="empty" style={{ padding: '20px 0' }}>
        No NPCs yet. Each one gets its own stats (trust, health, ferocity — whatever suits them),
        and a story card is created automatically so they show up in context when mentioned.
      </div>
    )
  }
  return (
    <div className="se se-npc-grid">
      {entries.map(([k, npc]) => (
        <NpcCard key={k} npcId={k} npc={npc || {}}
          onRenameKey={(nk) => rename(k, nk)}
          onChange={(n) => setNpc(k, n)}
          onRemove={() => remove(k)} />
      ))}
    </div>
  )
}

// Form-based editor for a stat_schema, minus the NPCs (see `NpcEditor`, which
// the scenario editor renders as its own page section). `schema` is the parsed
// object (or null); `onChange(nextSchema)` fires on every edit. Empty sections
// are dropped.
export default function SchemaEditor({ schema, onChange }) {
  const s = schema && typeof schema === 'object' ? schema : {}
  const setSection = (key, val) => {
    const next = { ...s }
    if (val && Object.keys(val).length) next[key] = val
    else delete next[key]
    onChange(next)
  }
  return (
    <div className="se">
      <StatSection title="World stats" defs={s.world || {}} onChange={(v) => setSection('world', v)}
        hint="Things about the situation, not any one character — time of day, the camp’s alert level." />
      <StatSection title="Player stats" defs={s.player || {}} onChange={(v) => setSection('player', v)}
        hint="The player’s own numbers — hp, gold, reputation — plus free-text ones like an outfit." />
      <FlagSection flags={s.flags || {}} onChange={(v) => setSection('flags', v)} />
      <MilestoneSection milestones={s.milestones || {}} onChange={(v) => setSection('milestones', v)} />
    </div>
  )
}
