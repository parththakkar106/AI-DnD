import { useRef, useState } from 'react'
import { ScenarioArt } from './components'

// Cards render the plate at 46px (92px on a 2x screen), and the editor preview
// at 76px. 400px square gives headroom for both plus any future larger use,
// while keeping the stored data URI in the tens of kilobytes.
const MAX_EDGE = 400
const WEBP_QUALITY = 0.82
// The backend caps the column at 400_000 chars; stay clearly under it so a
// pathological image fails here with a clear message rather than as a 422.
const MAX_DATA_URI = 360_000

// A spread of moods rather than a themed set — most scenarios find something
// close enough here and skip hunting for a picture.
const SUGGESTED_ICONS = [
  '⚔️', '🗡️', '🏹', '🛡️', '🔮', '🗝️', '📜', '🏰',
  '🐉', '👑', '💀', '🕯️', '🌑', '🌲', '⛰️', '🌊',
  '🚀', '🛰️', '🤖', '👁️', '🩸', '🃏',
]

/** Read a File and re-encode it small, as a WebP data URI.
 *
 * Downscaling in the browser means the upload never leaves the machine at full
 * size and the row stays small — no server-side image library required.
 */
function downscaleToDataURI(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onerror = () => reject(new Error('Could not read that file'))
    reader.onload = () => {
      const img = new Image()
      img.onerror = () => reject(new Error('That file is not an image the browser can read'))
      img.onload = () => {
        const scale = Math.min(1, MAX_EDGE / Math.max(img.width, img.height))
        const canvas = document.createElement('canvas')
        // Math.max(1, …) guards against a 0-dimension canvas, which throws.
        canvas.width = Math.max(1, Math.round(img.width * scale))
        canvas.height = Math.max(1, Math.round(img.height * scale))
        const ctx = canvas.getContext('2d')
        ctx.drawImage(img, 0, 0, canvas.width, canvas.height)
        let uri = canvas.toDataURL('image/webp', WEBP_QUALITY)
        // Safari only gained WebP encoding recently; if it silently fell back
        // to PNG, retry as JPEG so we don't store a huge lossless blob.
        if (!uri.startsWith('data:image/webp')) {
          uri = canvas.toDataURL('image/jpeg', WEBP_QUALITY)
        }
        if (uri.length > MAX_DATA_URI) {
          reject(new Error('That image is too detailed to store — try a smaller or simpler one'))
          return
        }
        resolve(uri)
      }
      img.src = reader.result
    }
    reader.readAsDataURL(file)
  })
}

/** Cover-art picker: upload a picture, choose an emoji, or leave it generated.
 *
 * `image` and `icon` are the stored values; `onChange({ image, icon })` gets
 * both on every change so the caller can persist them together.
 */
export default function ArtPicker({ title, image, icon, onChange, disabled }) {
  const fileRef = useRef(null)
  const [error, setError] = useState('')

  const pick = async (event) => {
    const file = event.target.files?.[0]
    // Reset immediately so picking the same file twice still fires onChange.
    event.target.value = ''
    if (!file) return
    setError('')
    try {
      const uri = await downscaleToDataURI(file)
      // An uploaded picture wins over an emoji, so clear the icon to make the
      // precedence visible rather than leaving a hidden value behind.
      onChange({ image: uri, icon: '' })
    } catch (err) {
      setError(err.message)
    }
  }

  const chooseIcon = (glyph) => {
    setError('')
    onChange({ image: '', icon: icon === glyph ? '' : glyph })
  }

  const clear = () => {
    setError('')
    onChange({ image: '', icon: '' })
  }

  return (
    <label className="field">
      <span className="label">Cover art</span>
      <div className="art-picker">
        <span className="art-preview">
          {/* Same three-tier fallback the cards use, at preview size. */}
          <ScenarioArt image={image} icon={icon} title={title} />
        </span>

        <div className="art-controls">
          <div className="art-buttons">
            <button type="button" disabled={disabled} onClick={() => fileRef.current?.click()}>
              Upload image
            </button>
            {(image || icon) && (
              <button type="button" disabled={disabled} onClick={clear}>Clear</button>
            )}
          </div>
          <input
            ref={fileRef}
            type="file"
            accept="image/png,image/jpeg,image/webp,image/gif,image/avif"
            hidden
            onChange={pick}
          />

          <div className="icon-row">
            {SUGGESTED_ICONS.map((glyph) => (
              <button
                key={glyph}
                type="button"
                disabled={disabled}
                className={icon === glyph ? 'active' : ''}
                title={`Use ${glyph}`}
                onClick={() => chooseIcon(glyph)}
              >
                {glyph}
              </button>
            ))}
          </div>

          {error ? (
            <p className="art-hint" style={{ color: 'var(--danger)' }}>{error}</p>
          ) : (
            <p className="art-hint">
              Pictures are shrunk to {MAX_EDGE}px before saving. With neither a picture
              nor an emoji, the card draws its own art from the title.
            </p>
          )}
        </div>
      </div>
    </label>
  )
}
