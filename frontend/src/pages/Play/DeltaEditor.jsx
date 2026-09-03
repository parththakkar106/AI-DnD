// The editor for the newest turn's state changes.
//
// The AI proposes a delta each turn — hp -40, trust +15 — and sometimes the
// number is wrong for what the scene actually described. This edits that
// proposal in place: change a number, drop a change the turn should not have
// made, add one it missed. The server replays the corrected delta through the
// same referee the AI's went through, so a value edited past the scenario's
// limits comes back clamped and says so, rather than being written as typed.
// Setting a value outright, limits and all, is what the World drawer's ✎ does.

import { useEffect, useMemo, useState } from 'react'
import { api } from '../../api'

// Every path this scenario allows, in the order the drawer lists them, each with
// what kind of value it takes. The path is what the delta is keyed by and what
// the referee resolves, so it is the id here as well as the label.
function catalogue(schema) {
  const out = []
  const addStats = (prefix, defs, group) => {
    for (const [name, def] of Object.entries(defs || {})) {
      if (!def || typeof def !== 'object') continue
      out.push({
        path: `${prefix}.${name}`,
        kind: def.type === 'text' ? 'text' : 'stat',
        label: `${group} ${name}`,
        def,
      })
    }
  }
  addStats('world', schema?.world, 'world')
  addStats('player', schema?.player, 'you')
  for (const [id, ndef] of Object.entries(schema?.npcs || {})) {
    addStats(`npc.${id}`, ndef?.stats, ndef?.name || id)
  }
  for (const [name, def] of Object.entries(schema?.flags || {})) {
    out.push({ path: `flags.${name}`, kind: 'flag', label: `flag ${name}`, def })
  }
  for (const [mid, def] of Object.entries(schema?.milestones || {})) {
    out.push({
      path: `milestones.${mid}`, kind: 'milestone',
      label: `milestone ${def?.desc || mid}`, def,
    })
  }
  return out
}

// The value a newly added row starts on. A stat starts at 0 rather than at a
// guess, so nothing is written until a number is typed. A milestone can only be
// marked reached, which is the only value the referee accepts for one.
function seedValue(kind) {
  if (kind === 'text') return ''
  if (kind === 'flag') return true
  if (kind === 'milestone') return true
  return 0
}

// A path the schema no longer defines. The turn sent it, so it has to be
// editable — otherwise saving would silently drop it — but nothing is known
// about it beyond its name.
function unknownEntry(path) {
  return { path, kind: 'stat', label: path, def: {}, unknown: true }
}

function Row({ entry, value, onChange, onRemove }) {
  const { kind, def } = entry
  return (
    <div className="dlt-row">
      <span className="dlt-path" title={def?.desc || entry.path}>{entry.label}</span>
      {kind === 'stat' && (
        <input
          className="dlt-num" type="number" value={value}
          // The delta's own bound, not the stat's: a change of at most
          // `max_delta_per_turn` is all the referee will take in one turn, and
          // a bigger one comes back clamped.
          step="1"
          onChange={(e) => onChange(e.target.value === '' ? '' : Number(e.target.value))}
        />
      )}
      {kind === 'text' && (
        <input className="dlt-text" type="text" value={value ?? ''}
          onChange={(e) => onChange(e.target.value)} />
      )}
      {kind === 'flag' && (
        <select className="dlt-pick" value={value ? 'on' : 'off'}
          onChange={(e) => onChange(e.target.value === 'on')}>
          <option value="on">turns on</option>
          <option value="off">turns off</option>
        </select>
      )}
      {kind === 'milestone' && <span className="dlt-fixed">reached</span>}
      <button className="dlt-drop" title="Take this change out of the turn"
        onClick={onRemove}>✕</button>
    </div>
  )
}

