// The plot panel: the adventure's own copy of the scenario text and cards.

import { useRef, useState } from 'react'
import { api } from '../../../api'
import { Field, StoryCardRow, downloadJSON, pickJSONFile, useToast } from '../../../components'
import { RefreshModal } from '../RefreshModal'

function PlotPanel({ adventure, setAdventure, onWorldStateChanged }) {
  const toast = useToast()
  const [plan, setPlan] = useState(null)      // non-null while the modal is open
  const [planning, setPlanning] = useState(false)
  // One timer per field/card: a single shared timer would cancel the pending
  // save of whatever was edited previously within the debounce window.
  const saveTimers = useRef(new Map())
  const debounceSave = (key, fn) => {
    clearTimeout(saveTimers.current.get(key))
    saveTimers.current.set(key, setTimeout(fn, 600))
  }

  const setField = (field, value) => {
    setAdventure({ ...adventure, [field]: value })
    debounceSave(field, () => api.updateAdventure(adventure.id, { [field]: value }))
  }

  const addCard = async () => {
    const card = await api.createStoryCard({ adventure_id: adventure.id })
    setAdventure({ ...adventure, story_cards: [...adventure.story_cards, card] })
  }

  const updateCard = (card) => {
    setAdventure({
      ...adventure,
      story_cards: adventure.story_cards.map((c) => (c.id === card.id ? card : c)),
    })
    debounceSave(`card-${card.id}`, () => {
      api.updateStoryCard(card.id, {
        name: card.name, type: card.type, keys: card.keys, entry: card.entry, notes: card.notes,
      })
    })
  }

  const deleteCard = async (cardId) => {
    await api.deleteStoryCard(cardId)
    setAdventure({
      ...adventure,
      story_cards: adventure.story_cards.filter((c) => c.id !== cardId),
    })
  }

  const exportCards = async () => {
    const cards = await api.exportStoryCards({ adventure_id: adventure.id })
    downloadJSON(cards, `${(adventure.title || 'adventure').replace(/\W+/g, '-')}-cards.json`)
  }

  const importCards = async () => {
    try {
      const parsed = await pickJSONFile()
      const cards = Array.isArray(parsed) ? parsed : (parsed.cards || parsed.storyCards)
      if (!Array.isArray(cards)) return toast('Expected a JSON array of story cards.', 'error')
      const created = await api.importStoryCards({ adventure_id: adventure.id, cards })
      setAdventure({ ...adventure, story_cards: [...adventure.story_cards, ...created] })
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  // Ask the server what a refresh would do, then let the player confirm it.
  const openRefresh = async () => {
    setPlanning(true)
    try {
      setPlan(await api.previewRefresh(adventure.id))
    } catch (err) {
      // 404 = the scenario was deleted or unshared; there's nothing to sync to.
      toast(err.message, 'error')
    } finally {
      setPlanning(false)
    }
  }

  const applyRefresh = async (placeholders) => {
    try {
      const updated = await api.refreshFromScenario(adventure.id, placeholders)
      setAdventure(updated)
      setPlan(null)
      onWorldStateChanged?.()
      toast('Updated from scenario.')
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  return (
    <div>
      {adventure.scenario_id != null && (
        <div className="plot-source">
          <span className="dim">
            Copied from its scenario when the adventure began; later scenario edits don't
            reach it on their own.
          </span>
          <button className="linklike" onClick={openRefresh} disabled={planning}
            title="Replace this adventure's plot text and scenario story cards with the scenario's current content">
            {planning ? 'Checking…' : '⟳ Update from scenario'}
          </button>
        </div>
      )}
      {plan && (
        <RefreshModal plan={plan} onConfirm={applyRefresh} onCancel={() => setPlan(null)} />
      )}

      <Field label="Plot Essentials (Memory)" value={adventure.memory}
        onChange={(v) => setField('memory', v)} textarea
        placeholder="Key facts the AI should always remember." />
      <Field label="Author's Note" value={adventure.authors_note}
        onChange={(v) => setField('authors_note', v)} textarea rows={2}
        placeholder="Style/theme guidance, injected near the end of context." />
      <Field label="AI Instructions" value={adventure.ai_instructions}
        onChange={(v) => setField('ai_instructions', v)} textarea rows={2}
        placeholder="Behavioral guidance for the model." />
      <Field label="Story Summary" value={adventure.story_summary}
        onChange={(v) => setField('story_summary', v)} textarea
        placeholder="Running summary of events so far. Updated automatically every 15 actions when auto-summarization is on; your edits are kept as the base for the next update." />

      <div className="page-header" style={{ marginTop: 18 }}>
        <h3 style={{ margin: 0 }}>Story Cards</h3>
        <div style={{ display: 'flex', gap: 6 }}>
          <button onClick={exportCards} disabled={adventure.story_cards.length === 0}>Export</button>
          <button onClick={importCards}>Import</button>
          <button onClick={addCard}>+ Add</button>
        </div>
      </div>
      {adventure.story_cards.length === 0 && (
        <div className="empty" style={{ padding: '12px 0' }}>No story cards yet.</div>
      )}
      {adventure.story_cards.map((card) => (
        <StoryCardRow key={card.id} card={card}
          onChange={updateCard} onDelete={() => deleteCard(card.id)} />
      ))}
    </div>
  )
}

export { PlotPanel }
