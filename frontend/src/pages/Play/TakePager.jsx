// The pager that steps between the attempts at one coordinate: ‹ 2/4 ›, and
// nothing else.
//
// SP7 shipped chips instead, because a chip could also offer "take this path"
// where a pager can only step. Driving it by hand said otherwise. The chip
// meant two things depending on where you were standing: a real switch at the
// tip, or a preview that needed a second button above it. Two meanings in one
// control is what made the tree unusable.
//
// So the pager is back, and stepping is all it does. Stepping tells the server
// nothing, because reading a take is not a decision. You decide by writing
// below a take, and that is where the branch is created (SP9, `after_id`).
//
// One step does reach the server, and it is not a fork either. A take that has
// a story of its own lives on its own branch, so going there is a branch
// switch. The story below it has to change, and only the server can say to
// what. A take on this branch is a leaf by construction. Whatever was played
// after this turn was played after the take that is live, so a take that is
// not live has nothing under it and the transcript ends there.

import { useEffect, useState } from 'react'
import { api } from '../../api'

function TakePager({
  advId, action, busy, preview, takesKey, onPreview, onSwitchedBranch, onError,
}) {
  const [takes, setTakes] = useState(null)
  const [loading, setLoading] = useState(false)
  // The cached list is only as good as the text in it. Editing a take
  // rewrites one of those rows, so the page says so and the list is fetched
  // again on the next step.
  useEffect(() => { setTakes(null) }, [takesKey])
  const count = action.take_count
  const live = action.take_index
  const current = preview ? preview.index : live

  async function step(delta) {
    const next = current + delta
    if (next < 0 || next >= count || loading || busy) return
    setLoading(true)
    try {
      // Fetched once per message, then cached — walking back and forth through
      // the takes should not re-hit the server for a list that has not changed.
      const list = takes || await api.listTakes(advId, action.id)
      if (!takes) setTakes(list)
      const target = list[next]
      if (target.branch_id !== action.branch_id) {
        // It has a story of its own. Only the server knows what is under it.
        onPreview(null)
        onSwitchedBranch(await api.switchBranch(advId, target.branch_id))
      } else if (next === live) {
        onPreview(null)
      } else {
        onPreview({
          actionId: action.id,
          index: next,
          // The take's own node id, never its ordinal: the group renumbers
          // whenever a take is added, and an ordinal held across that points
          // at a different one. This is what `after_id` is given if the reader
          // writes from here.
          takeId: target.id,
          text: target.text,
          reasoning: target.reasoning,
        })
      }
    } catch (err) {
      onError(err.message)
    } finally {
      setLoading(false)
    }
  }

  if (count < 2) return null
  return (
    <div className="take-pager">
      <button type="button" disabled={busy || loading || current === 0}
        onClick={() => step(-1)} title="The take before this one" aria-label="Previous take">‹</button>
      <span className="take-count" aria-live="polite">{current + 1}/{count}</span>
      <button type="button" disabled={busy || loading || current === count - 1}
        onClick={() => step(1)} title="The take after this one" aria-label="Next take">›</button>
      {preview && !preview.written && (
        <span className="take-note">write below to keep this one</span>
      )}
    </div>
  )
}

export { TakePager }
