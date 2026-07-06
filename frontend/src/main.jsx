import React from 'react'
import ReactDOM from 'react-dom/client'
import { createBrowserRouter, RouterProvider } from 'react-router-dom'
import App from './App.jsx'
import Home from './pages/Home.jsx'
import Scenarios from './pages/Scenarios.jsx'
import ScenarioEditor from './pages/ScenarioEditor.jsx'
import Play from './pages/Play.jsx'
import Scripts from './pages/Scripts.jsx'
import ScriptEditor from './pages/ScriptEditor.jsx'
import Settings from './pages/Settings.jsx'
import './index.css'

const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <Home /> },
      { path: 'scenarios', element: <Scenarios /> },
      { path: 'scenarios/:id', element: <ScenarioEditor /> },
      { path: 'play/:id', element: <Play /> },
      { path: 'scripts', element: <Scripts /> },
      { path: 'scripts/:id', element: <ScriptEditor /> },
      { path: 'settings', element: <Settings /> },
    ],
  },
])

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <RouterProvider router={router} />
  </React.StrictMode>,
)
