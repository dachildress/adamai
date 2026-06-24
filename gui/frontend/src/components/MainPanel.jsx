import { useMemo, useState, useEffect } from 'react'
import { ROUTING_REASON_LABELS, tokenizeContent, formatDuration, formatTimestamp, formatElapsed } from '../lib/agents'
import { artifactUrl, fetchProcessLogs } from '../lib/api'

export function MainPanel({ state, sessionMeta, selectedSessionId, onReview, onSelectSession }) {
  // Part 9: distinguish three "no events yet" cases:
  //   - User hasn't selected anything: nothing's selected
  //   - User selected a session, status=starting: show waiting message
  //   - User selected a session, status=errored, no events: show diagnostics
  //   - User selected a session with events: normal transcript (state.session_id is set)
  if (!state || !state.session_id) {
    if (!selectedSessionId) {
      return (
        <main className="main">
          <div className="empty">
            <div className="empty__title">No session selected</div>
            <div className="empty__message">
              Choose a session from the sidebar to view its deliberation,
              verifications, and skill invocations. Live sessions update
              automatically as new events arrive. Or click <strong>+ New Session</strong>
              to start a fresh deliberation.
            </div>
          </div>
        </main>
      )
    }

    // A session is selected but state.session_id is null because no
    // events have arrived yet. The sessionMeta from the sidebar is the
    // most authoritative source we have right now.
    const status = sessionMeta?.status || 'unknown'
    if (status === 'starting') {
      return (
        <main className="main">
          <StartingPanel sessionMeta={sessionMeta} />
        </main>
      )
    }
    if (status === 'errored') {
      return (
        <main className="main">
          <ErroredPanel sessionId={selectedSessionId} sessionMeta={sessionMeta} />
        </main>
      )
    }
    return (
      <main className="main">
        <div className="empty">
          <div className="empty__title">Waiting for events…</div>
          <div className="empty__message">
            Selected session has not yet produced any events. If this
            persists, the session may have ended before its first event
            was written.
          </div>
        </div>
      </main>
    )
  }

  const title    = state.seed ? extractTitle(state.seed) : (sessionMeta?.title || 'Untitled session')
  const subtitle = state.seed ? extractSubtitle(state.seed) : null
  const fullPrompt = state.seed || sessionMeta?.prompt_full || null
  const isActive = !state.ended

  return (
    <main className="main">
      <SessionTitle
        title={title}
        subtitle={subtitle}
        fullPrompt={fullPrompt}
        state={state}
        sessionMeta={sessionMeta}
      />
      <GovernanceBanner
        sessionMeta={sessionMeta}
        onReview={onReview}
        onSelectSession={onSelectSession}
      />
      <StatusGrid state={state} sessionMeta={sessionMeta} />
      <ContextRow state={state} />
      <Progress state={state} />
      <Transcript state={state} />
    </main>
  )
}


