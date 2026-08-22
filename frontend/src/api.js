// Multi-user mode: a 401 means our session cookie is missing/stale. Hitting
// /api/auth/me creates a fresh guest session, after which the original call
// is retried once.
async function ensureSession() {
  await fetch('/api/auth/me')
}

async function request(path, options = {}, isRetry = false) {
  const resp = await fetch(`/api${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
  if (resp.status === 401 && !isRetry && path !== '/auth/me') {
    await ensureSession()
    return request(path, options, true)
  }
  if (!resp.ok) {
    let detail = resp.statusText
    try {
      const body = await resp.json()
      detail = body.detail || detail
    } catch { /* non-JSON error body */ }
    throw new Error(detail)
  }
  if (resp.status === 204) return null
  return resp.json()
}

// POSTs to an SSE endpoint and dispatches events: {type: 'player'|'chunk'|'done'|'error', ...}
async function streamSSE(path, payload, onEvent, signal, isRetry = false) {
  const resp = await fetch(`/api${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
    signal,
  })
  if (resp.status === 401 && !isRetry) {
    await ensureSession()
    return streamSSE(path, payload, onEvent, signal, true)
  }
  if (!resp.ok) {
    let detail = resp.statusText
    try { detail = (await resp.json()).detail || detail } catch { /* non-JSON */ }
    throw new Error(detail)
  }
  const reader = resp.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const events = buffer.split('\n\n')
    buffer = events.pop() // keep incomplete tail
    for (const block of events) {
      for (const line of block.split('\n')) {
        if (line.startsWith('data:')) onEvent(JSON.parse(line.slice(5)))
      }
    }
  }
}

