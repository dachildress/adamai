import { useEffect, useReducer, useState, useMemo } from 'react'
import { Header } from './components/Header'
import { Sidebar } from './components/Sidebar'
import { MainPanel } from './components/MainPanel'
import { RightBar } from './components/RightBar'
import { PromptBar } from './components/PromptBar'
import { NewSessionModal } from './components/NewSessionModal'
import { ResumeModal } from './components/ResumeModal'
import { GovernanceAdminModal } from './components/GovernanceAdminModal'
import { DataSourcesModal } from './components/DataSourcesModal'
import { QueryDataSourceModal } from './components/QueryDataSourceModal'
import { LoginPage } from './components/LoginPage'
import { ChangePasswordPage } from './components/ChangePasswordPage'
import {
  fetchSessions, fetchSessionEvents, streamSessionEvents,
  whoami, logout,
} from './lib/api'
import { applyEvent, applyEvents, EMPTY_STATE } from './lib/reducer'
import { formatTimestamp, formatElapsed } from './lib/agents'


// useReducer reducer wrapper. Two action shapes:
//   { type: 'reset' }                        -> reset to EMPTY_STATE
//   { type: 'event', event: {...} }          -> applyEvent
//   { type: 'events', events: [...] }        -> applyEvents (for catch-up)
function sessionReducer(state, action) {
  switch (action.type) {
    case 'reset':   return EMPTY_STATE
    case 'event':   return applyEvent(state, action.event)
    case 'events':  return applyEvents(state, action.events)
    default: return state
  }
}


