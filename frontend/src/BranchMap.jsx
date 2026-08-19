import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { CORNER, PAD, ROW_H, branchLabel, headLineage, layoutTree, momentTicks } from './branches'

// The story tree, drawn.
//
// The Branches panel lists every line; this draws the same list as lanes on one
// clock, which is the thing a list cannot say — *where* two tellings parted, and
// how much story each of them is. A lane runs from the moment its branch left
// its parent to the moment it ends, so length is story and a fork is a corner.
//
// It reads nothing of its own. Everything here comes from the `GET /branches`
// the panel already made, and every operation goes back through the panel's, so
// there is one copy of the rules and one thing to keep honest.
//
// Portalled to <body>: this is opened from inside `.side-panel`, whose panel-in
// animation (fill mode `both`) makes it the containing block for position:fixed
// children — an overlay rendered in place would be trapped in the 420px panel
// and clipped by its overflow. Same trap for anything opened from a drawer.

// A label is drawn into the space its lane leaves. `perChar` is the average
// width of the face it is drawn in — about 7px for the 15px serif a name uses
// and 5px for the 10.5px UI face under it. Overshooting only costs an ellipsis,
// so this is deliberately a guess rather than a measurement pass.
function clipToWidth(text, px, perChar = 7) {
  const max = Math.max(4, Math.floor(px / perChar))
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`
}

export function BranchMap({ branches, busyId, onSwitch, onRename, onDelete, onClose }) {
  const [selectedId, setSelectedId] = useState(() => branches.find((b) => b.is_head)?.id ?? null)
  const [renameText, setRenameText] = useState(null)   // null = not renaming
  const [confirming, setConfirming] = useState(false)
  const [width, setWidth] = useState(0)
  const boxRef = useRef(null)

  const { lanes, maxDepth, height } = useMemo(() => layoutTree(branches), [branches])
  const lineage = useMemo(() => headLineage(branches), [branches])


  // The map is drawn in real pixels rather than scaled from a fixed viewBox,
  // so a narrow window gets a narrower map and not smaller writing.
  //
  // Both halves are load-bearing, and each covers the other's gap. The seed
  // measures the *content* box, because that is what the observer reports and
  // `clientWidth` is not — it counts the canvas padding, so seeding from it
  // drew an svg 24px wider than the box it sits in and the map ran off the
  // right edge. The observer then keeps up with a window being dragged; it
  // cannot be the only source, because its initial observation is not
  // guaranteed to arrive and without the seed the map never drew at all.
  useLayoutEffect(() => {
    const el = boxRef.current
    if (!el) return undefined
    const style = getComputedStyle(el)
    const padding = parseFloat(style.paddingLeft) + parseFloat(style.paddingRight)
    setWidth(el.clientWidth - padding)
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width))
    observer.observe(el)
    return () => observer.disconnect()
  }, [])

  // A branch can vanish under the selection — deleting one takes everything
  // forked from it, which is more rows than the one that was clicked.
  const selected = branches.find((b) => b.id === selectedId) || null
  useEffect(() => {
    if (!selected) {
      setSelectedId(branches.find((b) => b.is_head)?.id ?? null)
      setRenameText(null)
      setConfirming(false)
    }
  }, [selected, branches])

  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== 'Escape') return
      // Escape backs out of the smallest thing that is open, so it never
      // throws away a half-typed name along with the map.
      if (renameText !== null) setRenameText(null)
      else if (confirming) setConfirming(false)
      else onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [renameText, confirming, onClose])

  const W = Math.max(width, 320)
  const span = Math.max(maxDepth, 1)
  // Ticks are spaced by the room there is to print them in, not by a constant.
  const ticks = momentTicks(maxDepth, Math.max(3, Math.round(W / 160)))
  const x = (depth) => PAD.left + (depth / span) * (W - PAD.left - PAD.right)
  const laneY = (row) => PAD.top + row * ROW_H + 30

  const pick = (branch) => {
    setSelectedId(branch.id)
    setRenameText(null)
    setConfirming(false)
  }

  const busy = busyId !== null && busyId !== undefined
  // The server refuses to delete the line being read or anything it was forked
  // from; saying so on the button is friendlier than a toast after the click.
  const isLoadBearing = selected ? lineage.has(selected.id) : false
  const isRoot = selected ? selected.parent_branch_id === null : false

  // Each of these resolves to whether it worked. The panel owns the request
  // and reports its own failures, so all this has to decide is whether to put
  // the editor away — clearing a half-typed name on a rename that was refused
  // would throw away the only copy of it.
  const save = async () => { if (await onRename(selected, renameText)) setRenameText(null) }
  const drop = async () => { if (await onDelete(selected)) setConfirming(false) }

  return createPortal(
    <div className="modal-overlay branch-map-overlay" onClick={onClose}>
      <div className="branch-map" onClick={(e) => e.stopPropagation()}
        role="dialog" aria-modal="true" aria-label="The story so far">
        <div className="branch-map-header">
          <h2>The story so far</h2>
          <span className="branch-map-scale">
            {branches.length} {branches.length === 1 ? 'line' : 'lines'} · {maxDepth + 1} moments
          </span>
          <button type="button" className="branch-map-close" onClick={onClose} aria-label="Close">✕</button>
        </div>

        <div className="branch-map-canvas" ref={boxRef}>
          {width > 0 && (
            <svg className="branch-map-svg" width={W} height={height}
              role="img" aria-label={`${branches.length} branches over ${maxDepth + 1} moments`}>
              {/* The clock the whole map is read against. */}
              {ticks.map((depth) => (
                <g key={depth} className="bm-tick">
                  <line x1={x(depth)} y1={PAD.top - 16} x2={x(depth)} y2={height - PAD.bottom} />
                  <text x={x(depth)} y={PAD.top - 24} textAnchor="middle">{depth + 1}</text>
                </g>
              ))}

              {lanes.map((lane) => {
                const child = lane.branch.parent_branch_id !== null && lane.parentRow !== null
                const forkX = x(lane.from)
                const start = child ? forkX + CORNER : x(lane.from)
                // A branch forked but not yet written past still gets a stub,
                // or it would be a corner leading to nothing.
                const end = Math.max(x(lane.to), start + 8)
                const y = laneY(lane.row)
                const b = lane.branch
                const label = branchLabel(b)
                const meta = `${b.own_actions} of its own`
                  + (b.parent_branch_id !== null ? ` · left at ${b.fork_depth + 1}` : '')
                  + ` · ends at ${b.depth + 1}`
                // A lane that leaves late has no room to write in to its right,
                // so its labels hang back over the fork instead. The row is its
                // own band with nothing else in it, and the alternative is text
                // running off the edge — which is what a narrow window did.
                const roomRight = W - PAD.right - start
                const flip = roomRight < 170 && start - PAD.left > roomRight
                const textX = flip ? start - 4 : start
                const room = flip ? start - 4 - PAD.left : roomRight
                const classes = [
                  'bm-lane',
                  b.is_head ? 'here' : '',
                  b.id === selectedId ? 'picked' : '',
                ].join(' ')
                return (
                  <g key={b.id} className={classes}
                    role="button" tabIndex={0}
                    aria-label={`${label}, ${b.own_actions} of its own${b.is_head ? ', the line you are reading' : ''}`}
                    onClick={() => pick(b)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(b) }
                    }}>
                    {/* Full-width hit area: the lane itself is 3px tall and a
                        fork late in a long story is a very small target. */}
                    <rect className="bm-hit" x={0} y={PAD.top + lane.row * ROW_H}
                      width={W} height={ROW_H} />

                    {child && (
                      <path className="bm-fork"
                        d={`M ${forkX} ${laneY(lane.parentRow)} L ${forkX} ${y - CORNER} Q ${forkX} ${y} ${forkX + CORNER} ${y}`} />
                    )}
                    {child && <circle className="bm-fork-dot" cx={forkX} cy={laneY(lane.parentRow)} r={3.5} />}

                    <line className="bm-line" x1={start} y1={y} x2={end} y2={y} />

                    {/* The tip: a diamond for the line being read, so where you
                        are standing is findable without reading a word. */}
                    {b.is_head ? (
                      <rect className="bm-tip" x={end - 5} y={y - 5} width={10} height={10}
                        transform={`rotate(45 ${end} ${y})`} />
                    ) : (
                      <circle className="bm-tip" cx={end} cy={y} r={4.5} />
                    )}

                    <text className="bm-name" x={textX} y={y - 12}
                      textAnchor={flip ? 'end' : 'start'}>
                      {clipToWidth(label, room)}
                    </text>
                    <text className="bm-meta" x={textX} y={y + 19}
                      textAnchor={flip ? 'end' : 'start'}>
                      {clipToWidth(meta, room, 5)}
                    </text>
                  </g>
                )
              })}
            </svg>
          )}
        </div>

        {branches.length === 1 && (
          <p className="branch-map-hint">
            One thread so far. Retry a turn, then write on from an attempt the
            story moved past — that is what makes a second line.
          </p>
        )}

        {selected && (
          <div className={`branch-map-detail ${selected.is_head ? 'here' : ''}`}>
            <div className="bmd-head">
              {renameText !== null ? (
                <input
                  className="branch-rename"
                  autoFocus
                  maxLength={80}
                  value={renameText}
                  onChange={(e) => setRenameText(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') save()
                  }}
                />
              ) : (
                <span className="branch-name">{branchLabel(selected)}</span>
              )}
              {selected.is_head && <span className="branch-here">reading</span>}
            </div>

            <div className="branch-meta">
              {selected.own_actions} of its own
              {selected.parent_branch_id !== null && ` · forked at moment ${selected.fork_depth + 1}`}
              {` · ends at moment ${selected.depth + 1}`}
            </div>

            {confirming ? (
              <div className="branch-confirm">
                <span>Delete this branch and everything forked from it?</span>
                <button type="button" className="danger" disabled={busy} onClick={drop}>
                  Delete
                </button>
                <button type="button" onClick={() => setConfirming(false)}>Keep</button>
              </div>
            ) : (
              <div className="branch-tools">
                {!selected.is_head && (
                  <button type="button" disabled={busy}
                    onClick={() => onSwitch(selected)}>Switch to this line</button>
                )}
                {renameText !== null ? (
                  <>
                    <button type="button" disabled={busy} onClick={save}>Save</button>
                    <button type="button" onClick={() => setRenameText(null)}>Cancel</button>
                  </>
                ) : (
                  <button type="button" disabled={busy}
                    onClick={() => setRenameText(selected.name || '')}>Rename</button>
                )}
                {/* The root holds the turns every other branch borrows, and the
                    server refuses it — so it is not offered at all. A branch
                    the head stands on is offered, and says why it cannot go. */}
                {!isRoot && (
                  <button type="button" className="danger"
                    disabled={busy || isLoadBearing}
                    title={isLoadBearing
                      ? 'The line you are reading is built on this one. Switch away first.'
                      : undefined}
                    onClick={() => setConfirming(true)}>Delete</button>
                )}
              </div>
            )}
          </div>
        )}
      </div>
    </div>,
    document.body,
  )
}
