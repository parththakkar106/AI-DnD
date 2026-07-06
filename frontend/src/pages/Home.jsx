import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { downloadJSON, pickJSONFile } from '../components'

export default function Home() {
  const [adventures, setAdventures] = useState(null)
  const [search, setSearch] = useState('')
  const navigate = useNavigate()

  useEffect(() => {
    api.listAdventures().then(setAdventures).catch(() => setAdventures([]))
  }, [])

  const visible = useMemo(() => {
    if (!adventures) return null
    const q = search.trim().toLowerCase()
    if (!q) return adventures
    return adventures.filter((a) =>
      `${a.title} ${a.scenario_title || ''}`.toLowerCase().includes(q))
  }, [adventures, search])

  const remove = async (e, id) => {
    e.stopPropagation()
    if (!confirm('Delete this adventure permanently?')) return
    await api.deleteAdventure(id)
    setAdventures(adventures.filter((a) => a.id !== id))
  }

  const exportOne = async (e, adv) => {
    e.stopPropagation()
    const bundle = await api.exportAdventure(adv.id)
    const safe = adv.title.replace(/[^\w-]+/g, '_').slice(0, 60) || 'adventure'
    downloadJSON(bundle, `${safe}.json`)
  }

  const importOne = async () => {
    try {
      const bundle = await pickJSONFile()
      const adv = await api.importAdventure(bundle)
      navigate(`/play/${adv.id}`)
    } catch (err) {
      alert(err.message)
    }
  }

  return (
    <div className="page">
      <div className="page-header">
        <h1>Adventures</h1>
        <div style={{ display: 'flex', gap: 10 }}>
          <button onClick={importOne}>Import</button>
          <button className="primary" onClick={() => navigate('/scenarios')}>
            + New Adventure
          </button>
        </div>
      </div>

      {adventures?.length > 0 && (
        <div className="filter-bar">
          <input
            type="text"
            className="search-input"
            placeholder="Search adventures…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      )}

      {visible === null ? null : visible.length === 0 ? (
        <div className="empty">
          {adventures.length === 0
            ? 'No adventures yet. Head to Scenarios to begin your first story.'
            : 'No adventures match your search.'}
        </div>
      ) : (
        <div className="card-grid">
          {visible.map((adv) => (
            <div key={adv.id} className="card" onClick={() => navigate(`/play/${adv.id}`)}>
              <h3>{adv.title}</h3>
              <p>
                {adv.scenario_title ? `From “${adv.scenario_title}” · ` : ''}
                {adv.action_count} actions
              </p>
              <div className="meta">
                Last played {new Date(adv.updated_at + 'Z').toLocaleString()}
                <span style={{ float: 'right', display: 'inline-flex', gap: 4 }}>
                  <button
                    title="Export as JSON backup"
                    style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                    onClick={(e) => exportOne(e, adv)}
                  >
                    Export
                  </button>
                  <button
                    className="danger"
                    style={{ padding: '2px 8px', fontSize: '0.75rem' }}
                    onClick={(e) => remove(e, adv.id)}
                  >
                    Delete
                  </button>
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
