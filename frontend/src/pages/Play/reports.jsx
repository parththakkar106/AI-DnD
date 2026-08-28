// What a turn did, rendered five ways: world-state chips, the world-state
// report, the script report, the cache report, and the token breakdown.
//
// These read a turn's result and render it. None of them fetch.

import { useState } from 'react'
import { SECTION_LABELS, pctLabel, sectionColor } from './format'

// Reasons the engine gives for refusing a change, in the player's words.
const REJECT_REASONS = {
  'not a number': 'expected a number',
  'not a string': 'expected text',
  'not a boolean': 'expected on or off',
  'not true': 'a milestone can only be set',
  "counter can't decrease": 'this only counts up',
  cooldown: 'changed too recently',
  'unknown stat': 'no such stat',
  'unknown npc': 'no such character',
  'unknown npc stat': 'no such stat',
  'unknown flag': 'no such flag',
  'unknown milestone': 'no such milestone',
  'unknown path': 'unrecognized name',
}

// Compact chips shown under an AI message summarizing what state changed.
//
// Refused changes appear here too. A clamped stat is marked, and a stat whose
// clamp left it exactly where it started reads "no change" rather than "+0",
// which looked like an ordinary update.
function StateChangeChips({ changes }) {
  if (!changes?.length) return null
  const nice = (s) => String(s).replace(/_/g, ' ')
  return (
    <div className="turn-changes">
      {changes.map((c, i) => {
        if (c.kind === 'flag') {
          return <span key={i} className="chg chg-flag">{nice(c.label)}: <span className="chg-val">{c.on ? 'on' : 'off'}</span></span>
        }
        if (c.kind === 'milestone') {
          return <span key={i} className="chg chg-ms">✓ {nice(c.label)}</span>
        }
        if (c.kind === 'rejected') {
          const why = REJECT_REASONS[c.reason] || c.reason
          // The engine's own wording quotes the real limits, so prefer it.
          return (
            <span key={i} className="chg chg-refused" title={c.fix || `The story asked to change this and the rules refused: ${why}.`}>
              {nice(c.label)} <span className="chg-val">refused — {why}</span>
            </span>
          )
        }
        const d = c.delta
        // A clamp that cancels the change entirely is its own outcome. It is
        // neither an update nor a refusal, and "+0" read as the former.
        const blocked = c.clamped && d === 0
        const dir = blocked ? 'refused' : typeof d === 'number' ? (d > 0 ? 'up' : d < 0 ? 'down' : 'flat') : 'flat'
        const txt = blocked
          ? 'no change — at its limit'
          : typeof d === 'number' ? (d > 0 ? `+${d}` : `${d}`) : `→ ${c.value}`
        const title = blocked
          ? c.fix || 'The story asked to change this and it is already at the limit the scenario allows.'
          : c.clamped
            ? 'The scenario limits how far this can move in one turn, so the change was reduced.'
            : undefined
        return (
          <span key={i} className={`chg chg-stat chg-${dir}`} title={title}>
            {nice(c.label)} <span className="chg-val">{txt}</span>
            {c.clamped && !blocked ? <span className="chg-limited"> (limited)</span> : null}
          </span>
        )
      })}
    </div>
  )
}

// Per-turn RPG state change, shown when inspecting a past turn's snapshot.
function WorldStateReport({ worldState }) {
  if (!worldState) return null
  const delta = worldState.delta || {}
  const report = worldState.report || {}
  const paths = Object.keys(delta)
  const rejected = report.rejected || []
  const clamped = new Set((report.clamped || []).map((c) => c.path))
  if (paths.length === 0 && rejected.length === 0) {
    return (
      <div className="script-report">
        <div className="ctx-header" style={{ padding: '4px 0 2px' }}><span>World State</span></div>
        <div className="dim" style={{ fontSize: '0.82rem' }}>No changes this turn.</div>
      </div>
    )
  }
  return (
    <div className="script-report">
      <div className="ctx-header" style={{ padding: '4px 0 2px' }}><span>World State changes</span></div>
      <ul className="ws-report">
        {paths.map((p) => (
          <li key={p}>
            <code>{p}</code> <span className="ws-delta">{String(delta[p])}</span>
            {clamped.has(p) && <span className="ws-flag"> clamped</span>}
          </li>
        ))}
        {rejected.map((r, i) => (
          <li key={`r${i}`} className="ws-rejected">
            <code>{r.path}</code> rejected — {r.reason}
          </li>
        ))}
      </ul>
    </div>
  )
}

function ScriptReport({ script }) {
  if (!script || (!script.logs?.length && !script.errors?.length && !script.context_changed)) {
    return null
  }
  return (
    <div className="script-report">
      <div className="ctx-header" style={{ padding: '4px 0 2px' }}><span>Scripts</span></div>
      {script.errors?.map((e, i) => <div key={i} className="script-error">⚠ {e}</div>)}
      {script.logs?.length > 0 && (
        <pre className="script-logs">{script.logs.join('\n')}</pre>
      )}
      {script.context_changed && (
        <>
          <div className="ctx-section" style={{ borderLeftColor: sectionColor('script_context') }}>
            <div className="ctx-header" style={{ color: sectionColor('script_context') }}>
              <span>Context before script</span>
            </div>
            <pre>{script.context_before}</pre>
          </div>
          <div className="ctx-section" style={{ borderLeftColor: sectionColor('script_context') }}>
            <div className="ctx-header" style={{ color: sectionColor('script_context') }}>
              <span>Context after script (sent to AI)</span>
            </div>
            <pre>{script.context_after}</pre>
          </div>
        </>
      )}
    </div>
  )
}

