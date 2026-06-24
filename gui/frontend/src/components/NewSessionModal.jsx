import { useState, useRef, useEffect } from 'react'
import { createNewSession, continueSession, fetchGovernanceProfiles } from '../lib/api'

/**
 * Part 9: New Session modal.
 *
 * A focused form for starting a new ADAM deliberation. The user enters
 * a seed prompt, optionally uploads context files, optionally sets a
 * max_turns override, and clicks Start. The backend creates the session
 * directory, spawns ADAM in the background, and returns the new
 * session_id. The caller (App.jsx) selects the new session immediately
 * so the user sees the 'starting' state in the sidebar and the
 * transcript pane opens to it.
 *
 * The modal does NOT manage process lifecycle or events itself. Once
 * createNewSession returns, all further state arrives via the regular
 * events stream tied to the new session.
 *
 * Form discipline:
 *   - Seed is required (1-50000 chars; the textarea caps at 50000).
 *   - Context files: up to 20 files, each <= 10 MB. Files exceeding
 *     these caps are filtered locally with a warning; the backend
 *     also enforces caps as a backstop.
 *   - max_turns: optional integer 1..200. Empty means "use runtime default".
 *   - no_verify: a small advanced toggle; disables Truthseeker for the
 *     run (debug use only).
 *
 * Keyboard:
 *   - Escape closes the modal (unless a submit is in flight).
 *   - Cmd/Ctrl+Enter submits.
 *   - Plain Enter in the textarea inserts a newline (multi-line seeds
 *     are common).
 */
