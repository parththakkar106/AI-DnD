// The story card handlers that the scenario editor and the plot panel share.
//
// `owner` is the scenario or adventure that holds `story_cards`, and `setOwner`
// replaces it. `link` names the owner to the API, as `{ scenario_id }` or
// `{ adventure_id }`. `fileBase` starts the export file name.

import { api } from '../api'
import { downloadJSON, pickJSONFile, useToast } from '../components'
import { useDebouncedSave } from './useDebouncedSave'

function useStoryCards(owner, setOwner, link, fileBase) {
  const toast = useToast()
  const debounceSave = useDebouncedSave()
  const setCards = (cards) => setOwner({ ...owner, story_cards: cards })

  const addCard = async () => {
    const card = await api.createStoryCard(link)
    setCards([...owner.story_cards, card])
  }

  const updateCard = (card) => {
    setCards(owner.story_cards.map((c) => (c.id === card.id ? card : c)))
    debounceSave(`card-${card.id}`, () => {
      api.updateStoryCard(card.id, {
        name: card.name, type: card.type, keys: card.keys, entry: card.entry, notes: card.notes,
      })
    })
  }

  const deleteCard = async (cardId) => {
    await api.deleteStoryCard(cardId)
    setCards(owner.story_cards.filter((c) => c.id !== cardId))
  }

  const exportCards = async () => {
    const cards = await api.exportStoryCards(link)
    downloadJSON(cards, `${fileBase.replace(/\W+/g, '-')}-cards.json`)
  }

  const importCards = async () => {
    try {
      const parsed = await pickJSONFile()
      const cards = Array.isArray(parsed) ? parsed : (parsed.cards || parsed.storyCards)
      if (!Array.isArray(cards)) return toast('Expected a JSON array of story cards.', 'error')
      const created = await api.importStoryCards({ ...link, cards })
      setCards([...owner.story_cards, ...created])
    } catch (err) {
      toast(err.message, 'error')
    }
  }

  return { addCard, updateCard, deleteCard, exportCards, importCards }
}

export { useStoryCards }
