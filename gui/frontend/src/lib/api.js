/**
 * Backend client.
 *
 * Two responsibilities:
 *   1. Plain REST calls to /api/sessions, /api/sessions/<id>/state, etc.
 *   2. SSE connection management for /api/sessions/<id>/stream, with
 *      automatic reconnect.
 *
 * v5 multi-user: every fetch uses credentials: 'include' so the
 * browser sends the login cookie. The EventSource uses
 * { withCredentials: true } for the same reason. Without these, the
 * server treats every request as unauthenticated and returns 401.
 *
 * The SSE stream is the primary live-data path. When it disconnects
 * (idle timeout, network blip), we reconnect after a backoff. The
 * server replays events from the start on each connection, so the
 * reducer's idempotence-on-seq is what keeps reconnects safe.
 */

const API_BASE = '/api'

// ============================================================
// v5 multi-user auth helpers
// ============================================================

export async function login(username, password) {
  const r = await fetch(`${API_BASE}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ username, password }),
  })
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      if (body?.detail) detail = String(body.detail)
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}

export async function logout() {
  await fetch(`${API_BASE}/auth/logout`, {
    method: 'POST',
    credentials: 'include',
  })
}

export async function whoami() {
  const r = await fetch(`${API_BASE}/auth/whoami`, { credentials: 'include' })
  if (r.status === 401) return null
  if (!r.ok) throw new Error(`whoami: ${r.status}`)
  return r.json()
}

// ============================================================
// Existing endpoints (v4) -- every fetch now carries credentials
// ============================================================

export async function fetchHealth() {
  const r = await fetch(`${API_BASE}/health`, { credentials: 'include' })
  if (!r.ok) throw new Error(`health: ${r.status}`)
  return r.json()
}

export async function fetchSessions() {
  const r = await fetch(`${API_BASE}/sessions`, { credentials: 'include' })
  if (!r.ok) throw new Error(`sessions: ${r.status}`)
  return r.json()
}

export async function fetchSessionState(sessionId) {
  const r = await fetch(`${API_BASE}/sessions/${sessionId}/state`, { credentials: 'include' })
  if (!r.ok) return null
  return r.json()
}

export async function fetchSessionEvents(sessionId) {
  const r = await fetch(`${API_BASE}/sessions/${sessionId}/events`, { credentials: 'include' })
  if (!r.ok) return { events: [] }
  return r.json()
}

export async function fetchSessionVerifications(sessionId) {
  const r = await fetch(`${API_BASE}/sessions/${sessionId}/verifications`, { credentials: 'include' })
  if (!r.ok) return { verifications: [] }
  return r.json()
}

export async function fetchSessionSkills(sessionId) {
  const r = await fetch(`${API_BASE}/sessions/${sessionId}/skills`, { credentials: 'include' })
  if (!r.ok) return { invocations: [] }
  return r.json()
}

export function artifactUrl(sessionId, pathOrFilename) {
  if (!pathOrFilename) return null
  const normalized = String(pathOrFilename).replace(/\\/g, '/')
  const encoded = normalized
    .split('/')
    .filter(seg => seg.length > 0)
    .map(seg => encodeURIComponent(seg))
    .join('/')
  return `${API_BASE}/sessions/${sessionId}/artifacts/${encoded}`
}


export async function submitDirectorMessage(sessionId, content) {
  const r = await fetch(
    `${API_BASE}/sessions/${sessionId}/director_message`,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ content }),
    },
  )
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      if (body?.detail) {
        detail = typeof body.detail === 'string'
          ? body.detail
          : JSON.stringify(body.detail)
      }
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}


export async function createNewSession({
  seed,
  maxTurns,
  noVerify,
  contextFiles,
  governanceProfileId,
}) {
  const fd = new FormData()
  fd.append('seed', seed)
  if (maxTurns != null && maxTurns !== '') {
    fd.append('max_turns', String(maxTurns))
  }
  if (noVerify) {
    fd.append('no_verify', 'true')
  }
  if (governanceProfileId) {
    fd.append('governance_profile_id', governanceProfileId)
  }
  if (Array.isArray(contextFiles)) {
    for (const file of contextFiles) {
      fd.append('context_files', file, file.name)
    }
  }

  const r = await fetch(`${API_BASE}/sessions`, {
    method: 'POST',
    credentials: 'include',
    body: fd,
  })
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      if (body?.detail) {
        detail = typeof body.detail === 'string'
          ? body.detail
          : JSON.stringify(body.detail)
      }
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}


// Continue from a completed session. Mirrors createNewSession's contract
// exactly (same multipart fields, same error handling); the only
// differences are the URL (carries the parent id) and that `seed` here is
// the FOLLOW-UP prompt -- the backend composes the full child seed from
// the parent's result. A continuation is a full session and obeys the
// same per-role quota and turn caps as a fresh one.
export async function continueSession(parentId, {
  seed,
  maxTurns,
  noVerify,
  contextFiles,
}) {
  const fd = new FormData()
  fd.append('seed', seed)
  if (maxTurns != null && maxTurns !== '') {
    fd.append('max_turns', String(maxTurns))
  }
  if (noVerify) {
    fd.append('no_verify', 'true')
  }
  if (Array.isArray(contextFiles)) {
    for (const file of contextFiles) {
      fd.append('context_files', file, file.name)
    }
  }

  const r = await fetch(`${API_BASE}/sessions/${parentId}/continue`, {
    method: 'POST',
    credentials: 'include',
    body: fd,
  })
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      if (body?.detail) {
        detail = typeof body.detail === 'string'
          ? body.detail
          : JSON.stringify(body.detail)
      }
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}


// Slice 1/4: list the governance profiles available for a new session.
// Returns { default_profile_id, profiles: [{id, name, description, ...}] }.
// Used to populate the profile picker in the New Session modal.
export async function fetchGovernanceProfiles() {
  const r = await fetch(`${API_BASE}/governance/profiles`, { credentials: 'include' })
  if (!r.ok) {
    throw new Error(`HTTP ${r.status}`)
  }
  return r.json()
}


// Slice 4a: resume a session paused at the human-review gate. decision is
// 'approve' | 'redirect' | 'reject'; guidance is free-text direction; files
// are optional documents to inject (e.g. a privacy policy the agent asked
// for). Returns the new (resumed) session descriptor.
export async function resumeSession(pausedId, {
  decision,
  guidance,
  contextFiles,
}) {
  const fd = new FormData()
  fd.append('decision', decision || 'approve')
  if (guidance) {
    fd.append('guidance', guidance)
  }
  if (Array.isArray(contextFiles)) {
    for (const file of contextFiles) {
      fd.append('context_files', file, file.name)
    }
  }

  const r = await fetch(`${API_BASE}/sessions/${pausedId}/resume`, {
    method: 'POST',
    credentials: 'include',
    body: fd,
  })
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      if (body?.detail) {
        detail = typeof body.detail === 'string'
          ? body.detail
          : JSON.stringify(body.detail)
      }
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}


export async function fetchProcessLogs(sessionId) {
  const r = await fetch(`${API_BASE}/sessions/${sessionId}/process_logs`, { credentials: 'include' })
  if (!r.ok) {
    throw new Error(`HTTP ${r.status}`)
  }
  return r.json()
}


export function streamSessionEvents(sessionId, { onEvent, onStatus, onError } = {}) {
  let es = null
  let closed = false
  let reconnectTimer = null
  let backoffMs = 500

  const connect = () => {
    if (closed) return
    onStatus && onStatus('connecting')
    // v5: withCredentials so the login cookie travels on the SSE.
    es = new EventSource(`${API_BASE}/sessions/${sessionId}/stream`, { withCredentials: true })

    es.addEventListener('open', () => {
      backoffMs = 500
      onStatus && onStatus('connected')
    })

    es.addEventListener('adam_event', (evt) => {
      try {
        const obj = JSON.parse(evt.data)
        onEvent && onEvent(obj)
      } catch (e) {
        onError && onError(e)
      }
    })

    es.addEventListener('stream_closed', () => {
      closed = true
      es.close()
      onStatus && onStatus('closed')
    })

    es.addEventListener('no_events_file', () => {
      closed = true
      es.close()
      onStatus && onStatus('closed')
    })

    es.addEventListener('error', () => {
      if (closed) return
      try { es.close() } catch (_) {}
      onStatus && onStatus('disconnected')
      reconnectTimer = setTimeout(() => {
        backoffMs = Math.min(backoffMs * 2, 5000)
        connect()
      }, backoffMs)
    })
  }

  connect()

  return () => {
    closed = true
    if (reconnectTimer) clearTimeout(reconnectTimer)
    if (es) {
      try { es.close() } catch (_) {}
    }
  }
}