export function NewSessionModal({ onClose, onCreated, user, continuationFrom = null }) {
  // v5 multi-user: pilot detection drives the UI.
  // - max_turns input shown disabled, pinned to the pilot's quota
  // - if sessions_remaining == 0, replace the form with a quota-
  //   exhausted message
  // - server enforces all of this regardless of what we submit
  const isPilot = user?.role === 'pilot'
  const pilotMaxTurns = user?.max_turns_per_session ?? 0
  const sessionsRemaining = user?.sessions_remaining ?? 0
  const quotaExhausted = isPilot && sessionsRemaining <= 0

  const [seed, setSeed]           = useState('')
  const [maxTurns, setMaxTurns]   = useState(isPilot ? String(pilotMaxTurns) : '')
  const [noVerify, setNoVerify]   = useState(false)
  const [advanced, setAdvanced]   = useState(false)
  const [files, setFiles]         = useState([])     // File[]
  const [filesWarning, setFilesWarning] = useState(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError]         = useState(null)
  // Slice 1/4: governance profiles. Pilots see the picker but cannot change
  // it (locked to the default, like max_turns). Admins choose per session.
  const [profiles, setProfiles]   = useState([])     // [{id,name,description}]
  const [profileId, setProfileId] = useState('')     // selected; '' until loaded
  const seedRef = useRef(null)
  const fileInputRef = useRef(null)

  // Constants matching server-side caps. Duplicated here so we can
  // give immediate feedback before hitting the backend.
  const SEED_MAX_CHARS         = 50000
  const MAX_CONTEXT_FILES      = 20
  const MAX_CONTEXT_FILE_BYTES = 10 * 1024 * 1024

  // "Dirty" = the user has entered something they'd lose on close.
  // A typed seed or any attached file counts. (maxTurns is pre-filled
  // for pilots and otherwise optional, so it doesn't count as work.)
  function isDirty() {
    return seed.trim().length > 0 || files.length > 0
  }

  // Close that protects unsaved work. Explicit close actions (the ✕
  // button, Cancel) pass force=true and always close. Accidental
  // dismissals (backdrop click, Escape) pass force=false and only
  // close when nothing would be lost, or after the user confirms.
  function requestClose({ force } = { force: false }) {
    if (submitting) return
    if (!force && isDirty()) {
      const ok = window.confirm(
        'Discard this session? Your prompt and any attached files will be lost.'
      )
      if (!ok) return
    }
    onClose()
  }

  // Focus the seed textarea on mount
  useEffect(() => {
    if (seedRef.current) seedRef.current.focus()
  }, [])

  // Slice 1/4: load governance profiles and set the initial selection to
  // the default. Pilots can't change it, but they still see which profile
  // governs their session. A continuation inherits its parent's profile,
  // so the picker is informational there; for a fresh session it's the
  // active choice. If the fetch fails, the picker is simply hidden and the
  // backend's default profile applies.
  useEffect(() => {
    let cancelled = false
    fetchGovernanceProfiles()
      .then((data) => {
        if (cancelled) return
        const list = Array.isArray(data?.profiles) ? data.profiles : []
        setProfiles(list)
        setProfileId(data?.default_profile_id || (list[0]?.id ?? ''))
      })
      .catch(() => { /* picker hidden on failure; default applies */ })
    return () => { cancelled = true }
  }, [])

  // Keyboard shortcuts
  useEffect(() => {
    function onKeyDown(e) {
      if (submitting) return
      if (e.key === 'Escape') {
        e.preventDefault()
        requestClose({ force: false })
      } else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') {
        e.preventDefault()
        submit()
      }
    }
    window.addEventListener('keydown', onKeyDown)
    return () => window.removeEventListener('keydown', onKeyDown)
  }, [seed, maxTurns, noVerify, files, submitting])     // eslint-disable-line

  function handleFileSelect(ev) {
    const incoming = Array.from(ev.target.files || [])
    const warnings = []
    let merged = [...files, ...incoming]

    // Dedupe by name + size (cheap heuristic; same content from one
    // user-add cycle is exact, content from two adds may differ but we
    // accept the false negative)
    const seen = new Set()
    merged = merged.filter(f => {
      const key = `${f.name}::${f.size}`
      if (seen.has(key)) return false
      seen.add(key)
      return true
    })

    // Per-file size cap
    const oversized = merged.filter(f => f.size > MAX_CONTEXT_FILE_BYTES)
    if (oversized.length > 0) {
      warnings.push(
        `${oversized.length} file(s) exceed the 10 MB limit and were skipped: ` +
        oversized.map(f => f.name).join(', ')
      )
      merged = merged.filter(f => f.size <= MAX_CONTEXT_FILE_BYTES)
    }

    // File count cap
    if (merged.length > MAX_CONTEXT_FILES) {
      warnings.push(
        `Only the first ${MAX_CONTEXT_FILES} files are kept (you added ${merged.length}).`
      )
      merged = merged.slice(0, MAX_CONTEXT_FILES)
    }

    setFiles(merged)
    setFilesWarning(warnings.length > 0 ? warnings.join(' ') : null)
    // Allow re-selecting the same file later
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  function removeFile(idx) {
    setFiles(fs => fs.filter((_, i) => i !== idx))
    setFilesWarning(null)
  }

  async function submit() {
    setError(null)
    const trimmed = seed.trim()
    if (!trimmed) {
      setError('Seed text is required.')
      if (seedRef.current) seedRef.current.focus()
      return
    }
    if (trimmed.length > SEED_MAX_CHARS) {
      setError(`Seed exceeds ${SEED_MAX_CHARS} chars.`)
      return
    }
    let maxTurnsNum = null
    if (maxTurns.trim()) {
      const n = parseInt(maxTurns, 10)
      if (isNaN(n) || n < 1 || n > 200) {
        setError('max_turns must be an integer between 1 and 200.')
        return
      }
      maxTurnsNum = n
    }

    setSubmitting(true)
    try {
      const result = continuationFrom
        ? await continueSession(continuationFrom.session_id, {
            seed:         trimmed,
            maxTurns:     maxTurnsNum,
            noVerify:     noVerify,
            contextFiles: files,
          })
        : await createNewSession({
            seed:         trimmed,
            maxTurns:     maxTurnsNum,
            noVerify:     noVerify,
            contextFiles: files,
            // Only admins can change this; for pilots the select is
            // disabled and carries the default, and the backend enforces
            // the lock regardless. A continuation inherits its parent's
            // profile, so we only send it on the fresh path.
            governanceProfileId: profileId || null,
          })
      // Hand off to the caller; App.jsx will refresh the sidebar
      // and select the new session.
      onCreated(result)
    } catch (e) {
      setError(e.message || 'failed to create session')
      setSubmitting(false)
    }
  }

  function fmtBytes(n) {
    if (n < 1024) return `${n} B`
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
    return `${(n / 1024 / 1024).toFixed(2)} MB`
  }

  return (
    <div
      className="modal-backdrop"
      onClick={() => requestClose({ force: false })}
    >
      <div
        className="modal modal--new-session"
        role="dialog"
        aria-label="New Session"
        onClick={e => e.stopPropagation()}
      >
        <div className="modal__header">
          <div className="modal__title">
            {continuationFrom ? 'Continue Session' : 'New Session'}
          </div>
          <button
            className="modal__close"
            onClick={() => requestClose({ force: true })}
            disabled={submitting}
            aria-label="close"
          >
            ✕
          </button>
        </div>

        {continuationFrom && !quotaExhausted && (
          <div className="modal__continuation-banner" style={{
            margin: '0 0 4px',
            padding: '10px 12px',
            borderRadius: 8,
            background: 'var(--surface-2, #1a212b)',
            border: '1px solid var(--border, #2a3340)',
            fontSize: '.85rem',
            color: 'var(--text-muted, #9aa7b4)',
          }}>
            Continuing from <strong>{continuationFrom.title || continuationFrom.session_id}</strong>.
            The prior session's result is carried forward automatically — just
            describe what to do next below.
            {isPilot && ' This uses one of your remaining sessions.'}
          </div>
        )}

        {quotaExhausted ? (
          <div className="modal__body">
            <div className="modal__quota-exhausted">
              <div className="modal__quota-exhausted-title">
                Pilot allocation fully used
              </div>
              <div className="modal__quota-exhausted-body">
                You've used all of your allocated sessions for this pilot.
                To request additional sessions, please email David.
              </div>
              <div className="modal__actions" style={{ marginTop: 16 }}>
                <button className="btn btn--primary" onClick={() => requestClose({ force: true })}>Close</button>
              </div>
            </div>
          </div>
        ) : (
        <div className="modal__body">
          {/* Seed */}
          <label className="form-field">
            <span className="form-field__label">
              {continuationFrom ? 'Follow-up prompt' : 'Seed prompt'}
              <span className="form-field__required" aria-hidden="true"> *</span>
            </span>
            <textarea
              ref={seedRef}
              className="form-field__textarea"
              placeholder={continuationFrom
                ? "Describe what to do next, building on the prior session's result (e.g. \"redo the plan using Citizenship as the 5th C\" or \"add a board presentation version\")."
                : "State the question or task for the deliberation. ADAM will treat this as the starting prompt for all agents."}
              value={seed}
              onChange={e => setSeed(e.target.value)}
              disabled={submitting}
              rows={6}
              maxLength={SEED_MAX_CHARS}
            />
            <div className="form-field__hint">
              {seed.length}/{SEED_MAX_CHARS}
            </div>
          </label>

          {/* Context files */}
          <div className="form-field">
            <span className="form-field__label">
              Context files
              <span className="form-field__optional">(optional)</span>
            </span>
            <div className="form-field__upload">
              <input
                ref={fileInputRef}
                type="file"
                multiple
                onChange={handleFileSelect}
                disabled={submitting}
                style={{ display: 'none' }}
                id="new-session-files"
              />
              <label
                htmlFor="new-session-files"
                className={`upload-btn ${submitting ? 'upload-btn--disabled' : ''}`}
              >
                + Add files
              </label>
              <span className="form-field__hint">
                {files.length === 0
                  ? `No files (up to ${MAX_CONTEXT_FILES}, max 10 MB each)`
                  : `${files.length} file${files.length === 1 ? '' : 's'} attached`}
              </span>
            </div>
            {filesWarning && (
              <div className="form-field__warning">{filesWarning}</div>
            )}
            {files.length > 0 && (
              <ul className="upload-list">
                {files.map((f, i) => (
                  <li key={`${f.name}::${f.size}::${i}`} className="upload-list__item">
                    <span className="upload-list__name">{f.name}</span>
                    <span className="upload-list__size">{fmtBytes(f.size)}</span>
                    <button
                      className="upload-list__remove"
                      onClick={() => removeFile(i)}
                      disabled={submitting}
                      title="remove"
                    >
                      ✕
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* Advanced */}
          <div className="form-field">
            <button
              className="advanced-toggle"
              onClick={() => setAdvanced(a => !a)}
              disabled={submitting}
            >
              {advanced ? '− Advanced' : '+ Advanced'}
            </button>
            {advanced && (
              <div className="form-field__advanced">
                {profiles.length > 0 && (
                  <label className="form-field form-field--inline">
                    <span className="form-field__label">Governance profile</span>
                    <select
                      className="form-field__input form-field__input--small"
                      value={profileId}
                      onChange={e => setProfileId(e.target.value)}
                      disabled={submitting || isPilot || !!continuationFrom}
                      title={
                        isPilot
                          ? 'Set by your pilot allocation'
                          : continuationFrom
                            ? 'A continuation keeps the original session\u2019s profile'
                            : undefined
                      }
                    >
                      {profiles.map(p => (
                        <option key={p.id} value={p.id}>
                          {p.name || p.id}
                        </option>
                      ))}
                    </select>
                    <span className="form-field__hint form-field__hint--inline">
                      {isPilot
                        ? 'The governance rules applied to your session.'
                        : continuationFrom
                          ? 'Inherited from the session you\u2019re continuing.'
                          : 'Rules that bound what this session may do.'}
                    </span>
                  </label>
                )}
                <label className="form-field form-field--inline">
                  <span className="form-field__label">Max turns</span>
                  <input
                    type="number"
                    className="form-field__input form-field__input--small"
                    placeholder="(default)"
                    value={maxTurns}
                    onChange={e => setMaxTurns(e.target.value)}
                    disabled={submitting || isPilot}
                    min={1}
                    max={200}
                    title={isPilot ? `Set by your pilot allocation (${pilotMaxTurns})` : undefined}
                  />
                  <span className="form-field__hint form-field__hint--inline">
                    {isPilot
                      ? `Set by your pilot allocation (${pilotMaxTurns} turns).`
                      : '1–200; leave empty to use runtime.json default'}
                  </span>
                </label>
                <label className="form-field form-field--checkbox">
                  <input
                    type="checkbox"
                    checked={noVerify}
                    onChange={e => setNoVerify(e.target.checked)}
                    disabled={submitting || isPilot}
                  />
                  <span className="form-field__label form-field__label--inline">
                    Disable Truthseeker (debug only){isPilot && ' — admin only'}
                  </span>
                </label>
              </div>
            )}
          </div>

          {error && (
            <div className="modal__error">
              ✕ {error}
            </div>
          )}
        </div>
        )}

        {!quotaExhausted && (
        <div className="modal__footer">
          <div className="modal__footer-hint">
            ⌘+Enter to start · Esc to cancel
          </div>
          <div className="modal__footer-actions">
            <button
              className="btn btn--ghost"
              onClick={() => requestClose({ force: true })}
              disabled={submitting}
            >
              Cancel
            </button>
            <button
              className="btn btn--primary"
              onClick={submit}
              disabled={submitting || !seed.trim()}
            >
              {submitting ? 'Starting…' : '→ Start session'}
            </button>
          </div>
        </div>
        )}
      </div>
    </div>
  )
}
