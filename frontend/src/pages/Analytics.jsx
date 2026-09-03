/* Visit analytics — the owner's view of who came by and what they did.

   Everything on this page arrives in a single aggregate response (see
   backend/app/analytics.py), so changing the range is one small request, not a
   scan of anything. The page is hidden from everyone else: the nav link is
   gated on `me.analytics`, this component bounces, and the API 404s.

   Charts are plain HTML — a flex row of columns, a row of bars — rather than
   SVG or a charting library. At this size that is less code, responsive for
   free, and keeps the CSP as tight as it is. */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useOutletContext } from 'react-router-dom'
import { api } from '../api'
import { codeForCountry, countryLabel } from '../countries'

const RANGES = [
  { days: 7, label: '7 days' },
  { days: 30, label: '30 days' },
  { days: 90, label: '90 days' },
]

const nf = new Intl.NumberFormat()

// Weekday + day for a short range, day + month for a long one: a 90-day axis
// has no room for "Mon".
function dayLabel(iso, days) {
  const date = new Date(`${iso}T00:00:00Z`)
  const opts = days <= 14
    ? { weekday: 'short', timeZone: 'UTC' }
    : { day: 'numeric', month: 'short', timeZone: 'UTC' }
  return date.toLocaleDateString(undefined, opts)
}

function fullDate(iso) {
  return new Date(`${iso}T00:00:00Z`)
    .toLocaleDateString(undefined, { dateStyle: 'medium', timeZone: 'UTC' })
}

/* ---------- Pieces ---------- */

function StatTile({ label, value, hint }) {
  return (
    <div className="an-tile" title={hint || undefined}>
      <div className="an-tile-value">{nf.format(value ?? 0)}</div>
      <div className="an-tile-label">{label}</div>
      {hint && <div className="an-tile-hint">{hint}</div>}
    </div>
  )
}

/* A day-by-day column chart. `series` names the stacked segments bottom-up;
   one segment means one plain bar and no legend, since the title already says
   what it is. */