// Slice 3/4a: surface a policy block or human-review pause as a first-class
// banner. For a paused session it offers "Review & resume", which opens the
// resume modal. Driven by the sidebar summary (sessionMeta), which carries
// status + reason from session_state's governance block.
function GovernanceBanner({ sessionMeta, onReview, onSelectSession }) {
  if (!sessionMeta) return null
  const status = sessionMeta.status

  if (status === 'review_resolved') {
    const label = {
      approve: 'Approved',
      redirect: 'Redirected',
      declined: 'Declined',
      provided: 'Info provided',
    }[sessionMeta.review_decision] || sessionMeta.review_decision || 'Resolved'
    const childId = sessionMeta.resumed_as
    return (
      <div className="gov-banner gov-banner--resolved">
        <div className="gov-banner__body">
          <div className="gov-banner__title">Reviewed → {label}</div>
          {childId ? (
            <div className="gov-banner__reason">
              Resumed as session{' '}
              {onSelectSession ? (
                <button
                  type="button"
                  className="gov-banner__link"
                  onClick={() => onSelectSession(childId)}
                >
                  {childId.slice(0, 8)}…
                </button>
              ) : (
                <code>{childId.slice(0, 8)}…</code>
              )}
            </div>
          ) : (
            <div className="gov-banner__reason">No child session was created.</div>
          )}
        </div>
      </div>
    )
  }

  if (sessionMeta.resumed_from_review && sessionMeta.parent_session_id) {
    const label = {
      approve: 'approved',
      redirect: 'redirected',
      provided: 'info provided',
    }[sessionMeta.review_decision] || sessionMeta.review_decision || 'reviewed'
    return (
      <div className="gov-banner gov-banner--resolved">
        <div className="gov-banner__body">
          <div className="gov-banner__title">Resumed from reviewed parent</div>
          <div className="gov-banner__reason">
            Parent{' '}
            {onSelectSession ? (
              <button
                type="button"
                className="gov-banner__link"
                onClick={() => onSelectSession(sessionMeta.parent_session_id)}
              >
                {sessionMeta.parent_session_id.slice(0, 8)}…
              </button>
            ) : (
              <code>{sessionMeta.parent_session_id.slice(0, 8)}…</code>
            )}
            {' '}· decision: {label}
          </div>
        </div>
      </div>
    )
  }

  if (status === 'awaiting_human_review') {
    return (
      <div className="gov-banner gov-banner--review">
        <div className="gov-banner__body">
          <div className="gov-banner__title">Paused for your review</div>
          <div className="gov-banner__reason">
            {sessionMeta.review_reason
              || 'This session is waiting for your review before it produces anything.'}
          </div>
        </div>
        <button
          className="btn btn--primary gov-banner__action"
          onClick={() => onReview && onReview(sessionMeta)}
          type="button"
        >
          Review &amp; resume
        </button>
      </div>
    )
  }

  if (status === 'awaiting_information') {
    return (
      <div className="gov-banner gov-banner--review">
        <div className="gov-banner__body">
          <div className="gov-banner__title">Information needed</div>
          <div className="gov-banner__reason">
            {sessionMeta.information_reason
              || 'Deliberation paused until you provide missing input.'}
          </div>
        </div>
        <button
          className="btn btn--primary gov-banner__action"
          onClick={() => onReview && onReview(sessionMeta)}
          type="button"
        >
          Provide &amp; continue
        </button>
      </div>
    )
  }

  if (status === 'policy_blocked') {
    return (
      <div className="gov-banner gov-banner--blocked">
        <div className="gov-banner__body">
          <div className="gov-banner__title">Stopped by policy</div>
          <div className="gov-banner__reason">
            {sessionMeta.policy_block_reason
              || 'The governance profile for this session did not allow the planned action.'}
          </div>
        </div>
      </div>
    )
  }

  if (status === 'governance_boundary_blocked') {
    return (
      <div className="gov-banner gov-banner--blocked">
        <div className="gov-banner__body">
          <div className="gov-banner__title">Governance boundary</div>
          <div className="gov-banner__reason">
            {sessionMeta.governance_boundary_reason
              || 'This request would modify ADAM capabilities — a human-only action.'}
          </div>
        </div>
      </div>
    )
  }

  if (status === 'refusal_terminated') {
    return (
      <div className="gov-banner gov-banner--blocked">
        <div className="gov-banner__body">
          <div className="gov-banner__title">Refused — no artifact</div>
          <div className="gov-banner__reason">
            {sessionMeta.refusal_reason
              || 'The requested action was refused. ADAM ended without producing anything.'}
          </div>
        </div>
      </div>
    )
  }

  return null
}

function SessionTitle({ title, subtitle, fullPrompt, state, sessionMeta }) {
  const startedAt = state.session_started_at
  const [showPrompt, setShowPrompt] = useState(false)
  // The governance profile comes from the session summary (sessionMeta),
  // which carries it from .process_info.json / session_state.json. Falls
  // back to "not configured" only for genuinely ungoverned sessions.
  const profileId = sessionMeta?.governance_profile_id || null

  return (
    <div className="session-title">
      <div style={{ flex: 1 }}>
        <h1 className="session-title__heading">{title}</h1>
        <div className="session-title__crumbs">
          <span>
            <span className="session-title__crumb-label">Seed</span>
            {fullPrompt ? (
              <button
                type="button"
                onClick={() => setShowPrompt(v => !v)}
                style={{
                  background: 'none', border: 'none', padding: 0,
                  color: 'var(--accent, #4f8cff)', cursor: 'pointer',
                  fontStyle: 'italic', font: 'inherit',
                }}
                title="Show the full original prompt"
              >
                {showPrompt ? 'hide prompt' : 'view prompt'}
              </button>
            ) : (
              <span style={{ fontStyle: 'italic' }}>{subtitle || 'seed_file'}</span>
            )}
          </span>
          {startedAt && (
            <span>
              <span className="session-title__crumb-label">Started</span>
              {formatTimestamp(startedAt)}
            </span>
          )}
          <span>
            <span className="session-title__crumb-label">Director</span>
            {state.director?.display_name || state.director?.user_id || '—'}
          </span>
          <span>
            <span className="session-title__crumb-label">Governance Profile</span>
            <span style={{ color: profileId ? 'var(--mint)' : 'var(--text-faint)' }}>
              {profileId || 'not configured'}
            </span>
          </span>
        </div>
        {showPrompt && fullPrompt && (
          <div
            className="session-title__prompt"
            style={{
              marginTop: 10,
              padding: '12px 14px',
              borderRadius: 8,
              background: 'var(--surface-2, #1a212b)',
              border: '1px solid var(--border, #2a3340)',
              color: 'var(--text, #e6e6e6)',
              fontSize: '.9rem',
              lineHeight: 1.5,
              whiteSpace: 'pre-wrap',
              maxHeight: 300,
              overflowY: 'auto',
            }}
          >
            {fullPrompt}
          </div>
        )}
      </div>

      <div className="session-title__actions">
        <button
          className="btn"
          disabled
          data-tooltip="Pause is not yet implemented"
        >
          ⏸ Pause
        </button>
        <button
          className="btn btn--danger"
          disabled
          data-tooltip="GUI halt is not yet wired; use Ctrl+C in the ADAM terminal"
        >
          ⏹ Halt
        </button>
        <button
          className="btn"
          disabled
          data-tooltip="Session export coming with Phase C"
        >
          ↓ Export
        </button>
      </div>
    </div>
  )
}

