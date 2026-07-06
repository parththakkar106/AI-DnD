import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api'
import { Field, StoryCardRow, downloadJSON } from '../components'

export default function ScenarioEditor() {
  const { id } = useParams()
  const navigate = useNavigate()
  const [scenario, setScenario] = useState(null)
  const [allScripts, setAllScripts] = useState([])
  const [status, setStatus] = useState('')
  // One timer per field/card: a single shared timer would cancel the pending
  // save of whatever was edited previously within the debounce window.
  const saveTimers = useRef(new Map())
  const debounceSave = (key, fn) => {
    clearTimeout(saveTimers.current.get(key))
    saveTimers.current.set(key, setTimeout(fn, 600))
  }

  useEffect(() => {
    api.getScenario(id).then(setScenario).catch(() => navigate('/scenarios'))
    api.listScripts().then(setAllScripts).catch(() => {})
  }, [id, navigate])

  const setField = (field, value) => {
    const next = { ...scenario, [field]: value }
    setScenario(next)
    debounceSave(field, async () => {
      await api.updateScenario(id, { [field]: value })
      setStatus('Saved')
      setTimeout(() => setStatus(''), 1500)
    })
  }

  const addCard = async () => {
    const card = await api.createStoryCard({ scenario_id: Number(id) })
    setScenario({ ...scenario, story_cards: [...scenario.story_cards, card] })
  }

  const updateCard = (card) => {
    setScenario({
      ...scenario,
      story_cards: scenario.story_cards.map((c) => (c.id === card.id ? card : c)),
    })
    debounceSave(`card-${card.id}`, () => {
      api.updateStoryCard(card.id, {
        name: card.name, type: card.type, keys: card.keys, entry: card.entry, notes: card.notes,
      })
    })
  }

  const deleteCard = async (cardId) => {
    await api.deleteStoryCard(cardId)
    setScenario({ ...scenario, story_cards: scenario.story_cards.filter((c) => c.id !== cardId) })
  }

  const toggleScript = async (scriptId) => {
    const current = scenario.scripts.map((s) => s.id)
    const next = current.includes(scriptId)
      ? current.filter((sid) => sid !== scriptId)
      : [...current, scriptId]
    const updated = await api.updateScenario(id, { script_ids: next })
    setScenario(updated)
  }

  const exportScenario = async () => {
    downloadJSON(await api.exportScenario(id), `${scenario.title.replace(/\W+/g, '-')}.json`)
  }

  const deleteScenario = async () => {
    if (!confirm('Delete this scenario permanently?')) return
    await api.deleteScenario(id)
    navigate('/scenarios')
  }

  const play = async () => {
    const adv = await api.createAdventure({ scenario_id: Number(id) })
    navigate(`/play/${adv.id}`)
  }

  if (!scenario) return null

  return (
    <div className="page">
      <div className="page-header">
        <h1>Edit Scenario</h1>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span style={{ color: 'var(--text-dim)', fontSize: '0.85rem' }}>{status}</span>
          <button onClick={exportScenario}>Export</button>
          <button className="danger" onClick={deleteScenario}>Delete</button>
          <button className="primary" onClick={play}>Play</button>
        </div>
      </div>

      <Field label="Title" value={scenario.title} onChange={(v) => setField('title', v)} />
      <Field label="Description" value={scenario.description} onChange={(v) => setField('description', v)}
        textarea placeholder="Shown in the scenario list; not sent to the AI." />
      <Field label="Opening Prompt" value={scenario.prompt} onChange={(v) => setField('prompt', v)}
        textarea rows={6} placeholder="The opening story text. Supports ${placeholders} (Phase 5)." />
      <Field label="Plot Essentials (Memory)" value={scenario.memory} onChange={(v) => setField('memory', v)}
        textarea placeholder="Key facts the AI should always remember." />
      <Field label="Author's Note" value={scenario.authors_note} onChange={(v) => setField('authors_note', v)}
        textarea rows={2} placeholder="Style/theme guidance, injected near the end of context." />
      <Field label="AI Instructions" value={scenario.ai_instructions} onChange={(v) => setField('ai_instructions', v)}
        textarea rows={2} placeholder="Behavioral guidance for the model (always included)." />
      <Field label="Tags" value={scenario.tags} onChange={(v) => setField('tags', v)} placeholder="fantasy, mystery" />

      <div className="page-header" style={{ marginTop: 28 }}>
        <h2 style={{ margin: 0, fontFamily: 'Georgia, serif', fontSize: '1.2rem' }}>Story Cards</h2>
        <button onClick={addCard}>+ Add Card</button>
      </div>
      {scenario.story_cards.length === 0 && (
        <div className="empty" style={{ padding: '20px 0' }}>
          No story cards. Cards inject their Entry into context when a trigger word appears in the story.
        </div>
      )}
      {scenario.story_cards.map((card) => (
        <StoryCardRow key={card.id} card={card}
          onChange={updateCard} onDelete={() => deleteCard(card.id)} />
      ))}

      <div className="page-header" style={{ marginTop: 28 }}>
        <h2 style={{ margin: 0, fontFamily: 'Georgia, serif', fontSize: '1.2rem' }}>Attached Scripts</h2>
      </div>
      {allScripts.length === 0 ? (
        <div className="empty" style={{ padding: '20px 0' }}>
          No scripts in your library. Create some on the Scripts page.
        </div>
      ) : (
        allScripts.map((s) => (
          <label key={s.id} className="script-attach">
            <input
              type="checkbox"
              checked={scenario.scripts.some((att) => att.id === s.id)}
              onChange={() => toggleScript(s.id)}
            />
            <span>{s.name}</span>
            <span className="dim">{s.description}</span>
          </label>
        ))
      )}
    </div>
  )
}
