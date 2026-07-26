import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { CardSkeleton, downloadJSON, pickJSONFile, ScenarioArt, useToast } from '../components'

export default function Adventures() {
  const [adventures, setAdventures] = useState(null)
  const [search, setSearch] = useState('')
  const navigate = useNavigate()
  const toast = useToast()

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
    try {
      await api.deleteAdventure(id)
      setAdventures(adventures.filter((a) => a.id !== id))
      toast('Adventure deleted')
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  const exportOne = async (e, adv) => {
    e.stopPropagation()
    try {
      const bundle = await api.exportAdventure(adv.id)
      const safe = adv.title.replace(/[^\w-]+/g, '_').slice(0, 60) || 'adventure'
      downloadJSON(bundle, `${safe}.json`)
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  const importOne = async () => {
    try {
      const bundle = await pickJSONFile()
      const adv = await api.importAdventure(bundle)
      navigate(`/play/${adv.id}`)
    } catch (err) {
      toast(err.message, 'error')
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

      {visible === null ? (
        <CardSkeleton count={4} lines={3} />
      ) : visible.length === 0 ? (
        <div className="empty">
          {adventures.length === 0
            ? 'No adventures yet. Head to Scenarios to begin your first story.'
            : 'No adventures match your search.'}
        </div>
      ) : (
        <div className="card-grid wide">
          {visible.map((adv, i) => (
            <article
              key={adv.id}
              className="card tome enter"
              style={{ animationDelay: `${Math.min(i, 8) * 50}ms` }}
              onClick={() => navigate(`/play/${adv.id}`)}
            >
              <div className="card-head">
                <ScenarioArt image={adv.image_url} icon={adv.icon} title={adv.title} large />
                <div className="card-headings">
                  <h3>{adv.title}</h3>
                  <p className="card-from">
                    {/* Adventures inherit the scenario's title, so only name
                        the source when it actually differs. */}
                    {adv.scenario_title && adv.scenario_title !== adv.title
                      ? `From “${adv.scenario_title}” · `
                      : ''}
                    {adv.action_count} {adv.action_count === 1 ? 'turn' : 'turns'}
                  </p>
                </div>
              </div>

              {adv.snippet && <p className="snippet">{adv.snippet}</p>}

              <footer className="card-foot">
                <span className="turns">
                  Last played {new Date(adv.updated_at + 'Z').toLocaleDateString()}
                </span>
                <span className="card-actions">
                  <button className="tiny" title="Export as JSON backup" onClick={(e) => exportOne(e, adv)}>
                    Export
                  </button>
                  <button className="tiny danger" onClick={(e) => remove(e, adv.id)}>
                    Delete
                  </button>
                </span>
              </footer>
            </article>
          ))}
        </div>
      )}
    </div>
  )
}
