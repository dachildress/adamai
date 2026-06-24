import { useCallback, useEffect, useMemo, useState } from 'react'
import { artifactUrl, fetchSessionVerifications, overrideVerificationClaim } from '../lib/api'

const OVERRIDE_STATUSES = [
  'VERIFIED',
  'PARTIALLY_VERIFIED',
  'UNSUPPORTED',
  'CONTRADICTED',
  'NEEDS_HUMAN_REVIEW',
  'NOT_WEB_VERIFIABLE',
  'DOCUMENT_GROUNDED_NOT_WEB_VERIFIED',
]

export function RightBar({ state, sessionId, user }) {
  return (
    <aside className="rightbar">
      <VerifierPanel state={state} sessionId={sessionId} user={user} />
      <SkillRegistryPanel state={state} />
    </aside>
  )
}

function VerifierPanel({ state, sessionId, user }) {
  const [claims, setClaims] = useState([])
  const [summary, setSummary] = useState({ total: 0, status_counts: {}, overridden: 0 })
  const [loading, setLoading] = useState(false)
  const [expandedId, setExpandedId] = useState(null)
  const [overrideTarget, setOverrideTarget] = useState(null)
  const isAdmin = user?.role === 'admin'

  const loadClaims = useCallback(async () => {
    if (!sessionId) {
      setClaims([])
      setSummary({ total: 0, status_counts: {}, overridden: 0 })
      return
    }
    setLoading(true)
    try {
      const data = await fetchSessionVerifications(sessionId)
      setClaims(data.claims || data.verifications || [])
      setSummary(data.summary || { total: 0, status_counts: {}, overridden: 0 })
    } catch (_) {
      /* keep prior data on transient errors */
    } finally {
      setLoading(false)
    }
  }, [sessionId])

  useEffect(() => {
    loadClaims()
  }, [loadClaims, state.verifications.length])

  const sortedClaims = useMemo(() => {
    return [...claims].sort((a, b) => {
      const ta = a.source_turn ?? 0
      const tb = b.source_turn ?? 0
      if (tb !== ta) return tb - ta
      return (a.claim || '').localeCompare(b.claim || '')
    })
  }, [claims])

  const liveCount = state.verifications.reduce((s, v) => s + (v.claims_checked || 0), 0)

  return (
    <div className="panel">
      <div className="panel__header">
        <div className="panel__title">Verifier</div>
        <div className="panel__meta">
          {summary.total || liveCount} / session
          {summary.overridden > 0 && (
            <span className="verif__override-count"> · {summary.overridden} overridden</span>
          )}
        </div>
      </div>

      {loading && claims.length === 0 ? (
        <div className="verif__empty">Loading claims…</div>
      ) : sortedClaims.length === 0 ? (
        <div className="verif__empty">
          No verifications yet. Truthseeker fires after advisory and
          non-Operator wrap-up turns.
        </div>
      ) : (
        <div className="verif-list">
          {sortedClaims.map(claim => (
            <ClaimCard
              key={claim.claim_id}
              claim={claim}
              expanded={expandedId === claim.claim_id}
              onToggle={() => setExpandedId(
                expandedId === claim.claim_id ? null : claim.claim_id
              )}
              isAdmin={isAdmin}
              onOverride={() => setOverrideTarget(claim)}
            />
          ))}
        </div>
      )}

      {overrideTarget && (
        <OverrideModal
          claim={overrideTarget}
          onClose={() => setOverrideTarget(null)}
          onSaved={async () => {
            setOverrideTarget(null)
            await loadClaims()
          }}
          sessionId={sessionId}
        />
      )}
    </div>
  )
}