export const api = {
  // Analytics. The beacon is deliberately not a `request()`: it must never
  // retry, never bootstrap a session, and never surface an error — a counter
  // that can interrupt the app it is counting is worse than no counter.
  trackPageview: (path, { referrer = '', first = false } = {}) => {
    try {
      fetch('/api/analytics/collect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path, referrer, first }),
        keepalive: true,
      }).catch(() => {})
    } catch { /* no beacon, no problem */ }
  },
  getAnalytics: (days) => request(`/analytics/summary?days=${days}`),
  getAccessLog: ({ beforeId, kind, q, limit = 50 } = {}) => {
    const params = new URLSearchParams({ limit })
    if (beforeId != null) params.set('before_id', beforeId)
    if (kind) params.set('kind', kind)
    if (q) params.set('q', q)
    return request(`/analytics/access?${params}`)
  },

  // Auth (Phase 8 — no-ops in local mode beyond getMe)
  getMe: () => request('/auth/me'),
  register: (email, password) =>
    request('/auth/register', { method: 'POST', body: JSON.stringify({ email, password }) }),
  login: (email, password) =>
    request('/auth/login', { method: 'POST', body: JSON.stringify({ email, password }) }),
  logout: () => request('/auth/logout', { method: 'POST' }),

  // Scenarios
  listScenarios: () => request('/scenarios'),
  getScenario: (id) => request(`/scenarios/${id}`),
  createScenario: (data) => request('/scenarios', { method: 'POST', body: JSON.stringify(data) }),
  updateScenario: (id, data) => request(`/scenarios/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteScenario: (id) => request(`/scenarios/${id}`, { method: 'DELETE' }),

  // Adventures
  listAdventures: () => request('/adventures'),
  getAdventure: (id) => request(`/adventures/${id}`),
  getScriptState: (id) => request(`/adventures/${id}/script-state`),
  getWorldState: (id) => request(`/adventures/${id}/world-state`),
  overrideWorldState: (id, overrides) =>
    request(`/adventures/${id}/world-state`, { method: 'PUT', body: JSON.stringify(overrides) }),
  createAdventure: (data) => request('/adventures', { method: 'POST', body: JSON.stringify(data) }),
  updateAdventure: (id, data) => request(`/adventures/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteAdventure: (id) => request(`/adventures/${id}`, { method: 'DELETE' }),
  // A page of the story, walking backwards. `beforeId` is the oldest action
  // already on screen; omit it for the newest window. Anchored on an action
  // rather than an offset so a turn landing mid-scroll cannot shift the page
  // out from under the reader.
  getActions: (advId, { beforeId, limit } = {}) => {
    const params = new URLSearchParams()
    if (beforeId != null) params.set('before_id', beforeId)
    if (limit != null) params.set('limit', limit)
    const query = params.toString()
    return request(`/adventures/${advId}/actions${query ? `?${query}` : ''}`)
  },
  updateAction: (advId, actionId, text) =>
    request(`/adventures/${advId}/actions/${actionId}`, { method: 'PATCH', body: JSON.stringify({ text }) }),
  deleteAction: (advId, actionId) =>
    request(`/adventures/${advId}/actions/${actionId}`, { method: 'DELETE' }),
  // Retry history. The adventure payload carries only the counts, so the
  // attempts themselves are fetched when the reader actually pages through.
  listVariants: (advId, actionId) =>
    request(`/adventures/${advId}/actions/${actionId}/variants`),
  // The same endpoint under the name the pager uses. "Take" is what the UI
  // calls one of these now, and the vocabulary is worth keeping straight —
  // `variant` belongs to the pre-tree pair of columns SP8 drops.
  listTakes: (advId, actionId) =>
    request(`/adventures/${advId}/actions/${actionId}/variants`),
  // No `selectVariant` / `forkFromAttempt` here any more. Both endpoints still
  // exist and are tested, but the pager needs neither: stepping between takes
  // tells the server nothing, and what used to be "take this path" is now
  // whatever the reader writes next, carried by `after_id` on the turn itself.

  // The story tree (Phase 14). One request draws the whole shape however many
  // forks there are. The three that change it answer with the story as it now
  // stands, so the caller replaces its window instead of reloading everything.
  listBranches: (advId) => request(`/adventures/${advId}/branches`),
  switchBranch: (advId, branchId) =>
    request(`/adventures/${advId}/branches/${branchId}/switch`, { method: 'POST' }),
  renameBranch: (advId, branchId, name) =>
    request(`/adventures/${advId}/branches/${branchId}`, {
      method: 'PATCH', body: JSON.stringify({ name }),
    }),
  deleteBranch: (advId, branchId) =>
    request(`/adventures/${advId}/branches/${branchId}`, { method: 'DELETE' }),
  // Play a turn again, differently (SP9). An AI turn regenerates; a player's
  // own takes the text given. Streams, because it is a turn like any other.
  //
  // Reaches any turn, not only the newest — which is the whole difference from
  // `retry`, and the reason the pager can offer this on every message.
  addTake: (advId, actionId, text, handlers, signal) =>
    streamSSE(`/adventures/${advId}/actions/${actionId}/takes`, { text }, handlers, signal),

  // `afterId` names the take the turn is played after. Omitted it means the
  // tip, which is every ordinary turn. Naming a take the story moved past is
  // what forks a branch — stepping between takes to read them does not, and
  // the server is never told about it.
  sendAction: (advId, payload, handlers, signal) =>
    streamSSE(`/adventures/${advId}/actions`, payload, handlers, signal),
  retry: (advId, handlers, signal) => streamSSE(`/adventures/${advId}/retry`, {}, handlers, signal),
  exportAdventure: (id) => request(`/adventures/${id}/export`),
  importAdventure: (bundle) => request('/adventures/import', { method: 'POST', body: JSON.stringify(bundle) }),
  undo: (advId) => request(`/adventures/${advId}/undo`, { method: 'POST' }),
  getAdventureContext: (advId) => request(`/adventures/${advId}/context`),
  // "Update from scenario": GET describes what would change, POST applies it.
  previewRefresh: (advId) => request(`/adventures/${advId}/refresh`),
  refreshFromScenario: (advId, placeholders = {}) =>
    request(`/adventures/${advId}/refresh`, {
      method: 'POST', body: JSON.stringify({ placeholders }),
    }),
  getActionContext: (advId, actionId) => request(`/adventures/${advId}/actions/${actionId}/context`),

  // Memory bank
  listMemories: (advId) => request(`/adventures/${advId}/memories`),
  createMemory: (advId, text) =>
    request(`/adventures/${advId}/memories`, { method: 'POST', body: JSON.stringify({ text }) }),
  updateMemory: (advId, memoryId, data) =>
    request(`/adventures/${advId}/memories/${memoryId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteMemory: (advId, memoryId) =>
    request(`/adventures/${advId}/memories/${memoryId}`, { method: 'DELETE' }),

  listAdventureScripts: (advId) => request(`/adventures/${advId}/scripts`),
  updateAdventureScript: (advId, scriptId, data) =>
    request(`/adventures/${advId}/scripts/${scriptId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  syncAdventureScript: (advId, scriptId) =>
    request(`/adventures/${advId}/scripts/${scriptId}/sync`, { method: 'POST' }),

  // Scripts
  listScripts: () => request('/scripts'),
  getScript: (id) => request(`/scripts/${id}`),
  createScript: (data) => request('/scripts', { method: 'POST', body: JSON.stringify(data) }),
  updateScript: (id, data) => request(`/scripts/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteScript: (id) => request(`/scripts/${id}`, { method: 'DELETE' }),
  testScript: (id, data) => request(`/scripts/${id}/test`, { method: 'POST', body: JSON.stringify(data) }),
  exportScript: (id) => request(`/scripts/${id}/export`),
  importScript: (bundle) => request('/scripts/import', { method: 'POST', body: JSON.stringify(bundle) }),

  // Scenario import/export
  exportScenario: (id) => request(`/scenarios/${id}/export`),
  importScenario: (bundle) => request('/scenarios/import', { method: 'POST', body: JSON.stringify(bundle) }),

  // Story cards
  createStoryCard: (data) => request('/story-cards', { method: 'POST', body: JSON.stringify(data) }),
  updateStoryCard: (id, data) => request(`/story-cards/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteStoryCard: (id) => request(`/story-cards/${id}`, { method: 'DELETE' }),
  // Bulk import/export in AI Dungeon world-info format. `owner` is
  // { scenario_id } or { adventure_id }.
  exportStoryCards: (owner) => request(`/story-cards/export?${new URLSearchParams(owner)}`),
  importStoryCards: (payload) => request('/story-cards/import', { method: 'POST', body: JSON.stringify(payload) }),

  // AI Chat (power users only — 404 for everyone else)
  getChatConfig: () => request('/chat/config'),
  chatStream: (payload, handlers, signal) => streamSSE('/chat/stream', payload, handlers, signal),

  // Debug
  getDebugRequests: () => request('/debug/requests'),

  // Settings
  getSettings: () => request('/settings'),
  updateSettings: (data) => request('/settings', { method: 'PUT', body: JSON.stringify(data) }),
  testConnection: () => request('/settings/test', { method: 'POST' }),
}
