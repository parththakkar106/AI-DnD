// The right drawer: the RPG world state, and the form that edits it.
//
// Editing builds a flat draft keyed by path, so a nested value can be edited
// without rebuilding the tree on every keystroke. `buildWorldStateDraft` makes
// it and `sliceDraft` reads one section back out.

import { useCallback, useEffect, useState } from 'react'
import { api } from '../../../api'
import { npcInitials } from '../../../components'

// Returns the word label for a value, read from the stat def's bands. This
// mirrors `worldstate.band_label` on the server.
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

export { WorldStateDrawer }
