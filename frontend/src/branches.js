// The story tree, as numbers something can be drawn from.
//
// `GET /adventures/{id}/branches` answers two numbers per branch — `fork_depth`,
// where a line leaves its parent, and `depth`, where it currently ends — which
// is deliberately enough to draw the whole shape without walking a single node.
// Both readers of that shape live behind this file: the list in the Branches
// panel and the map overlay. They order and label a branch the same way because
// they order and label it *here*, so a branch cannot appear in one and not the
// other, or be called two different things by the two of them.

// Derived, never stored: a generated name in the column would go stale the
// moment a branch before it is deleted. A fork depth is a coordinate, so it
// says the same thing whatever else is thrown away.
export function branchLabel(branch) {
  if (branch.name) return branch.name
  if (branch.parent_branch_id === null) return 'The first telling'
  return `Fork at moment ${branch.fork_depth + 1}`
}

// Parents before children, each child under the branch it left.
export function orderBranches(branches) {
  const kids = new Map()
  for (const b of branches) {
    const key = b.parent_branch_id
    if (!kids.has(key)) kids.set(key, [])
    kids.get(key).push(b)
  }
  const out = []
  const walk = (parentId, indent) => {
    for (const b of kids.get(parentId) || []) {
      out.push({ branch: b, indent })
      walk(b.id, indent + 1)
    }
  }
  // Anything whose parent is missing would otherwise never be walked. That
  // cannot happen through the API, but a list that silently drops a branch is
  // the one bug this panel exists to make impossible to have.
  walk(null, 0)
  const seen = new Set(out.map((row) => row.branch.id))
  for (const b of branches) if (!seen.has(b.id)) out.push({ branch: b, indent: 0 })
  return out
}

// The branches the head is standing on: itself, and everything it borrows from.
//
// This is the client's copy of the server's delete rule — `parent_branch_id`
// cascades, so deleting an ancestor of the head takes the head with it and
// leaves `head_branch_id` pointing at a row that is gone. The server refuses
// exactly this set; computing it here only means the button can say so before
// it is pressed. The server stays the authority.
export function headLineage(branches) {
  const byId = new Map(branches.map((b) => [b.id, b]))
  const out = new Set()
  let cur = branches.find((b) => b.is_head)
  // The guard is against a cycle, which the schema forbids and a walk should
  // still never hang on.
  while (cur && !out.has(cur.id)) {
    out.add(cur.id)
    cur = cur.parent_branch_id === null ? null : byId.get(cur.parent_branch_id)
  }
  return out
}

// ---------- Map geometry ----------

export const ROW_H = 64          // one branch, name above the lane and meta below
export const PAD = { top: 44, right: 26, bottom: 20, left: 24 }
export const CORNER = 11         // radius of the elbow a fork turns through

// Place every branch on its own horizontal lane, in tree order.
//
// A lane runs from where its branch left its parent to where its branch ends,
// so the horizontal axis is the story's own clock: two branches at the same x
// are at the same moment, and the length of a lane is how much of the story it
// covers. Nothing here is measured in pixels — the component owns the mapping
// from a moment to an x, because only it knows how wide it ended up.
export function layoutTree(branches) {
  const rows = orderBranches(branches)
  const rowOf = new Map(rows.map((row, i) => [row.branch.id, i]))
  const lanes = rows.map(({ branch }, row) => ({
    branch,
    row,
    // The first telling starts where the story does; every other line starts
    // where it walked away from another one.
    from: branch.parent_branch_id === null ? 0 : (branch.fork_depth ?? 0),
    to: branch.depth,
    parentRow: branch.parent_branch_id === null
      ? null
      : (rowOf.has(branch.parent_branch_id) ? rowOf.get(branch.parent_branch_id) : null),
  }))
  return {
    lanes,
    // The whole map is scaled to the longest path, so a short branch reads as
    // short. `|| 1` keeps a one-moment story from dividing by zero.
    maxDepth: Math.max(0, ...branches.map((b) => b.depth)),
    height: PAD.top + rows.length * ROW_H + PAD.bottom,
  }
}

// Round tick marks for the moment axis: about `count` of them, landing on
// numbers a person would have chosen (1, 2, 5, 10, 25, 50 …) rather than on
// whatever `maxDepth / 6` happens to be.
export function momentTicks(maxDepth, count = 6) {
  if (maxDepth <= 0) return [0]
  const raw = maxDepth / count
  const magnitude = 10 ** Math.floor(Math.log10(raw))
  const step = [1, 2, 2.5, 5, 10].map((m) => m * magnitude).find((s) => s >= raw) || magnitude * 10
  const out = []
  for (let d = 0; d <= maxDepth; d += step) out.push(Math.round(d))
  // The last moment is worth naming, but not on top of the tick before it —
  // at a narrow width `26` and `27` printed as `2627`.
  const last = out[out.length - 1]
  if (maxDepth - last > step * 0.6) out.push(maxDepth)
  else if (last !== maxDepth) out[out.length - 1] = maxDepth
  return out
}
