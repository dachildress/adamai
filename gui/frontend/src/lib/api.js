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
// Pass 1 hardening: CSRF (signed double-submit cookie)
// ============================================================
//
// The backend sets a JS-readable `adam_csrf` cookie on login (and
// re-issues it on /auth/whoami). For every MUTATING request (POST, PUT,
// PATCH, DELETE) we read that cookie and echo its value back in the
// X-CSRF-Token header -- that round-trip is the "double submit" the
// server validates. GET requests and the login call are exempt.
//
// This applies to multipart/FormData calls too (session create /
// continue / resume): we must NOT set Content-Type ourselves on those
// (the browser sets the multipart boundary), so csrfHeaders() returns
// only the CSRF header for them.

export function readCsrfToken() {
  if (typeof document === 'undefined' || !document.cookie) return ''
  const m = document.cookie.match(/(?:^|;\s*)adam_csrf=([^;]+)/)
  return m ? decodeURIComponent(m[1]) : ''
}

// Merge the X-CSRF-Token header into any caller-provided headers. Pass
// the JSON content-type in `extra` for JSON bodies; pass nothing for
// FormData bodies so the browser can set the multipart boundary.
function csrfHeaders(extra = {}) {
  const token = readCsrfToken()
  return token ? { ...extra, 'X-CSRF-Token': token } : { ...extra }
}


// FastAPI error bodies put the message in `detail`, which can be:
//   - a string  -> use as-is
//   - an array of validation objects (422) -> join their msg fields
//   - an object (e.g. {errors:[...], message:"..."}) -> use message/errors
// Anything else is stringified safely. This NEVER returns "[object Object]"
// (the bug that masked real errors: String([{...}]) === "[object Object]").
export function formatApiDetail(detail, status) {
  const prefix = status ? `HTTP ${status}: ` : ''
  if (detail == null) return `HTTP ${status || ''}`.trim()
  if (typeof detail === 'string') return prefix + detail
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d === 'object' ? (d.msg || JSON.stringify(d)) : String(d)))
      .filter(Boolean)
    return prefix + (msgs.join('; ') || 'request validation failed')
  }
  if (typeof detail === 'object') {
    if (detail.message) return prefix + detail.message
    if (Array.isArray(detail.errors)) return prefix + detail.errors.join('; ')
    return prefix + JSON.stringify(detail)
  }
  return prefix + String(detail)
}

// Parse a fetch Response as JSON, or throw an Error whose message is the
// readable FastAPI detail (never "[object Object]"). The thrown error
// carries .status so callers can branch on it. Used by the newer
// user-management calls; older calls inline the same pattern.
async function asJsonOrThrow(r) {
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      detail = formatApiDetail(body?.detail, r.status)
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}

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
      detail = formatApiDetail(body?.detail, r.status)
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
    headers: csrfHeaders(),
    credentials: 'include',
  })
}

export async function whoami() {
  const r = await fetch(`${API_BASE}/auth/whoami`, { credentials: 'include' })
  if (r.status === 401) return null
  if (!r.ok) throw new Error(`whoami: ${r.status}`)
  return r.json()
}

// Authenticated user-driven password change (also used for the forced
// first-login change). Mutating -> carries the CSRF header.
export async function changePassword(currentPassword, newPassword) {
  const r = await fetch(`${API_BASE}/auth/change-password`, {
    method: 'POST',
    headers: csrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'include',
    body: JSON.stringify({
      current_password: currentPassword,
      new_password: newPassword,
    }),
  })
  return asJsonOrThrow(r)
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
  if (!r.ok) return { claims: [], verifications: [], summary: { total: 0, status_counts: {} } }
  return r.json()
}

