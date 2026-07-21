import { useEffect, useState } from 'react'

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

function Num({ label, value, onChange }) {
  return (
    <label className="se-num">
      <span>{label}</span>
      <input type="number" value={value ?? ''}
        onChange={(e) => onChange(e.target.value === '' ? undefined : Number(e.target.value))} />
    </label>
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
    <div className="se-stat">
      <div className="se-row-top">
        <KeyInput value={statKey} onCommit={onRenameKey} placeholder="stat_name" />
        <button type="button" className="se-remove" onClick={onRemove} title="Remove stat">✕</button>
      </div>
      <div className="se-fields">
        {isText ? (
          <label className="se-num">
            <span>initial</span>
            <input type="text" value={def.initial ?? ''}
              onChange={(e) => set('initial', e.target.value)} />
          </label>
        ) : (
          <>
            <Num label="min" value={def.min} onChange={(v) => set('min', v)} />
            <Num label="max" value={def.max} onChange={(v) => set('max', v)} />
            <Num label="initial" value={def.initial} onChange={(v) => set('initial', v)} />
            <Num label="±/turn" value={def.max_delta_per_turn} onChange={(v) => set('max_delta_per_turn', v)} />
          </>
        )}
        <Num label="cooldown" value={def.cooldown} onChange={(v) => set('cooldown', v)} />
        <label className="se-num">
          <span>kind</span>
          <select value={isText ? 'text' : (def.type === 'counter' ? 'counter' : 'number')}
            onChange={(e) => setType(e.target.value === 'number' ? undefined : e.target.value)}>
            <option value="number">number</option>
            <option value="counter">counts up only</option>
            <option value="text">free text</option>
          </select>
        </label>
      </div>
      <input className="se-text" value={def.desc || ''} placeholder="description (shown to the AI)"
        onChange={(e) => set('desc', e.target.value || undefined)} />
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

function StatSection({ title, defs, onChange, addLabel = '+ stat', nested }) {
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

function NpcSection({ npcs, onChange }) {
  const entries = Object.entries(npcs || {})
  const rename = (o, n) => { const x = withRenamedKey(npcs, o, n); if (x) onChange(x) }
  const setNpc = (k, val) => onChange({ ...npcs, [k]: val })
  const remove = (k) => { const x = { ...npcs }; delete x[k]; onChange(x) }
  const add = () => {
    let i = 1, key = 'npc'
    while (npcs[key]) key = `npc${i++}`
    onChange({ ...npcs, [key]: { name: '', keys: '', desc: '', stats: {} } })
  }
  return (
    <div className="se-section">
      <div className="se-section-head">
        <span className="se-section-title">NPCs</span>
        <button type="button" className="se-add" onClick={add}>+ NPC</button>
      </div>
      {entries.length === 0 && <div className="se-empty">No NPCs.</div>}
      {entries.map(([k, npc]) => (
        <div key={k} className="se-npc">
          <div className="se-row-top">
            <KeyInput value={k} onCommit={(nk) => rename(k, nk)} placeholder="npc_id" />
            <span className="schema-npc-id">npc.{k}</span>
            <button type="button" className="se-remove" onClick={() => remove(k)} title="Remove NPC">✕</button>
          </div>
          <input className="se-text" value={npc.name || ''} placeholder="Display name"
            onChange={(e) => setNpc(k, { ...npc, name: e.target.value })} />
          <input className="se-text" value={npc.keys || ''} placeholder="trigger words, comma-separated (e.g. Gwen, ranger)"
            onChange={(e) => setNpc(k, { ...npc, keys: e.target.value })} />
          <input className="se-text" value={npc.desc || ''} placeholder="description (lore + shown to the AI)"
            onChange={(e) => setNpc(k, { ...npc, desc: e.target.value })} />
          <StatSection title="Stats" defs={npc.stats || {}} nested
            onChange={(nd) => setNpc(k, { ...npc, stats: nd })} />
        </div>
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
        <span className="se-section-title">Flags (on/off)</span>
        <button type="button" className="se-add" onClick={add}>+ flag</button>
      </div>
      {entries.length === 0 && <div className="se-empty">No flags.</div>}
      {entries.map(([k, f]) => (
        <div key={k} className="se-stat">
          <div className="se-row-top">
            <KeyInput value={k} onCommit={(nk) => rename(k, nk)} placeholder="flag_name" />
            <label className="se-check">
              <input type="checkbox" checked={!!f.initial}
                onChange={(e) => setFlag(k, { ...f, initial: e.target.checked })} />
              <span>on by default</span>
            </label>
            <button type="button" className="se-remove" onClick={() => remove(k)} title="Remove flag">✕</button>
          </div>
          <input className="se-text" value={f.desc || ''} placeholder="description"
            onChange={(e) => setFlag(k, { ...f, desc: e.target.value })} />
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
        <span className="se-section-title">Milestones (sticky objectives)</span>
        <button type="button" className="se-add" onClick={add}>+ milestone</button>
      </div>
      {entries.length === 0 && <div className="se-empty">No milestones.</div>}
      {entries.map(([k, m]) => (
        <div key={k} className="se-stat">
          <div className="se-row-top">
            <KeyInput value={k} onCommit={(nk) => rename(k, nk)} placeholder="milestone_id" />
            <button type="button" className="se-remove" onClick={() => remove(k)} title="Remove milestone">✕</button>
          </div>
          <input className="se-text" value={m?.desc || ''} placeholder="objective description"
            onChange={(e) => setM(k, { ...m, desc: e.target.value })} />
        </div>
      ))}
    </div>
  )
}

// Form-based editor for a stat_schema. `schema` is the parsed object (or null);
// `onChange(nextSchema)` fires on every edit. Empty sections are dropped.
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
      <StatSection title="World stats" defs={s.world || {}} onChange={(v) => setSection('world', v)} />
      <StatSection title="Player stats" defs={s.player || {}} onChange={(v) => setSection('player', v)} />
      <NpcSection npcs={s.npcs || {}} onChange={(v) => setSection('npcs', v)} />
      <FlagSection flags={s.flags || {}} onChange={(v) => setSection('flags', v)} />
      <MilestoneSection milestones={s.milestones || {}} onChange={(v) => setSection('milestones', v)} />
    </div>
  )
}
