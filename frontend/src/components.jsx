import { useState } from 'react'

export function downloadJSON(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  a.click()
  URL.revokeObjectURL(url)
}

export function pickJSONFile() {
  return new Promise((resolve, reject) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = '.json,application/json'
    input.onchange = () => {
      const file = input.files[0]
      if (!file) return reject(new Error('No file selected'))
      const reader = new FileReader()
      reader.onload = () => {
        try { resolve(JSON.parse(reader.result)) }
        catch { reject(new Error('Not valid JSON')) }
      }
      reader.onerror = () => reject(new Error('Could not read file'))
      reader.readAsText(file)
    }
    input.click()
  })
}

// Unique ${Placeholder} names, in order of first appearance, across the given texts.
export function extractPlaceholders(...texts) {
  const names = []
  for (const text of texts) {
    for (const match of (text || '').matchAll(/\$\{([^}]+)\}/g)) {
      const name = match[1].trim()
      if (name && !names.includes(name)) names.push(name)
    }
  }
  return names
}

export function PlaceholderModal({ title, names, onSubmit, onCancel }) {
  const [values, setValues] = useState(Object.fromEntries(names.map((n) => [n, ''])))

  const submit = (e) => {
    e.preventDefault()
    onSubmit(values)
  }

  return (
    <div className="modal-overlay" onClick={onCancel}>
      <form className="modal" onClick={(e) => e.stopPropagation()} onSubmit={submit}>
        <h2>{title}</h2>
        <p className="modal-hint">This scenario asks a few questions before you begin.</p>
        {names.map((name, i) => (
          <label key={name} className="field">
            <span className="label">{name}</span>
            <input
              type="text"
              autoFocus={i === 0}
              value={values[name]}
              onChange={(e) => setValues({ ...values, [name]: e.target.value })}
            />
          </label>
        ))}
        <div className="modal-buttons">
          <button type="button" onClick={onCancel}>Cancel</button>
          <button type="submit" className="primary">Begin Adventure</button>
        </div>
      </form>
    </div>
  )
}

export function Field({ label, value, onChange, textarea, rows, placeholder }) {
  return (
    <label className="field">
      <span className="label">{label}</span>
      {textarea ? (
        <textarea rows={rows || 3} value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      ) : (
        <input type="text" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
      )}
    </label>
  )
}

export function StoryCardRow({ card, onChange, onDelete }) {
  return (
    <div className="storycard">
      <div className="row">
        <input type="text" placeholder="Name (not sent to AI)" value={card.name}
          onChange={(e) => onChange({ ...card, name: e.target.value })} />
        <input type="text" placeholder="Type (e.g. Character)" value={card.type}
          onChange={(e) => onChange({ ...card, type: e.target.value })} />
      </div>
      <div className="row">
        <input type="text" placeholder="Triggers, comma-separated" value={card.keys}
          onChange={(e) => onChange({ ...card, keys: e.target.value })} />
      </div>
      <textarea rows={2} placeholder="Entry — sent to the AI when a trigger matches" value={card.entry}
        onChange={(e) => onChange({ ...card, entry: e.target.value })} />
      <div style={{ textAlign: 'right', marginTop: 6 }}>
        <button className="danger" style={{ padding: '3px 10px', fontSize: '0.78rem' }} onClick={onDelete}>
          Remove
        </button>
      </div>
    </div>
  )
}