export async function overrideVerificationClaim(sessionId, { claimId, status, reason, feedback }) {
  const r = await fetch(`${API_BASE}/sessions/${sessionId}/verifications/override`, {
    method: 'POST',
    headers: csrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'include',
    body: JSON.stringify({
      claim_id: claimId,
      status,
      reason,
      feedback: feedback || null,
    }),
  })
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      detail = formatApiDetail(body?.detail, r.status)
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
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
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
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
    headers: csrfHeaders(),   // CSRF on multipart too; no Content-Type (browser sets boundary)
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
    headers: csrfHeaders(),   // CSRF on multipart too; no Content-Type (browser sets boundary)
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


// Slice 4.2 Phase 1: full governance config for the admin view (admin only).
export async function fetchGovernanceAdmin() {
  const r = await fetch(`${API_BASE}/admin/governance`, { credentials: 'include' })
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      detail = formatApiDetail(body?.detail, r.status)
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}


export async function validateGovernanceConfig(config) {
  const r = await fetch(`${API_BASE}/admin/governance/validate`, {
    method: 'POST',
    headers: csrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'include',
    body: JSON.stringify(config),
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


export async function saveGovernanceConfig(config) {
  const r = await fetch(`${API_BASE}/admin/governance`, {
    method: 'PUT',
    headers: csrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'include',
    body: JSON.stringify(config),
  })
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    let errors = null
    try {
      const body = await r.json()
      if (body?.detail?.errors) errors = body.detail.errors
      if (body?.detail?.message) detail = body.detail.message
      else if (body?.detail) {
        detail = typeof body.detail === 'string'
          ? body.detail
          : JSON.stringify(body.detail)
      }
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    err.errors = errors
    throw err
  }
  return r.json()
}


export async function fetchAdminUsers() {
  const r = await fetch(`${API_BASE}/admin/users`, { credentials: 'include' })
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      detail = formatApiDetail(body?.detail, r.status)
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}


export async function patchUserGovernanceProfile(username, governanceProfile) {
  const r = await fetch(
    `${API_BASE}/admin/users/${encodeURIComponent(username)}/governance-profile`,
    {
      method: 'PATCH',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      credentials: 'include',
      body: JSON.stringify({
        governance_profile: governanceProfile || null,
      }),
    },
  )
  if (!r.ok) {
    let detail = `HTTP ${r.status}`
    try {
      const body = await r.json()
      detail = formatApiDetail(body?.detail, r.status)
    } catch {}
    const err = new Error(detail)
    err.status = r.status
    throw err
  }
  return r.json()
}


// ============================================================
// Admin user CRUD (usercrud pass) — all mutating, all CSRF-protected.
// Errors surface readable text via asJsonOrThrow/formatApiDetail.
// ============================================================

// Create a user. Returns { user, temporary_password } — the temp
// password is shown to the admin once.
export async function createUser(payload) {
  const r = await fetch(`${API_BASE}/admin/users`, {
    method: 'POST',
    headers: csrfHeaders({ 'Content-Type': 'application/json' }),
    credentials: 'include',
    body: JSON.stringify(payload),
  })
  return asJsonOrThrow(r)
}

// Edit a user's profile fields (display_name, email, role, quotas).
export async function editUser(username, payload) {
  const r = await fetch(
    `${API_BASE}/admin/users/${encodeURIComponent(username)}`,
    {
      method: 'PATCH',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      credentials: 'include',
      body: JSON.stringify(payload),
    },
  )
  return asJsonOrThrow(r)
}

// Suspend a user (the UI "delete" action — no hard delete).
export async function suspendUser(username) {
  const r = await fetch(
    `${API_BASE}/admin/users/${encodeURIComponent(username)}/suspend`,
    { method: 'POST', headers: csrfHeaders(), credentials: 'include' },
  )
  return asJsonOrThrow(r)
}

// Reactivate a suspended user.
export async function reactivateUser(username) {
  const r = await fetch(
    `${API_BASE}/admin/users/${encodeURIComponent(username)}/reactivate`,
    { method: 'POST', headers: csrfHeaders(), credentials: 'include' },
  )
  return asJsonOrThrow(r)
}

// Reset a user's password. Returns { user, temporary_password } — shown once.
export async function resetUserPassword(username) {
  const r = await fetch(
    `${API_BASE}/admin/users/${encodeURIComponent(username)}/reset-password`,
    { method: 'POST', headers: csrfHeaders(), credentials: 'include' },
  )
  return asJsonOrThrow(r)
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
    headers: csrfHeaders(),   // CSRF on multipart too; no Content-Type (browser sets boundary)
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
