import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import {
  CardSkeleton,
  ScenarioArt,
  ScenarioCard,
  useBeginAdventure,
} from '../components'

// How many in-progress stories the landing page shows before deferring to
// "See all". Two rows on a wide screen; enough to recognise, not a full index.
const CONTINUE_LIMIT = 4
const SCENARIO_LIMIT = 6

function relativeTime(iso) {
  // Stored timestamps are naive UTC, hence the appended Z (matches the rest of
  // the app's date handling).
  const then = new Date(iso + 'Z')
  const minutes = Math.round((Date.now() - then.getTime()) / 60000)
  if (minutes < 2) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 7) return `${days}d ago`
  return then.toLocaleDateString()
}

/** Section heading framed by ornamental rules — the illuminated-tome motif. */
function Rule({ children, action }) {
  return (
    <div className="rule-head">
      <h2 className="rule">
        <span>{children}</span>
      </h2>
      {action}
    </div>
  )
}

export default function Home() {
  const [adventures, setAdventures] = useState(null)
  const [scenarios, setScenarios] = useState(null)
  const navigate = useNavigate()
  const { start: startAdventure, modal } = useBeginAdventure()

  useEffect(() => {
    api.listAdventures().then(setAdventures).catch(() => setAdventures([]))
    api.listScenarios().then(setScenarios).catch(() => setScenarios([]))
  }, [])

  const ongoing = useMemo(() => (adventures || []).slice(0, CONTINUE_LIMIT), [adventures])
  const featured = useMemo(() => (scenarios || []).slice(0, SCENARIO_LIMIT), [scenarios])

  const loading = adventures === null || scenarios === null
  const nothingAtAll = !loading && adventures.length === 0 && scenarios.length === 0

  return (
    <div className="page">
      {/* Returning players want their story first, so the only thing above the
          fold is a compact banner — no full-height splash. */}
      <header className="hall">
        <p className="hall-eyebrow">Welcome back</p>
        <h1 className="hall-title">The table is set</h1>
        <p className="hall-sub">
          {loading
            ? 'Gathering your stories…'
            : adventures.length > 0
              ? `${adventures.length} ${adventures.length === 1 ? 'story' : 'stories'} in progress · ${scenarios.length} ${scenarios.length === 1 ? 'world' : 'worlds'} to explore`
              : `${scenarios.length} ${scenarios.length === 1 ? 'world' : 'worlds'} waiting for a first line`}
        </p>
      </header>

      {/* ---------- Continue ---------- */}
      {(loading || adventures.length > 0) && (
        <section className="home-section">
          <Rule
            action={
              adventures?.length > CONTINUE_LIMIT ? (
                <button className="linklike ornate" onClick={() => navigate('/adventures')}>
                  See all {adventures.length} ❖
                </button>
              ) : null
            }
          >
            Continue
          </Rule>

          {loading ? (
            <CardSkeleton count={2} lines={3} />
          ) : (
            <div className="card-grid wide">
              {ongoing.map((adv, i) => (
                <article
                  key={adv.id}
                  className="card tome enter"
                  style={{ animationDelay: `${i * 60}ms` }}
                  onClick={() => navigate(`/play/${adv.id}`)}
                >
                  <span className="seal" title="In progress" aria-hidden="true">✦</span>
                  <div className="card-head">
                    <ScenarioArt image={adv.image_url} icon={adv.icon} title={adv.title} large />
                    <div className="card-headings">
                      <h3>{adv.title}</h3>
                      {/* An adventure created from a scenario inherits its
                          title, so showing both just prints it twice. */}
                      {adv.scenario_title && adv.scenario_title !== adv.title && (
                        <p className="card-from">From “{adv.scenario_title}”</p>
                      )}
                    </div>
                  </div>
                  {adv.snippet ? (
                    <p className="snippet dropcap">{adv.snippet}</p>
                  ) : (
                    <p className="snippet muted">Not a word written yet. Open it and begin.</p>
                  )}
                  <footer className="card-foot">
                    <span className="turns">
                      {adv.action_count} {adv.action_count === 1 ? 'turn' : 'turns'}
                    </span>
                    <span className="dot" aria-hidden="true">·</span>
                    <span className="turns">{relativeTime(adv.updated_at)}</span>
                    <span className="resume">Resume →</span>
                  </footer>
                </article>
              ))}
            </div>
          )}
        </section>
      )}

      {/* ---------- Scenarios ---------- */}
      <section className="home-section">
        <Rule
          action={
            scenarios?.length > SCENARIO_LIMIT ? (
              <button className="linklike ornate" onClick={() => navigate('/scenarios')}>
                See all {scenarios.length} ❖
              </button>
            ) : null
          }
        >
          {adventures?.length > 0 ? 'Begin a new story' : 'Choose a world'}
        </Rule>

        {loading ? (
          <CardSkeleton count={3} />
        ) : nothingAtAll ? (
          <div className="empty">
            Nothing here yet. Create a scenario to define your first world.
          </div>
        ) : (
          <div className="card-grid">
            {featured.map((sc, i) => (
              <ScenarioCard
                key={sc.id}
                scenario={sc}
                delay={i * 60}
                maxTags={2}
                onOpen={() => navigate(`/scenarios/${sc.id}`)}
                onPlay={startAdventure}
              />
            ))}
          </div>
        )}
      </section>

      {modal}
    </div>
  )
}
