/* The Play screen.
 *
 * This file holds the page component and the two helpers only it uses. The
 * panels, drawers, and reports it renders are separate modules, listed in the
 * imports below.
 *
 * The page component still owns all of the session state. Lifting it into a
 * `usePlaySession` hook waits for Stage 5 of `plan/17-refactor.md`, which adds
 * a frontend test runner. Moving eighteen `useState` calls and seven
 * `useEffect` calls is a rewrite rather than a move, and nothing would catch a
 * mistake in it today.
 */

import { Fragment, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../../api'
import { AutoTextarea } from '../../components'
import { StateChangeChips } from './reports'
import { TakePager } from './TakePager'
import { StatusDrawer } from './drawers/StatusDrawer'
import { WorldStateDrawer } from './drawers/WorldStateDrawer'
import { BranchPanel } from './panels/BranchPanel'
import { InsightsPanel } from './panels/InsightsPanel'
import { MemoryPanel } from './panels/MemoryPanel'
import { PlotPanel } from './panels/PlotPanel'
import { ScriptsPanel } from './panels/ScriptsPanel'

const MODES = ['do', 'say', 'story']
const PLAYER_TYPES = ['do', 'say', 'story']

// Models often emit light markdown emphasis; render **bold** / *italic*
// instead of showing raw asterisks. Everything else stays plain text.
function renderEmphasis(text) {
  const re = /\*\*([^*\n]+)\*\*|\*([^*\n]+)\*/g
  const parts = []
  let last = 0
  let match
  while ((match = re.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index))
    parts.push(match[1] !== undefined
      ? <b key={match.index}>{match[1]}</b>
      : <i key={match.index}>{match[2]}</i>)
    last = match.index + match[0].length
  }
  if (parts.length === 0) return text
  if (last < text.length) parts.push(text.slice(last))
  return parts
}

function ReasoningBlock({ text, streaming }) {
  if (!text) return null
  return (
    <details className="reasoning" open={streaming || undefined}>
      <summary>💭 Reasoning{streaming ? '…' : ''}</summary>
      <div className="reasoning-text">{text}</div>
    </details>
  )
}


