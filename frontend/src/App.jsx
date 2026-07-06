import { NavLink, Outlet } from 'react-router-dom'

export default function App() {
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
      </nav>
      <Outlet />
    </>
  )
}
