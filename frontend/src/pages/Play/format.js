// Labels and formatting shared by the panels that report on a turn.
//
// `SECTION_COLORS` and `SECTION_FALLBACK` stay private. Read a color through
// `sectionColor`, so an unknown section gets the fallback rather than
// `undefined`.

const SECTION_LABELS = {
  narrator: 'Narrator prompt',
  script_context: 'Script context',
  ai_instructions: 'AI Instructions',
  plot_essentials: 'Plot Essentials',
  story_summary: 'Story Summary',
  used_memories: 'Used Memories (memory bank)',
  world_state_guide: 'World State (stat guide)',
  world_state: 'World State (live totals)',
  world_state_rule: 'World State (delta reporting rule)',
  world_lore: 'World Lore (story cards)',
  history: 'Story history',
  authors_note: "Author's Note",
  recent_history: 'Recent history',
  front_memory: 'Front memory',
  length_hint: 'Length guidance',
  world_state_reminder: 'World State (delta reminder)',
}

// One colour per context section, and the single source of truth for it: the
// token bar, the legend and each section's own header all read from here, so a
// slice of the bar and the text it stands for always carry the same colour.
// Related sections share a hue family but never an exact shade — in a stacked
// bar two identical colours read as one section.
const SECTION_COLORS = {
  narrator: '#7d8fc9',
  ai_instructions: '#9c8fd6',
  plot_essentials: '#c97dc0',
  script_context: '#d99ad0',
  story_summary: '#7dc9a2',
  used_memories: '#5fb8c9',
  world_lore: '#c9b47d',
  world_state: '#d79a63',
  world_state_guide: '#b8834a',
  world_state_rule: '#9d7a52',
  world_state_reminder: '#8a6f52',
  history: '#74748c',
  recent_history: '#9d9db4',
  authors_note: '#c97d7d',
  front_memory: '#d99a9a',
  length_hint: '#98a06b',
}
const SECTION_FALLBACK = '#6a6a78'
const sectionColor = (label) => SECTION_COLORS[label] || SECTION_FALLBACK

// Share of the prompt, rounded for glanceability. Sections too small to round
// to a whole percent still say so rather than showing a misleading 0%.
const pctLabel = (pct) => (pct > 0 && pct < 1 ? '<1%' : `${Math.round(pct)}%`)

const FIELD_LABELS = {
  memory: 'Plot Essentials (Memory)',
  authors_note: "Author's Note",
  ai_instructions: 'AI Instructions',
}

function clip(text, n = 90) {
  const one = (text || '').replace(/\s+/g, ' ').trim()
  return one.length > n ? `${one.slice(0, n)}…` : (one || '(empty)')
}

export { SECTION_LABELS, sectionColor, pctLabel, FIELD_LABELS, clip }