export default function Play() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [adventure, setAdventure] = useState(null)
  const [actions, setActions] = useState([])
  const [mode, setMode] = useState('do')
  const [input, setInput] = useState('')
  const [streaming, setStreaming] = useState(null)
  const [reasoningStream, setReasoningStream] = useState(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState(null)
  const [editing, setEditing] = useState(null)
  const [panel, setPanel] = useState(null) // null | 'plot' | 'insights'
  // Bumped when something outside the turn loop changes the drawers' state
  // (currently "Update from scenario"), which no action count would reflect.
  const [stateKey, setStateKey] = useState(0)
  const [inspectActionId, setInspectActionId] = useState(null)
  // Which take is being read, when it is not the live one (see TakePager).
  // One at a time; null when every message is showing the take the story tells.
  //
  // Purely local: the server is not told, because reading a take is not a
  // decision. It becomes one when something is written below it, and that is
  // what `after_id` carries.
  const [preview, setPreview] = useState(null)
  // The take a turn was just written below, held from the moment Send is
  // pressed until the re-read lands.
  //
  // Writing below a take is the one moment the server IS told (`after_id`), and
  // it obeys immediately — the take is made live before a single token is
  // generated. The transcript only learns that from the resync afterwards, so
  // dropping the preview at Send time put the *replaced* take back on screen
  // for the whole length of the turn, and left it there for good if the resync
  // never landed (a failed turn, a lost connection, a closed tab) — the story
  // read one way and reloading the page read another.
  //
  // So the text stays pinned to what was chosen. Unlike `preview` it does not
  // truncate the transcript below it: the turn being played goes there.
  const [pinned, setPinned] = useState(null)
  // Bumped when a take's stored text changes under the pagers, which cache the
  // list they fetched. Nothing else invalidates it: a take is added by playing
  // a turn, and that re-reads the whole window anyway.
  const [takesKey, setTakesKey] = useState(0)
  // The transcript is a window on the story, not the whole of it: the page
  // load brings the newest page and older ones arrive as the reader scrolls
  // up. `total` is the story's real length, for the "N earlier" line.
  const [total, setTotal] = useState(0)
  const [hasMore, setHasMore] = useState(false)
  const [loadingOlder, setLoadingOlder] = useState(false)
  const storyEndRef = useRef(null)
  const abortRef = useRef(null)
  const pinnedRef = useRef(true) // autoscroll only while the reader is at the bottom
  const inputRef = useRef(null)
  // Set just before older actions are prepended, read once afterwards to put
  // the reader back where they were. See the layout effect below.
  const restoreScrollRef = useRef(null)
  const loadingOlderRef = useRef(false)
  // Earliest time another attempt is allowed after a failure. See the catch
  // in loadOlder.
  const retryAfterRef = useRef(0)

  // The drop cap belongs to the story's first narrated beat. `start` is the
  // scenario's opening prompt, so it's usually that; an adventure begun blank
  // has no `start` action and the first AI reply takes it instead. Tracked by
  // id rather than position so deleting earlier turns moves the cap correctly
  // instead of stranding it on a removed row.
  const firstNarrationId = useMemo(
    () => actions.find((a) => a.type === 'start' || a.type === 'ai')?.id ?? null,
    [actions],
  )
  // Where the transcript stops while a take that is not the live one is being
  // read. Such a take is a leaf by construction — whatever was played after
  // this turn was played after the take that *is* live — so there is nothing
  // under it, and showing the rest would attach one line's story to another's
  // text. -1 while nothing is being previewed, which is the ordinary case.
  const previewCutoff = useMemo(
    () => (preview ? actions.findIndex((a) => a.id === preview.actionId) : -1),
    [preview, actions],
  )
  // send() sets streaming to '' before the request goes out; reasoningStream
  // stays null until reasoning tokens (if any) arrive. Both still at those
  // values means the request is in flight with nothing to show yet.
  const waitingForFirstToken = streaming === '' && reasoningStream === null

  // Grow the action box with its content (CSS caps it at ~4 lines, then
  // scrolls); shrinks back after send() clears the text.
  useEffect(() => {
    const el = inputRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${el.scrollHeight}px`
  }, [input])

  useEffect(() => {
    api.getAdventure(id)
      .then((adv) => {
        setAdventure(adv)
        setActions(adv.actions)
        setTotal(adv.action_count ?? adv.actions.length)
        setHasMore(adv.actions.length < (adv.action_count ?? adv.actions.length))
      })
      .catch(() => navigate('/'))
  }, [id, navigate])

  // Take the story the server just handed back, whole.
  //
  // Switching a branch and forking one both answer with the newest window of
  // the story as it now stands, so there is nothing to merge — the window on
  // screen belonged to a path that is no longer the one being read. Pinning
  // back to the bottom is deliberate: a switch lands the reader at the tip of
  // the line they moved to, which is where the next turn will appear.
  const adoptWindow = useCallback((page) => {
    pinnedRef.current = true
    setPreview(null)
    setPinned(null)
    setActions(page.actions)
    setTotal(page.total)
    setHasMore(page.has_more)
    // The script and world state come back to what that branch's tip left
    // behind, so anything drawn from them is now showing another line's
    // numbers until it re-reads.
    setStateKey((k) => k + 1)
  }, [])

  // Fetch the page above the one on screen and prepend it.
  //
  // Anchored on the oldest action we hold rather than on a count, so a turn
  // landing while the reader scrolls cannot shift the page. Guarded by a ref
  // as well as state because scroll fires far faster than React re-renders,
  // and two in-flight requests would fetch the same page twice.
  const loadOlder = useCallback(async () => {
    if (loadingOlderRef.current || !hasMore) return
    if (Date.now() < retryAfterRef.current) return
    const oldest = actions[0]
    if (!oldest) return
    loadingOlderRef.current = true
    setLoadingOlder(true)
    try {
      const page = await api.getActions(id, { beforeId: oldest.id })
      if (page.actions.length) {
        // Reading backwards is the opposite of following along, so stop
        // autoscrolling. Without this the bottom-pinning effect below fires on
        // the same `actions` change and throws the reader to the end of the
        // story — worst exactly where the button matters, on a window short
        // enough that it never scrolled and so never un-pinned itself.
        pinnedRef.current = false
        // Record the height before the prepend; the layout effect below uses
        // it to keep the reader looking at the same paragraph.
        restoreScrollRef.current = {
          height: document.documentElement.scrollHeight,
          top: window.scrollY,
        }
        setActions((prev) => {
          // Defensive: never let a page the reader already holds duplicate a
          // message. Cheap, and the alternative is a visibly doubled turn.
          const known = new Set(prev.map((a) => a.id))
          return [...page.actions.filter((a) => !known.has(a.id)), ...prev]
        })
      }
      setTotal(page.total)
      setHasMore(page.has_more)
    } catch {
      // Leave hasMore alone: a failed fetch should let the reader try again
      // by scrolling, not permanently hide the rest of their story. But hold
      // off briefly first — parked near the top, momentum scrolling fires this
      // dozens of times a second, and against an endpoint that is failing that
      // is a retry storm rather than a retry.
      retryAfterRef.current = Date.now() + 3000
    } finally {
      loadingOlderRef.current = false
      setLoadingOlder(false)
    }
  }, [actions, hasMore, id])

  // Put the viewport back after a prepend. useLayoutEffect, not useEffect:
  // this has to run before the browser paints, or the reader sees the story
  // jump and then snap back.
  useLayoutEffect(() => {
    const mark = restoreScrollRef.current
    if (!mark) return
    restoreScrollRef.current = null
    const grown = document.documentElement.scrollHeight - mark.height
    if (grown > 0) window.scrollTo({ top: mark.top + grown })
  }, [actions])

  useEffect(() => {
    const onScroll = () => {
      pinnedRef.current =
        window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 120
      // Start the next page before the reader reaches the top, so the story
      // is usually already there by the time they would have noticed its end.
      if (window.scrollY < 400) loadOlder()
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [loadOlder])

  useEffect(() => {
    // Snap to the real document bottom (below the sticky composer), not to
    // storyEndRef — that ref sits above the composer, so block:'end' would
    // stop short and fight a reader scrolling down. Instant, not smooth: at
    // streaming speed a queued smooth animation never settles.
    if (pinnedRef.current) {
      window.scrollTo({ top: document.documentElement.scrollHeight })
    }
  }, [actions, streaming, reasoningStream])

  const handleScriptReport = useCallback((script) => {
    if (!script) return
    if (script.errors?.length) {
      setToast({ text: `Script error: ${script.errors[0]}`, isError: true })
    } else if (script.message) {
      setToast({ text: script.message, isError: false })
    }
  }, [])

  const handleEvent = useCallback((event) => {
    if (event.type === 'player') {
      setActions((prev) => [...prev, event.action])
      // The window grew at the bottom, so the story did too. Kept in step by
      // hand because nothing re-reads the count between turns.
      setTotal((n) => n + 1)
    } else if (event.type === 'chunk') {
      setStreaming((prev) => (prev ?? '') + event.text)
    } else if (event.type === 'reasoning') {
      setReasoningStream((prev) => (prev ?? '') + event.text)
    } else if (event.type === 'done') {
      setStreaming(null)
      setReasoningStream(null)
      setActions((prev) => [...prev, event.action])
      setTotal((n) => n + 1)
      handleScriptReport(event.script)
    } else if (event.type === 'stopped') {
      setStreaming(null)
      setReasoningStream(null)
      handleScriptReport(event.script)
    } else if (event.type === 'error') {
      setStreaming(null)
      setReasoningStream(null)
      setToast({ text: event.detail, isError: true })
    }
  }, [handleScriptReport])

  async function runTurn(run) {
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(true)
    setToast(null)
    setStreaming('')
    pinnedRef.current = true
    try {
      await run(controller.signal)
    } catch (err) {
      if (err.name === 'AbortError') {
        setToast({ text: 'Generation stopped.', isError: false })
      } else {
        setToast({ text: err.message, isError: true })
      }
    } finally {
      abortRef.current = null
      setStreaming(null)
      setReasoningStream(null)
      setBusy(false)
    }
  }

  function stopGeneration() {
    abortRef.current?.abort()
  }

  function send(type = mode) {
    const text = input.trim()
    // Where the reader is standing. Stepping to a take the story moved past
    // told the server nothing; this is the moment it has to be told, and it is
    // the moment the branch is made (SP9).
    const after_id = preview?.takeId
    // The window below belongs to the line being left, so it is re-read rather
    // than appended to — same reasoning as `addTake`.
    const run = (payload) => runTurn(async (signal) => {
      try {
        await api.sendAction(id, payload, handleEvent, signal)
      } finally {
        if (after_id) await resync()
      }
    })
    // Keep the chosen take on screen while the turn runs. The server has
    // already been told to stand on it, so this is not optimism — it is the
    // transcript catching up with a decision that is already made.
    if (preview) setPinned({ ...preview, written: true })
    setPreview(null)
    if (type === 'continue') {
      // Continue never consumes typed text — leave it in the box.
      run({ type: 'continue', text: '', after_id })
      return
    }
    const payload = { type: text ? type : 'continue', text, after_id }
    setInput('')
    run(payload)
  }

  function retry() {
    setPreview(null)
    setActions((prev) =>
      prev.length && prev[prev.length - 1].type === 'ai' ? prev.slice(0, -1) : prev)
    runTurn(async (signal) => {
      try {
        await api.retry(id, handleEvent, signal)
      } catch (err) {
        // Failed retry (409, network): the optimistically removed action may
        // still exist server-side — resync instead of guessing. Resyncing
        // collapses the transcript back to the newest window, which is the
        // right call: the reader's place is already lost by the failure.
        api.getAdventure(id).then((adv) => {
          setActions(adv.actions)
          setTotal(adv.action_count ?? adv.actions.length)
          setHasMore(adv.actions.length < (adv.action_count ?? adv.actions.length))
        }).catch(() => {})
        throw err
      }
    })
  }

  async function undo() {
    setToast(null)
    setPreview(null)
    setPinned(null)
    try {
      // A window, not the whole story — undo is the action most likely to be
      // repeated several times running, so it must not re-fetch everything.
      const page = await api.undo(id)
      setActions(page.actions)
      setTotal(page.total)
      setHasMore(page.has_more)
    } catch (err) {
      setToast({ text: err.message, isError: true })
    }
  }

  // Ctrl+Z undo / Ctrl+R retry, ignored while typing in a field.
  useEffect(() => {
    const lastIsAi = actions.length > 0 && actions[actions.length - 1].type === 'ai'
    const canUndo = actions.length > 0 && actions[actions.length - 1].type !== 'start'
    const onKey = (e) => {
      if (!(e.ctrlKey || e.metaKey) || busy) return
      if (e.target.closest?.('input, textarea, select, [contenteditable]')) return
      if (e.key.toLowerCase() === 'z' && canUndo) {
        e.preventDefault()
        undo()
      } else if (e.key.toLowerCase() === 'r' && lastIsAi) {
        e.preventDefault()
        retry()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  // A take-edit belongs to the preview that opened it. Anything that leaves
  // that take — playing a turn, switching branch — takes the box with it, so
  // the pending edit goes too rather than being saved onto a take nobody is
  // looking at any more.
  useEffect(() => {
    if (editing?.take && preview?.takeId !== editing.id) setEditing(null)
  }, [editing, preview])

  async function saveEdit() {
    const { id: actionId, text, fork, take } = editing
    setEditing(null)
    if (fork) {
      // Not an edit at all: the turn is played again with this text, and what
      // the story made of the old text is kept on the line it was written on.
      addTake(actionId, text)
      return
    }
    try {
      const updated = await api.updateAction(id, actionId, text)
      // A take that is only being read is not in `actions` — the row there is
      // the live one — so the new text goes back into the preview, which is
      // what that row is drawing. The pager holds the take list it fetched, so
      // it is told to drop it: stepping away and back would otherwise show the
      // words before the edit.
      if (take) {
        setPreview((p) => (p && p.takeId === actionId ? { ...p, text: updated.text } : p))
        setTakesKey((k) => k + 1)
      } else {
        setActions((prev) => prev.map((a) => (a.id === actionId ? updated : a)))
      }
    } catch (err) {
      setToast({ text: err.message, isError: true })
    }
  }

  // Play a turn again, differently. Anywhere in the story, either kind of node.
  //
  // The transcript is re-read rather than appended to, which is the difference
  // from an ordinary turn: a take above the tip leaves the line it was on and
  // the whole window below it belongs to a story this branch no longer tells.
  // `handleEvent` appends the new node as it streams; the resync afterwards is
  // what drops everything that is no longer under it.
  function addTake(actionId, text) {
    setPreview(null)
    runTurn(async (signal) => {
      try {
        await api.addTake(id, actionId, text, handleEvent, signal)
      } finally {
        await resync()
      }
    })
  }

  // Re-read the newest window from the server.
  //
  // For a turn that left the line it was on: `handleEvent` appends the new node
  // as it streams, and everything already on screen below the take belongs to a
  // story this branch no longer tells. Only the server can say what replaces
  // it. A failed resync leaves the transcript stale rather than wrong, so it is
  // swallowed — the next page load settles it.
  async function resync() {
    try {
      const adv = await api.getAdventure(id)
      // The window now says which take is live, so the pin has done its job.
      // Left in place if this read fails, which is the whole point of it.
      setPinned(null)
      setActions(adv.actions)
      setTotal(adv.action_count ?? adv.actions.length)
      setHasMore(adv.actions.length < (adv.action_count ?? adv.actions.length))
      // The branch, the script state and the world state can all have moved.
      setStateKey((k) => k + 1)
    } catch { /* stale beats wrong */ }
  }

  async function removeAction(actionId) {
    try {
      await api.deleteAction(id, actionId)
      setActions((prev) => prev.filter((a) => a.id !== actionId))
      setTotal((n) => Math.max(0, n - 1))
    } catch (err) {
      setToast({ text: err.message, isError: true })
    }
  }

  function inspect(actionId) {
    setInspectActionId(actionId)
    setPanel('insights')
  }

  if (!adventure) return null

  const lastIsAi = actions.length > 0 && actions[actions.length - 1].type === 'ai'
  const canUndo = actions.length > 0 && actions[actions.length - 1].type !== 'start'

  return (
    <div className={`play-layout ${panel ? 'with-panel' : ''}`}>
      {/* Both drawers read per-adventure state that a branch switch puts back,
          so neither can key on the story's length alone: switching between two
          branches whose windows are both full changes every number in here
          without changing `actions.length` by one. */}
      <WorldStateDrawer advId={id} refreshKey={`${actions.length}:${stateKey}`} />
      <StatusDrawer advId={id} refreshKey={`${actions.length}:${stateKey}`} />
      <div className="page play-page">
        <div className="page-header">
          <h1>{adventure.title}</h1>
          <div className="panel-toggles">
            <button className={panel === 'plot' ? 'active' : ''}
              onClick={() => setPanel(panel === 'plot' ? null : 'plot')}>Plot</button>
            <button className={panel === 'memory' ? 'active' : ''}
              onClick={() => setPanel(panel === 'memory' ? null : 'memory')}>Memory</button>
            <button className={panel === 'scripts' ? 'active' : ''}
              onClick={() => setPanel(panel === 'scripts' ? null : 'scripts')}>Scripts</button>
            <button className={panel === 'branches' ? 'active' : ''}
              onClick={() => setPanel(panel === 'branches' ? null : 'branches')}>Branches</button>
            <button className={panel === 'insights' ? 'active' : ''}
              onClick={() => { setInspectActionId(null); setPanel(panel === 'insights' ? null : 'insights') }}>
              Insights
            </button>
          </div>
        </div>

        <div className="story">
          {actions.length === 0 && streaming === null && (
            <div className="empty">A blank page. Type something below to begin your story.</div>
          )}
          {/* Scrolling up loads the rest. The button is not decoration: on a
              short viewport the story may not be tall enough to scroll at all,
              and a reader who cannot scroll must still be able to get back to
              the beginning. */}
          {hasMore && (
            <div className="story-earlier">
              {loadingOlder ? (
                <span className="dim">Turning back the pages…</span>
              ) : (
                <button type="button" onClick={loadOlder}>
                  {Math.max(total - actions.length, 0)} earlier
                  {total - actions.length === 1 ? ' moment' : ' moments'}
                </button>
              )}
            </div>
          )}
          {actions.map((action, i) => {
            // Below the take being read there is nothing on this line yet.
            if (previewCutoff !== -1 && i > previewCutoff) return null
            const isPlayer = PLAYER_TYPES.includes(action.type)
            // A player action opens a new turn, so that's where the ornamental
            // break belongs — never above the very first line on the page.
            const sceneBreak = isPlayer && i > 0
            // Drop cap goes on the first narrated beat only. `firstNarrationId`
            // is derived once above rather than per row.
            const opening = action.id === firstNarrationId
            // Non-null while the reader is browsing an older attempt of this
            // message without making it active (earlier turns only).
            const previewing = preview?.actionId === action.id ? preview : null
            // The take this row was written below, still on screen because the
            // re-read has not landed yet. Same text override as a preview, but
            // it never truncates the story under it — see `pinned`.
            const shown = previewing
              || (pinned?.actionId === action.id ? pinned : null)

            // The editor stands in for the row it was opened from. That row is
            // keyed by the live node, so an edit on a take the pager is parked
            // on carries the take's id instead and is matched through the
            // preview.
            const editingHere = editing
              && (editing.take ? previewing?.takeId === editing.id : editing.id === action.id)

            return editingHere ? (
              <div key={action.id} className="action-edit">
                <AutoTextarea
                  autoFocus
                  value={editing.text}
                  onChange={(e) => setEditing({ ...editing, text: e.target.value })}
                />
                <div className="edit-buttons">
                  <button className="primary" onClick={saveEdit}>Save</button>
                  <button onClick={() => setEditing(null)}>Cancel</button>
                </div>
              </div>
            ) : (
              <Fragment key={action.id}>
                {sceneBreak && <div className="scene-break" aria-hidden="true">❖</div>}
                <div className={`action ${isPlayer ? 'player' : ''}${opening ? ' opening' : ''}`}>
                  <ReasoningBlock text={shown ? shown.reasoning : action.reasoning} />
                  {renderEmphasis(shown ? shown.text : action.text)}
                  {/* The chips describe the *active* attempt's state changes,
                      which the take on screen didn't make — so they're hidden
                      rather than shown against the wrong text. */}
                  {action.type === 'ai' && !shown && (
                    <StateChangeChips changes={action.world_changes} />
                  )}
                  {/* On every kind of node, not only the AI's: a player's own
                      turn can be played again too (SP9), so it can have takes
                      to step through. The pager draws nothing for a count of
                      one, which is most turns. */}
                  <TakePager
                    advId={id}
                    action={action}
                    busy={busy}
                    // The pinned take counts as the one on screen, so the
                    // ordinal matches the words above it.
                    preview={shown}
                    takesKey={takesKey}
                    onPreview={setPreview}
                    onSwitchedBranch={adoptWindow}
                    onError={(message) => setToast({ text: message, isError: true })}
                  />
                  {!busy && (
                    <span className="action-tools">
                      {action.type === 'ai' && (
                        <button title="View the exact prompt that produced this"
                          onClick={() => inspect(action.id)}>🔍</button>
                      )}
                      {/* Edits the take that is *on screen*, which is not the
                          live one while the pager is parked on another. The
                          row is keyed by the live node's id, so seeding from
                          `action` here opened the editor on take 4/4's text
                          while 2/4 was being read — and saved over it. A take
                          is an ordinary row to the edit endpoint, on the path
                          or not, so its own id is all this needs. */}
                      <button title="Edit"
                        onClick={() => setEditing(previewing
                          ? { id: previewing.takeId, text: previewing.text, take: true }
                          : { id: action.id, text: action.text })}>✎</button>
                      {/* Play this turn again, differently. On the AI's turn
                          that is a regeneration; on your own it opens the text
                          so you can say something else. Either way the story
                          that followed the old take is kept, on the line it
                          was written on.

                          The id stays the live node's even while another take
                          is being read: adding a take branches just above the
                          turn, and the server only accepts a turn that is on
                          the path. Only the seeded text follows the screen, so
                          varying the take you are reading starts from its
                          words. */}
                      {action.type !== 'start' && (
                        <button
                          title={action.type === 'ai'
                            ? 'Another take on this turn'
                            : 'Say this differently, and keep both'}
                          onClick={() => (action.type === 'ai'
                            ? addTake(action.id, '')
                            : setEditing({
                              id: action.id,
                              text: previewing ? previewing.text : action.text,
                              fork: true,
                            }))}
                        >⑂</button>
                      )}
                      <button title="Delete" onClick={() => removeAction(action.id)}>✕</button>
                    </span>
                  )}
                </div>
              </Fragment>
            )
          })}
          {waitingForFirstToken && (
            <div className="thinking" role="status">
              <i /><i /><i />
              <span>Weaving</span>
            </div>
          )}
          {streaming !== null && !waitingForFirstToken && (
            <div className="action streaming">
              <ReasoningBlock text={reasoningStream} streaming />
              {renderEmphasis(streaming)}
              <span className="cursor">▋</span>
            </div>
          )}
          <div ref={storyEndRef} />
        </div>

        <div className="play-controls">
          <div className="turn-buttons">
            <button onClick={() => send('continue')} disabled={busy}>Continue ▸</button>
            <button onClick={retry} disabled={busy || !lastIsAi} title="Ctrl+R">↻ Retry</button>
            <button onClick={undo} disabled={busy || !canUndo} title="Ctrl+Z">↶ Undo</button>
          </div>
          <div className="input-bar">
            <div className="mode-select">
              {MODES.map((m) => (
                <button key={m} className={mode === m ? 'active' : ''}
                  onClick={() => setMode(m)} disabled={busy}>
                  {m[0].toUpperCase() + m.slice(1)}
                </button>
              ))}
            </div>
            <textarea
              ref={inputRef}
              rows={1}
              value={input}
              disabled={busy}
              placeholder={
                mode === 'do' ? 'What do you do?'
                  : mode === 'say' ? 'What do you say?'
                  : 'What happens next?'
              }
              onChange={(e) => setInput(e.target.value)}
              // Enter sends; Shift+Enter inserts a newline.
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey && !busy) { e.preventDefault(); send() }
              }}
            />
            {busy ? (
              <button className="danger" onClick={stopGeneration}>■ Stop</button>
            ) : (
              <button className="primary" onClick={() => send()}>Send</button>
            )}
          </div>
        </div>
      </div>

      {panel && (
        <div className="side-panel">
          <div className="side-panel-header">
            <h2>{{ plot: 'Plot Components', memory: 'Memory Bank', scripts: 'Scripts', branches: 'Branches', insights: 'Insights' }[panel]}</h2>
            <button onClick={() => setPanel(null)}>✕</button>
          </div>
          {panel === 'plot' ? (
            <PlotPanel adventure={adventure} setAdventure={setAdventure}
              onWorldStateChanged={() => setStateKey((k) => k + 1)} />
          ) : panel === 'memory' ? (
            <MemoryPanel adventure={adventure} setAdventure={setAdventure}
              // The bank is adventure-wide, so a switch does not change it —
              // but deleting a branch deletes the memories that hung off it,
              // and that happens without a turn being played.
              refreshKey={`${actions.length}:${stateKey}`} />
          ) : panel === 'scripts' ? (
            <ScriptsPanel advId={id} />
          ) : panel === 'branches' ? (
            <BranchPanel
              advId={id}
              // Not `actions.length` alone. A fork taken from the story column
              // replaces one window with another of the same size, so the
              // length is unchanged and the panel would go on showing a tree
              // with one branch in it while the story is being read on a
              // second. `stateKey` is bumped by adoptWindow, which is exactly
              // the two operations that move the head.
              refreshKey={`${actions.length}:${stateKey}`}
              onSwitched={adoptWindow}
              onTreeChanged={() => setStateKey((k) => k + 1)}
              onError={(message) => setToast({ text: message, isError: true })}
            />
          ) : (
            // Insights is the prompt as it would be sent *now*, which is built
            // from the story on the current path — so of everything on this
            // screen it is the panel a branch switch changes most completely.
            <InsightsPanel advId={id} inspectActionId={inspectActionId}
              onClearInspect={() => setInspectActionId(null)}
              refreshKey={`${actions.length}:${stateKey}`} />
          )}
        </div>
      )}

      {toast && (
        <div className={`play-toast ${toast.isError ? '' : 'ok'}`}>
          {toast.text}
          {toast.isError && (
            <button style={{ marginLeft: 12 }} onClick={retry}>Retry</button>
          )}
          <button style={{ marginLeft: 8 }} onClick={() => setToast(null)}>✕</button>
        </div>
      )}
    </div>
  )
}