// What the endpoint charged for the turn, and how much of the prompt it read
// back out of its cache instead of billing in full. Only shown on a past turn:
// the "next turn" view has not been sent anywhere yet, so it has no usage. A
// cached read costs a tenth of a fresh one, which is the whole reason the
// prompt is laid out static-first — so this is the number that says whether
// that layout is working.
function CacheReport({ usage }) {
  if (!usage) return null
  const prompt = usage.prompt_tokens || 0
  const cached = usage.prompt_tokens_details?.cached_tokens || 0
  if (!prompt) return null
  const pct = Math.round((cached / prompt) * 100)
  return (
    <div className="insights-history">
      Prompt cache: {cached} of {prompt} prompt tokens read from cache ({pct}%)
      {usage.cost != null && ` · cost $${Number(usage.cost).toFixed(5)}`}
    </div>
  )
}

const LEGEND_VISIBLE = 6 // enough to cover what actually moves the budget

// Where the prompt's tokens actually went: one stacked bar scaled to the
// budget (so leftover width IS the remaining headroom) plus a matching legend.
// Bar and legend both run biggest-share-first — the order that answers "what is
// eating my context?" — rather than the prompt order the sections below use.
function TokenBreakdown({ sections, tokens, used, onJump }) {
  const [hovered, setHovered] = useState(null)
  const [expanded, setExpanded] = useState(false)
  if (used <= 0) return null
  const budget = tokens.budget || 0
  // Over budget there is no headroom to draw, so the bar scales to what the
  // prompt actually costs and the total line above it carries the warning.
  const scale = Math.max(budget, used)
  const free = Math.max(0, budget - used)
  const ranked = sections
    .map((s, i) => ({ ...s, i, pct: (s.tokens / used) * 100 }))
    .filter((s) => s.tokens > 0)
    .sort((a, b) => b.tokens - a.tokens)
  // The panel is narrow, so the legend is one column: list the sections that
  // actually move the budget and fold the long tail behind a count.
  const shown = expanded ? ranked : ranked.slice(0, LEGEND_VISIBLE)
  const rest = ranked.slice(shown.length)
  const restPct = rest.reduce((n, s) => n + s.pct, 0)
  const describe = (s) =>
    `${SECTION_LABELS[s.label] || s.label} — ${s.tokens} tok · ${pctLabel(s.pct)}`

  return (
    <>
      <div
        className={`token-split ${tokens.total > budget ? 'over' : ''}`}
        role="img"
        aria-label={`Context breakdown: ${ranked.map(describe).join(', ')}`}
      >
        {ranked.map((s) => (
          <div
            key={s.i}
            className={`token-seg ${hovered !== null && hovered !== s.i ? 'faded' : ''}`}
            style={{ width: `${(s.tokens / scale) * 100}%`, background: sectionColor(s.label) }}
            title={describe(s)}
            onMouseEnter={() => setHovered(s.i)}
            onMouseLeave={() => setHovered(null)}
          />
        ))}
        {free > 0 && (
          <div
            className="token-seg token-seg-free"
            style={{ width: `${(free / scale) * 100}%` }}
            title={`${free} tokens of budget unused`}
          />
        )}
      </div>
      <div className="token-legend">
        {shown.map((s) => (
          <button
            key={s.i}
            type="button"
            className={`token-legend-item ${hovered !== null && hovered !== s.i ? 'faded' : ''}`}
            title={`${describe(s)} — click to jump to this section`}
            onMouseEnter={() => setHovered(s.i)}
            onMouseLeave={() => setHovered(null)}
            onClick={() => onJump(s.i)}
          >
            <span className="token-swatch" style={{ background: sectionColor(s.label) }} />
            <span className="token-legend-name">{SECTION_LABELS[s.label] || s.label}</span>
            <span className="token-legend-pct">{pctLabel(s.pct)}</span>
            <span className="token-legend-tok">{s.tokens}</span>
          </button>
        ))}
        {rest.length > 0 && (
          <button type="button" className="token-legend-more" onClick={() => setExpanded(true)}>
            + {rest.length} smaller {rest.length === 1 ? 'section' : 'sections'} ({pctLabel(restPct)})
          </button>
        )}
        {expanded && ranked.length > LEGEND_VISIBLE && (
          <button type="button" className="token-legend-more" onClick={() => setExpanded(false)}>
            Show less
          </button>
        )}
      </div>
    </>
  )
}

export { StateChangeChips, WorldStateReport, ScriptReport, CacheReport, TokenBreakdown }
