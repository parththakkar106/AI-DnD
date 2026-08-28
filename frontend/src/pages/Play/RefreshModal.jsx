// The confirmation dialog for copying a scenario's current content.
//
// The dialog shows exactly what the update changes, and collects any
// `${Placeholder}` answers the adventure has no stored value for. An adventure
// started before those were saved has none, and so does one whose author added
// a placeholder since. The update overwrites plot text and scenario-derived
// cards, so nothing happens until you press Update.

import { useState } from 'react'
import { createPortal } from 'react-dom'
import { FIELD_LABELS, clip } from './format'

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

export { RefreshModal }
