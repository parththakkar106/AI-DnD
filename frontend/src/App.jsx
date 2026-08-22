import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router-dom'
import { api } from './api'
import { AuthModal, ToastHost } from './components'
import Embers from './Embers.jsx'

export default function App() {
  // null until /auth/me resolves; in local mode multi_user=false hides all auth UI.
  const [me, setMe] = useState(null)
  const [authMode, setAuthMode] = useState(null) // 'register' | 'login' | null
  const [navOpen, setNavOpen] = useState(false) // mobile hamburger menu
  const location = useLocation()
  const lastPath = useRef(null)

  useEffect(() => {
    api.getMe().then(setMe).catch(() => {})
  }, [])

  // One pageview per route the reader actually lands on. Guarded on the path
  // rather than fired on every render: StrictMode runs effects twice in dev,
  // and a re-render for unrelated state is not a new page.
  useEffect(() => {
    if (lastPath.current === location.pathname) return
    const first = lastPath.current === null
    lastPath.current = location.pathname
    // document.referrer survives client-side navigation, so it is only honest
    // on the first view — after that this was our own page, not a referral.
    api.trackPageview(location.pathname, {
      referrer: first ? document.referrer : '',
      first,
    })
  }, [location.pathname])

  const onAuthed = (newMe, mode) => {
    setAuthMode(null)
    if (mode === 'login') {
      // Different user now — reload so every page refetches its scoped data.
      window.location.reload()
    } else {
      setMe(newMe) // register upgrades the same user in place; data unchanged
    }
  }

  const logout = async () => {
    try { await api.logout() } catch { /* already logged out */ }
    window.location.reload()
  }

  return (
    <ToastHost>
      <Embers />
      <nav className="topnav">
        <span className="brand">⚔ AI D&D</span>
        <button
          className="nav-hamburger"
          aria-label="Menu"
          aria-expanded={navOpen}
          onClick={() => setNavOpen((o) => !o)}
        >
          {navOpen ? '✕' : '☰'}
        </button>
        <div className={`nav-links${navOpen ? ' open' : ''}`} onClick={() => setNavOpen(false)}>
          <NavLink to="/" end className={({ isActive }) => `navlink${isActive ? ' active' : ''}`}>
            Home
          </NavLink>
          <NavLink to="/adventures" className={({ isActive }) => `navlink${isActive ? ' active' : ''}`}>
            Adventures
          </NavLink>
          <NavLink to="/scenarios" className={({ isActive }) => `navlink${isActive ? ' active' : ''}`}>
            Scenarios
          </NavLink>
          <NavLink to="/scripts" className={({ isActive }) => `navlink${isActive ? ' active' : ''}`}>
            Scripts
          </NavLink>
          <NavLink to="/settings" className={({ isActive }) => `navlink${isActive ? ' active' : ''}`}>
            Settings
          </NavLink>
          {/* Power-user tooling, not part of the game — hidden for everyone else. */}
          {me?.power_user && (
            <NavLink to="/chat" className={({ isActive }) => `navlink${isActive ? ' active' : ''}`}>
              AI Chat
            </NavLink>
          )}
          {/* Owner only: the site's own traffic, on its own allowlist. */}
          {me?.analytics && (
            <NavLink to="/analytics" className={({ isActive }) => `navlink${isActive ? ' active' : ''}`}>
              Visitors
            </NavLink>
          )}
          {me?.multi_user && (
            <div className="nav-account">
              {me.is_guest ? (
                <>
                  <span className="guest-nudge"
                    title={me.guest_retention_days
                      ? `Guest adventures are deleted after ${me.guest_retention_days} days without a visit.`
                      : undefined}>
                    Playing as guest — sign up to keep your adventures
                  </span>
                  <button onClick={() => setAuthMode('login')}>Log in</button>
                  <button className="primary" onClick={() => setAuthMode('register')}>Sign up</button>
                </>
              ) : (
                <>
                  <span className="account-email" title={me.email}>{me.email}</span>
                  <button onClick={logout}>Log out</button>
                </>
              )}
            </div>
          )}
        </div>
      </nav>
      <Outlet context={{ me, setMe }} />
      {authMode && (
        <AuthModal mode={authMode} onClose={() => setAuthMode(null)} onAuthed={onAuthed}
          retentionDays={me?.guest_retention_days} />
      )}
    </ToastHost>
  )
}
