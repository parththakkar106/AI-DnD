import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import {
  CardSkeleton,
  extractPlaceholders,
  pickJSONFile,
  BeginAdventureModal,
  ScenarioArt,
  useToast,
} from '../components'

function splitTags(tags, { isPublic = false } = {}) {
  const all = (tags || '').split(',').map((t) => t.trim()).filter(Boolean)
  // Public scenarios already carry a "demo ✦" badge; the tag would repeat it.
  return isPublic ? all.filter((t) => t.toLowerCase() !== 'demo') : all
}

export default function Scenarios() {
  const [scenarios, setScenarios] = useState(null)
  const [search, setSearch] = useState('')
  const [tagFilter, setTagFilter] = useState(null)
  const [pending, setPending] = useState(null) // { scenario, names } awaiting placeholder values
  const navigate = useNavigate()
  const toast = useToast()

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

  const begin = async (scenarioId, { persona = {}, placeholders = {} } = {}) => {
    const adv = await api.createAdventure({
      scenario_id: scenarioId,
      // A blank adventure has no scenario to take a title from, so name it here
      // exactly as the button that starts it did before the modal existed.
      title: scenarioId ? null : 'Blank Adventure',
      placeholders,
      persona_name: persona.name || '',
      persona_pronouns: persona.pronouns || '',
      persona_desc: persona.desc || '',
    })
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
    // Always open the modal, even with no placeholders: it is where the
    // player names their character.
    setPending({ scenario, names })
  }

  // A blank adventure has no scenario, so there are no placeholders to collect,
  // but the player still names their character. `pending.scenario` is null for
  // this path, and `begin` is called with no scenario id.
  const startBlank = () => setPending({ scenario: null, names: [] })

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
            <article
              key={sc.id}
              className="card tome enter"
              style={{ animationDelay: `${Math.min(i, 10) * 50}ms` }}
              onClick={() => navigate(`/scenarios/${sc.id}`)}
            >
              <div className="card-head">
                <ScenarioArt image={sc.image_url} icon={sc.icon} title={sc.title} />
                <div className="card-headings">
                  <h3>{sc.title}</h3>
                </div>
              </div>
              <p className="snippet">{sc.description || 'No description yet.'}</p>
              <footer className="card-foot">
                <div className="tag-cluster">
                  {sc.is_public && (
                    <span className="tag small" title="Shared demo scenario (read-only)">demo ✦</span>
                  )}
                  {splitTags(sc.tags, { isPublic: sc.is_public }).slice(0, 3).map((tag) => (
                    <span key={tag} className="tag small">{tag}</span>
                  ))}
                </div>
                <button className="primary compact" onClick={(e) => startAdventure(e, sc.id)}>
                  Play
                </button>
              </footer>
            </article>
          ))}
        </div>
      )}

      {pending && (
        <BeginAdventureModal
          title={pending.scenario ? pending.scenario.title : 'Blank Adventure'}
          names={pending.names}
          onCancel={() => setPending(null)}
          onSubmit={(answers) => {
            setPending(null)
            begin(pending.scenario ? pending.scenario.id : null, answers)
          }}
        />
      )}
    </div>
  )
}
