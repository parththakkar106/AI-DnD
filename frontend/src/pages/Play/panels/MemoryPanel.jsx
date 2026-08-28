// The memory bank panel: the entries retrieval can pull into a prompt.

import { useCallback, useEffect, useState } from 'react'
import { api } from '../../../api'
import { AutoTextarea } from '../../../components'

function MemoryRow({ memory, onChange, onDelete }) {
  const [editText, setEditText] = useState(null)

  const save = () => {
    const text = editText.trim()
    setEditText(null)
    if (text && text !== memory.text) onChange({ text })
  }

  return (
    <div className={`memory-row ${memory.forgotten ? 'forgotten' : ''}`}>
      {editText !== null ? (
        <div className="action-edit" style={{ margin: 0 }}>
          <AutoTextarea autoFocus value={editText} rows={3}
            onChange={(e) => setEditText(e.target.value)} />
          <div className="edit-buttons">
            <button className="primary" onClick={save}>Save</button>
            <button onClick={() => setEditText(null)}>Cancel</button>
          </div>
        </div>
      ) : (
        <>
          <div className="memory-text">{memory.text}</div>
          <div className="memory-meta">
            {memory.pinned && <span className="memory-badge">📌 pinned</span>}
            {memory.forgotten && <span className="memory-badge">forgotten</span>}
            {!memory.embedded && !memory.forgotten && (
              <span className="memory-badge" title="Embedded on the next turn">not embedded yet</span>
            )}
            {memory.source_start != null && (
              <span className="dim">actions {memory.source_start}–{memory.source_end}</span>
            )}
            <span className="dim">used {memory.use_count}×</span>
            <span className="action-tools">
              <button title={memory.pinned ? 'Unpin' : 'Pin (always included in context)'}
                onClick={() => onChange({ pinned: !memory.pinned })}>📌</button>
              {memory.forgotten && (
                <button title="Restore this memory"
                  onClick={() => onChange({ forgotten: false })}>↩</button>
              )}
              <button title="Edit" onClick={() => setEditText(memory.text)}>✎</button>
              <button title="Delete" onClick={onDelete}>✕</button>
            </span>
          </div>
        </>
      )}
    </div>
  )
}

function MemoryPanel({ adventure, setAdventure, refreshKey }) {
  const [memories, setMemories] = useState(null)
  const [newText, setNewText] = useState('')

  const load = useCallback(() => {
    api.listMemories(adventure.id).then(setMemories).catch(() => setMemories([]))
  }, [adventure.id])

  // Summarization runs in the background after a turn, so also refresh shortly after.
  useEffect(() => {
    load()
    const timer = setTimeout(load, 4000)
    return () => clearTimeout(timer)
  }, [load, refreshKey])

  const setFlag = async (field, value) => {
    setAdventure({ ...adventure, [field]: value })
    await api.updateAdventure(adventure.id, { [field]: value })
  }

  const change = async (memory, data) => {
    const updated = await api.updateMemory(adventure.id, memory.id, data)
    setMemories((prev) => prev.map((m) => (m.id === memory.id ? updated : m)))
  }

  const remove = async (memory) => {
    await api.deleteMemory(adventure.id, memory.id)
    setMemories((prev) => prev.filter((m) => m.id !== memory.id))
  }

  const add = async () => {
    const text = newText.trim()
    if (!text) return
    setNewText('')
    const memory = await api.createMemory(adventure.id, text)
    setMemories((prev) => [...prev, memory])
  }

  const active = memories?.filter((m) => !m.forgotten) ?? []
  const forgotten = memories?.filter((m) => m.forgotten) ?? []

  return (
    <div>
      <label className="script-attach">
        <input type="checkbox" checked={adventure.auto_summarize}
          onChange={(e) => setFlag('auto_summarize', e.target.checked)} />
        <span>Auto-summarization</span>
        <span className="dim">memories every 6 actions, Story Summary every 15</span>
      </label>
      <label className="script-attach">
        <input type="checkbox" checked={adventure.memory_bank_enabled}
          onChange={(e) => setFlag('memory_bank_enabled', e.target.checked)} />
        <span>Memory Bank</span>
        <span className="dim">retrieve relevant memories into context (needs an embedding model)</span>
      </label>

      <div className="page-header" style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0 }}>Memories {memories && `(${active.length})`}</h3>
        <button onClick={load}>Refresh</button>
      </div>
      {!memories && <div className="empty" style={{ padding: '12px 0' }}>Loading…</div>}
      {memories && active.length === 0 && (
        <div className="empty" style={{ padding: '12px 0' }}>
          No memories yet. They are generated automatically as you play (from action 12
          onward), or add one below.
        </div>
      )}
      {active.map((m) => (
        <MemoryRow key={m.id} memory={m}
          onChange={(data) => change(m, data)} onDelete={() => remove(m)} />
      ))}

      <div className="input-bar" style={{ marginTop: 10 }}>
        <input type="text" value={newText} placeholder="Add a memory manually…"
          onChange={(e) => setNewText(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') add() }} />
        <button onClick={add}>+ Add</button>
      </div>

      {forgotten.length > 0 && (
        <>
          <div className="page-header" style={{ marginTop: 18 }}>
            <h3 style={{ margin: 0 }}>Forgotten ({forgotten.length})</h3>
          </div>
          {forgotten.map((m) => (
            <MemoryRow key={m.id} memory={m}
              onChange={(data) => change(m, data)} onDelete={() => remove(m)} />
          ))}
        </>
      )}
    </div>
  )
}

export { MemoryPanel }