function DayChart({ title, data, series, days }) {
  const [hover, setHover] = useState(null)
  const max = Math.max(1, ...data.map((d) => series.reduce((sum, s) => sum + (d[s.key] || 0), 0)))
  // Only ever three x labels. Every column labelled is unreadable at 90 days
  // and redundant at 7 — the tooltip carries the exact date either way.
  const ticks = new Set([0, Math.floor((data.length - 1) / 2), data.length - 1])
  const empty = data.every((d) => series.every((s) => !d[s.key]))

  return (
    <section className="an-card an-chart">
      <header className="an-card-head">
        <h2>{title}</h2>
        {series.length > 1 && (
          <div className="an-legend">
            {[...series].reverse().map((s) => (
              <span key={s.key} className="an-legend-item">
                <i className="an-swatch" style={{ background: s.color }} />
                {s.label}
              </span>
            ))}
          </div>
        )}
      </header>
      <div className="an-plot" onMouseLeave={() => setHover(null)}>
        <div className="an-gridline" style={{ bottom: '100%' }}><span>{nf.format(max)}</span></div>
        <div className="an-gridline" style={{ bottom: '50%' }}><span>{nf.format(Math.round(max / 2))}</span></div>
        <div className="an-columns">
          {data.map((point, i) => {
            const total = series.reduce((sum, s) => sum + (point[s.key] || 0), 0)
            return (
              <div
                key={point.day}
                className={`an-column${hover === i ? ' hot' : ''}`}
                onMouseEnter={() => setHover(i)}
                onFocus={() => setHover(i)}
                tabIndex={0}
                aria-label={`${fullDate(point.day)}: ${total}`}
              >
                <div className="an-stack">
                  {[...series].reverse().map((s) => (
                    (point[s.key] || 0) > 0 && (
                      <div
                        key={s.key}
                        className="an-bar"
                        style={{
                          height: `${((point[s.key] || 0) / max) * 100}%`,
                          background: s.color,
                        }}
                      />
                    )
                  ))}
                </div>
                {hover === i && (
                  <div className="an-tip">
                    <strong>{fullDate(point.day)}</strong>
                    {series.map((s) => (
                      <span key={s.key}>
                        <i className="an-swatch" style={{ background: s.color }} />
                        {s.label}: {nf.format(point[s.key] || 0)}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      </div>
      <div className="an-axis">
        {data.map((point, i) => (
          <span key={point.day}>{ticks.has(i) ? dayLabel(point.day, days) : ''}</span>
        ))}
      </div>
      {empty && <div className="an-overlay-empty">Nothing recorded in this range</div>}
    </section>
  )
}

/* The funnel. Each step's bar is drawn against the first step, so the shape of
   the drop-off is the picture; the percentage beside it is of the step above,
   which is the number you act on. */
function Funnel({ steps }) {
  const top = steps[0]?.count || 0
  return (
    <section className="an-card">
      <header className="an-card-head">
        <h2>Where visitors get to</h2>
        <span className="an-note">people, counted once each</span>
      </header>
      {top === 0 ? (
        <div className="an-empty">No visitors in this range.</div>
      ) : (
        <div className="an-funnel">
          {steps.map((step, i) => {
            const previous = i === 0 ? step.count : steps[i - 1].count
            const share = previous ? Math.round((step.count / previous) * 100) : 0
            return (
              <div key={step.step} className="an-funnel-row">
                <div className="an-funnel-label">{step.step}</div>
                <div className="an-funnel-track">
                  <div
                    className="an-funnel-bar"
                    style={{ width: `${top ? (step.count / top) * 100 : 0}%` }}
                  />
                </div>
                <div className="an-funnel-value">
                  {nf.format(step.count)}
                  {i > 0 && <span className="an-funnel-share">{share}%</span>}
                </div>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

/* `labelOf` exists for the country list, whose labels are stored as ISO codes
   and are unreadable as they stand. Every other list already holds words. */
function TopList({ title, rows, note, empty = 'Nothing yet', labelOf = (label) => label }) {
  const max = Math.max(1, ...rows.map((r) => r.hits))
  return (
    <section className="an-card">
      <header className="an-card-head">
        <h2>{title}</h2>
        {note && <span className="an-note">{note}</span>}
      </header>
      {rows.length === 0 ? (
        <div className="an-empty">{empty}</div>
      ) : (
        <ul className="an-list">
          {rows.map((row) => (
            <li key={row.label}>
              {/* The bar is the row's own background, so a long label stays
                  readable on top of it instead of being squeezed beside it. */}
              <span className="an-list-fill" style={{ width: `${(row.hits / max) * 100}%` }} />
              <span className="an-list-label" title={row.label}>{labelOf(row.label)}</span>
              <span className="an-list-value">{nf.format(row.hits)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/* ---------- Access log ---------- */

const KINDS = [
  { key: '', label: 'Everything' },
  { key: 'session', label: 'Sessions' },
  { key: 'login', label: 'Sign-ins' },
  { key: 'register', label: 'Registrations' },
  { key: 'login_failed', label: 'Failed' },
]

const KIND_LABEL = {
  session: 'Session',
  login: 'Signed in',
  register: 'Registered',
  login_failed: 'Failed sign-in',
}

function when(iso) {
  const date = new Date(iso.endsWith('Z') ? iso : `${iso}Z`)
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

/* What one person did, opened from their row in the table.

   The table answers who arrived. This answers what they came for: counts,
   dates, the shared scenarios they chose, and the addresses they arrived from.
   It shows nothing they wrote. The server does not send their adventure titles
   or their text, and it counts the scenarios they wrote themselves without
   naming them. */

const DEVICE_WORD = { desktop: 'Desktop', mobile: 'Phone', tablet: 'Tablet', bot: 'Bot' }

// What the server says when a user-agent tells it nothing. Worth a dash rather
// than the word, in a column of names.
const UNKNOWN = '(unknown)'

const known = (word) => word && word !== UNKNOWN
const deviceWord = (device) => DEVICE_WORD[device] || (known(device) ? device : '—')

/* "Safari 17 · iOS 17", and half of it when only half is recognised. Some
   agents name neither, which is what the full string on the hover is for. */
function agentLine(row) {
  return [row.browser, row.platform].filter(known).join(' · ')
}

function Fact({ term, children }) {
  return (
    <div className="an-fact">
      <dt>{term}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function PersonCard({ detail }) {
  if (!detail) return <div className="an-person an-empty">Reading…</div>
  if (detail.error) {
    return <div className="an-person an-empty">Couldn’t open this person: {detail.error}</div>
  }

  const log = detail.log
  const played = detail.scenarios.length > 0 || detail.own_scenarios > 0
  return (
    <div className="an-person">
      <div className="an-person-tiles">
        <div className="an-person-tile">
          <b>{nf.format(detail.adventures)}</b><span>Adventures</span>
        </div>
        <div className="an-person-tile">
          <b>{nf.format(detail.turns)}</b><span>Turns played</span>
        </div>
        {/* One session row per day per address, so this is days here rather
            than visits, and it is the honest name for it. */}
        <div className="an-person-tile">
          <b>{nf.format(log.sessions)}</b><span>Days seen</span>
        </div>
        <div className="an-person-tile">
          <b>{nf.format(log.logins)}</b><span>Sign-ins</span>
        </div>
      </div>

      <div className="an-person-cols">
        <dl className="an-facts">
          <Fact term="First seen">{when(log.first_at)}</Fact>
          <Fact term="Last seen">{when(log.last_at)}</Fact>
          <Fact term="Last played">
            {detail.last_played_at ? when(detail.last_played_at) : 'No turns yet'}
          </Fact>
          <Fact term="Account">
            {!detail.account_exists
              ? 'Deleted — the log kept the rows'
              : detail.is_guest
                ? 'Guest, never registered'
                : log.registered_at
                  ? `Registered ${when(log.registered_at)}`
                  : `Account since ${when(detail.account_since)}`}
          </Fact>
          {detail.demo_turns_today > 0 && (
            <Fact term="On the demo key">
              {nf.format(detail.demo_turns_today)} turns today
            </Fact>
          )}
        </dl>

        <div className="an-person-block">
          <h4>What they played</h4>
          {!played ? (
            <p className="an-person-note">Nothing started yet.</p>
          ) : (
            <ul className="an-person-list">
              {detail.scenarios.map((row) => (
                <li key={row.title}>
                  <span className="an-person-name" title={row.title}>{row.title}</span>
                  <span className="an-cell-dim">{nf.format(row.adventures)}</span>
                </li>
              ))}
            </ul>
          )}
          {detail.own_scenarios > 0 && (
            <p className="an-person-note">
              {nf.format(detail.own_scenarios)} scenario
              {detail.own_scenarios === 1 ? '' : 's'} of their own, which this
              page counts but doesn’t name.
            </p>
          )}
        </div>

        <div className="an-person-block">
          <h4>Where from</h4>
          <ul className="an-person-list">
            {detail.places.map((place) => (
              <li key={`${place.ip}|${place.country}`}>
                <span className="an-cell-mono">{place.ip || '—'}</span>
                <span className="an-cell-dim" title={place.country || undefined}>
                  {place.country ? countryLabel(place.country) : '—'}
                </span>
              </li>
            ))}
          </ul>
        </div>

        {/* One line per browser they have arrived in, newest first — the
            machines to reproduce anything they report on. The full agent
            string is on the hover, for the ones the parser doesn't know. */}
        <div className="an-person-block">
          <h4>Browsers</h4>
          <ul className="an-person-list">
            {detail.devices.map((row) => (
              <li key={`${row.device}|${row.user_agent}`} title={row.user_agent || undefined}>
                <span className="an-person-name">
                  {agentLine(row) || 'Unrecognised browser'}
                </span>
                <span className="an-cell-dim">{deviceWord(row.device)}</span>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </div>
  )
}

/* The browsers the log has seen, which is the list to test against. Grouped by
   what the agent strings mean rather than by the strings, so one person who
   updated Chrome twice is one row. It reads the same table the log below does,
   so it needs no new collection and covers every visit ever recorded. */
function DevicesSeen() {
  const [rows, setRows] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let live = true
    api.getAccessDevices(10).then(
      (result) => { if (live) setRows(result.devices) },
      (err) => { if (live) setError(err.message) },
    )
    return () => { live = false }
  }, [])

  if (error) return <div className="an-card an-empty">Couldn’t load devices: {error}</div>
  if (!rows) return null
  return (
    <section className="an-card">
      <header className="an-card-head">
        <h2>What to test on</h2>
        <span className="an-note">every visit logged, most people first</span>
      </header>
      {rows.length === 0 ? (
        <div className="an-empty">Nothing logged yet.</div>
      ) : (
        <div className="an-table-scroll">
          <table className="an-table an-table-tight">
            <thead>
              <tr>
                <th>Browser</th><th>System</th><th>Kind</th>
                <th>People</th><th>Arrivals</th><th>Last seen</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.browser}|${row.platform}|${row.device}`}>
                  <td>{known(row.browser) ? row.browser : 'Unrecognised'}</td>
                  <td>{known(row.platform) ? row.platform : '—'}</td>
                  <td className="an-cell-dim">{deviceWord(row.device)}</td>
                  {/* A failed sign-in is an arrival that answers for no
                      account, so a row can have arrivals and nobody. */}
                  <td>{nf.format(row.people)}</td>
                  <td className="an-cell-dim">{nf.format(row.hits)}</td>
                  <td className="an-cell-dim">{row.last_at ? when(row.last_at) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}

function AccessLog() {
  const [kind, setKind] = useState('')
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(null)   // { events, has_more }
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [openRow, setOpenRow] = useState(null)   // which row is expanded
  // user id -> their detail, kept so reopening a person is instant. The same
  // person usually owns several rows on screen, and each one opens the same
  // card. A missing entry is one still in flight, which the card reads as
  // "Reading…".
  const [people, setPeople] = useState({})

  // Typing shouldn't fire a request per keystroke against a table scan.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 350)
    return () => clearTimeout(timer)
  }, [search])

  // The country column is stored as an ISO code, so a search for "Germany"
  // would match nothing. A search that names exactly one country is sent as
  // that country's code; anything else is sent as typed.
  const lookup = useMemo(() => codeForCountry(query) || query, [query])

  useEffect(() => {
    let live = true
    setPage(null)
    setOpenRow(null)
    api.getAccessLog({ kind, q: lookup }).then(
      (result) => { if (live) { setPage(result); setError(null) } },
      (err) => { if (live) setError(err.message) },
    )
    return () => { live = false }
  }, [kind, lookup])

  // A row with no user id is a failed sign-in: it names an address that no
  // account answers to, so there is nothing to open. Asked-for ids live in a
  // ref rather than in `people`, so that opening the same person twice while
  // the first request is still out doesn't send a second one.
  const asked = useRef(new Set())
  const openPerson = useCallback((event) => {
    if (!event.user_id) return
    setOpenRow((current) => (current === event.id ? null : event.id))
    if (asked.current.has(event.user_id)) return
    asked.current.add(event.user_id)
    api.getPerson(event.user_id).then(
      (detail) => setPeople((now) => ({ ...now, [event.user_id]: detail })),
      (err) => setPeople((now) => ({ ...now, [event.user_id]: { error: err.message } })),
    )
  }, [])

  const loadMore = useCallback(async () => {
    if (!page?.events.length || busy) return
    setBusy(true)
    try {
      // Anchored on the oldest row already on screen, so rows arriving while
      // this is open can't shift the next page.
      const next = await api.getAccessLog({
        kind, q: lookup, beforeId: page.events[page.events.length - 1].id,
      })
      setPage({ events: [...page.events, ...next.events], has_more: next.has_more })
    } catch (err) {
      setError(err.message)
    } finally {
      setBusy(false)
    }
  }, [page, kind, lookup, busy])

  return (
    <section className="an-card">
      <header className="an-card-head an-log-head">
        <div className="an-ranges">
          {KINDS.map((option) => (
            <button
              key={option.key}
              className={kind === option.key ? 'primary' : ''}
              onClick={() => setKind(option.key)}
            >
              {option.label}
            </button>
          ))}
        </div>
        <input
          type="text"
          className="search-input an-log-search"
          placeholder="Search email, IP, country…"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </header>

      {error && <div className="an-empty">Couldn’t load the log: {error}</div>}
      {!page && !error && <div className="an-empty">Reading…</div>}

      {page && (page.events.length === 0 ? (
        <div className="an-empty">
          {query || kind ? 'Nothing matches that.' : 'Nothing logged yet.'}
        </div>
      ) : (
        <>
          <div className="an-table-scroll">
            <table className="an-table">
              <thead>
                <tr>
                  <th>When</th><th>Who</th><th>Event</th>
                  <th>IP</th><th>Country</th><th>Device</th>
                </tr>
              </thead>
              <tbody>
                {page.events.map((event) => (
                  <Fragment key={event.id}>
                    <tr className={event.kind === 'login_failed' ? 'failed' : undefined}>
                      <td className="an-cell-dim">{when(event.at)}</td>
                      <td>
                        {/* The name is the way in to what this person did.
                            A failed sign-in has no account behind it, so that
                            row's name is plain text. */}
                        {event.user_id ? (
                          <button
                            type="button"
                            className="an-who"
                            aria-expanded={openRow === event.id}
                            onClick={() => openPerson(event)}
                          >
                            <span className="an-who-mark">
                              {openRow === event.id ? '▾' : '▸'}
                            </span>
                            {event.who}
                          </button>
                        ) : event.who}
                        {event.is_guest && <span className="an-tag">guest</span>}
                      </td>
                      <td>{KIND_LABEL[event.kind] || event.kind}</td>
                      <td className="an-cell-mono">{event.ip || '—'}</td>
                      {/* Stored as an ISO code, read as a country. */}
                      <td title={event.country || undefined}>
                        {event.country ? countryLabel(event.country) : '—'}
                      </td>
                      {/* "Phone" alone doesn't say what to test on, so the
                          browser and the system sit under it. The full
                          user-agent is a wall of text and stays on the hover,
                          where it doesn't push the table off screen. */}
                      <td className="an-cell-dim" title={event.user_agent || undefined}>
                        {deviceWord(event.device)}
                        {agentLine(event) && (
                          <div className="an-cell-sub">{agentLine(event)}</div>
                        )}
                      </td>
                    </tr>
                    {openRow === event.id && (
                      <tr className="an-person-row">
                        <td colSpan={6}><PersonCard detail={people[event.user_id]} /></td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
          {page.has_more && (
            <div className="an-more">
              <button onClick={loadMore} disabled={busy}>
                {busy ? 'Loading…' : 'Load older'}
              </button>
            </div>
          )}
        </>
      ))}
    </section>
  )
}

/* ---------- Page ---------- */

export default function Analytics() {
  const { me } = useOutletContext() ?? {}
  const navigate = useNavigate()
  const [tab, setTab] = useState('overview')
  const [days, setDays] = useState(30)
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const loaded = useRef(false)

  // me is null until /auth/me resolves; only bounce once we know.
  useEffect(() => {
    if (me && !me.analytics) navigate('/', { replace: true })
  }, [me, navigate])

  useEffect(() => {
    if (tab !== 'overview') return undefined
    let live = true
    api.getAnalytics(days).then(
      (result) => { if (live) { setData(result); setError(null); loaded.current = true } },
      (err) => { if (live) setError(err.message) },
    )
    return () => { live = false }
  }, [days, tab])

  const totals = data?.totals ?? {}
  const visitorSeries = useMemo(() => ([
    { key: 'returning', label: 'Returning', color: 'var(--chart-2)' },
    { key: 'new', label: 'New', color: 'var(--chart-1)' },
  ]), [])
  // The server sends new-vs-total; the chart stacks, so it wants the remainder.
  const visitorDays = useMemo(
    () => (data?.series ?? []).map((d) => ({ ...d, returning: d.visitors - d.new })),
    [data],
  )

  if (me && !me.analytics) return null

  return (
    <div className="page an-page">
      <div className="page-header">
        <h1>Visitors</h1>
        {tab === 'overview' && (
          <div className="an-ranges">
            {RANGES.map((range) => (
              <button
                key={range.days}
                className={days === range.days ? 'primary' : ''}
                onClick={() => setDays(range.days)}
              >
                {range.label}
              </button>
            ))}
          </div>
        )}
      </div>

      <div className="an-tabs">
        <button
          className={tab === 'overview' ? 'active' : ''}
          onClick={() => setTab('overview')}
        >
          Overview
        </button>
        <button
          className={tab === 'access' ? 'active' : ''}
          onClick={() => setTab('access')}
        >
          Access log
        </button>
      </div>

      {tab === 'access' && (
        <>
          <DevicesSeen />
          <AccessLog />
        </>
      )}

      {tab === 'overview' && error && (
        <div className="an-empty">Couldn’t load analytics: {error}</div>
      )}
      {tab === 'overview' && !data && !error && <div className="an-empty">Counting…</div>}

      {tab === 'overview' && data && (
        <>
          <div className="an-tiles">
            <StatTile label="Visitors" value={totals.visitors}
              hint={`${nf.format(totals.new_visitors || 0)} first-time`} />
            <StatTile label="Visits" value={totals.visits}
              hint={`${totals.pages_per_visit || 0} pages each`} />
            <StatTile label="Pageviews" value={totals.pageviews} />
            <StatTile label="Adventures started" value={totals.adventures} />
            <StatTile label="Turns played" value={totals.turns}
              hint={`${totals.turns_per_visit || 0} per visit`} />
            <StatTile label="Sign-ups" value={totals.signups} />
            <StatTile label="Demo-key turns" value={totals.demo_turns}
              hint="billed to the shared key" />
            <StatTile label="Failed turns" value={totals.turn_errors}
              hint={`${nf.format(totals.errors || 0)} API errors`} />
          </div>

          <div className="an-grid">
            <DayChart
              title="Visitors per day"
              data={visitorDays}
              series={visitorSeries}
              days={days}
            />
            <DayChart
              title="Turns played per day"
              data={data.series}
              series={[{ key: 'turns', label: 'Turns', color: 'var(--chart-3)' }]}
              days={days}
            />
          </div>

          <Funnel steps={data.funnel} />

          <div className="an-grid">
            <TopList title="Pages" rows={data.pages} />
            <TopList
              title="Where they came from"
              rows={data.referrers}
              note="per visit"
              empty="No referrals yet — every visit was typed or bookmarked."
            />
            <TopList title="Scenarios started" rows={data.scenarios}
              note="shared scenarios only" empty="No adventures started yet." />
            <TopList title="Countries" rows={data.countries} labelOf={countryLabel} />
            <TopList title="Devices" rows={data.devices} />
            <TopList title="API errors" rows={data.errors} empty="None — clean run." />
          </div>

          <p className="an-footnote">
            {fullDate(data.since)} – {fullDate(data.until)}, UTC. Your own visits aren’t
            counted here. These totals are anonymous — visitors are counted as one-way
            hashes, and nothing on this tab can be traced back to a player or their
            stories. The access log tab is the separate, identifying record.
          </p>
        </>
      )}
    </div>
  )
}