function DeltaEditor({ advId, actionId, onClose, onSaved }) {
  const [schema, setSchema] = useState(null)
  const [rows, setRows] = useState(null)   // [{ path, value }], in the turn's order
  const [failed, setFailed] = useState('')
  const [saving, setSaving] = useState(false)
  // What the referee did with the last save: the same clamps and refusals a
  // turn's own delta gets. Shown instead of closing, so a change that did not
  // land says why while the numbers that caused it are still on screen.
  const [report, setReport] = useState(null)

  useEffect(() => {
    let live = true
    Promise.all([api.getWorldState(advId), api.getWorldDelta(advId, actionId)])
      .then(([ws, wd]) => {
        if (!live) return
        setSchema(ws.schema)
        setRows(Object.entries(wd.delta || {}).map(([path, value]) => ({ path, value })))
      })
      .catch((err) => live && setFailed(err.message))
    return () => { live = false }
  }, [advId, actionId])

  const paths = useMemo(() => catalogue(schema), [schema])
  const byPath = useMemo(
    () => Object.fromEntries(paths.map((p) => [p.path, p])), [paths],
  )
  const used = new Set((rows || []).map((r) => r.path))
  const spare = paths.filter((p) => !used.has(p.path))

  const setValue = (i, value) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, value } : r)))
  const drop = (i) => setRows((rs) => rs.filter((_, j) => j !== i))
  const add = (path) => {
    const entry = byPath[path]
    if (!entry) return
    setRows((rs) => [...rs, { path, value: seedValue(entry.kind) }])
  }

  async function save() {
    setSaving(true)
    setReport(null)
    try {
      // A stat left mid-edit ("" in the input) is sent as 0, which the referee
      // records as a change that moved nothing. Dropping the row is how a
      // change is removed, so an empty box must not mean the same thing.
      const delta = {}
      for (const r of rows) {
        const kind = (byPath[r.path] || unknownEntry(r.path)).kind
        delta[r.path] = kind === 'stat' && r.value === '' ? 0 : r.value
      }
      const result = await api.reviseWorldDelta(advId, actionId, delta)
      setReport(result.report)
      onSaved(result)
    } catch (err) {
      setFailed(err.message)
    } finally {
      setSaving(false)
    }
  }

  if (failed) {
    return (
      <div className="delta-editor">
        <div className="dlt-failed">{failed}</div>
        <div className="dlt-actions"><button onClick={onClose}>Close</button></div>
      </div>
    )
  }
  if (rows === null) return <div className="delta-editor dim">Loading changes…</div>
  if (!schema) {
    return (
      <div className="delta-editor">
        <div className="dim">This adventure tracks no world state.</div>
        <div className="dlt-actions"><button onClick={onClose}>Close</button></div>
      </div>
    )
  }

  const refused = [
    ...(report?.rejected || []),
    ...(report?.clamped || []),
  ].filter((e) => e.fix)

  return (
    <div className="delta-editor">
      <div className="dlt-head">
        What this turn changed
        <span className="dim"> · the same limits apply as when the AI sends it</span>
      </div>
      {rows.length === 0 && <div className="dim dlt-empty">This turn changes nothing.</div>}
      {rows.map((r, i) => (
        <Row key={r.path} entry={byPath[r.path] || unknownEntry(r.path)} value={r.value}
          onChange={(v) => setValue(i, v)} onRemove={() => drop(i)} />
      ))}
      {spare.length > 0 && (
        <select className="dlt-add" value="" disabled={saving}
          onChange={(e) => add(e.target.value)}>
          <option value="">+ add a change…</option>
          {spare.map((p) => <option key={p.path} value={p.path}>{p.label}</option>)}
        </select>
      )}
      {refused.length > 0 && (
        <ul className="dlt-report">
          {refused.map((e, i) => <li key={i}>{e.fix}</li>)}
        </ul>
      )}
      <div className="dlt-actions">
        <button className="primary" onClick={save} disabled={saving}>
          {saving ? 'Applying…' : 'Apply'}
        </button>
        <button onClick={onClose} disabled={saving}>Close</button>
      </div>
    </div>
  )
}

export { DeltaEditor }
