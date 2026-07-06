import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { downloadJSON, pickJSONFile } from '../components'

export default function Scripts() {
  const [scripts, setScripts] = useState(null)
  const navigate = useNavigate()

  useEffect(() => {
    api.listScripts().then(setScripts).catch(() => setScripts([]))
  }, [])

  const createScript = async () => {
    const script = await api.createScript({
      name: 'New Script',
      input_js: '// onInput — modify the player\'s input\nconst modifier = (text) => {\n  return { text }\n}\nmodifier(text)\n',
    })
    navigate(`/scripts/${script.id}`)
  }

  const importScript = async () => {
    try {
      const bundle = await pickJSONFile()
      const script = await api.importScript(bundle)
      navigate(`/scripts/${script.id}`)
    } catch (err) {
      alert(err.message)
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h1>Scripts</h1>
        <div style={{ display: 'flex', gap: 10 }}>
          <button onClick={importScript}>Import</button>
          <button className="primary" onClick={createScript}>+ New Script</button>
        </div>
      </div>
      {scripts === null ? null : scripts.length === 0 ? (
        <div className="empty">
          No scripts yet. Scripts are AI Dungeon-compatible JavaScript modifiers
          (onInput / onModelContext / onOutput) you can attach to scenarios.
        </div>
      ) : (
        <div className="card-grid">
          {scripts.map((s) => (
            <div key={s.id} className="card" onClick={() => navigate(`/scripts/${s.id}`)}>
              <h3>{s.name}</h3>
              <p>{s.description || 'No description'}</p>
              <div className="meta">
                {['library', 'input', 'context', 'output']
                  .filter((slot) => s[`${slot}_js`].trim())
                  .join(' · ') || 'empty'}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
