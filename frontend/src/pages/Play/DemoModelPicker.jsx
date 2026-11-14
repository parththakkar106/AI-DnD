/* The model menu and backup toggle for players on the shared demo key.
 *
 * It renders nothing for a player with their own key, because their model
 * lives in Settings. The server checks every pick against the demo model list,
 * so this menu only offers what `/auth/me` returns.
 */

import { useEffect, useState } from 'react'
import { api } from '../../api'

// Turns `google/gemma-4-31b-it:free` into `gemma-4-31b-it`.
function shortName(model) {
  return model.split('/').pop().replace(/:free$/, '')
}

export function DemoModelPicker({ disabled }) {
  const [demo, setDemo] = useState(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    api.getMe().then((me) => setDemo(me?.demo)).catch(() => {})
  }, [])

  if (!demo?.using_demo || !demo.models?.length) return null

  // Show the change at once, then replace it with what the server saved. If
  // the save fails, the server's answer puts the previous choice back.
  async function save(change) {
    setSaving(true)
    setDemo({
      ...demo,
      ...('demo_model' in change && { model: change.demo_model }),
      ...('demo_fallback' in change && { fallback: change.demo_fallback }),
    })
    try {
      await api.updateSettings(change)
    } catch {
      // The reload below shows the choice that is still in use.
    }
    try {
      setDemo((await api.getMe())?.demo)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="demo-model-picker">
      <label>
        <span className="dim">Model</span>
        <select value={demo.model} disabled={disabled || saving}
          onChange={(e) => save({ demo_model: e.target.value })}>
          {demo.models.map((m) => <option key={m} value={m}>{shortName(m)}</option>)}
        </select>
      </label>
      <label title="If this model is busy, another free model answers the turn.">
        <input type="checkbox" checked={!!demo.fallback} disabled={disabled || saving}
          onChange={(e) => save({ demo_fallback: e.target.checked })} />
        <span className="dim">Use backup models when busy</span>
      </label>
    </div>
  )
}
