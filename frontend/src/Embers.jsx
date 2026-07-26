import { useEffect, useRef } from 'react'

// Motes per million CSS pixels of viewport. Tuned so a laptop gets ~55 and a
// phone ~20: enough to read as drifting dust, few enough to stay cheap.
const DENSITY = 34
const MAX_PARTICLES = 90

/** Slow-drifting gold motes behind the whole app.
 *
 * Fixed, non-interactive, and drawn on one canvas. It sits below every page in
 * the stacking order and never receives pointer events, so it cannot affect
 * layout or intercept clicks. Disabled outright for `prefers-reduced-motion`,
 * and paused whenever the tab is hidden so a backgrounded tab costs nothing.
 */
export default function Embers() {
  const canvasRef = useRef(null)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)')
    if (reduced.matches) return

    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // Cap the backing store at 2x: beyond that the cost is real and nobody can
    // see the difference on a blurred mote.
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    let width = 0
    let height = 0
    let particles = []
    let frame = 0

    const spawn = (scattered) => ({
      x: Math.random() * width,
      // New motes enter from just below the fold; the first batch is scattered
      // across the whole height so the screen isn't empty on load.
      y: scattered ? Math.random() * height : height + Math.random() * 40,
      radius: 0.6 + Math.random() * 1.6,
      speed: 0.08 + Math.random() * 0.3,
      sway: Math.random() * Math.PI * 2,
      swaySpeed: 0.004 + Math.random() * 0.01,
      alpha: 0.12 + Math.random() * 0.4,
    })

    const resize = () => {
      width = window.innerWidth
      height = window.innerHeight
      canvas.width = Math.round(width * dpr)
      canvas.height = Math.round(height * dpr)
      canvas.style.width = `${width}px`
      canvas.style.height = `${height}px`
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
      const target = Math.min(
        MAX_PARTICLES,
        Math.max(12, Math.round((width * height * DENSITY) / 1_000_000)),
      )
      particles = Array.from({ length: target }, () => spawn(true))
    }

    const draw = () => {
      ctx.clearRect(0, 0, width, height)
      for (let i = 0; i < particles.length; i++) {
        const p = particles[i]
        p.sway += p.swaySpeed
        p.y -= p.speed
        p.x += Math.sin(p.sway) * 0.28
        if (p.y < -8) {
          particles[i] = spawn(false)
          continue
        }
        // Fade out toward the top of the screen so motes dissolve rather than
        // vanishing at the edge.
        const fade = Math.min(1, p.y / height)
        const glow = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, p.radius * 4)
        glow.addColorStop(0, `rgba(232, 196, 118, ${(p.alpha * fade).toFixed(3)})`)
        glow.addColorStop(1, 'rgba(212, 169, 78, 0)')
        ctx.fillStyle = glow
        ctx.beginPath()
        ctx.arc(p.x, p.y, p.radius * 4, 0, Math.PI * 2)
        ctx.fill()
      }
      frame = requestAnimationFrame(draw)
    }

    const start = () => {
      if (!frame) frame = requestAnimationFrame(draw)
    }
    const stop = () => {
      cancelAnimationFrame(frame)
      frame = 0
    }
    const onVisibility = () => (document.hidden ? stop() : start())

    resize()
    start()
    window.addEventListener('resize', resize)
    document.addEventListener('visibilitychange', onVisibility)

    return () => {
      stop()
      window.removeEventListener('resize', resize)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  }, [])

  return <canvas ref={canvasRef} className="embers" aria-hidden="true" />
}