function StatusGrid({ state, sessionMeta }) {
  // Governance status comes from the session SUMMARY (sessionMeta), which
  // carries the governance block from session_state.json. The live `state`
  // (event stream) does not, so reading governance off `state` would
  // wrongly show "not configured" / "complete" on governed/paused
  // sessions. The banner already reads sessionMeta correctly; the cards
  // now do too.
  const govStatus   = sessionMeta?.status
  const isPaused    = govStatus === 'awaiting_human_review'
      || govStatus === 'awaiting_information'
  const isReviewResolved = govStatus === 'review_resolved'
  const isBlocked   = govStatus === 'policy_blocked'
      || govStatus === 'governance_boundary_blocked'
      || govStatus === 'refusal_terminated'
  const profileId   = sessionMeta?.governance_profile_id || null
  const reviewMode  = isPaused ? 'required' : (profileId ? 'configured' : null)

  // Decision card. A paused or policy-blocked session did NOT end normally
  // -- don't claim "Ratified" / "Ended" for it.
  let decisionLabel, decisionClass = 'status-card--ok', decisionIcon = '✓'
  if (isPaused) {
    decisionLabel = 'Paused'; decisionClass = 'status-card--review'; decisionIcon = '❚❚'
  } else if (isReviewResolved) {
    decisionLabel = 'Reviewed'; decisionClass = 'status-card--ok'; decisionIcon = '✓'
  } else if (isBlocked) {
    decisionLabel = 'Blocked'; decisionClass = 'status-card--blocked'; decisionIcon = '✕'
  } else if (state.ended) {
    decisionLabel = state.end_reason?.includes('complete') ? 'Ratified' : 'Ended'
  } else {
    decisionLabel = 'In progress'; decisionClass = 'status-card--running'; decisionIcon = '▶'
  }

  // Execution card. Operator only runs once allowed; a pause/block means it
  // has NOT executed and an artifact was NOT produced.
  let execLabel, execClass, execIcon
  if (isPaused) {
    execLabel = 'Awaiting review'; execClass = 'status-card--review'; execIcon = '❚❚'
  } else if (isBlocked) {
    execLabel = 'Not executed'; execClass = 'status-card--blocked'; execIcon = '✕'
  } else if (state.ended) {
    execLabel = 'Complete'; execClass = 'status-card--ok'; execIcon = '✓'
  } else {
    execLabel = 'Running'; execClass = 'status-card--running'; execIcon = '▶'
  }

  // Truthseeker is informational
  const verifCount = state.verifications.length

  return (
    <div className="status-grid">
      <div className={`status-card ${decisionClass}`}>
        <div className="status-card__icon">{decisionIcon}</div>
        <div>
          <div className="status-card__label">Decision</div>
          <div className="status-card__value">{decisionLabel}</div>
        </div>
      </div>

      <div className={`status-card ${execClass}`}>
        <div className="status-card__icon">{execIcon}</div>
        <div>
          <div className="status-card__label">Execution</div>
          <div className="status-card__value">{execLabel}</div>
        </div>
      </div>

      <div className="status-card status-card--info">
        <div className="status-card__icon">⟁</div>
        <div>
          <div className="status-card__label">Truthseeker</div>
          <div className="status-card__value">
            {state.truthseeker_enabled === false
              ? 'Disabled'
              : `${verifCount} check${verifCount === 1 ? '' : 's'}`}
          </div>
        </div>
      </div>

      {/*
        HUMAN REVIEW: reflects this session's actual review posture. A
        paused session shows "Required"; a governed session shows its mode;
        an ungoverned session (no profile) shows "not configured".
      */}
      <div className={`status-card ${isPaused ? 'status-card--review' : 'status-card--mute'}`}>
        <div className="status-card__icon">{isPaused ? '❚❚' : '○'}</div>
        <div>
          <div className="status-card__label">Human Review</div>
          <div className={`status-card__value ${isPaused ? '' : 'status-card__value--mute'}`}>
            {isPaused ? 'Required' : (reviewMode ? 'Configured' : 'not configured')}
          </div>
        </div>
      </div>

      {/*
        POLICY BOUNDS: shows the governance profile governing this session,
        or "policy blocked" if the gate stopped it.
      */}
      <div className={`status-card ${isBlocked ? 'status-card--blocked' : (profileId ? 'status-card--info' : 'status-card--mute')}`}>
        <div className="status-card__icon">{isBlocked ? '✕' : '◇'}</div>
        <div>
          <div className="status-card__label">Policy Bounds</div>
          <div className={`status-card__value ${(isBlocked || profileId) ? '' : 'status-card__value--mute'}`}>
            {isBlocked ? 'Blocked' : (profileId || 'not configured')}
          </div>
        </div>
      </div>
    </div>
  )
}

