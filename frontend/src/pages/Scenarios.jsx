import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { extractPlaceholders, pickJSONFile, PlaceholderModal } from '../components'

function splitTags(tags) {
  return (tags || '').split(',').map((t) => t.trim()).filter(Boolean)
}

export default function Scenarios() {
  const [scenarios, setScenarios] = useState(null)
  const [search, setSearch] = useState('')
  const [tagFilter, setTagFilter] = useState(null)
  const [pending, setPending] = useState(null) // { scenario, names } awaiting placeholder values
  const navigate = useNavigate()

  useEffect(() => {
    api.listScenarios().then(setScenarios).catch(() => setScenarios([]))
  }, [])

  const allTags = useMemo(() => {
    const tags = new Set()
    for (const sc of scenarios || []) splitTags(sc.tags).forEach((t) => tags.add(t))
    return [...tags].sort()
  }, [scenarios])

  const visible = useMemo(() => {
    if (!scenarios) return null
    const q = search.trim().toLowerCase()
    return scenarios.filter((sc) => {
      if (tagFilter && !splitTags(sc.tags).includes(tagFilter)) return false
      if (q && !`${sc.title} ${sc.description} ${sc.tags}`.toLowerCase().includes(q)) return false
      return true
    })
  }, [scenarios, search, tagFilter])

  const createScenario = async () => {
    const scenario = await api.createScenario({ title: 'New Scenario' })
    navigate(`/scenarios/${scenario.id}`)
  }

  const begin = async (scenarioId, placeholders = {}) => {
    const adv = await api.createAdventure({ scenario_id: scenarioId, placeholders })
    navigate(`/play/${adv.id}`)
  }

  const startAdventure = async (e, scenarioId) => {
    e.stopPropagation()
    const scenario = await api.getScenario(scenarioId)
    const names = extractPlaceholders(
      scenario.prompt, scenario.memory, scenario.authors_note, scenario.ai_instructions,
      // Cards can carry ${placeholders} in trigger keys too, not just entries.
      ...scenario.story_cards.flatMap((c) => [c.keys, c.entry]),
    )
    if (names.length === 0) return begin(scenarioId)
    setPending({ scenario, names })
  }

  const startBlank = async () => {
    const adv = await api.createAdventure({ title: 'Blank Adventure' })
    navigate(`/play/${adv.id}`)
  }

  return (
    <div className="page">
      <div className="page-header">
        <h1>Scenarios</h1>
        <div style={{ display: 'flex', gap: 10 }}>
          <button onClick={startBlank}>Blank Adventure</button>
          <button onClick={async () => {
            try {
              const bundle = await pickJSONFile()
              const { scenario, unmapped_keys } = await api.importScenario(bundle)
              if (unmapped_keys.length) alert(`Imported. Unmapped fields ignored: ${unmapped_keys.join(', ')}`)
              navigate(`/scenarios/${scenario.id}`)
            } catch (err) { alert(err.message) }
          }}>Import</button>
          <button className="primary" onClick={createScenario}>+ New Scenario</button>
        </div>
      </div>

      <div className="filter-bar">
        <input
          type="text"
          className="search-input"
          placeholder="Search scenarios…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        {allTags.length > 0 && (
          <div className="tag-row">
            {allTags.map((tag) => (
              <button
                key={tag}
                className={`tag ${tagFilter === tag ? 'active' : ''}`}
                onClick={() => setTagFilter(tagFilter === tag ? null : tag)}
              >
                {tag}
              </button>
            ))}
          </div>
        )}
      </div>

      {visible === null ? null : visible.length === 0 ? (
        <div className="empty">
          {scenarios.length === 0
            ? 'No scenarios yet. Create one to define a reusable story template.'
            : 'No scenarios match your search.'}
        </div>
      ) : (
        <div className="card-grid">
          {visible.map((sc) => (
            <div key={sc.id} className="card" onClick={() => navigate(`/scenarios/${sc.id}`)}>
              <h3>{sc.title}</h3>
              <p>{sc.description || 'No description'}</p>
              <div className="meta">
                {sc.is_public && <span className="tag small" title="Shared demo scenario (read-only)">demo ✦</span>}
                {splitTags(sc.tags).map((tag) => (
                  <span key={tag} className="tag small">{tag}</span>
                ))}
                <button
                  className="primary"
                  style={{ float: 'right', padding: '3px 10px', fontSize: '0.78rem' }}
                  onClick={(e) => startAdventure(e, sc.id)}
                >
                  Play
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {pending && (
        <PlaceholderModal
          title={pending.scenario.title}
          names={pending.names}
          onCancel={() => setPending(null)}
          onSubmit={(values) => { setPending(null); begin(pending.scenario.id, values) }}
        />
      )}
    </div>
  )
}