function ClaimCard({ claim, expanded, onToggle, isAdmin, onOverride }) {
  const status = claim.effective_status || claim.original_status || 'UNKNOWN'
  const overridden = !!claim.override
  const tierNum = tierLabelToNum(claim.highest_source_tier)

  return (
    <div className={`verif verif--card ${expanded ? 'verif--expanded' : ''}`}>
      <button type="button" className="verif__header-btn" onClick={onToggle}>
        <div className="verif__claim">{claim.claim || '(no claim text)'}</div>
        <div className="verif__meta">
          <span className={`verif__status verif__status--${status}`}>
            {status.replace(/_/g, ' ')}
          </span>
          {overridden && (
            <span className="verif__override-badge" title={`Was ${claim.original_status}`}>
              overridden
            </span>
          )}
          {claim.source_turn != null && (
            <span className="verif__turn">T{claim.source_turn}</span>
          )}
          {claim.source_agent && (
            <span className="verif__agent">{claim.source_agent}</span>
          )}
          {claim.confidence && claim.confidence !== 'N/A' && (
            <span className="verif__conf">conf {claim.confidence}</span>
          )}
          {tierNum && (
            <span className="verif__tier">
              top <span className={`verif__tier-num verif__tier-num--${tierNum}`}>
                T{tierNum}
              </span>
            </span>
          )}
          <span className="verif__chevron">{expanded ? '▾' : '▸'}</span>
        </div>
      </button>

      {expanded && (
        <div className="verif__detail">
          {overridden && (
            <div className="verif__override-note">
              <strong>Admin override:</strong> {claim.override.reason}
              <span className="verif__override-by">
                — {claim.override.by}, {claim.override.at}
              </span>
            </div>
          )}
          {claim.note && (
            <div className="verif__note">Note: {claim.note}</div>
          )}
          {claim.source_file && (
            <div className="verif__note">
              Context: {claim.context_id || '—'} · {claim.source_file}
            </div>
          )}
          {claim.sources?.length > 0 ? (
            <div className="verif__sources">
              <div className="verif__sources-label">
                Sources ({claim.source_count || claim.sources.length})
              </div>
              {claim.sources
                .slice()
                .sort((a, b) => (b.tier_score || 0) - (a.tier_score || 0))
                .map((src, i) => (
                  <SourceRow key={`${src.url}-${i}`} source={src} />
                ))}
            </div>
          ) : (
            <div className="verif__sources verif__sources--empty">
              No web sources consulted
              {status === 'DOCUMENT_GROUNDED_NOT_WEB_VERIFIED'
                ? ' (document-grounded claim)'
                : ''}
            </div>
          )}
          {isAdmin && (
            <button
              type="button"
              className="verif__override-btn"
              onClick={e => { e.stopPropagation(); onOverride() }}
            >
              Override verdict
            </button>
          )}
        </div>
      )}
    </div>
  )
}

function SourceRow({ source }) {
  const tierNum = tierLabelToNum(source.tier)
  const support = source.supports_claim || 'unknown'
  return (
    <div className="verif__source">
      <div className="verif__source-head">
        {tierNum && (
          <span className={`verif__tier-num verif__tier-num--${tierNum}`}>
            T{tierNum}
          </span>
        )}
        <span className={`verif__support verif__support--${support}`}>
          {support}
        </span>
        <a
          className="verif__source-link"
          href={source.url}
          target="_blank"
          rel="noopener noreferrer"
          onClick={e => e.stopPropagation()}
        >
          {source.title || source.domain || source.url}
        </a>
      </div>
      {source.notes && (
        <div className="verif__source-notes">{source.notes}</div>
      )}
    </div>
  )
}

