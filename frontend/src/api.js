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
  updateAction: (advId, actionId, text) =>
    request(`/adventures/${advId}/actions/${actionId}`, { method: 'PATCH', body: JSON.stringify({ text }) }),
  deleteAction: (advId, actionId) =>
    request(`/adventures/${advId}/actions/${actionId}`, { method: 'DELETE' }),

  sendAction: (advId, payload, handlers, signal) =>
    streamSSE(`/adventures/${advId}/actions`, payload, handlers, signal),
  retry: (advId, handlers, signal) => streamSSE(`/adventures/${advId}/retry`, {}, handlers, signal),
  exportAdventure: (id) => request(`/adventures/${id}/export`),
  importAdventure: (bundle) => request('/adventures/import', { method: 'POST', body: JSON.stringify(bundle) }),
  undo: (advId) => request(`/adventures/${advId}/undo`, { method: 'POST' }),
  getAdventureContext: (advId) => request(`/adventures/${advId}/context`),
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

  // Debug
  getDebugRequests: () => request('/debug/requests'),

  // Settings
  getSettings: () => request('/settings'),
  updateSettings: (data) => request('/settings', { method: 'PUT', body: JSON.stringify(data) }),
  testConnection: () => request('/settings/test', { method: 'POST' }),
}
