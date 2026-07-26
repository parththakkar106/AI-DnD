import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend port. Overridable because 8000 is a popular default and another
// local app squatting it silently shadows this API (its SPA catch-all answers
// GET /api/* with index.html), which looks like an empty database rather than
// a proxy problem. Start the backend elsewhere and set AIDND_API_PORT to match.
const apiPort = process.env.AIDND_API_PORT || '8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': `http://localhost:${apiPort}`,
    },
  },
})
