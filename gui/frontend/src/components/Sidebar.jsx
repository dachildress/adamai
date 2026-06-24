import { useState, useEffect } from 'react'
import { formatAge } from '../lib/agents'

export function Sidebar({ sessions, selectedSessionId, onSelect, sessionInfo, onNewSession, onContinueSession }) {
  // Right-click context menu state. `menu` is null when closed, else
  // { x, y, session }. A session can be continued once it has completed.
  const [menu, setMenu] = useState(null)

  useEffect(() => {
    if (!menu) return
    const close = () => setMenu(null)
    // Any click, scroll, or Escape dismisses the menu.
    window.addEventListener('click', close)
    window.addEventListener('scroll', close, true)
    const onKey = (e) => { if (e.key === 'Escape') close() }
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('click', close)
      window.removeEventListener('scroll', close, true)
      window.removeEventListener('keydown', onKey)
    }
  }, [menu])

  // A session is continuable when it has finished (a child seeds from
  // the parent's result, which only exists once the parent completed).
  function isContinuable(s) {
    return s.status === 'complete'
  }

  function handleContextMenu(e, s) {
    if (!onContinueSession) return
    e.preventDefault()
    e.stopPropagation()
    setMenu({ x: e.clientX, y: e.clientY, session: s })
  }

  // Map a parent session_id to its short label for the lineage hint.
  const byId = {}
  for (const s of (sessions || [])) byId[s.session_id] = s

  return (
    <aside className="sidebar">
      <div className="sidebar__heading">
        <span>Sessions</span>
        <span className="sidebar__count">{sessions?.length || 0}</span>
      </div>

      {/* Part 9: New Session button. Triggers the modal in App. */}
      {onNewSession && (
        <div className="sidebar__actions">
          <button
            className="btn btn--primary btn--full"
            onClick={onNewSession}
          >
            + New Session
          </button>
        </div>
      )}

      <div className="sidebar__list">
        {(!sessions || sessions.length === 0) ? (
          <div style={{ padding: '12px 16px', fontSize: 12, color: 'var(--text-faint)' }}>
            No sessions found for this director.
          </div>
        ) : (
          sessions.map(s => {
            const parent = s.parent_session_id ? byId[s.parent_session_id] : null
            return (
            <div
              key={s.session_id}
              className={`session-item ${s.session_id === selectedSessionId ? 'session-item--selected' : ''}`}
              onClick={() => onSelect(s.session_id)}
              onContextMenu={(e) => handleContextMenu(e, s)}
              title={isContinuable(s) ? 'Right-click to continue from this session' : undefined}
            >
              <span className={`session-item__dot session-item__dot--${s.status || 'unknown'}`} />
              <div>
                <div className="session-item__title">
                  {s.parent_session_id && (
                    <span
                      className="session-item__lineage"
                      title={parent ? `Continued from: ${parent.title || s.parent_session_id.slice(0,8)}` : 'Continued from a prior session'}
                      style={{ marginRight: 4, color: 'var(--text-faint)' }}
                    >↳</span>
                  )}
                  {s.title || s.session_id.slice(0, 8) + '...'}
                </div>
                <div className="session-item__meta">
                  {s.status === 'starting'
                    ? <span className="session-item__starting">starting…</span>
                    : (
                      <>
                        {s.turn_count != null ? `T${s.turn_count}` : ''}
                        {s.skills_used > 0 ? ` · ${s.skills_used} skill${s.skills_used === 1 ? '' : 's'}` : ''}
                        {s.status === 'awaiting_human_review' && (
                          <span className="session-item__badge session-item__badge--review">
                            needs review
                          </span>
                        )}
                        {s.status === 'policy_blocked' && (
                          <span className="session-item__badge session-item__badge--blocked">
                            policy blocked
                          </span>
                        )}
                      </>
                    )}
                </div>
              </div>
              <div className="session-item__age">{formatAge(s.started_at)}</div>
            </div>
            )
          })
        )}
      </div>

      {/* Right-click context menu. Rendered at the cursor; positioned
          fixed so it floats above the sidebar. */}
      {menu && (
        <div
          className="context-menu"
          style={{
            position: 'fixed',
            top: menu.y,
            left: menu.x,
            zIndex: 1000,
            background: 'var(--surface-2, #1a212b)',
            border: '1px solid var(--border, #2a3340)',
            borderRadius: 8,
            boxShadow: '0 8px 24px rgba(0,0,0,.4)',
            padding: 4,
            minWidth: 200,
          }}
          onClick={(e) => e.stopPropagation()}
        >
          {isContinuable(menu.session) ? (
            <button
              className="context-menu__item"
              style={{
                display: 'block', width: '100%', textAlign: 'left',
                padding: '8px 12px', background: 'none', border: 'none',
                color: 'var(--text, #e6e6e6)', cursor: 'pointer',
                borderRadius: 6, fontSize: '.9rem',
              }}
              onClick={() => { setMenu(null); onContinueSession(menu.session) }}
            >
              Continue from this session
            </button>
          ) : (
            <div style={{ padding: '8px 12px', fontSize: '.85rem', color: 'var(--text-faint, #6b7785)' }}>
              {menu.session.status === 'active' || menu.session.status === 'starting'
                ? 'Session still running'
                : 'Only completed sessions can be continued'}
            </div>
          )}
        </div>
      )}

      {/*
        Session info footer — pinned to the bottom of the sidebar.
        Shows ID, started_at, elapsed, and context summary for the
        currently-selected session. ADAM emits all of this; no
        aspirational fields here.
      */}
      {sessionInfo && (
        <div className="session-info">
          <div className="session-info__label">Session ID</div>
          <div className="session-info__value">{sessionInfo.session_id?.slice(0, 8) || '—'}</div>

          <div className="session-info__label">Started</div>
          <div className="session-info__value">{sessionInfo.started_short || '—'}</div>

          <div className="session-info__label">Elapsed</div>
          <div className="session-info__value">{sessionInfo.elapsed || '—'}</div>

          <div className="session-info__label">Context</div>
          <div className="session-info__value">{sessionInfo.context_summary || '—'}</div>

          <div className={`session-info__status ${sessionInfo.is_active ? '' : 'session-info__status--ready'}`}>
            <span className="session-info__status-dot" />
            <span>{sessionInfo.status_label || 'idle'}</span>
          </div>
        </div>
      )}
    </aside>
  )
}
