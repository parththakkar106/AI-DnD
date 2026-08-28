// The branch panel: every line the story has taken, and the map of them.
//
// One request draws the whole panel. `fork_depth` says where a branch leaves
// its parent and `depth` says where it currently ends, so the shape is two
// numbers a row rather than a walk.
//
// Delete belongs here rather than in a later subphase because nothing prunes
// the tree on its own. This panel is the first place you can make a fork, so
// it has to be the first place you can unmake one.

import { useEffect, useState } from 'react'
import { api } from '../../../api'
import { BranchMap } from '../../../BranchMap'
import { branchLabel, headLineage, orderBranches } from '../../../branches'

function BranchPanel({ advId, refreshKey, onSwitched, onTreeChanged, onError }) {
  const [branches, setBranches] = useState(null)
  const [failed, setFailed] = useState(null)
  const [busyId, setBusyId] = useState(null)
  const [renaming, setRenaming] = useState(null)   // { id, text }
  const [confirming, setConfirming] = useState(null)
  const [mapOpen, setMapOpen] = useState(false)
  const [tick, setTick] = useState(0)

  useEffect(() => {
    let cancelled = false
    setFailed(null)
    api.listBranches(advId)
      .then((list) => { if (!cancelled) setBranches(list) })
      .catch((err) => { if (!cancelled) setFailed(err.message) })
    return () => { cancelled = true }
  }, [advId, refreshKey, tick])

  // Answers whether it worked. Both callers keep an editor open on a refusal —
  // a rename the server turned down must not take the typed name with it.
  async function run(branchId, work) {
    setBusyId(branchId)
    try {
      await work()
      setTick((t) => t + 1)
      // Deleting a branch takes its memories with it, and nothing else on the
      // screen would hear about that — no turn is played, and the story on the
      // current path does not change by a single action.
      onTreeChanged()
      return true
    } catch (err) {
      onError(err.message)
      return false
    } finally {
      setBusyId(null)
    }
  }

  // One copy of each operation. The list below and the map overlay both go
  // through these, so a rule cannot hold in one view and not the other, and a
  // failure is reported one way wherever it was asked for.
  const switchTo = (b) => run(b.id, async () => onSwitched(await api.switchBranch(advId, b.id)))
  const renameTo = (b, name) => run(b.id, () => api.renameBranch(advId, b.id, name))
  const removeBranch = (b) => run(b.id, () => api.deleteBranch(advId, b.id))

  const saveName = async (b) => { if (await renameTo(b, renaming.text)) setRenaming(null) }
  const remove = async (b) => { if (await removeBranch(b)) setConfirming(null) }

  if (failed) return <div className="panel-empty">Couldn’t read the branches — {failed}</div>
  if (!branches) return <div className="panel-empty">Reading the tree…</div>

  const lineage = headLineage(branches)

  return (
    <div className="branch-panel">
      {/* The list says which lines exist; the map says where they parted and
          how much story each one is, which is the part a list cannot draw. */}
      <button type="button" className="branch-map-open" onClick={() => setMapOpen(true)}>
        ⌗ See the tree
      </button>
      {branches.length === 1 && (
        <p className="branch-intro">
          One thread so far. Retry a turn, then take an attempt the story moved
          past — that is what makes a second one.
        </p>
      )}
      <div className="branch-list">
        {orderBranches(branches).map(({ branch, indent }) => {
          const isRenaming = renaming?.id === branch.id
          const isConfirming = confirming === branch.id
          const busy = busyId === branch.id
          // The server refuses to delete the line being read or any line it
          // was forked from. The button said nothing about that and answered
          // with a toast; it now says so before it is pressed.
          const loadBearing = lineage.has(branch.id)
          return (
            <div key={branch.id} className={`branch-row ${branch.is_head ? 'here' : ''}`}
              style={{ marginLeft: indent * 12 }}>
              <div className="branch-head">
                <span className="branch-glyph" aria-hidden="true">
                  {branch.parent_branch_id === null ? '●' : '└'}
                </span>
                {isRenaming ? (
                  <input
                    className="branch-rename"
                    autoFocus
                    maxLength={80}
                    value={renaming.text}
                    onChange={(e) => setRenaming({ ...renaming, text: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') saveName(branch)
                      if (e.key === 'Escape') setRenaming(null)
                    }}
                  />
                ) : (
                  <span className="branch-name">{branchLabel(branch)}</span>
                )}
                {branch.is_head && <span className="branch-here">reading</span>}
              </div>

              <div className="branch-meta">
                {branch.own_actions} of its own
                {branch.parent_branch_id !== null && ` · forked at moment ${branch.fork_depth + 1}`}
                {` · ends at ${branch.depth + 1}`}
              </div>

              {isConfirming ? (
                <div className="branch-confirm">
                  <span>Delete this branch and everything forked from it?</span>
                  <button type="button" className="danger" disabled={busy}
                    onClick={() => remove(branch)}>Delete</button>
                  <button type="button" onClick={() => setConfirming(null)}>Keep</button>
                </div>
              ) : (
                <div className="branch-tools">
                  {!branch.is_head && (
                    <button type="button" disabled={busy} onClick={() => switchTo(branch)}>Switch</button>
                  )}
                  {isRenaming ? (
                    <>
                      <button type="button" disabled={busy} onClick={() => saveName(branch)}>Save</button>
                      <button type="button" onClick={() => setRenaming(null)}>Cancel</button>
                    </>
                  ) : (
                    <button type="button" disabled={busy}
                      onClick={() => setRenaming({ id: branch.id, text: branch.name || '' })}>
                      Rename
                    </button>
                  )}
                  {/* The root holds the turns every other branch borrows, and
                      the server refuses it — so it is not offered. */}
                  {branch.parent_branch_id !== null && (
                    <button type="button" className="danger" disabled={busy || loadBearing}
                      title={loadBearing
                        ? 'The line you are reading is built on this one. Switch away first.'
                        : undefined}
                      onClick={() => setConfirming(branch.id)}>Delete</button>
                  )}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {mapOpen && (
        <BranchMap
          branches={branches}
          busyId={busyId}
          onSwitch={switchTo}
          onRename={renameTo}
          onDelete={removeBranch}
          onClose={() => setMapOpen(false)}
        />
      )}
    </div>
  )
}

export { BranchPanel }
