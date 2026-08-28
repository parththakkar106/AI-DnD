// Delays a save until its field has been quiet for a moment.
//
// Every `key` gets its own timer. One shared timer cancels the pending save of
// whatever was edited before it, so editing two fields inside the same window
// saves only the second one.

import { useRef } from 'react'

function useDebouncedSave(delay = 600) {
  const timers = useRef(new Map())
  return (key, fn) => {
    clearTimeout(timers.current.get(key))
    timers.current.set(key, setTimeout(fn, delay))
  }
}

export { useDebouncedSave }
