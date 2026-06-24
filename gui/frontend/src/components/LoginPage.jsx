import { useState, useEffect, useRef } from 'react'
import { login } from '../lib/api'

/**
 * v5 multi-user: login screen.
 *
 * Shown when whoami() returns null (no valid login cookie). Submits
 * username + password to /api/auth/login; on success, calls
 * onLoginSuccess(user) with the user profile so the parent can
 * transition to the dashboard.
 *
 * Error handling:
 *   - 401: "invalid credentials" -- shown verbatim. Doesn't leak
 *     whether the username exists vs the password was wrong; the
 *     server returns the same message for both.
 *   - 403: "account is not active" -- shown verbatim. Tells the
 *     user to contact the admin.
 *   - other: shown as-is.
 *
 * The form is keyboard-friendly: Enter submits, both fields autofocus
 * appropriately, and the error message clears as soon as the user
 * starts editing.
 */
export function LoginPage({ onLoginSuccess }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)
  const usernameRef = useRef(null)

  useEffect(() => {
    if (usernameRef.current) usernameRef.current.focus()
  }, [])

  async function submit() {
    if (!username || !password) {
      setError('username and password are required')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      const user = await login(username, password)
      onLoginSuccess(user)
    } catch (e) {
      setError(e.message || 'login failed')
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
          <div className="login-card__subtitle">Governance Core</div>
        </div>

        <div className="login-card__body">
          <label className="login-field">
            <span className="login-field__label">username</span>
            <input
              ref={usernameRef}
              type="text"
              className="login-field__input"
              autoComplete="username"
              value={username}
              onChange={e => { setUsername(e.target.value); setError(null) }}
              onKeyDown={onKeyDown}
              disabled={submitting}
              spellCheck={false}
              autoCapitalize="off"
            />
          </label>

          <label className="login-field">
            <span className="login-field__label">password</span>
            <input
              type="password"
              className="login-field__input"
              autoComplete="current-password"
              value={password}
              onChange={e => { setPassword(e.target.value); setError(null) }}
              onKeyDown={onKeyDown}
              disabled={submitting}
            />
          </label>

          {error && (
            <div className="login-error">
              {error}
            </div>
          )}

          <button
            className="btn btn--primary btn--full"
            onClick={submit}
            disabled={submitting || !username || !password}
          >
            {submitting ? 'signing in…' : 'sign in'}
          </button>
        </div>

        <div className="login-card__footer">
          accounts are created by administrators · contact David for access
        </div>
      </div>
    </div>
  )
}
