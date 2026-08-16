import React from 'react'
import ReactDOM from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import App from './App.jsx'
import Home from './pages/Home.jsx'
import Adventures from './pages/Adventures.jsx'
import Scenarios from './pages/Scenarios.jsx'
import ScenarioEditor from './pages/ScenarioEditor.jsx'
import Play from './pages/Play.jsx'
import Scripts from './pages/Scripts.jsx'
import ScriptEditor from './pages/ScriptEditor.jsx'
import Settings from './pages/Settings.jsx'
import Chat from './pages/Chat.jsx'
import { trackKeyboardInset } from './keyboard.js'
import './index.css'

trackKeyboardInset()

const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Home /> },
      { path: 'adventures', element: <Adventures /> },
      { path: 'scenarios', element: <Scenarios /> },
      { path: 'scenarios/:id', element: <ScenarioEditor /> },
      { path: 'play/:id', element: <Play /> },
      { path: 'scripts', element: <Scripts /> },
      { path: 'scripts/:id', element: <ScriptEditor /> },
      { path: 'settings', element: <Settings /> },
      // Power users only — the page redirects home and the API 404s otherwise.
      { path: 'chat', element: <Chat /> },
    ],
  },
])

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
)
