// The insights panel: what the last turn sent, and what it cost.

import { useEffect, useState } from 'react'
import { api } from '../../../api'
import { SECTION_LABELS, pctLabel, sectionColor } from '../format'
import { CacheReport, ScriptReport, TokenBreakdown, WorldStateReport } from '../reports'

function InsightsPanel({ advId, inspectActionId, onClearInspect, refreshKey }) {
  const [report, setReport] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let stale = false // a slow earlier request must not clobber a newer one
    setError(null)
    const load = inspectActionId
      ? api.getActionContext(advId, inspectActionId)
      : api.getAdventureContext(advId)
    load
      .then((r) => { if (!stale) setReport(r) })
      .catch((err) => { if (!stale) { setReport(null); setError(err.message) } })
    return () => { stale = true }
  }, [advId, inspectActionId, refreshKey])

  if (error) return <div className="empty">{error}</div>
  if (!report) return <div className="empty">Loading…</div>

  const { tokens, cards, history, sections } = report
  const overBudget = tokens.total > tokens.budget
  // The per-section sum, not tokens.total: the total also counts the separators
  // between sections, and percentages have to add up to 100 for the reader.
  const sectionTotal = sections.reduce((n, s) => n + s.tokens, 0)
  const jumpToSection = (i) => {
    document.getElementById(`ctx-sec-${i}`)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  return (
    <div className="insights">
      <div className="insights-meta">
        {inspectActionId ? (
          <div className="insights-mode">
            Snapshot of a past turn
            <button onClick={onClearInspect} style={{ marginLeft: 8 }}>Back to next turn</button>
          </div>
        ) : (
          <div className="insights-mode">What will be sent on the next turn</div>
        )}
        <div className={`token-total ${overBudget ? 'over' : ''}`}>
          {tokens.total} / {tokens.budget} tokens
        </div>
        <TokenBreakdown
          sections={sections}
          tokens={tokens}
          used={sectionTotal}
          onJump={jumpToSection}
        />
        <div className="insights-history">
          History: {history.included} of {history.total} actions in context
          {history.total > history.included && ' (older history trimmed)'}
          {history.oldest_truncated && ' — oldest entry cut mid-text'}
        </div>
        <CacheReport usage={report.usage} />
        {cards.length > 0 && (
          <div className="insights-cards">
            {cards.map((c, i) => (
              <div key={i} className={c.included ? '' : 'dropped'}>
                ▸ <b>{c.name || '(unnamed card)'}</b> triggered on “{c.keyword}”
                {!c.included && ' — dropped (over card budget)'}
              </div>
            ))}
          </div>
        )}
        {report.memories && (
          <div className="insights-cards">
            {report.memories.error && (
              <div className="dropped">⚠ Memory bank: {report.memories.error}</div>
            )}
            {report.memories.used?.map((m, i) => (
              <div key={i}>
                ▸ memory retrieved ({m.pinned ? 'pinned' : `similarity ${m.similarity.toFixed(2)}`}):
                {' '}{m.text.length > 90 ? m.text.slice(0, 90) + '…' : m.text}
              </div>
            ))}
            {!report.memories.error && report.memories.used?.length === 0 && (
              <div className="dim">Memory bank on — no memories retrieved.</div>
            )}
          </div>
        )}
      </div>

      {sections.map((s, i) => (
        <div
          key={i}
          id={`ctx-sec-${i}`}
          className={`ctx-section ctx-${s.label}`}
          style={{ borderLeftColor: sectionColor(s.label) }}
        >
          <div className="ctx-header" style={{ color: sectionColor(s.label) }}>
            <span>{SECTION_LABELS[s.label] || s.label}</span>
            <span>
              {s.tokens} tok
              {sectionTotal > 0 && ` · ${pctLabel((s.tokens / sectionTotal) * 100)}`}
            </span>
          </div>
          <pre>{s.text}</pre>
        </div>
      ))}
      <ScriptReport script={report.script} />
      <WorldStateReport worldState={report.world_state} />
      {report.raw_output && (
        <div className="ctx-section">
          <div className="ctx-header">
            <span>Raw AI output (before state block was stripped)</span>
          </div>
          <pre>{report.raw_output}</pre>
        </div>
      )}
    </div>
  )
}

export { InsightsPanel }
