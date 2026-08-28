// The left drawer: the script state an adventure's scripts read and write.
//
// `StateTree` and `StateValue` render a value of any shape, because script
// state is whatever the scripts put there.

import { useCallback, useEffect, useState } from 'react'
import { api } from '../../../api'

// Renders one script-state value. A primitive renders inline, typed and
// colored. An object or an array renders as a collapsible indented tree, and
// the render recurses, so deep state shows its structure rather than one flat
// JSON blob.
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

export { StatusDrawer }
