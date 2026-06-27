import { useState, useEffect, useRef } from 'react'
import { changePassword } from '../lib/api'

/**
 * Forced / voluntary password-change screen.
 *
 * Rendered by App when the logged-in user has must_change_password=true
 * (new account or admin-reset). The user cannot reach the main app until
 * the change succeeds. Submits to POST /api/auth/change-password; on
 * success calls onChanged() so the parent re-fetches whoami() and the
 * flag clears.
 *
 * Errors render via the readable message thrown by api.changePassword
 * (formatApiDetail), never "[object Object]".
 */
export function ChangePasswordPage({ onChanged, onLogout, forced = true }) {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const currentRef = useRef(null)

  useEffect(() => {
    if (currentRef.current) currentRef.current.focus()
  }, [])

  async function submit() {
    if (!current || !next) {
      setError('current and new password are required')
      return
    }
    if (next.length < 8) {
      setError('new password must be at least 8 characters')
      return
    }
    if (next !== confirm) {
      setError('new password and confirmation do not match')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await changePassword(current, next)
      onChanged()
    } catch (e) {
      setError(e.message || 'password change failed')
      setSubmitting(false)
    }
  }

  function onKeyDown(e) {
    if (e.key === 'Enter' && !submitting) {
      e.preventDefault()
      submit()
    }
  }

  return (
    <div className="login-page">
      <div className="login-card">
        <div className="login-card__header">
          <div className="login-card__brand">ADAM</div>
          <div className="login-card__subtitle">
            {forced ? 'Set a new password' : 'Change password'}
          </div>
        </div>

        <div className="login-card__body">
          {forced && (
            <div className="login-note">
              Your password was issued by an administrator and must be changed
              before you can continue.
            </div>
          )}

          <label className="login-field">
            <span className="login-field__label">current password</span>
            <input
              ref={currentRef}
              type="password"
              className="login-field__input"
              autoComplete="current-password"
              value={current}
              onChange={e => { setCurrent(e.target.value); setError(null) }}
              onKeyDown={onKeyDown}
              disabled={submitting}
            />
          </label>

          <label className="login-field">
            <span className="login-field__label">new password</span>
            <input
              type="password"
              className="login-field__input"
              autoComplete="new-password"
              value={next}
              onChange={e => { setNext(e.target.value); setError(null) }}
              onKeyDown={onKeyDown}
              disabled={submitting}
            />
          </label>

          <label className="login-field">
            <span className="login-field__label">confirm new password</span>
            <input
              type="password"
              className="login-field__input"
              autoComplete="new-password"
              value={confirm}
              onChange={e => { setConfirm(e.target.value); setError(null) }}
              onKeyDown={onKeyDown}
              disabled={submitting}
            />
          </label>

          {error && <div className="login-error">{error}</div>}

          <button
            className="btn btn--primary btn--full"
            onClick={submit}
            disabled={submitting || !current || !next || !confirm}
          >
            {submitting ? 'updating…' : 'update password'}
          </button>

          {onLogout && (
            <button
              className="btn btn--ghost btn--full"
              onClick={onLogout}
              disabled={submitting}
              style={{ marginTop: 8 }}
            >
              sign out
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
