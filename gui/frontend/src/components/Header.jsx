/**
 * v5 multi-user: header uses authenticated `user` (from whoami), not
 * the .env director. Adds:
 *   - role label under display name
 *   - sessions_remaining indicator (pilots only)
 *   - logout button next to the avatar
 */
export function Header({ user, state, selectedSession, connectionStatus, onLogout, onOpenGovernance, onOpenDataSources, onOpenQuery }) {
  const isActive = state && !state.ended && state.session_id != null
  const sessionStatus = state?.ended ? 'complete' : (isActive ? 'active' : 'idle')

  const currentTurn = state?.turns?.length || 0
  const maxTurns    = state?.max_turns || '—'

  const trustSize    = state?.trust_registry?.size
  const truthseeker  = state?.truthseeker_enabled

  const displayName = user?.display_name || user?.username || '—'
  const initials = displayName
    .split(/\s+/)
    .map(s => s[0])
    .slice(0, 2)
    .join('')
    .toUpperCase()

  const sessionsRemaining = user?.sessions_remaining
  const sessionsRemainingLabel = sessionsRemaining === -1
    ? '∞ sessions'
    : `${sessionsRemaining} session${sessionsRemaining === 1 ? '' : 's'} left`

  const roleLabel = user?.role
    ? user.role.charAt(0).toUpperCase() + user.role.slice(1)
    : 'User'

  return (
    <header className="header">
      <div className="header__brand">
        ADAM
        <span className="header__brand--version">v0.9.5</span>
        <span className="header__brand--dot" />
        <span className="header__subtitle">Governance Core</span>
      </div>

      <div className="header__stats">
        <div className="header__stat">
          <span className={`header__pulse ${isActive ? '' : 'header__pulse--dim'}`} />
          <span className="header__stat-label">Session</span>
          <span className={`header__stat-value ${isActive ? 'header__stat-value--active' : 'header__stat-value--mute'}`}>
            {sessionStatus}
          </span>
          {isActive && (
            <span className="header__stat-value header__stat-value--mute">
              · t{currentTurn} of {maxTurns}
            </span>
          )}
        </div>

        <div className="header__stat">
          <span className="header__stat-label">Truthseeker</span>
          <span className={`header__stat-value ${truthseeker ? 'header__stat-value--active' : 'header__stat-value--mute'}`}>
            {truthseeker == null ? '—' : truthseeker ? 'enabled' : 'disabled'}
          </span>
        </div>

        <div className="header__stat">
          <span className="header__stat-label">Trust Registry</span>
          <span className="header__stat-value">
            {trustSize != null ? `${trustSize} entries` : '—'}
          </span>
        </div>

        {user?.role === 'pilot' && (
          <div className="header__stat">
            <span className="header__stat-label">Quota</span>
            <span className={`header__stat-value ${sessionsRemaining === 0 ? 'header__stat-value--mute' : 'header__stat-value--active'}`}>
              {sessionsRemainingLabel}
            </span>
          </div>
        )}

        {connectionStatus && connectionStatus !== 'connected' && connectionStatus !== 'closed' && (
          <div className="header__stat">
            <span className="header__stat-label">Stream</span>
            <span className="header__stat-value header__stat-value--mute">{connectionStatus}</span>
          </div>
        )}
      </div>

      <div className="header__director">
        {onOpenQuery && (
          <button
            type="button"
            className="header__gov-btn"
            onClick={onOpenQuery}
            title="Query an approved data source"
          >
            Query Data
          </button>
        )}
        {user?.role === 'admin' && onOpenDataSources && (
          <button
            type="button"
            className="header__gov-btn"
            onClick={onOpenDataSources}
            title="Configure and approve data sources"
          >
            Data Sources
          </button>
        )}
        {user?.role === 'admin' && onOpenGovernance && (
          <button
            type="button"
            className="header__gov-btn"
            onClick={onOpenGovernance}
            title="View and manage governance profiles"
          >
            Governance
          </button>
        )}
        <div className="header__director-info">
          <div className="header__director-name">{displayName}</div>
          <div className="header__director-role">{roleLabel}</div>
        </div>
        <div className="header__avatar" title={user?.email}>{initials}</div>
        <button
          className="header__logout"
          onClick={onLogout}
          title="Sign out"
          aria-label="Sign out"
        >
          ⏻
        </button>
      </div>
    </header>
  )
}
