import { useEffect, useState } from 'react'
import { NavLink, Outlet } from 'react-router-dom'
import { api } from './api'
import { AuthModal } from './components'

export default function App() {
  // null until /auth/me resolves; in local mode multi_user=false hides all auth UI.
  const [me, setMe] = useState(null)
  const [authMode, setAuthMode] = useState(null) // 'register' | 'login' | null

  useEffect(() => {
    api.getMe().then(setMe).catch(() => {})
  }, [])

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
    <>
      <nav className="topnav">
        <span className="brand">⚔ AI D&D</span>
        <NavLink to="/" end className={({ isActive }) => `navlink${isActive ? ' active' : ''}`}>
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
        {me?.multi_user && (
          <div className="nav-account">
            {me.is_guest ? (
              <>
                <span className="guest-nudge">Playing as guest — sign up to keep your adventures</span>
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
      </nav>
      <Outlet context={{ me, setMe }} />
      {authMode && (
        <AuthModal mode={authMode} onClose={() => setAuthMode(null)} onAuthed={onAuthed} />
      )}
    </>
  )
}
