import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import {
  CardSkeleton,
  pickJSONFile,
  ScenarioCard,
  splitTags,
  useBeginAdventure,
  useToast,
} from '../components'

export default function Scenarios() {
  const [scenarios, setScenarios] = useState(null)
  const [search, setSearch] = useState('')
  const [tagFilter, setTagFilter] = useState(null)
  const navigate = useNavigate()
  const toast = useToast()
  const { start: startAdventure, startBlank, modal } = useBeginAdventure()

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
              if (unmapped_keys.length) {
                toast(`Imported. Ignored unknown fields: ${unmapped_keys.join(', ')}`)
              }
              navigate(`/scenarios/${scenario.id}`)
            } catch (err) { toast(err.message, 'error') }
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

      {visible === null ? (
        <CardSkeleton count={6} />
      ) : visible.length === 0 ? (
        <div className="empty">
          {scenarios.length === 0
            ? 'No scenarios yet. Create one to define a reusable story template.'
            : 'No scenarios match your search.'}
        </div>
      ) : (
        <div className="card-grid">
          {visible.map((sc, i) => (
            <ScenarioCard
              key={sc.id}
              scenario={sc}
              delay={Math.min(i, 10) * 50}
              maxTags={3}
              onOpen={() => navigate(`/scenarios/${sc.id}`)}
              onPlay={startAdventure}
            />
          ))}
        </div>
      )}

      {modal}
    </div>
  )
}
