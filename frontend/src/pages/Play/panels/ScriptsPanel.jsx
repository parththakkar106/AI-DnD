// The scripts panel: this adventure's copies of the library scripts.

import { useEffect, useState } from 'react'
import { api } from '../../../api'
import { downloadJSON } from '../../../components'

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

export { ScriptsPanel }