function ContextRow({ state }) {
  const files = state.context?.files || []
  if (files.length === 0) return null

  return (
    <div className="context-row">
      <span className="context-row__label">Context</span>
      {files.map(f => {
        const ext = (f.filename || '').split('.').pop() || ''
        return (
          <span key={f.context_id || f.filename} className="context-pill">
            <span className="context-pill__ext">{ext}</span>
            <span className="context-pill__name">{f.filename}</span>
            <span className="context-pill__ctx-id">[{f.context_id}]</span>
          </span>
        )
      })}
      <button
        className="context-pill context-pill--attach"
        disabled
        data-tooltip="Mid-session attach coming with Phase C"
      >
        + ATTACH
      </button>
    </div>
  )
}

function Progress({ state }) {
  const steps = useMemo(() => {
    const out = state.turns.map(t => ({
      turn:   t.turn,
      agent:  t.agent,
      label:  `T${t.turn}`,
      status: t.status === 'running' ? 'current' : 'done',
    }))
    // Add a single placeholder for remaining turns
    if (state.max_turns && state.turns.length > 0 && state.turns.length < state.max_turns) {
      const lastTurn = state.turns[state.turns.length - 1].turn
      if (lastTurn < state.max_turns) {
        out.push({
          turn:   state.max_turns,
          agent:  null,
          label:  `T${lastTurn + 1}_T${state.max_turns}`,
          status: 'future',
        })
      }
    }
    return out
  }, [state.turns, state.max_turns])

  const runningTurn = state.turns.find(t => t.status === 'running')
  const lastTurn    = state.turns[state.turns.length - 1]
  const headTurn    = runningTurn || lastTurn

  const fillPercent = useMemo(() => {
    if (steps.length <= 1) return 0
    const doneCount = steps.filter(s => s.status === 'done').length
    const currentCount = steps.filter(s => s.status === 'current').length
    const totalCount = steps.length
    return ((doneCount + currentCount * 0.5) / (totalCount - 1)) * 100
  }, [steps])

  const elapsed = formatElapsed(state.session_started_at, state.session_ended_at)
  const maxTurns = state.max_turns || '—'

  return (
    <div className="progress">
      <div className="progress__header">
        <div className="progress__heading">Progress</div>
        <div className="progress__current">
          {headTurn
            ? <>Turn {headTurn.turn} · <span className="progress__current-agent">{headTurn.agent}</span> <span className="progress__current-state">({headTurn.status === 'running' ? 'executing' : 'complete'})</span></>
            : 'Waiting'
          }
        </div>
        <div className="progress__meta">
          {elapsed && `${elapsed} elapsed`}
          {' · '}
          max {maxTurns} turns
        </div>
      </div>

      {/*
        Part 9.3: line position pinned to the dot's vertical center via
        CSS top calc, not derived from align-items. Previous version
        had the line drifting below the dots because it sat at 50% of
        the tall step column (dot + T# + agent name). Now the structure
        is unchanged but the line offset is computed from padding + dot
        radius so dots sit ON the line.
      */}
      <div className="progress__track">
        <div className="progress__line" />
        <div className="progress__line-fill" style={{ width: `${fillPercent}%` }} />
        {steps.map((s, i) => (
          <div key={i} className={`progress__step progress__step--${s.status}`}>
            <div className="progress__dot" />
            <div className="progress__label">{s.label}</div>
            <div className="progress__agent">
              {s.status === 'future' ? 'to be routed' : (s.agent || '')}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

function Transcript({ state }) {
  if (state.turns.length === 0) {
    return (
      <div className="transcript">
        <div className="empty">
          <div className="empty__title">Awaiting first turn</div>
          <div className="empty__message">
            The deliberation has not yet produced any agent turns.
            Turns will appear here as ADAM streams them.
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="transcript">
      {state.turns.map(t => <TurnCard key={t.turn} turn={t} sessionId={state.session_id} />)}

      {/*
        End-of-session marker. Visible only after session_ended fires.
      */}
      {state.ended && (
        <div className="turn turn--system">
          <div className="turn__header">
            <span className="turn__t">END</span>
            <span className="turn__agent turn__agent--System">System</span>
            <span className="turn__reason">{state.end_reason}</span>
            <span className="turn__timestamp">{formatTimestamp(state.session_ended_at)}</span>
          </div>
          <div className="turn__body">
            {state.end_reason === 'awaiting_human_review' ? (
              <>Paused for review before execution. Use <strong>Review &amp; resume</strong> above to approve, redirect, or decline.</>
            ) : state.end_reason === 'awaiting_information' ? (
              <>Paused mid-deliberation for missing information. Use <strong>Provide &amp; continue</strong> above to supply what was requested.</>
            ) : state.end_reason === 'policy_blocked' ? (
              <>Stopped by policy before execution. Operator did not run and no artifact was produced.</>
            ) : state.end_reason === 'governance_boundary_blocked' ? (
              <>Stopped at a governance boundary before execution. Operator did not run and no artifact was produced.</>
            ) : state.end_reason === 'refusal_terminated' ? (
              <>The requested action was refused. Operator did not run and no artifact was produced.</>
            ) : (
              <>
                Session complete. {state.final_summary?.skill_summary
                  ? `${state.final_summary.skill_summary.successes} skill invocation${state.final_summary.skill_summary.successes === 1 ? '' : 's'} succeeded`
                  : 'No skills invoked.'}
                {state.final_summary?.wrap_up?.continuations > 0 &&
                  ` (${state.final_summary.wrap_up.continuations} continuation${state.final_summary.wrap_up.continuations === 1 ? '' : 's'} granted)`
                }
                .
              </>
            )}
          </div>
        </div>
      )}
    </div>
  )
}

function TurnCard({ turn, sessionId }) {
  const isWrapUp     = turn.routing_reason?.startsWith('wrap-up-')
  const isContinuation = turn.routing_reason === 'operator-continuation'

  const classes = [
    'turn',
    isWrapUp ? 'turn--wrap-up' : '',
    isContinuation ? 'turn--continuation' : '',
  ].filter(Boolean).join(' ')

  const reasonLabel = ROUTING_REASON_LABELS[turn.routing_reason] || turn.routing_reason

  // Render an outcome card for every skill_invoked attached to this
  // turn -- not just file-producing ones. The card shape adapts to
  // what the skill produced (file vs. action vs. failure), so a turn
  // that creates a slidedeck AND emails it gets two cards side by side.
  const skills = turn.skills || []

  return (
    <div className={classes}>
      <div className="turn__header">
        <span className="turn__t">T{turn.turn}</span>
        <span className={`turn__agent turn__agent--${turn.agent}`}>{turn.agent}</span>
        <span className="turn__reason">{reasonLabel}</span>
        {turn.model_id && <span className="turn__model">· {turn.model_id}</span>}
        {turn.status === 'running' && <span className="turn__streaming">EXECUTING</span>}
        <span className="turn__timestamp">
          {turn.status === 'running'
            ? 'started ' + formatTimestamp(turn.started_at, { timeOnly: true })
            : formatTimestamp(turn.completed_at, { timeOnly: true })}
          {turn.duration_ms != null && <> · {formatDuration(turn.duration_ms)}</>}
        </span>
      </div>
      <div className="turn__body">
        {turn.status === 'running' ? (
          <span style={{ color: 'var(--text-faint)', fontStyle: 'italic' }}>
            Streaming from {turn.model_id}...
          </span>
        ) : turn.status === 'error' ? (
          <span style={{ color: 'var(--coral)' }}>
            Turn errored: {turn.error_type}: {turn.error_message}
          </span>
        ) : (
          <RichText text={turn.content} />
        )}
      </div>
      {skills.length > 0 && (
        <div className="outcome-cards">
          {skills.map((s, i) => <OutcomeCard key={i} skill={s} sessionId={sessionId} />)}
        </div>
      )}
    </div>
  )
}

/**
 * One card per skill_invoked event in a turn. The card type is
 * derived from what the skill produced rather than from the skill
 * name -- so a hypothetical future skill that also writes a file
 * gets the same artifact card without any code change here.
 *
 * Decision tree:
 *   - status != success         -> failed card (no link, error visible)
 *   - filename is set           -> artifact card (with Open link)
 *   - to/message_id is set      -> email card (recipients, subject, etc.)
 *   - otherwise                 -> generic card (skill, action, status)
 */
function OutcomeCard({ skill, sessionId }) {
  const succeeded = skill.status === 'success'
  if (!succeeded) {
    return <FailedCard skill={skill} />
  }
  if (skill.filename) {
    return <ArtifactCard skill={skill} sessionId={sessionId} />
  }
  if (skill.to || skill.message_id) {
    return <EmailCard skill={skill} sessionId={sessionId} />
  }
  return <GenericSkillCard skill={skill} />
}

function ArtifactCard({ skill, sessionId }) {
  const sizeLabel = skill.size_bytes != null ? formatFileSize(skill.size_bytes) : null
  const skillLabel = skill.skill && skill.action ? `${skill.skill}.${skill.action}` : skill.skill
  // Part 9.2: prefer relpath (session-artifacts-relative, used by
  // multi-file skills like coder) over filename (flat, used by
  // document/slidedeck). The artifactUrl helper handles both shapes.
  // If neither is present (shouldn't happen for a success card but
  // could on a malformed payload), href is null and the Open button
  // is suppressed.
  const linkTarget = skill.relpath || skill.filename
  const href = sessionId && linkTarget ? artifactUrl(sessionId, linkTarget) : null

  return (
    <div className="outcome-card outcome-card--artifact">
      <div className="outcome-card__header">
        <span className="outcome-card__label">
          <span className="outcome-card__label-icon">▣</span>
          Artifact Created
        </span>
        <span className="outcome-card__skill">{skillLabel}</span>
      </div>
      <div className="outcome-card__body">
        <span className="outcome-card__body-label">File</span>
        <span className="outcome-card__body-value outcome-card__filename">{skill.filename}</span>
        {sizeLabel && <>
          <span className="outcome-card__body-label">Size</span>
          <span className="outcome-card__body-value outcome-card__body-value--mute">{sizeLabel}</span>
        </>}
        {skill.format && <>
          <span className="outcome-card__body-label">Format</span>
          <span className="outcome-card__body-value outcome-card__body-value--mute">{skill.format}</span>
        </>}
        {/* Part 9.2: surface the workspace path for multi-file
            packages so the user can locate the full directory on
            disk. Informational only -- the Open link goes to the
            primary file. */}
        {skill.workspace_relpath && <>
          <span className="outcome-card__body-label">Package</span>
          <span className="outcome-card__body-value outcome-card__body-value--mono-small">
            artifacts/{skill.workspace_relpath}/
          </span>
        </>}
      </div>
      {href && (
        <a className="outcome-card__action" href={href} target="_blank" rel="noopener noreferrer">
          <span className="outcome-card__action-icon">↗</span>
          Open
        </a>
      )}
    </div>
  )
}

function EmailCard({ skill, sessionId }) {
  const skillLabel = skill.skill && skill.action ? `${skill.skill}.${skill.action}` : skill.skill
  const recipients = Array.isArray(skill.to) ? skill.to.join(', ') : (skill.to || '—')
  const cc         = Array.isArray(skill.cc) && skill.cc.length > 0 ? skill.cc.join(', ') : null
  const attachList = Array.isArray(skill.attachments) ? skill.attachments : (skill.attachments ? [skill.attachments] : [])

  return (
    <div className="outcome-card outcome-card--email">
      <div className="outcome-card__header">
        <span className="outcome-card__label">
          <span className="outcome-card__label-icon">✉</span>
          Email Sent
        </span>
        <span className="outcome-card__skill">{skillLabel}</span>
      </div>
      <div className="outcome-card__body">
        <span className="outcome-card__body-label">To</span>
        <span className="outcome-card__body-value">{recipients}</span>
        {cc && <>
          <span className="outcome-card__body-label">Cc</span>
          <span className="outcome-card__body-value">{cc}</span>
        </>}
        {skill.bcc_count > 0 && <>
          <span className="outcome-card__body-label">Bcc</span>
          <span className="outcome-card__body-value outcome-card__body-value--mute">{skill.bcc_count} hidden</span>
        </>}
        {skill.subject && <>
          <span className="outcome-card__body-label">Subject</span>
          <span className="outcome-card__body-value">{skill.subject}</span>
        </>}
        {attachList.length > 0 && <>
          <span className="outcome-card__body-label">Attached</span>
          <span className="outcome-card__body-value">
            {attachList.map((name, i) => (
              <span key={i}>
                {/* If the attachment matches a known artifact filename
                    in this session, the GUI could link it -- but the
                    artifact-URL contract only works for files in THIS
                    session's artifacts/ directory, which is exactly
                    what email attaches in the common case, so a link
                    is generally safe. We keep it as plain text here
                    for safety; the artifact card from the same turn
                    already provides the link. */}
                {name}{i < attachList.length - 1 ? ', ' : ''}
              </span>
            ))}
          </span>
        </>}
        {skill.message_id && <>
          <span className="outcome-card__body-label">Message ID</span>
          <span className="outcome-card__body-value outcome-card__body-value--mono-small">{skill.message_id}</span>
        </>}
        {skill.provider && <>
          <span className="outcome-card__body-label">Via</span>
          <span className="outcome-card__body-value outcome-card__body-value--mute">{skill.provider}</span>
        </>}
      </div>
    </div>
  )
}

function GenericSkillCard({ skill }) {
  const skillLabel = skill.skill && skill.action ? `${skill.skill}.${skill.action}` : skill.skill
  return (
    <div className="outcome-card outcome-card--artifact">
      <div className="outcome-card__header">
        <span className="outcome-card__label">
          <span className="outcome-card__label-icon">●</span>
          Skill Invoked
        </span>
        <span className="outcome-card__skill">{skillLabel}</span>
      </div>
      <div className="outcome-card__body">
        <span className="outcome-card__body-label">Status</span>
        <span className="outcome-card__body-value">{skill.status}</span>
        {skill.artifact_id && <>
          <span className="outcome-card__body-label">Artifact ID</span>
          <span className="outcome-card__body-value outcome-card__body-value--mono-small">{skill.artifact_id}</span>
        </>}
      </div>
    </div>
  )
}

function FailedCard({ skill }) {
  const skillLabel = skill.skill && skill.action ? `${skill.skill}.${skill.action}` : skill.skill
  return (
    <div className="outcome-card outcome-card--failed">
      <div className="outcome-card__header">
        <span className="outcome-card__label">
          <span className="outcome-card__label-icon">✕</span>
          Skill Failed
        </span>
        <span className="outcome-card__skill">{skillLabel}</span>
      </div>
      {(skill.error_class || skill.error_message) && (
        <div className="outcome-card__error">
          {skill.error_class && <strong>{skill.error_class}: </strong>}
          {skill.error_message || 'invocation did not complete successfully'}
        </div>
      )}
    </div>
  )
}

function formatFileSize(bytes) {
  if (bytes == null) return ''
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 / 1024).toFixed(2)} MB`
}

function RichText({ text }) {
  const tokens = tokenizeContent(text)
  return (
    <>
      {tokens.map((t, i) => {
        if (t.type === 'ctx') return <span key={i} className="ctx-id">{t.value}</span>
        if (t.type === 'doc') return <span key={i} className="doc-marker">{t.value}</span>
        if (t.type === 'tag') return <span key={i} className="doc-marker">{t.value}</span>
        return <span key={i}>{t.value}</span>
      })}
    </>
  )
}

// Heuristic: first line of the seed = title. Strip leading """ from
// triple-quoted Python prompts so the title doesn't read as """.
function extractTitle(seed) {
  if (!seed) return 'Untitled'
  const cleaned = seed.replace(/^"""/, '').trim()
  const firstLine = cleaned.split('\n')[0].trim()
  if (firstLine.length === 0) {
    // Fall back to the second line if the first is empty
    const second = cleaned.split('\n')[1]?.trim()
    return second?.slice(0, 100) || 'Untitled'
  }
  return firstLine.slice(0, 100)
}

function extractSubtitle(seed) {
  return 'seed_file'
}


/**
 * Part 9: panel shown when a session is in 'starting' state.
 *
 * 'Starting' means: .process_info.json exists, the PID is alive, and
 * events.jsonl hasn't been written to yet (or is empty). ADAM is doing
 * its startup work (loading agents, building trust registry, loading
 * context, etc.). Typically clears in 5-15 seconds.
 *
 * If startup takes much longer than expected, the user can click
 * 'Show diagnostic logs' to see the captured stdout/stderr, but those
 * are noisy during normal operation and are NOT shown by default --
 * events.jsonl is the primary state source, not the process logs.
 */
function StartingPanel({ sessionMeta }) {
  const [showLogs, setShowLogs] = useState(false)
  const startedAt = sessionMeta?.started_at
  const pid       = sessionMeta?.process?.pid
  const command   = sessionMeta?.process?.command

  return (
    <div className="diagnostic-panel diagnostic-panel--starting">
      <div className="diagnostic-panel__icon">⋯</div>
      <div className="diagnostic-panel__title">Starting ADAM…</div>
      <div className="diagnostic-panel__message">
        ADAM is initializing this session. Agent registry, trust loader, and
        context loader run before the first event is emitted. This usually takes
        5–15 seconds. The transcript will appear automatically once the first
        event arrives.
      </div>
      <div className="diagnostic-panel__meta">
        {startedAt && <div>Started: <span className="mono">{startedAt}</span></div>}
        {pid && <div>PID: <span className="mono">{pid}</span></div>}
      </div>
      {sessionMeta?.title && (
        <div className="diagnostic-panel__seed-preview">
          <div className="diagnostic-panel__seed-label">Seed:</div>
          <div className="diagnostic-panel__seed-text">{sessionMeta.title}</div>
        </div>
      )}
      <div className="diagnostic-panel__footer">
        <button
          className="btn btn--ghost btn--small"
          onClick={() => setShowLogs(s => !s)}
        >
          {showLogs ? 'Hide' : 'Show'} startup logs
        </button>
      </div>
      {showLogs && <ProcessLogsView sessionId={sessionMeta?.session_id} />}
    </div>
  )
}


/**
 * Part 9: panel shown when a session is in 'errored' state with no events.
 *
 * This happens when ADAM was spawned but exited before producing any
 * deliberation events. Common causes:
 *   - Missing or invalid API keys
 *   - Bad runtime.json config
 *   - Missing model libraries (anthropic, openai, etc.)
 *   - Permissions / disk space issues
 *
 * The diagnostic info lives in process_stderr.log, which we fetch via
 * the /api/sessions/<id>/process_logs endpoint. The tail of stderr is
 * almost always enough to identify the cause.
 */
function ErroredPanel({ sessionId, sessionMeta }) {
  return (
    <div className="diagnostic-panel diagnostic-panel--errored">
      <div className="diagnostic-panel__icon">✕</div>
      <div className="diagnostic-panel__title">Session failed to start</div>
      <div className="diagnostic-panel__message">
        ADAM exited before producing any deliberation events. The captured
        process logs below show what happened. Common causes include missing
        API keys, invalid runtime configuration, or missing dependencies.
      </div>
      <div className="diagnostic-panel__meta">
        {sessionMeta?.process?.pid && (
          <div>PID: <span className="mono">{sessionMeta.process.pid}</span> (no longer running)</div>
        )}
        {sessionMeta?.started_at && (
          <div>Started: <span className="mono">{sessionMeta.started_at}</span></div>
        )}
      </div>
      <ProcessLogsView sessionId={sessionId} autoLoad />
    </div>
  )
}


/**
 * Inline display of process_stdout.log and process_stderr.log for a
 * session. Used by both StartingPanel (manual reveal) and ErroredPanel
 * (auto-loaded).
 */
function ProcessLogsView({ sessionId, autoLoad = false }) {
  const [logs, setLogs] = useState(null)
  const [loading, setLoading] = useState(false)
  const [err, setErr] = useState(null)

  useEffect(() => {
    if (!sessionId) return
    if (!autoLoad) return
    loadLogs()
  }, [sessionId, autoLoad])

  async function loadLogs() {
    setLoading(true)
    setErr(null)
    try {
      const data = await fetchProcessLogs(sessionId)
      setLogs(data)
    } catch (e) {
      setErr(e.message || 'failed to load logs')
    } finally {
      setLoading(false)
    }
  }

  if (!autoLoad && !logs && !loading) {
    return (
      <div className="process-logs">
        <button className="btn btn--ghost btn--small" onClick={loadLogs}>
          Load logs
        </button>
      </div>
    )
  }

  if (loading) return <div className="process-logs__loading">loading logs…</div>
  if (err) return <div className="process-logs__error">error: {err}</div>
  if (!logs) return null

  return (
    <div className="process-logs">
      <div className="process-logs__refresh">
        <button className="btn btn--ghost btn--small" onClick={loadLogs}>
          ↻ Refresh
        </button>
      </div>
      {logs.stderr?.text && (
        <div className="process-logs__section">
          <div className="process-logs__heading">
            stderr · {logs.stderr.size} bytes{logs.stderr.truncated ? ' (tail)' : ''}
          </div>
          <pre className="process-logs__pre process-logs__pre--stderr">
            {logs.stderr.text}
          </pre>
        </div>
      )}
      {logs.stdout?.text && (
        <div className="process-logs__section">
          <div className="process-logs__heading">
            stdout · {logs.stdout.size} bytes{logs.stdout.truncated ? ' (tail)' : ''}
          </div>
          <pre className="process-logs__pre">
            {logs.stdout.text}
          </pre>
        </div>
      )}
      {!logs.stderr?.text && !logs.stdout?.text && (
        <div className="process-logs__empty">
          No process logs captured. The session may have ended before any
          output was produced.
        </div>
      )}
    </div>
  )
}