function OverrideModal({ claim, sessionId, onClose, onSaved }) {
  const [status, setStatus] = useState(claim.effective_status || claim.original_status)
  const [reason, setReason] = useState('')
  const [feedback, setFeedback] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState(null)

  async function handleSubmit(e) {
    e.preventDefault()
    if (!reason.trim()) {
      setError('Reason is required')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await overrideVerificationClaim(sessionId, {
        claimId: claim.claim_id,
        status,
        reason: reason.trim(),
        feedback: feedback.trim() || null,
      })
      onSaved()
    } catch (err) {
      setError(String(err.message || err))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={() => !submitting && onClose()}>
      <div
        className="modal verif-override-modal"
        role="dialog"
        aria-modal="true"
        onClick={e => e.stopPropagation()}
      >
        <div className="modal__header">
          <h2 className="modal__title">Override verdict</h2>
          <button type="button" className="modal__close" onClick={onClose} disabled={submitting}>×</button>
        </div>
        <form className="modal__body" onSubmit={handleSubmit}>
          <p className="verif-override-modal__claim">{claim.claim}</p>
          <p className="verif-override-modal__orig">
            Truthseeker: <strong>{claim.original_status}</strong>
          </p>
          <label className="gov-admin__field">
            <span className="gov-admin__field-label">New status</span>
            <select
              className="gov-admin__input"
              value={status}
              onChange={e => setStatus(e.target.value)}
            >
              {OVERRIDE_STATUSES.map(s => (
                <option key={s} value={s}>{s.replace(/_/g, ' ')}</option>
              ))}
            </select>
          </label>
          <label className="gov-admin__field">
            <span className="gov-admin__field-label">Reason (audit trail)</span>
            <textarea
              className="gov-admin__input"
              rows={2}
              value={reason}
              onChange={e => setReason(e.target.value)}
              placeholder="Why is this override correct?"
              required
            />
          </label>
          <label className="gov-admin__field">
            <span className="gov-admin__field-label">Feedback for Truthseeker tuning (optional)</span>
            <textarea
              className="gov-admin__input"
              rows={2}
              value={feedback}
              onChange={e => setFeedback(e.target.value)}
              placeholder="What should Truthseeker do differently next time?"
            />
          </label>
          {error && <div className="modal__error">{error}</div>}
          <div className="modal__footer">
            <div className="modal__footer-actions">
              <button type="button" className="btn btn--ghost" onClick={onClose} disabled={submitting}>
                Cancel
              </button>
              <button type="submit" className="btn btn--primary" disabled={submitting}>
                {submitting ? 'Saving…' : 'Save override'}
              </button>
            </div>
          </div>
        </form>
      </div>
    </div>
  )
}

function tierLabelToNum(tier) {
  if (!tier) return null
  const m = String(tier).match(/(\d)/)
  return m ? Number(m[1]) : null
}

function SkillRegistryPanel({ state }) {
  const skills = state.skill_registry?.skills || []
  const invocations = state.skill_invocations
  const [searchTerm, setSearchTerm] = useState('')
  const [selectedSkill, setSelectedSkill] = useState(null)

  const skillsAugmented = useMemo(() => {
    return skills.map(s => {
      const invs = invocations.filter(i => i.skill === s.name)
      const successes = invs.filter(i => i.status === 'success').length
      return {
        ...s,
        invocations: invs,
        used: invs.length,
        successes,
        last_invocation: invs[invs.length - 1] || null,
      }
    })
  }, [skills, invocations])

  const filtered = useMemo(() => {
    if (!searchTerm) return skillsAugmented
    const q = searchTerm.toLowerCase()
    return skillsAugmented.filter(s =>
      s.name.toLowerCase().includes(q) ||
      (s.actions || []).some(a => a.toLowerCase().includes(q))
    )
  }, [skillsAugmented, searchTerm])

  const effectiveSelected = selectedSkill || filtered[0]?.name

  return (
    <div className="panel">
      <div className="panel__header">
        <div className="panel__title">Skill Registry</div>
        <div className="panel__meta">{skills.length} loaded</div>
      </div>

      <div className="skill-search">
        <span className="skill-search__icon">⌕</span>
        <input
          className="skill-search__input"
          placeholder="search skills..."
          value={searchTerm}
          onChange={e => setSearchTerm(e.target.value)}
        />
      </div>

      {filtered.length === 0 ? (
        <div style={{ fontSize: 12, color: 'var(--text-faint)', padding: '8px 0' }}>
          {skills.length === 0
            ? 'No skills registered.'
            : 'No matching skills.'}
        </div>
      ) : (
        filtered.map(s => (
          <div
            key={s.name}
            className={`skill-card ${s.name === effectiveSelected ? 'skill-card--selected' : ''}`}
            onClick={() => setSelectedSkill(s.name)}
          >
            <div className="skill-card__header">
              <span className="skill-card__name">{s.name}</span>
              {s.used > 0
                ? <span className="skill-card__badge skill-card__badge--used">
                    USED · {s.used}
                  </span>
                : <span className="skill-card__badge skill-card__badge--ready">READY</span>
              }
            </div>
          </div>
        ))
      )}

      {effectiveSelected && (
        <SkillDetail
          skill={skillsAugmented.find(s => s.name === effectiveSelected)}
          sessionId={state.session_id}
        />
      )}
    </div>
  )
}

function SkillDetail({ skill, sessionId }) {
  if (!skill) return null

  const riskRaw = skill.risk || null
  const riskClass = riskRaw ? riskRaw.toLowerCase().replace(/[ -]/g, '_') : ''

  return (
    <div className="skill-detail">
      <div className="skill-detail__title-row">
        <span className="skill-detail__name">{skill.name}</span>
        {riskRaw
          ? <span className={`skill-detail__risk skill-detail__risk--${riskClass}`}>
              RISK · {riskRaw}
            </span>
          : <span className="skill-detail__risk skill-detail__risk--low" style={{ color: 'var(--text-faint)', background: 'transparent', borderBottom: '1px dashed var(--line-base)' }}>
              RISK · —
            </span>
        }
      </div>

      <div className="skill-detail__description">
        Actions: <code>{(skill.actions || []).join(', ')}</code>
        {skill.version && <span> · version {skill.version}</span>}
      </div>

      <div className="skill-detail__section">
        <div className="skill-detail__section-label">Allowed Callers</div>
        <div className="skill-detail__args">
          {(skill.allowed_callers || []).map(c => (
            <span key={c} style={{ marginRight: 8 }}>{c}</span>
          ))}
        </div>
      </div>

      {skill.last_invocation && (
        <div className="skill-detail__section">
          <div className="skill-detail__section-label">Last Invocation</div>
          <div className="skill-detail__last-invocation">
            T{skill.last_invocation.turn}
            {skill.last_invocation.size_bytes && <> · {(skill.last_invocation.size_bytes / 1024).toFixed(1)} KB</>}
            {' · '}{skill.last_invocation.status}
          </div>
          {sessionId
            && (skill.last_invocation.relpath || skill.last_invocation.filename)
            && skill.last_invocation.status === 'success' && (
            <a
              className="skill-detail__artifact-link"
              href={artifactUrl(
                sessionId,
                skill.last_invocation.relpath || skill.last_invocation.filename
              )}
              target="_blank"
              rel="noopener noreferrer"
            >
              <span className="skill-detail__artifact-link-icon">↗</span>
              <span>open {skill.last_invocation.filename}</span>
            </a>
          )}
        </div>
      )}
    </div>
  )
}
