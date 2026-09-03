/* Two-letter country codes, spelled out.

   The edge sends an ISO code, so a code is what both the counters and the
   access log store. "SG" is the right thing to store and the wrong thing to
   read, and a column of them is a puzzle rather than a fact, so every place
   that shows one runs it through here first.

   The names come from Intl.DisplayNames rather than from a table of 250 names
   shipped to every visitor, and they arrive in the reader's own language. The
   flag is arithmetic, not data: a regional indicator letter sits a fixed
   distance above its ASCII letter, so two of them are the flag. */

const CODE = /^[A-Za-z]{2}$/
const INDICATOR = 0x1f1e6 - 'A'.charCodeAt(0)

// Built once. Constructing one of these per table cell is the expensive way to
// do this, and the constructor throws on an environment without it.
let names = null
try {
  names = new Intl.DisplayNames(undefined, { type: 'region' })
} catch { /* no Intl.DisplayNames — codes it is */ }

export function flagOf(code) {
  if (!CODE.test(code)) return ''
  return String.fromCodePoint(
    ...[...code.toUpperCase()].map((letter) => letter.charCodeAt(0) + INDICATOR),
  )
}

/* The name for a code, or the value itself when it isn't one. The analytics
   list also holds "(unknown)" and "(other)", which are already words and pass
   straight through. */
export function countryName(code) {
  if (!CODE.test(code)) return code || ''
  const upper = code.toUpperCase()
  try {
    return names?.of(upper) || upper
  } catch {
    // `of` rejects a well-formed pair that is not an assigned region.
    return upper
  }
}

export function countryLabel(code) {
  const flag = flagOf(code)
  return flag ? `${flag} ${countryName(code)}` : countryName(code)
}

/* The reverse direction, built once and only if a search needs it. There is no
   list of codes to iterate, so this walks all 676 letter pairs and keeps the
   ones that name a region. */
let byName = null
function index() {
  if (byName) return byName
  byName = new Map()
  const letters = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
  for (const first of letters) {
    for (const second of letters) {
      const code = first + second
      const name = countryName(code)
      if (name && name !== code) byName.set(name.toLowerCase(), code)
    }
  }
  return byName
}

/* The code for a typed country name, or null when the text names no country or
   more than one. The log stores "IN", so a search for "India" would otherwise
   find nothing. "Ind" is India and Indonesia, so it stays a plain text search
   rather than becoming a guess. */
export function codeForCountry(text) {
  const typed = text.trim().toLowerCase()
  if (typed.length < 2 || !names) return null
  let found = null
  for (const [name, code] of index()) {
    if (!name.startsWith(typed)) continue
    if (found) return null
    found = code
  }
  return found
}
