// Mobile virtual keyboards vs. the bottom-sticky composers.
//
// A phone keyboard covers the bottom of the screen without shrinking the
// layout viewport, so `position: sticky; bottom: 0` keeps pinning the composer
// to a bottom edge the user can no longer see — tap the box and it vanishes
// behind the keyboard. Chromium can be told to shrink the layout viewport
// instead (`interactive-widget=resizes-content` on the viewport meta in
// index.html), which fixes it there. iOS Safari ignores that flag and only
// ever shrinks the *visual* viewport, so measure the overlap ourselves and
// publish it as --kb-inset for the composers to lift by.
//
// Where the browser did resize the layout viewport, window.innerHeight shrank
// along with it and this measures ~0 — the same CSS is a no-op there rather
// than a double lift.

const MIN_KEYBOARD = 80 // px — below this it's browser chrome, not a keyboard

export function trackKeyboardInset() {
  const vv = window.visualViewport
  if (!vv) return
  const apply = () => {
    // Pinch-zoom shrinks the visual viewport too, and that is not a keyboard.
    const covered = vv.scale > 1.01 ? 0 : window.innerHeight - vv.height - vv.offsetTop
    const inset = covered > MIN_KEYBOARD ? Math.round(covered) : 0
    document.documentElement.style.setProperty('--kb-inset', `${inset}px`)
  }
  // resize fires as the keyboard animates in; scroll fires as the page pans
  // under a keyboard that is already up, which moves offsetTop.
  vv.addEventListener('resize', apply)
  vv.addEventListener('scroll', apply)
  apply()
}