export default function App() {
  // v5 multi-user: identity comes from /api/auth/whoami, NOT .env.
  // - `user` is null while we check, and stays null if not logged in.
  // - `authChecking` is true during the initial whoami() round-trip;
  //   prevents a flash of LoginPage for already-authenticated users
  //   on page reload.
  const [user, setUser]                     = useState(null)
  const [authChecking, setAuthChecking]     = useState(true)

  const [sessions, setSessions]             = useState([])
  const [selectedId, setSelectedId]         = useState(null)
  const [connectionStatus, setConnectionStatus] = useState('idle')
  const [backendError, setBackendError]     = useState(null)
  const [tick, setTick]                     = useState(0)

  const [showNewSession, setShowNewSession] = useState(false)
  const [continuationFrom, setContinuationFrom] = useState(null)
  // Slice 4a: the session currently being reviewed/resumed (or null).
  const [reviewPaused, setReviewPaused] = useState(null)
  const [reviewMessage, setReviewMessage] = useState(null)
  const [showGovernanceAdmin, setShowGovernanceAdmin] = useState(false)
  const [showDataSources, setShowDataSources] = useState(false)
  const [showQuery, setShowQuery] = useState(false)

  const [state, dispatch] = useReducer(sessionReducer, EMPTY_STATE)

  // ---- Helpers ----

  async function refreshSessions() {
    try {
      const s = await fetchSessions()
      setSessions(s.sessions || [])
      return s.sessions || []
    } catch (e) {
      // 401 means the cookie expired; send to login. Don't surface
      // it as a "backend unreachable" error.
      if (String(e).includes('401')) {
        setUser(null)
        return []
      }
      setBackendError(String(e))
      return []
    }
  }

  async function refreshUser() {
    try {
      const u = await whoami()
      if (u) setUser(u)
    } catch (_) { /* ignore */ }
  }

  async function handleSessionCreated(result) {
    setShowNewSession(false)
    setContinuationFrom(null)
    await refreshSessions()
    await refreshUser()
    if (result?.session_id) {
      setSelectedId(result.session_id)
    }
  }

  async function handleLogout() {
    try { await logout() } catch (_) { /* ignore */ }
    setUser(null)
    setSessions([])
    setSelectedId(null)
    dispatch({ type: 'reset' })
  }

  function handleLoginSuccess(loggedInUser) {
    setUser(loggedInUser)
    setAuthChecking(false)
    setBackendError(null)
  }

  // ---- Effects ----

  // On mount: check whoami(). Sets user (or leaves it null). The
  // pre-v5 mount effect (fetchHealth + fetchSessions) was removed;
  // those calls now happen only after we know we're authenticated.
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const u = await whoami()
        if (cancelled) return
        setUser(u)
      } catch (e) {
        // Real network error (backend down). Don't show LoginPage
        // because the problem isn't auth; it's the backend.
        if (!cancelled) setBackendError(String(e))
      } finally {
        if (!cancelled) setAuthChecking(false)
      }
    })()
    return () => { cancelled = true }
  }, [])

  // Once user is set, fetch initial sessions list. Guard on user so
  // we don't get 401s flashing through before the LoginPage renders.
  useEffect(() => {
    if (!user) return
    let cancelled = false
    ;(async () => {
      try {
        const s = await fetchSessions()
        if (cancelled) return
        setSessions(s.sessions || [])
        if (!cancelled && s.sessions?.length > 0) {
          setSelectedId(prev => prev || s.sessions[0].session_id)
        }
      } catch (e) {
        if (String(e).includes('401')) {
          if (!cancelled) setUser(null)
        } else if (!cancelled) {
          setBackendError(String(e))
        }
      }
    })()
    return () => { cancelled = true }
  }, [user])

  // Periodic poll. Adaptive: 2s when any session is 'starting', else 5s.
  // Gated on user so we don't spam 401s while logged out.
  useEffect(() => {
    if (!user) return
    const hasStarting = sessions.some(s => s.status === 'starting')
    const intervalMs = hasStarting ? 2000 : 5000
    const id = setInterval(async () => {
      try {
        const s = await fetchSessions()
        setSessions(s.sessions || [])
      } catch (e) {
        if (String(e).includes('401')) setUser(null)
      }
    }, intervalMs)
    return () => clearInterval(id)
  }, [sessions, user])

  // Tick once per second so elapsed-time fields update live
  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [])

  // SSE for the selected session.
  useEffect(() => {
    if (!selectedId || !user) {
      dispatch({ type: 'reset' })
      return
    }
    dispatch({ type: 'reset' })
    setConnectionStatus('connecting')

    let stop = null
    let cancelled = false

    ;(async () => {
      try {
        const { events } = await fetchSessionEvents(selectedId)
        if (cancelled) return
        if (events && events.length > 0) {
          dispatch({ type: 'events', events })
        }
      } catch (_) { /* live stream will catch us up */ }

      if (cancelled) return

      stop = streamSessionEvents(selectedId, {
        onEvent:  (evt) => dispatch({ type: 'event', event: evt }),
        onStatus: setConnectionStatus,
        onError:  (e)   => console.error('SSE error', e),
      })
    })()

    return () => {
      cancelled = true
      if (stop) stop()
    }
  }, [selectedId, user])

  const sessionInfo = useMemo(() => {
    if (!state.session_id) return null
    const selected = sessions.find(s => s.session_id === state.session_id)
    return {
      session_id: state.session_id,
      started_short: formatTimestamp(state.session_started_at),
      elapsed: formatElapsed(state.session_started_at, state.ended ? state.session_ended_at : null),
      context_summary: state.context.files.length > 0
        ? `${state.context.files.length} docs · ${state.context.background_block_chars} chars`
        : null,
      is_active: !state.ended,
      status_label: state.ended ? 'session complete' : (
        connectionStatus === 'connected' ? 'all systems operational' :
        connectionStatus === 'connecting' ? 'stream connecting…' :
        connectionStatus === 'disconnected' ? 'stream lost — retrying' :
        'idle'
      ),
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state, sessions, connectionStatus, tick])

  useEffect(() => {
    if (!reviewMessage) return undefined
    const t = setTimeout(() => setReviewMessage(null), 6000)
    return () => clearTimeout(t)
  }, [reviewMessage])

  // ---- Render ----

  // Backend genuinely unreachable (network error, not 401):
  if (backendError) {
    return (
      <div className="app" style={{ display: 'block', padding: 24 }}>
        <h2 style={{ color: 'var(--coral)' }}>Cannot reach the ADAM GUI backend</h2>
        <p style={{ color: 'var(--text-muted)', fontSize: 13 }}>
          The backend at /api is not responding. Make sure
          {' '}<code>python adam_gui.py</code>{' '}
          (or <code>python -m backend.server</code>) is running, and reload this page.
        </p>
        <pre style={{
          background: 'var(--bg-raised)',
          padding: 12,
          marginTop: 12,
          fontSize: 12,
          color: 'var(--text-faint)',
          fontFamily: 'var(--font-mono)',
        }}>{backendError}</pre>
      </div>
    )
  }

  // Initial auth check still in flight — render nothing.
  if (authChecking) {
    return <div className="app" style={{ display: 'block' }} />
  }

  // Not logged in -> LoginPage.
  if (!user) {
    return <LoginPage onLoginSuccess={handleLoginSuccess} />
  }

  // Logged in but must change password first (new account or admin reset).
  // Block the dashboard until the change succeeds. whoami() re-fetch clears
  // the flag and falls through to the dashboard.
  if (user.must_change_password) {
    return (
      <ChangePasswordPage
        forced
        onChanged={refreshUser}
        onLogout={handleLogout}
      />
    )
  }

  // Authenticated dashboard.
  return (
    <div className="app">
      <Header
        user={user}
        state={state}
        selectedSession={selectedId}
        connectionStatus={connectionStatus}
        onLogout={handleLogout}
        onOpenGovernance={() => setShowGovernanceAdmin(true)}
        onOpenDataSources={() => setShowDataSources(true)}
        onOpenQuery={() => setShowQuery(true)}
      />
      {reviewMessage && (
        <div className="review-toast" role="status">{reviewMessage}</div>
      )}
      <Sidebar
        sessions={sessions}
        selectedSessionId={selectedId}
        onSelect={setSelectedId}
        sessionInfo={sessionInfo}
        onNewSession={() => { setContinuationFrom(null); setShowNewSession(true) }}
        onContinueSession={(session) => {
          setContinuationFrom({
            session_id: session.session_id,
            title: session.title,
          })
          setShowNewSession(true)
        }}
        user={user}
      />
      <MainPanel
        state={state}
        sessionMeta={sessions.find(s => s.session_id === selectedId)}
        selectedSessionId={selectedId}
        onReview={(meta) => setReviewPaused(meta)}
        onSelectSession={setSelectedId}
      />
      <PromptBar
        sessionId={selectedId}
        ended={!!state.ended}
        consumedMessageIds={state.consumed_message_ids}
        erroredMessageIds={state.errored_message_ids}
      />
      <RightBar state={state} sessionId={selectedId} user={user} />

      {showNewSession && (
        <NewSessionModal
          onClose={() => { setShowNewSession(false); setContinuationFrom(null) }}
          onCreated={handleSessionCreated}
          user={user}
          continuationFrom={continuationFrom}
        />
      )}

      {reviewPaused && (
        <ResumeModal
          paused={{
            session_id:          reviewPaused.session_id,
            pause_type:          reviewPaused.pause_type || reviewPaused.status,
            review_reason:       reviewPaused.review_reason,
            information_reason: reviewPaused.information_reason,
            synthesis_preview:   reviewPaused.synthesis_preview,
          }}
          onClose={() => setReviewPaused(null)}
          onResumed={async (result) => {
            setReviewPaused(null)
            const decision = result?.review_decision || 'approved'
            setReviewMessage(
              result?.declined
                ? 'Review declined — session closed.'
                : `Review ${decision} — session resumed.`,
            )
            await refreshSessions()
            await refreshUser()
            if (!result?.declined && result?.session_id) {
              setSelectedId(result.session_id)
            }
          }}
        />
      )}

      {showGovernanceAdmin && (
        <GovernanceAdminModal
          onClose={() => setShowGovernanceAdmin(false)}
          currentUsername={user.username}
        />
      )}

      {showDataSources && user.role === 'admin' && (
        <DataSourcesModal onClose={() => setShowDataSources(false)} />
      )}

      {showQuery && (
        <QueryDataSourceModal onClose={() => setShowQuery(false)} />
      )}
    </div>
  )
}
